"""Single-machine provisioning lock for launcher-created RunPod pods.

Guarantees that at most one launcher process on this machine performs RunPod
pod creation at a time. The lock is a file in the launcher home
(``<home>\\provision.lock``) created exclusively (``os.O_CREAT | O_EXCL``);
the file descriptor is held for the whole critical section. The file content
is diagnostic only (pid, hostname, timestamp, intent, pod name) and is never
a security boundary.

Staleness rules, checked in order on every poll:

1. metadata parseable, same hostname, dead PID        -> stale
2. metadata parseable, same hostname, ``acquired_at`` older than
   ``STALE_AGE``                                       -> stale
3. metadata parseable, foreign hostname               -> stale
4. metadata unparseable/missing, mtime older than 60s -> stale
5. otherwise                                          -> held

Rule 2 exists because rule 1 alone can wedge provisioning for ever: a
crashed run leaves its PID in the file, and once Windows recycles that PID
any other process makes ``pid_is_alive`` answer True — the lock then looks
held by a live owner that does not exist, and every new launcher burns the
full ``MAX_WAIT`` before failing.

Takeover is best-effort: unlink the stale file, then retry the exclusive
create. ``release`` unlinks the lock file only while its mtime still matches
the mtime recorded at acquire time — a lock file replaced by another process
is never deleted. The same check is applied before a *stale* takeover, so a
file that was replaced between the staleness verdict and the unlink is left
alone. A transiently blocked unlink (e.g. an antivirus scanner holding the
file) is retried with bounded backoff; if it persistently fails, the file is
tombstoned with non-JSON content so the fresh-mtime staleness rule takes it
over within ``STALE_MTIME_AGE``.

Standard library only. Poll interval and maximum wait are module constants
overridable per instance (tests use short values).
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Optional

from . import runtime_state
from .pod_registry import default_home

POLL_INTERVAL = 5.0
MAX_WAIT = 300.0
STALE_MTIME_AGE = 60.0
#: A same-host lock older than this is stale whatever its PID says. Well above
#: any real provisioning run (a cold pod's docker pull is ~10 min) so it can
#: never cut a live provisioning short, and low enough that a wedged lock does
#: not block the machine for ever.
STALE_AGE = 3600.0
UNLINK_RETRY_BACKOFF = (0.1, 0.2, 0.4, 0.8, 1.6)


class LockTimeoutError(RuntimeError):
    """Raised when the lock could not be acquired within the maximum wait."""


class ProvisioningLock:
    """Advisory, crash-safe file lock guarding the pod-creation path."""

    def __init__(
        self,
        path: Optional[Path] = None,
        poll_interval: float = POLL_INTERVAL,
        max_wait: float = MAX_WAIT,
    ) -> None:
        self._path = (
            Path(path) if path is not None else default_home() / "provision.lock"
        )
        self._poll_interval = poll_interval
        self._max_wait = max_wait
        self._acquired_mtime: Optional[float] = None
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self, intent: str = "provision", pod_name: str = "") -> bool:
        """Acquire the lock, polling until success or *max_wait* elapses.

        Returns True once held. Raises :class:`LockTimeoutError` (with an
        actionable message) when the wait expires. Never deletes a lock file
        that is not provably stale.
        """
        if self._held:
            return True
        deadline = time.monotonic() + self._max_wait
        while True:
            if self._try_create(intent, pod_name):
                return True
            if time.monotonic() >= deadline:
                raise LockTimeoutError(
                    "Another launcher instance appears to be provisioning a "
                    f"RunPod pod (lock held: {self._path}). Waited "
                    f"{self._max_wait:.0f}s. If no provisioning is actually in "
                    "progress, verify no other launcher is running, then remove "
                    "the stale lock file and retry."
                )
            if self._stale():
                self._remove_stale()
            time.sleep(self._poll_interval)

    def release(self) -> None:
        """Release the lock (idempotent).

        Unlinks the file only while its mtime matches the mtime recorded at
        acquire time, so a lock replaced by another process is never deleted.
        The unlink is retried with bounded backoff against transient
        failures, and tombstoned as a last resort so a failed delete can
        never dead-lock provisioning while this process lives.
        """
        if not self._held:
            return
        self._held = False
        try:
            st = self._path.stat()
        except OSError:
            return
        if st.st_mtime != self._acquired_mtime:
            return
        for delay in (0.0, *UNLINK_RETRY_BACKOFF):
            if delay:
                time.sleep(delay)
            try:
                self._path.unlink()
                return
            except OSError:
                continue
        self._tombstone()

    def _tombstone(self) -> None:
        """Best-effort overwrite of our own lock with non-JSON content.

        The fresh-mtime staleness rule then expires the file within
        ``STALE_MTIME_AGE`` even if the owning process is still alive.
        """
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                fh.write("released")
        except OSError:
            pass

    def _try_create(self, intent: str, pod_name: str) -> bool:
        try:
            fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
        try:
            meta = {
                "version": 1,
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "acquired_at": time.time(),
                "intent": intent,
                "pod_name": pod_name,
            }
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(meta))
        except OSError:
            # Best effort: metadata is diagnostic only; the lock itself is
            # the exclusive file. Clean up on failure.
            try:
                self._path.unlink()
            except OSError:
                pass
            return False
        try:
            self._acquired_mtime = self._path.stat().st_mtime
        except OSError:
            self._acquired_mtime = None
        self._held = True
        return True

    def _read_meta(self) -> Optional[tuple]:
        """Return ``(pid, hostname, acquired_at)`` or None when unparseable."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        pid = data.get("pid")
        hostname = data.get("hostname")
        if not isinstance(pid, int) or not isinstance(hostname, str):
            return None
        acquired_at = data.get("acquired_at")
        if not isinstance(acquired_at, (int, float)):
            acquired_at = None
        return pid, hostname, acquired_at

    def _stale(self) -> bool:
        info = self._read_meta()
        if info is not None:
            pid, hostname, acquired_at = info
            if hostname == socket.gethostname():
                # Rule 1: same machine — stale when the PID is dead.
                if not runtime_state.pid_is_alive(pid):
                    return True
                # Rule 2: the PID is "alive", but a crashed owner's PID can be
                # recycled by any other process. An absolute age bound is the
                # only defence; without it the lock is held for ever.
                if acquired_at is not None:
                    return (time.time() - acquired_at) > STALE_AGE
                return False
            # Rule 3: a foreign hostname can never hold our local lock.
            return True
        # Rule 4: unparseable metadata — stale only when clearly old.
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return True
        return (time.time() - mtime) > STALE_MTIME_AGE

    def _remove_stale(self) -> None:
        """Unlink the stale lock, re-verifying it is still the same file.

        Between the staleness verdict and the unlink the owner can release and
        a third process can acquire: unlinking blindly would then delete a
        *fresh, valid* lock and let two provisioners run at once — the exact
        failure this class exists to prevent. ``release`` already guards its
        unlink this way; the stale path did not.
        """
        try:
            before = self._path.stat()
        except OSError:
            return
        if not self._stale():
            return
        try:
            after = self._path.stat()
        except OSError:
            return
        if (after.st_mtime, after.st_size) != (before.st_mtime, before.st_size):
            return
        try:
            self._path.unlink()
        except OSError:
            pass

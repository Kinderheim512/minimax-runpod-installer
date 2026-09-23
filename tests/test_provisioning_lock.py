"""Tests for the single-machine provisioning lock (launcher.provisioning_lock).

Uses real files, real threads, a real short-lived subprocess for the dead-PID
case, and ``os.utime`` for mtime-based staleness — but no network and no
RunPod calls. All waits use short poll intervals so the suite stays fast.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from launcher.provisioning_lock import LockTimeoutError, ProvisioningLock


def _lock(path=None, poll_interval=0.05, max_wait=2.0):
    return ProvisioningLock(path=path, poll_interval=poll_interval, max_wait=max_wait)


def _write_lock(path, *, pid=None, hostname=None, corrupt=False, acquired_at=None):
    """Write a lock file as if another (possibly foreign) process did."""
    if corrupt:
        path.write_bytes(b"\x00\x01definitely-not-json")
        return
    meta = {
        "version": 1,
        "pid": pid if pid is not None else os.getpid(),
        "hostname": hostname if hostname is not None else socket.gethostname(),
        "acquired_at": time.time() if acquired_at is None else acquired_at,
        "intent": "provision",
        "pod_name": "foreign-pod",
    }
    path.write_text(json.dumps(meta), encoding="utf-8")


def test_acquire_creates_metadata_and_release_removes(tmp_path) -> None:
    path = tmp_path / "provision.lock"
    lock = _lock(path=path)
    lock.acquire(intent="provision", pod_name="my-pod")
    try:
        assert path.exists()
        meta = json.loads(path.read_text(encoding="utf-8"))
        assert meta["pid"] == os.getpid()
        assert meta["hostname"] == socket.gethostname()
        assert meta["pod_name"] == "my-pod"
        assert meta["intent"] == "provision"
    finally:
        lock.release()
    assert not path.exists()


def test_blocked_then_succeeds_after_release(tmp_path) -> None:
    path = tmp_path / "provision.lock"
    holder = _lock(path=path, max_wait=10.0)
    holder.acquire()

    result = {}

    def waiter():
        wait = _lock(path=path, max_wait=10.0)
        try:
            wait.acquire()
            result["ok"] = True
        finally:
            wait.release()

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.2)  # waiter is now polling
    holder.release()
    t.join(timeout=10)
    assert result.get("ok") is True
    assert not path.exists()


def test_timeout_raises_actionable_and_never_deletes_held_lock(tmp_path) -> None:
    path = tmp_path / "provision.lock"
    holder = _lock(path=path, max_wait=10.0)
    holder.acquire()
    waiter = _lock(path=path, max_wait=0.3)
    with pytest.raises(LockTimeoutError) as exc:
        waiter.acquire()
    message = str(exc.value)
    assert "another" in message.lower()
    assert "provision" in message.lower()
    assert path.exists()  # the waiter must not destroy the holder's lock
    holder.release()


def test_dead_pid_same_hostname_takeover(tmp_path) -> None:
    out = subprocess.run(
        [sys.executable, "-c", "import os;print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    dead_pid = int(out.stdout.strip())  # process exited: the PID is dead
    path = tmp_path / "provision.lock"
    _write_lock(path, pid=dead_pid)  # same hostname, dead pid -> stale
    lock = _lock(path=path, max_wait=2.0)
    lock.acquire()
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        assert meta["pid"] == os.getpid()  # taken over by this process
    finally:
        lock.release()
    assert not path.exists()


def test_foreign_hostname_takeover_even_with_live_pid(tmp_path) -> None:
    path = tmp_path / "provision.lock"
    _write_lock(path, pid=os.getpid(), hostname="definitely-not-this-machine")
    lock = _lock(path=path, max_wait=2.0)
    lock.acquire()  # foreign hostname -> stale regardless of pid liveness
    lock.release()
    assert not path.exists()


def test_corrupt_old_mtime_is_taken_over(tmp_path) -> None:
    path = tmp_path / "provision.lock"
    _write_lock(path, corrupt=True)
    old = time.time() - 120.0
    os.utime(path, (old, old))  # > STALE_MTIME_AGE (60s)
    lock = _lock(path=path, max_wait=2.0)
    lock.acquire()
    lock.release()
    assert not path.exists()


def test_live_pid_but_ancient_lock_is_taken_over(tmp_path) -> None:
    """A recycled PID must not wedge provisioning for ever.

    The recorded PID is alive (it is *this* process), but the lock was
    acquired long before ``STALE_AGE``: the real owner crashed and its PID was
    reused. Without the age bound the lock looks held for ever and every new
    launcher burns the whole ``max_wait`` before failing.
    """
    import launcher.provisioning_lock as module

    path = tmp_path / "provision.lock"
    _write_lock(path, pid=os.getpid(), acquired_at=time.time() - (module.STALE_AGE + 60))
    lock = _lock(path=path, max_wait=2.0)
    lock.acquire()
    try:
        assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    finally:
        lock.release()
    assert not path.exists()


def test_recent_lock_with_a_live_pid_is_not_stolen(tmp_path) -> None:
    """The age bound must never cut a real provisioning run short."""
    path = tmp_path / "provision.lock"
    _write_lock(path, pid=os.getpid(), acquired_at=time.time())
    lock = _lock(path=path, max_wait=0.3)
    with pytest.raises(LockTimeoutError):
        lock.acquire()
    assert path.exists()


def test_stale_takeover_does_not_delete_a_fresh_lock(tmp_path, monkeypatch) -> None:
    """Re-verify before unlinking: a lock replaced mid-flight must survive.

    Between the staleness verdict and the unlink the owner can release and a
    third process can acquire; unlinking blindly would delete a fresh, valid
    lock and let two provisioners run at once.
    """
    import launcher.provisioning_lock as module

    path = tmp_path / "provision.lock"
    _write_lock(path, pid=os.getpid(), hostname="some-other-host")  # stale: rule 3
    lock = _lock(path=path)

    def replaced_mid_verdict(path_self):
        # The verdict was computed on the stale file; by the time we unlink,
        # another process has acquired a fresh lock.
        path.write_text("fresh lock " + "x" * 500, encoding="utf-8")
        return True

    monkeypatch.setattr(module.ProvisioningLock, "_stale", replaced_mid_verdict)
    lock._remove_stale()
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("fresh lock")


def test_corrupt_fresh_mtime_held_until_timeout(tmp_path) -> None:
    path = tmp_path / "provision.lock"
    _write_lock(path, corrupt=True)  # fresh mtime -> presumed live
    waiter = _lock(path=path, max_wait=0.3)
    with pytest.raises(LockTimeoutError):
        waiter.acquire()
    assert path.exists()  # never taken over while fresh


def test_two_threads_exactly_one_winner(tmp_path) -> None:
    path = tmp_path / "provision.lock"
    barrier = threading.Barrier(2)
    outcomes = []
    holder_windows = []
    timeout_times = []

    def attempt():
        barrier.wait()
        lock = _lock(path=path, poll_interval=0.05, max_wait=0.15)
        acquired_at = None
        try:
            lock.acquire()
            acquired_at = time.monotonic()
            outcomes.append(True)
            time.sleep(0.3)  # hold past the loser's deadline
        except LockTimeoutError:
            outcomes.append(False)
            timeout_times.append(time.monotonic())
        finally:
            released_at = time.monotonic()
            lock.release()
            if acquired_at is not None:
                holder_windows.append((acquired_at, released_at))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(outcomes) == 2
    holder_windows.sort()
    if len(holder_windows) == 2:
        # Two holders may never overlap, even if the race degrades into a
        # sequential hand-off under thread preemption.
        assert holder_windows[0][1] <= holder_windows[1][0]
    raced = any(s <= t <= e for t in timeout_times for s, e in holder_windows)
    if raced:
        # The loser timed out while the winner held the lock: the intended
        # race — exactly one winner.
        assert sorted(outcomes) == [False, True]
    assert not path.exists()


def test_release_on_exception_path(tmp_path) -> None:
    path = tmp_path / "provision.lock"
    lock = _lock(path=path)
    lock.acquire()
    try:
        try:
            raise RuntimeError("simulated crash mid-provisioning")
        except RuntimeError:
            pass
    finally:
        lock.release()
    assert not path.exists()


def test_no_steal_on_release_when_lock_replaced(tmp_path) -> None:
    path = tmp_path / "provision.lock"
    lock = _lock(path=path)
    lock.acquire()
    # Simulate another process overwriting the lock after our acquire. The
    # mtime is set explicitly: on coarse-granularity clocks (e.g. Windows
    # CI runners) two writes within one tick compare equal, which would
    # make release() mistake the replacement for our own lock.
    time.sleep(0.02)
    _write_lock(path, pid=os.getpid())
    later = time.time() + 5.0
    os.utime(path, (later, later))
    lock.release()
    assert path.exists()  # mtime changed -> we must not unlink someone else's lock
    path.unlink()


def test_release_retries_unlink_when_transiently_blocked(tmp_path, monkeypatch) -> None:
    import launcher.provisioning_lock as pl

    sleeps = []
    monkeypatch.setattr(pl.time, "sleep", lambda s: sleeps.append(s))
    real_unlink = pl.Path.unlink
    calls = {"n": 0}

    def flaky_unlink(self, *a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError(32, "file is being used by another process")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(pl.Path, "unlink", flaky_unlink)
    path = tmp_path / "provision.lock"
    lock = _lock(path=path)
    lock.acquire()
    lock.release()
    assert not path.exists()
    assert calls["n"] == 3
    assert len(sleeps) == 2 and all(s > 0 for s in sleeps)


def test_release_tombstones_when_unlink_persistently_fails(tmp_path, monkeypatch) -> None:
    import launcher.provisioning_lock as pl

    monkeypatch.setattr(pl.time, "sleep", lambda s: None)

    def broken_unlink(self, *a, **k):
        raise OSError(32, "file is being used by another process")

    monkeypatch.setattr(pl.Path, "unlink", broken_unlink)
    path = tmp_path / "provision.lock"
    lock = _lock(path=path)
    lock.acquire()
    lock.release()
    # The lock file remains but is no longer parseable metadata: the
    # fresh-mtime staleness rule takes it over after STALE_MTIME_AGE, so a
    # failed unlink can never dead-lock provisioning while this process lives.
    assert path.exists()
    raw = path.read_bytes()
    try:
        json.loads(raw)
        parsed = True
    except ValueError:
        parsed = False
    assert not parsed


def test_release_does_not_tombstone_someones_lock(tmp_path, monkeypatch) -> None:
    import launcher.provisioning_lock as pl

    monkeypatch.setattr(pl.time, "sleep", lambda s: None)
    real_unlink = pl.Path.unlink

    def broken_unlink(self, *a, **k):
        raise OSError(32, "file is being used by another process")

    monkeypatch.setattr(pl.Path, "unlink", broken_unlink)
    path = tmp_path / "provision.lock"
    lock = _lock(path=path)
    lock.acquire()
    _write_lock(path, pid=os.getpid())  # replaced by another writer
    later = time.time() + 5.0
    os.utime(path, (later, later))  # deterministically different mtime
    lock.release()
    meta = json.loads(path.read_text(encoding="utf-8"))
    assert meta["pid"] == os.getpid()  # untouched: no unlink, no tombstone
    real_unlink(path)

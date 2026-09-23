"""Persistent runtime-state registry for the MiniMax H3 Launcher.

Tracks the OS PIDs of processes started by this launcher (SSH tunnels and the
child process) across separate CLI invocations. A ``python -m launcher start``
process records PIDs; a later ``python -m launcher stop`` process reads them and
terminates the owned processes.

Design:

* State is a single JSON file at a local, launcher-scoped path (no secrets).
* Each entry records the PID, a human-readable label, the port it owns, and a
  creation timestamp. A per-entry ``marker`` (a short non-secret token) is
  recorded for reporting/verification but is not itself a secret.
* An entry may record the workload stack that owns it (``owner_stack``). That
  is what lets ``stop --stack X`` tear down only X's local processes while
  another stack's tunnel and the text-agent session keep running.
* ``stop`` only terminates PIDs that were recorded by the launcher (the file is
  only written by the launcher), and re-checks liveness first, so unrelated
  processes are never killed.
* Stale/dead PIDs are pruned safely; a successful ``stop`` removes the file.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import logging as launcher_logging

logger = launcher_logging.get_logger("minimax-launcher.runtime-state")


def _default_state_path() -> Path:
    """Return the launcher-local runtime-state file path.

    Uses the per-user temp directory so it works across CLI invocations and on
    Windows without requiring a config directory.
    """
    base = os.environ.get("MINIMAX_LAUNCHER_STATE_DIR")
    if base:
        return Path(base) / "runtime.json"
    return Path(tempfile.gettempdir()) / "minimax-launcher" / "runtime.json"


def pending_stop_path() -> Path:
    """Path of the pending pod-stop schedule (same dir as runtime.json).

    Holds ``{"deadline": <epoch>, "power_mode": <str>}`` so a stop scheduled
    by the launcher timer survives a launcher restart and fires on the next
    start (a missed deadline must not silently keep the pod billing).
    """
    return _default_state_path().parent / "pending_stop.json"


@dataclass(frozen=True)
class ProcessEntry:
    """A recorded launcher-owned process."""

    pid: int
    label: str
    port: int
    marker: str
    created_at: float
    #: Workload stack that owns this process (``""`` for legacy records and
    #: for processes shared by several stacks). Used by a targeted
    #: ``stop --stack X`` so it never tears down another stack's process.
    owner_stack: str = ""


class RuntimeState:
    """Read/write the persistent runtime-state registry."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _default_state_path()

    def load(self) -> dict[str, ProcessEntry]:
        """Return the recorded entries keyed by name (``tunnels``/``comfy``).

        Tolerant by construction: a hand-edited, truncated or wrongly-shaped
        state file must never raise. Every caller uses ``load()`` unguarded
        (``stop``, ``tunnel.stop``, recovery), so an
        exception here used to turn a plain shutdown into a traceback while the
        recorded tunnels kept running — and, worse, made the *next* write
        (load → mutate → save) overwrite the file with only its own entry.
        """
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(raw, dict):
            return {}
        result: dict[str, ProcessEntry] = {}
        for name, entry in raw.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            if "pid" not in entry:
                continue
            try:
                result[name] = ProcessEntry(
                    pid=int(entry["pid"]),
                    label=str(entry.get("label", "")),
                    port=int(entry.get("port", 0)),
                    marker=str(entry.get("marker", "")),
                    created_at=float(entry.get("created_at", 0.0)),
                    owner_stack=str(entry.get("owner_stack", "") or ""),
                )
            except (TypeError, ValueError):
                # One malformed entry must not hide the healthy ones: the
                # entries next to it are live processes that still need reaping.
                continue
        return result

    def save(self, entries: dict[str, ProcessEntry]) -> None:
        """Write *entries* to disk atomically, creating parents as needed.

        The content is written to a temp file in the same directory and then
        ``os.replace``-d over the state file: a crash mid-write (the launcher
        is routinely ``taskkill /F``-ed by its own cleanup) can never truncate
        the existing state, which would make the recorded PIDs — the only
        handle on owned tunnels/child processes — unfindable orphans.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._preserve_unreadable_state()
        data = {
            name: {
                "pid": e.pid,
                "label": e.label,
                "port": e.port,
                "marker": e.marker,
                "created_at": e.created_at,
                "owner_stack": e.owner_stack,
            }
            for name, e in entries.items()
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=self._path.name + ".", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, indent=2))
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _preserve_unreadable_state(self) -> None:
        """Move a corrupt state file aside before overwriting it.

        ``load()`` cannot tell "no file" from "unreadable file" (both return an
        empty mapping), and every writer is load → mutate → save. Replacing a
        corrupt file silently would therefore destroy the only record of live
        tunnels — which then become orphans the launcher can never reap. The
        bad file is kept, renamed, for the operator to inspect.
        """
        if not self._path.exists():
            return
        try:
            json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
        else:
            return
        backup = self._path.with_name(
            f"{self._path.name}.corrupt-{int(time.time())}"
        )
        try:
            os.replace(self._path, backup)
        except OSError:
            pass

    def clear(self) -> None:
        """Remove the runtime-state file."""
        try:
            self._path.unlink()
        except OSError:
            pass


def pid_is_alive(pid: int) -> bool:
    """Return True if a process with *pid* currently exists.

    Windows: ``os.kill(pid, 0)`` is not supported for signal 0; use the
    ``OpenProcess``/``GetExitCodeProcess`` approach via ctypes, falling back to
    ``os.kill`` on POSIX.

    The answer errs towards "alive": when liveness cannot be proven either way
    (access denied, an unreadable process table) the PID is reported alive, so
    a live process is never dropped from the state and never becomes
    unmanageable.
    """
    if pid is None or pid <= 0:
        # ``os.kill(0, sig)`` targets the *caller's process group* on POSIX —
        # a ``pid: 0`` record would otherwise turn ``stop`` into a signal to
        # the launcher itself.
        return False
    if os.name == "nt":
        return _pid_is_alive_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # A live process owned by another user: existence is proven, access is
        # not. Reporting it dead would drop a live record.
        return True
    except OSError:
        return False


def _pid_is_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_PARAMETER = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Explicit prototypes: the default restype is ``c_long`` (32-bit on
    # Windows), which truncates a 64-bit HANDLE — GetExitCodeProcess then gets
    # a bogus handle, fails, and a *live* process is reported dead.
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # OpenProcess returns NULL for both "no such PID" and "access denied":
        # only the last error tells them apart.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # Cannot prove death: keep the record rather than orphan the process.
            return True
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_created_at(pid: int) -> Optional[float]:
    """Creation time of *pid* as an epoch timestamp, or None when unknown.

    Used to tell "the process we recorded" from "a different process that
    happened to inherit the same PID": Windows recycles PIDs aggressively and
    the state file keeps entries indefinitely.
    """
    if pid is None or pid <= 0:
        return None
    if os.name == "nt":
        return _process_created_at_windows(pid)
    return _process_created_at_posix(pid)


def _process_created_at_windows(pid: int) -> Optional[float]:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = FILETIME()
        exit_time = FILETIME()
        kernel_time = FILETIME()
        user_time = FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        # FILETIME counts 100-ns intervals since 1601-01-01 UTC.
        return ticks / 10_000_000.0 - 11_644_473_600.0
    finally:
        kernel32.CloseHandle(handle)


def _process_created_at_posix(pid: int) -> Optional[float]:
    """Linux: ``/proc/<pid>/stat`` (field 22); macOS: ``ps -o lstart=``."""
    from_linux = _process_created_at_linux(pid)
    if from_linux is not None:
        return from_linux
    return _process_created_at_macos(pid)


def _process_created_at_linux(pid: int) -> Optional[float]:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        after_comm = stat.rsplit(")", 1)[1].split()
        start_ticks = int(after_comm[19])  # field 22 overall
        sysconf = getattr(os, "sysconf", None)
        if sysconf is None:
            return None
        ticks_per_second = sysconf("SC_CLK_TCK")
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("btime "):
                    boot_time = int(line.split()[1])
                    return boot_time + start_ticks / ticks_per_second
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return None


def _process_created_at_macos(pid: int) -> Optional[float]:
    """macOS has no ``/proc``: read the start time from ``ps -o lstart=``.

    ``ps`` renders the timestamp in the current locale, which is why the parse
    is attempted against both ``%a %b %d %H:%M:%S %Y`` (C locale) and the
    numeric ``%Y-%m-%d`` shape some builds emit; an unparsable answer returns
    ``None``, which the identity check treats as "cannot tell" (never as
    "recycled").
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or "").strip()
    if not text:
        return None
    import calendar
    import time as _time

    for fmt in ("%a %b %d %H:%M:%S %Y", "%b %d %H:%M:%S %Y"):
        try:
            parsed = _time.strptime(" ".join(text.split()), fmt)
        except ValueError:
            continue
        return float(calendar.timegm(parsed))
    return None


#: How far a recorded creation time may differ from the real one before the PID
#: is treated as recycled. Generous on purpose: the entry is written a moment
#: after the spawn, and the two clocks are not the same source.
PID_IDENTITY_TOLERANCE_S = 300.0


def pid_matches_record(pid: int, created_at: float) -> bool:
    """False only when *pid* is provably NOT the process we recorded.

    When the identity cannot be determined (no permission, no ``/proc``) this
    returns True: refusing to act on an unverifiable PID would leave owned
    tunnels running, which is worse than the risk it guards against.
    """
    if not created_at:
        return True
    actual = process_created_at(pid)
    if actual is None:
        return True
    return abs(actual - float(created_at)) <= PID_IDENTITY_TOLERANCE_S


def terminate_recorded_pid(entry: "ProcessEntry") -> bool:
    """Terminate the process a :class:`ProcessEntry` recorded, if it is still it.

    ``terminate_pid`` only knows a number, and Windows reuses numbers: a tunnel
    recorded days ago can name a completely unrelated process today, and
    ``taskkill /T /F`` would then kill that process *and its children*. The
    recorded creation time is the only evidence available, so it is checked
    before anything is killed.
    """
    if not pid_matches_record(entry.pid, entry.created_at):
        logger.warning(
            "PID %d was recycled (recorded at %.0f, now a different process); "
            "leaving it alone and dropping the stale record",
            entry.pid,
            entry.created_at,
        )
        return False
    return terminate_pid(entry.pid)


def terminate_pid(pid: int) -> bool:
    """Terminate the process *pid* (and its tree on Windows).

    Returns True only when the process is *confirmed* gone: the caller
    (``tunnel.stop``, ``orchestrator.stop``) drops the
    runtime-state entry on True, and that entry is the launcher's only handle
    on the process. Reporting success for a kill that failed (access denied,
    a protected process, a race with exit) used to orphan a live process that
    could never be found or killed again.
    """
    if pid is None or pid <= 0:
        return False
    if not pid_is_alive(pid):
        return False
    if os.name == "nt":
        import subprocess

        from . import winproc

        # taskkill /T terminates the tree; /F forces. No shell=True — argv list.
        winproc.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            os.kill(pid, 15)  # SIGTERM
        except OSError:
            return not pid_is_alive(pid)
    return _wait_until_dead(pid, timeout=5.0)


def _wait_until_dead(pid: int, timeout: float) -> bool:
    """Poll *pid* until it disappears or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while True:
        if not pid_is_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)

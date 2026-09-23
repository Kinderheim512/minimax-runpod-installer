"""Port ownership helpers for the MiniMax H3 Launcher.

Finds the process listening on a local port and — only after explicit user
approval, enforced by the callers — terminates that process (and its tree).
Used by the port pre-flight check: an external process holding the
configured port is never adopted silently; the launcher kills a
port occupant only when the user confirmed closing it.

Stdlib only, on every platform: ``netstat`` + ``tasklist`` on Windows,
``lsof`` (falling back to ``ss``) + ``ps`` on macOS and Linux. Every function
degrades to ``None``/``False`` instead of raising so a diagnostic failure can
never break a startup sequence.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from typing import Callable, Optional

from . import runtime_state
from . import winproc

# Console flags live in one place (``launcher.winproc``); re-exported here so
# the historical ``portctl.CREATE_NO_WINDOW`` name keeps working.
CREATE_NO_WINDOW = winproc.CREATE_NO_WINDOW

#: ``netstat`` / ``tasklist`` spawns stay console-free (windowed launcher).
_run = winproc.run
_pid_alive = runtime_state.pid_is_alive
_terminate_pid = runtime_state.terminate_pid


def _which(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def _run_text(argv: list[str], timeout: float = 15.0) -> str:
    """Run *argv* and return its stdout (empty string on any failure)."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def _find_listener_pid_posix(port: int) -> Optional[int]:
    """macOS/Linux: the PID owning a LISTEN socket on *port*.

    ``lsof`` is the portable answer where it exists (macOS ships it, most
    Linux distros do too). ``ss`` is the modern Linux fallback; it is the one
    that is actually installed on a slim container. Neither present means
    "unknown", which keeps the caller on its "unknown process" wording.
    """
    if _which("lsof"):
        out = _run_text(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]
        )
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None
    if _which("ss"):
        out = _run_text(["ss", "-lntpH", f"sport = :{port}"])
        match = re.search(r"pid=(\d+)", out)
        if match:
            return int(match.group(1))
    return None


def find_listener_pid(port: int) -> Optional[int]:
    """Return the PID listening on *port* (any address), or None.

    Windows: parsed from ``netstat -ano``. The state string is locale-
    dependent (``LISTENING`` / ``ÉCOUTE`` / ...) and the command's pipe
    output is encoded in the OEM code page (cp850 on French Windows), so the
    detection leans on the locale-independent marker of a listening socket —
    a zero foreign address — with the known state words as a bonus.

    macOS/Linux: ``lsof`` (``ss`` as a Linux fallback). Neither present means
    ``None``.
    """
    if os.name != "nt":
        return _find_listener_pid_posix(port)
    try:
        proc = _run(
            ["netstat", "-ano"],
            capture_output=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        raw = proc.stdout or b""
        lines = raw.decode("utf-8", "replace").splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    suffix = f":{port}"
    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0].upper()
        local = parts[1]
        foreign = parts[2]
        state = parts[3].upper()
        if proto not in ("TCP", "TCP6"):
            continue
        if not local.endswith(suffix):
            continue
        if state not in ("LISTENING", "ÉCOUTE") and not foreign.endswith(":0"):
            continue
        try:
            return int(parts[4])
        except ValueError:
            continue
    return None


def get_process_name(pid: Optional[int]) -> Optional[str]:
    """Return the image name of *pid* (e.g. ``node.exe``), or None."""
    if pid is None:
        return None
    if os.name != "nt":
        out = _run_text(["ps", "-o", "comm=", "-p", str(int(pid))])
        first = out.strip().splitlines()[0].strip() if out.strip() else ""
        return os.path.basename(first) or None
    try:
        proc = _run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        raw = proc.stdout or b""
        lines = [
            line.strip()
            for line in raw.decode("utf-8", "replace").splitlines()
            if line.strip()
        ]
    except (OSError, subprocess.SubprocessError):
        return None
    if not lines or not lines[0].startswith('"'):
        return None
    name = lines[0].split('"')[1]
    return name or None


def kill_port_owner(
    port: int,
    wait_timeout: float = 10.0,
    poll_interval: float = 0.5,
    probe: Optional[Callable[[int], bool]] = None,
) -> tuple[bool, Optional[int]]:
    """Terminate the process listening on *port* (tree kill).

    Returns ``(released, pid)``: *released* is True once the port no longer
    accepts connections; *pid* is the PID that was terminated (or None when
    no listener was found / the port was already free).

    The port must be verified occupied first; when it is busy but no
    listener could be identified, the port is left untouched (``False``) —
    the launcher never guesses at a PID to kill. Never raises.
    """
    check = probe or is_port_open
    if not check(port):
        return True, None
    pid = find_listener_pid(port)
    if pid is None or not _pid_alive(pid):
        return False, None
    try:
        _terminate_pid(pid)
    except OSError:
        return False, pid
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        if not check(port):
            return True, pid
        time.sleep(poll_interval)
    return (not check(port)), pid


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Return True if a TCP connection to *host*:*port* succeeds.

    Used to detect an already-running local service (the port pre-flight check
    and the readiness probes).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        except OSError:
            return False
        return True

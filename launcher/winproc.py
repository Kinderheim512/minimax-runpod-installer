"""Every subprocess the launcher spawns goes through this module.

The launcher ships as a **windowed** executable (``console=False`` in the
PyInstaller specs). On Windows, a console-less parent that spawns a console
application (``ssh.exe``, ``tar.exe``, ``scp.exe``, ``taskkill.exe``,
``icacls.exe``, ``npm.cmd``, ...) makes Windows allocate a *new visible
console window* for the child — ``capture_output=True`` does not prevent it.
The only way to keep the user's screen clean is to pass ``CREATE_NO_WINDOW``
to every spawn.

That flag used to be re-derived (or forgotten) in each module: the ComfyUI
start alone flashed a window per SSH probe and per uploaded asset. This module
is the single place that knows the Windows console flags; every other module
calls :func:`run` / :func:`Popen`, which default to *no window* and only opt
out through an explicit ``creationflags`` argument. ``tests/test_winproc.py``
enforces the invariant — no module outside this one may call ``subprocess``'s
spawning helpers directly.

Stdlib only, import-safe on every platform: on POSIX the Windows-only flags
degrade to no keyword at all (``subprocess`` rejects a non-zero
``creationflags`` there).
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import subprocess

#: Give the child its own console window (the Codex TUI needs one).
CREATE_NEW_CONSOLE = 0x00000010 if hasattr(subprocess, "CREATE_NEW_CONSOLE") else 0
#: Never allocate a console window for the child.
CREATE_NO_WINDOW = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
#: Start the child detached from this process (it must outlive the parent).
DETACHED_PROCESS = 0x00000008 if hasattr(subprocess, "DETACHED_PROCESS") else 0
#: Make the child its own process group (needed alongside DETACHED_PROCESS).
CREATE_NEW_PROCESS_GROUP = 0x00000200 if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else 0

#: Explicit "inherit the parent's console" opt-out. Zero is a valid
#: ``creationflags`` on every platform (``subprocess`` rejects only non-zero
#: values on POSIX), so an intentional console-inheriting spawn stays portable.
INHERIT_CONSOLE = 0


def hidden_kwargs() -> dict:
    """Keyword arguments that keep a child console-free.

    Empty on POSIX (the flag does not exist there) and
    ``{"creationflags": CREATE_NO_WINDOW}`` on Windows.
    """
    if CREATE_NO_WINDOW:
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def inherit_kwargs() -> dict:
    """Keyword arguments for a child that must *keep* the parent's console.

    The launcher only needs this when its own stdout is the terminal (the CLI
    start path, where the agent's output is meant to be visible). Passing the
    flag explicitly is what lets :func:`run` / :func:`Popen` default to hidden
    without ever overriding a deliberate choice.
    """
    return {"creationflags": INHERIT_CONSOLE}


def new_console_kwargs() -> dict:
    """Keyword arguments that give the child its own console window."""
    if CREATE_NEW_CONSOLE:
        return {"creationflags": CREATE_NEW_CONSOLE}
    return {}


def detached_kwargs() -> dict:
    """Keyword arguments for a detached, console-free child.

    ``DETACHED_PROCESS`` alone already implies no console; ``CREATE_NO_WINDOW``
    is kept alongside it so a start-marker process can never flash one either.
    """
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    if flags:
        return {"creationflags": flags}
    return {}


def _argv(argv):
    """Normalise *argv* to a list, leaving a shell command string untouched."""
    return argv if isinstance(argv, (str, bytes)) else list(argv)


def run(argv, **kwargs):
    """``subprocess.run`` that never opens a console window by default.

    An explicit ``creationflags`` in *kwargs* always wins, so a caller that
    needs the parent's console (or its own) stays in control.
    """
    if "creationflags" not in kwargs:
        kwargs.update(hidden_kwargs())
    return subprocess.run(_argv(argv), **kwargs)


def Popen(argv, **kwargs):
    """``subprocess.Popen`` that never opens a console window by default."""
    if "creationflags" not in kwargs:
        kwargs.update(hidden_kwargs())
    return subprocess.Popen(_argv(argv), **kwargs)

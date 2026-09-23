"""Audible alerts and desktop notifications for the MiniMax H3 Launcher.

Plays a short system sound asynchronously (no bundled audio file) and shows a
desktop notification, on every platform:

* Windows — ``winsound`` + a .NET tray balloon through PowerShell;
* macOS — ``afplay`` (a built-in system sound) + ``osascript`` (Notification
  Center);
* Linux — ``paplay``/``aplay`` + ``notify-send`` (freedesktop).

Never raises and never blocks: a sound or notification failure is silent by
design — the launcher must not break a startup sequence because an audio
device is missing or no notification daemon is running. A platform whose
helper is absent simply does nothing.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import os
import sys


def _spawn(argv: list[str]) -> bool:
    """Fire-and-forget a helper process; True when it was launched."""
    import subprocess

    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception:  # noqa: BLE001 - a missing helper is not an error
        return False
    return True


def _which(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def play_pod_ready() -> bool:
    """Play a short success chime ("the pod is finally up").

    Returns True when a sound was triggered, False otherwise (no audio device,
    no helper installed, or a playback failure).
    """
    try:
        if os.name == "nt":
            import winsound

            winsound.PlaySound(
                "SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC
            )
            return True
        if sys.platform == "darwin":
            for candidate in (
                "/System/Library/Sounds/Glass.aiff",
                "/System/Library/Sounds/Ping.aiff",
            ):
                if os.path.exists(candidate):
                    return _spawn(["afplay", candidate])
            return _spawn(["osascript", "-e", "beep"])
        if _which("paplay"):
            for candidate in (
                "/usr/share/sounds/freedesktop/stereo/complete.oga",
                "/usr/share/sounds/freedesktop/stereo/bell.oga",
            ):
                if os.path.exists(candidate):
                    return _spawn(["paplay", candidate])
        if _which("aplay"):
            for candidate in (
                "/usr/share/sounds/alsa/Front_Center.wav",
                "/usr/share/sounds/alsa/Noise.wav",
            ):
                if os.path.exists(candidate):
                    return _spawn(["aplay", "-q", candidate])
        return False
    except Exception:  # noqa: BLE001 - a sound is never fatal
        return False


#: Balloon shown through the .NET notification area. The text travels in
#: environment variables (never interpolated into the script), so no title or
#: message can escape into the shell.
_NOTIFY_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$n = New-Object System.Windows.Forms.NotifyIcon;"
    "$n.Icon = [System.Drawing.SystemIcons]::Information;"
    "$n.Visible = $true;"
    "$n.ShowBalloonTip(8000, $env:MINIMAX_NOTIFY_TITLE,"
    "$env:MINIMAX_NOTIFY_MESSAGE,"
    "[System.Windows.Forms.ToolTipIcon]::Info);"
    "Start-Sleep -Seconds 9;"
    "$n.Dispose()"
)


#: AppleScript for Notification Center. The text is passed as ``argv`` items,
#: never interpolated into the script, so no title or message can escape into
#: the AppleScript source.
_MACOS_NOTIFY_SCRIPT = (
    "on run argv\n"
    "display notification (item 2 of argv) with title (item 1 of argv)\n"
    "end run"
)


def _notify_windows(title: str, message: str) -> bool:
    """Windows: a .NET tray balloon through a detached, hidden PowerShell."""
    import subprocess

    from . import winproc

    env = dict(os.environ)
    env["MINIMAX_NOTIFY_TITLE"] = (title or "MiniMax H3 Launcher")[:120]
    env["MINIMAX_NOTIFY_MESSAGE"] = (message or "")[:400]
    try:
        winproc.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _NOTIFY_SCRIPT,
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **winproc.detached_kwargs(),
        )
    except Exception:
        return False
    return True


def _notify_macos(title: str, message: str) -> bool:
    if not _which("osascript"):
        return False
    return _spawn(
        [
            "osascript", "-e", _MACOS_NOTIFY_SCRIPT,
            (title or "MiniMax H3 Launcher")[:120],
            (message or "")[:400],
        ]
    )


def _notify_linux(title: str, message: str) -> bool:
    if not _which("notify-send"):
        return False
    return _spawn(
        [
            "notify-send",
            (title or "MiniMax H3 Launcher")[:120],
            (message or "")[:400],
        ]
    )


def notify_windows(title: str, message: str) -> bool:
    """Show a desktop notification (best effort, never raises).

    The fallback used when the launcher has no tray icon (the optional
    ``tray`` extra is not installed). Windows uses a PowerShell balloon, macOS
    Notification Center through ``osascript``, Linux ``notify-send``. Returns
    True when the helper was launched — the notification itself is
    fire-and-forget, so a closed session simply shows nothing. The name is
    kept for the call sites; the implementation is platform-dispatched.
    """
    try:
        if os.name == "nt":
            return _notify_windows(title, message)
        if sys.platform == "darwin":
            return _notify_macos(title, message)
        return _notify_linux(title, message)
    except Exception:  # noqa: BLE001 - a notification is never fatal
        return False

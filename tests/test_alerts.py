"""Tests for the audible alert helpers (launcher.alerts)."""

from launcher import alerts


def test_play_pod_ready_is_silent_when_no_helper_exists(monkeypatch) -> None:
    """A platform with no sound helper is a no-op, never an error.

    The alert is no longer Windows-only (``afplay`` on macOS, ``paplay`` /
    ``aplay`` on Linux), so the old "non-Windows is always False" assertion
    stopped describing the behaviour. What must hold on every platform is that
    a missing helper degrades to silence — which means *both* the PATH probe
    and the spawner have to come up empty, including the macOS ``osascript``
    fallback that runs when no ``afplay`` sound file exists.
    """
    monkeypatch.setattr(alerts.os, "name", "posix")
    monkeypatch.setattr(alerts, "_which", lambda _name: False)
    monkeypatch.setattr(alerts.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(alerts, "_spawn", lambda _argv: False)
    assert alerts.play_pod_ready() is False


def test_play_pod_ready_never_raises(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("no audio device")

    monkeypatch.setattr(alerts, "_spawn", boom)
    assert alerts.play_pod_ready() in (True, False)



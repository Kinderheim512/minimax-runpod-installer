"""Tests for the audible alert helpers (launcher.alerts)."""

from launcher import alerts


def test_play_pod_ready_non_windows_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(alerts.os, "name", "posix")
    assert alerts.play_pod_ready() is False



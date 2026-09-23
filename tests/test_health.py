"""Tests for the stack readiness probes (launcher.health)."""

import json

import pytest

from launcher.health import (
    HealthError,
    check_comfy,
    check_train,
    get_comfy_queue,
    get_comfy_stats,
    wait_comfy,
    wait_train,
)


class _FakeResponse:
    def __init__(self, body: bytes = b"", status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        return None


def _install_urlopen(monkeypatch, responder):
    def fake_urlopen(req, timeout):
        return responder(req)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def _json_responder(payload, status: int = 200):
    body = json.dumps(payload).encode("utf-8")

    def responder(_req):
        return _FakeResponse(body, status)

    return responder


# --------------------------------------------------------------- ComfyUI ----


def test_check_comfy_true_on_system_stats(monkeypatch) -> None:
    _install_urlopen(monkeypatch, _json_responder({"system": {"os": "linux"}}))
    assert check_comfy("http://127.0.0.1:8188") is True


def test_check_comfy_false_without_a_system_block(monkeypatch) -> None:
    _install_urlopen(monkeypatch, _json_responder({"devices": []}))
    assert check_comfy("http://127.0.0.1:8188") is False


def test_check_comfy_false_on_invalid_json(monkeypatch) -> None:
    _install_urlopen(monkeypatch, lambda _req: _FakeResponse(b"not json"))
    assert check_comfy("http://127.0.0.1:8188") is False


def test_check_comfy_false_when_unreachable(monkeypatch) -> None:
    import urllib.error

    def boom(_req):
        raise urllib.error.URLError("refused")

    _install_urlopen(monkeypatch, boom)
    assert check_comfy("http://127.0.0.1:8188") is False


def test_get_comfy_stats_degrades_to_empty(monkeypatch) -> None:
    import urllib.error

    def boom(_req):
        raise urllib.error.URLError("refused")

    _install_urlopen(monkeypatch, boom)
    assert get_comfy_stats("http://127.0.0.1:8188") == {}


def test_get_comfy_queue_counts_running_and_pending(monkeypatch) -> None:
    _install_urlopen(
        monkeypatch,
        _json_responder({"queue_running": [1, 2], "queue_pending": [3]}),
    )
    assert get_comfy_queue("http://127.0.0.1:8188") == {"running": 2, "pending": 1}


def test_wait_comfy_returns_once_the_probe_succeeds(monkeypatch) -> None:
    calls = {"n": 0}

    def responder(_req):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("not yet")
        return _FakeResponse(json.dumps({"system": {}}).encode("utf-8"))

    _install_urlopen(monkeypatch, responder)
    monkeypatch.setattr("launcher.health.time.sleep", lambda _s: None)
    wait_comfy("http://127.0.0.1:8188", timeout=5.0, interval=0.0)
    assert calls["n"] == 3


def test_wait_comfy_raises_with_an_actionable_message(monkeypatch) -> None:
    _install_urlopen(monkeypatch, _json_responder({}))
    monkeypatch.setattr("launcher.health.time.sleep", lambda _s: None)
    with pytest.raises(HealthError) as excinfo:
        wait_comfy("http://127.0.0.1:8188", timeout=0.0)
    assert "not ready" in str(excinfo.value)


# ------------------------------------------------------- training desktop ----


def test_check_train_treats_401_as_ready(monkeypatch) -> None:
    """KasmVNC answers 401 to an anonymous probe — that IS the ready signal."""
    import urllib.error

    def responder(req):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    _install_urlopen(monkeypatch, responder)
    assert check_train("http://127.0.0.1:6080") is True


def test_check_train_accepts_a_2xx(monkeypatch) -> None:
    _install_urlopen(monkeypatch, lambda _req: _FakeResponse(b"ok", 200))
    assert check_train("http://127.0.0.1:6080") is True


def test_check_train_rejects_other_http_statuses(monkeypatch) -> None:
    import urllib.error

    def responder(req):
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)

    _install_urlopen(monkeypatch, responder)
    assert check_train("http://127.0.0.1:6080") is False


def test_check_train_false_when_nothing_answers(monkeypatch) -> None:
    import urllib.error

    def boom(_req):
        raise urllib.error.URLError("refused")

    _install_urlopen(monkeypatch, boom)
    assert check_train("http://127.0.0.1:6080") is False


def test_wait_train_returns_once_the_desktop_answers(monkeypatch) -> None:
    calls = {"n": 0}

    def responder(_req):
        calls["n"] += 1
        if calls["n"] < 2:
            raise OSError("not yet")
        return _FakeResponse(b"", 401)

    _install_urlopen(monkeypatch, responder)
    monkeypatch.setattr("launcher.health.time.sleep", lambda _s: None)
    wait_train("http://127.0.0.1:6080", timeout=5.0, interval=0.0)
    assert calls["n"] == 2


def test_wait_train_raises_after_the_budget(monkeypatch) -> None:
    import urllib.error

    def boom(_req):
        raise urllib.error.URLError("refused")

    _install_urlopen(monkeypatch, boom)
    monkeypatch.setattr("launcher.health.time.sleep", lambda _s: None)
    with pytest.raises(HealthError):
        wait_train("http://127.0.0.1:6080", timeout=0.0)

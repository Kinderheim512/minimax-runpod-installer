"""Tests for the RunPod REST client (credential-independent)."""

import json

import pytest

from launcher.runpod import (
    AccessDeniedError,
    ApiError,
    ContractError,
    InsufficientFundsError,
    InvalidActionError,
    PlacementError,
    Pod,
    PodNotFoundError,
    RateLimitedError,
    RunPodClient,
    RunPodError,
    STATUS_ERROR,
    STATUS_EXITED,
    STATUS_RUNNING,
    STATUS_STARTING,
    SshEndpoint,
    TemplateNotFoundError,
    TransientApiError,
    TransientNetworkError,
    Wallet,
    _parse_iso_epoch,
    parse_pod,
)


def _pod_payload(**overrides):
    payload = {
        "id": "pod_abc123",
        "name": "my-pod",
        "status": "RUNNING",
        "actions": ["stop", "restart", "terminate"],
        "cudaVersion": "12.9",
        "gpu": {"id": "NVIDIA RTX A6000"},
        "ssh": {
            "proxy": {"host": "ssh.runpod.io", "port": 22, "username": "abc-123"},
            "direct": {"host": "1.2.3.4", "port": 2222, "username": "root"},
        },
    }
    payload.update(overrides)
    return payload


def test_parse_pod_full() -> None:
    pod = parse_pod(_pod_payload())
    assert pod.id == "pod_abc123"
    assert pod.name == "my-pod"
    assert pod.status == "RUNNING"
    assert pod.actions == ("stop", "restart", "terminate")
    assert pod.cuda_version == "12.9"
    assert pod.gpu_id == "NVIDIA RTX A6000"
    assert pod.is_running is True


def test_parse_pod_ssh_endpoints() -> None:
    pod = parse_pod(_pod_payload())
    assert pod.proxy_endpoint == SshEndpoint("ssh.runpod.io", 22, "abc-123")
    assert pod.direct_endpoint == SshEndpoint("1.2.3.4", 2222, "root")


def test_parse_pod_missing_ssh_is_none() -> None:
    pod = parse_pod(_pod_payload(ssh=None))
    assert pod.proxy_endpoint is None
    assert pod.direct_endpoint is None


def test_parse_pod_runtime_tcp_ssh_endpoint() -> None:
    # Live RunPod scenario: ssh.direct and ssh.proxy are empty, but the exposed
    # TCP 22 endpoint is present under runtime.ports.
    payload = _pod_payload(
        ssh={"proxy": None, "direct": None},
        runtime={
            "uptime": 3600,
            "ports": [
                {"private": 22, "public": 22138, "type": "tcp", "ip": "194.68.245.94"},
                {"private": 8000, "public": None, "type": "http", "ip": None},
            ],
        },
    )
    pod = parse_pod(payload)
    assert pod.direct_endpoint is None
    assert pod.tcp_ssh_endpoint == SshEndpoint("194.68.245.94", 22138, "root")
    assert pod.ssh_tunnel_endpoint() == SshEndpoint("194.68.245.94", 22138, "root")


def test_parse_pod_ssh_tunnel_endpoint_prefers_direct() -> None:
    # When both ssh.direct and runtime.ports are present, direct wins.
    payload = _pod_payload(
        runtime={
            "ports": [
                {"private": 22, "public": 22138, "type": "tcp", "ip": "194.68.245.94"},
            ],
        },
    )
    pod = parse_pod(payload)
    assert pod.ssh_tunnel_endpoint() == SshEndpoint("1.2.3.4", 2222, "root")


def test_parse_pod_no_ssh_endpoint_when_no_ports() -> None:
    payload = _pod_payload(ssh={"proxy": None, "direct": None}, runtime={"uptime": 1, "ports": []})
    pod = parse_pod(payload)
    assert pod.ssh_tunnel_endpoint() is None


def test_parse_pod_ignores_http_port_for_ssh() -> None:
    payload = _pod_payload(
        ssh={"proxy": None, "direct": None},
        runtime={"ports": [{"private": 8000, "public": 12345, "type": "http", "ip": "1.2.3.4"}]},
    )
    pod = parse_pod(payload)
    assert pod.tcp_ssh_endpoint is None  # only tcp/22 is considered SSH


def test_pod_is_startable() -> None:
    assert parse_pod(_pod_payload(status=STATUS_EXITED)).is_startable is True
    assert parse_pod(_pod_payload(status=STATUS_ERROR)).is_startable is True
    assert parse_pod(_pod_payload(status=STATUS_RUNNING)).is_startable is False


def test_pod_is_transient() -> None:
    assert parse_pod(_pod_payload(status=STATUS_STARTING)).is_transient is True


def test_client_requires_api_key() -> None:
    with pytest.raises(RunPodError):
        RunPodClient("")


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_get_pod_parses_response(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.get_header("Authorization")
        return _FakeResponse(_pod_payload())

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)

    client = RunPodClient("secret_key")
    pod = client.get_pod("pod_abc123")

    assert captured["url"] == "https://api.runpod.io/v2/pods/pod_abc123"
    assert captured["method"] == "GET"
    assert captured["auth"] == "Bearer secret_key"
    assert pod.status == "RUNNING"


def test_get_pod_404_raises_pod_not_found(monkeypatch) -> None:
    import urllib.error

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", None, _FakeResponse({"detail": "pod not found"})
        )

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(PodNotFoundError):
        RunPodClient("k").get_pod("missing")


def test_action_409_raises_invalid_action(monkeypatch) -> None:
    import urllib.error

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 409, "Conflict", None, _FakeResponse({"detail": "bad state"})
        )

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(InvalidActionError):
        RunPodClient("k").start_pod("pod_abc123")


def test_action_sends_correct_body(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = req.data
        return _FakeResponse(_pod_payload(status="EXITED"))

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)

    RunPodClient("k").start_pod("pod_abc123")

    assert json.loads(captured["body"]) == {"action": "start"}


def test_start_pod_no_free_gpu_maps_to_placement_error(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(
        monkeypatch,
        _HttpErrorFactory(400, "There are not enough free GPUs on the host machine to start this pod."),
    )
    with pytest.raises(PlacementError) as exc:
        client.start_pod("pod_abc123")
    assert "free GPU" in str(exc.value)
    assert len(scripted.requests) == 1


def test_start_pod_other_400_maps_to_api_error(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(
        monkeypatch,
        _HttpErrorFactory(400, "some other bad request"),
    )
    with pytest.raises(ApiError):
        client.start_pod("pod_abc123")
    assert len(scripted.requests) == 1


def test_wait_ready_returns_when_running(monkeypatch) -> None:
    states = iter(["STARTING", "RUNNING"])

    def fake_urlopen(req, timeout):
        return _FakeResponse(_pod_payload(status=next(states)))

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    pod = RunPodClient("k").wait_ready("pod_abc123", timeout=60, interval=0)
    assert pod.status == "RUNNING"


def test_wait_ready_raises_on_error(monkeypatch) -> None:
    def fake_urlopen(req, timeout):
        return _FakeResponse(_pod_payload(status="ERROR"))

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    with pytest.raises(RunPodError):
        RunPodClient("k").wait_ready("pod_abc123", timeout=60, interval=0)


def test_wait_ready_raises_on_timeout(monkeypatch) -> None:
    def fake_urlopen(req, timeout):
        return _FakeResponse(_pod_payload(status="STARTING"))

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    with pytest.raises(RunPodError):
        RunPodClient("k").wait_ready("pod_abc123", timeout=0.01, interval=0)


def test_wait_ready_fails_fast_on_exited(monkeypatch) -> None:
    def fake_urlopen(req, timeout):
        return _FakeResponse(_pod_payload(status="EXITED"))

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    with pytest.raises(RunPodError, match="EXITED"):
        RunPodClient("k").wait_ready("pod_abc123", timeout=60, interval=0)


def test_wait_ssh_endpoint_waits_until_mapping_populates(monkeypatch) -> None:
    no_mapping = _pod_payload(
        ssh={"proxy": {"host": "ssh.runpod.io", "port": 22, "username": "abc-123"}, "direct": None},
    )
    mapped = _pod_payload(status="RUNNING")  # ssh.direct populated

    scripted = _ScriptedUrn(
        _FakeResponse(no_mapping),
        _FakeResponse(no_mapping),
        _FakeResponse(mapped),
    )
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    pod = RunPodClient("k").wait_ssh_endpoint("pod_abc123", timeout=600, interval=5)

    assert pod.ssh_tunnel_endpoint() is not None
    assert len(scripted.requests) == 3  # waited two polls, succeeded on the third


def test_wait_ssh_endpoint_accepts_runtime_tcp_22_mapping(monkeypatch) -> None:
    # Mapping arrives via runtime.ports (private=22, tcp, public) with ssh.direct null.
    via_runtime = _pod_payload(
        ssh={"proxy": {"host": "ssh.runpod.io", "port": 22, "username": "abc-123"}, "direct": None},
        runtime={
            "uptime": 120,
            "ports": [{"private": 22, "public": 37338, "type": "tcp", "ip": "38.147.83.19"}],
        },
    )
    scripted = _ScriptedUrn(_FakeResponse(via_runtime))
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    pod = RunPodClient("k").wait_ssh_endpoint("pod_abc123", timeout=600, interval=5)

    assert pod.ssh_tunnel_endpoint() == SshEndpoint("38.147.83.19", 37338, "root")


def test_wait_ssh_endpoint_raises_on_timeout(monkeypatch) -> None:
    no_mapping = _pod_payload(
        ssh={"proxy": {"host": "ssh.runpod.io", "port": 22, "username": "abc-123"}, "direct": None},
    )

    def fake_urlopen(req, timeout):
        return _FakeResponse(no_mapping)

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    with pytest.raises(RunPodError, match="mapping"):
        RunPodClient("k").wait_ssh_endpoint("pod_abc123", timeout=0.01, interval=0)


class _NetworkErrorFactory:
    """Callable that raises a URLError wrapping an ssl.SSLError.

    Models a connection-level failure (TLS reset / EOF mid-protocol): the API
    never produced an HTTP response, so no _RawHttpError is available.
    """

    def __init__(self):
        import ssl
        import urllib.error

        self._error = urllib.error.URLError(
            ssl.SSLError(1010, "EOF occurred in violation of protocol (_ssl.c:1010)")
        )

    def __call__(self, req, timeout):
        raise self._error


# ---------------------------------------------------------------------------
# polling loops: tolerance for transient network errors
# ---------------------------------------------------------------------------


def test_wait_ready_survives_transient_network_errors(monkeypatch) -> None:
    scripted = _ScriptedUrn(
        _NetworkErrorFactory(),
        _NetworkErrorFactory(),
        _FakeResponse(_pod_payload(status="RUNNING")),
    )
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    pod = RunPodClient("k").wait_ready("pod_abc123", timeout=60, interval=0)

    assert pod.status == "RUNNING"
    assert len(scripted.requests) == 3


def test_wait_ready_exhausts_on_persistent_network_error(monkeypatch) -> None:
    scripted = _ScriptedUrn(_NetworkErrorFactory())
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    with pytest.raises(TransientNetworkError, match="not reachable"):
        RunPodClient("k").wait_ready("pod_abc123", timeout=0.01, interval=0)
    assert len(scripted.requests) >= 1


def test_wait_ready_fails_fast_on_pod_not_found(monkeypatch) -> None:
    scripted = _ScriptedUrn(
        _HttpErrorFactory(404, "nope"),
        _FakeResponse(_pod_payload(status="RUNNING")),
    )
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    with pytest.raises(PodNotFoundError):
        RunPodClient("k").wait_ready("pod_abc123", timeout=60, interval=0)
    assert len(scripted.requests) == 1  # non-transient errors are never retried


def test_wait_ssh_endpoint_survives_transient_network_errors(monkeypatch) -> None:
    no_mapping = _pod_payload(
        ssh={"proxy": {"host": "ssh.runpod.io", "port": 22, "username": "abc-123"}, "direct": None},
    )
    mapped = _pod_payload(status="RUNNING")  # ssh.direct populated

    scripted = _ScriptedUrn(
        _NetworkErrorFactory(),
        _FakeResponse(no_mapping),
        _NetworkErrorFactory(),
        _FakeResponse(mapped),
    )
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    pod = RunPodClient("k").wait_ssh_endpoint("pod_abc123", timeout=600, interval=5)

    assert pod.ssh_tunnel_endpoint() is not None
    assert len(scripted.requests) == 4


def test_wait_ssh_endpoint_exhausts_on_persistent_network_error(monkeypatch) -> None:
    scripted = _ScriptedUrn(_NetworkErrorFactory())
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    with pytest.raises(TransientNetworkError, match="not reachable"):
        RunPodClient("k").wait_ssh_endpoint("pod_abc123", timeout=0.01, interval=0)
    assert len(scripted.requests) >= 1


def _no_public_8188_payload():
    return _pod_payload(
        ssh={"proxy": {"host": "ssh.runpod.io", "port": 22, "username": "abc-123"}, "direct": None},
        runtime={"uptime": 60, "ports": []},
    )


def _public_8188_payload():
    return _pod_payload(
        runtime={
            "uptime": 300,
            "ports": [{"private": 8188, "public": 60936, "type": "http", "ip": "100.65.18.211"}],
        },
    )


def test_wait_public_port_waits_until_mapping_populates(monkeypatch) -> None:
    scripted = _ScriptedUrn(
        _FakeResponse(_no_public_8188_payload()),
        _FakeResponse(_no_public_8188_payload()),
        _FakeResponse(_public_8188_payload()),
    )
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    pod = RunPodClient("k").wait_public_port("pod_abc123", 8188, timeout=600, interval=5)

    assert pod.public_url_for_port(8188) == "https://100.65.18.211:60936"
    assert len(scripted.requests) == 3  # waited two polls, succeeded on the third


def test_wait_public_port_raises_on_timeout(monkeypatch) -> None:
    def fake_urlopen(req, timeout):
        return _FakeResponse(_no_public_8188_payload())

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    with pytest.raises(RunPodError, match="port"):
        RunPodClient("k").wait_public_port("pod_abc123", 8188, timeout=0.01, interval=0)


def test_wait_public_port_survives_transient_network_errors(monkeypatch) -> None:
    scripted = _ScriptedUrn(
        _NetworkErrorFactory(),
        _FakeResponse(_no_public_8188_payload()),
        _FakeResponse(_public_8188_payload()),
    )
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)

    pod = RunPodClient("k").wait_public_port("pod_abc123", 8188, timeout=600, interval=5)

    assert pod.public_url_for_port(8188) is not None
    assert len(scripted.requests) == 3


def test_get_pod_network_error_maps_to_transient_network_error(monkeypatch) -> None:
    scripted = _ScriptedUrn(_NetworkErrorFactory())
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)

    with pytest.raises(TransientNetworkError):
        RunPodClient("k").get_pod("pod_abc123")


def test_get_pod_connection_abort_maps_to_transient_network_error(monkeypatch) -> None:
    """A connection dropped after the request was sent is retryable, not fatal.

    ``urllib`` only wraps the *request* phase in ``URLError``: a server that
    accepts the TCP connection and then closes makes ``getresponse``/``read``
    raise a bare ``ConnectionAbortedError``. That escaped every caller that
    only knows ``RunPodError`` and crashed the launcher.
    """

    def boom(req, timeout):
        raise ConnectionAbortedError(10053, "aborted by the software in your host machine")

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", boom)
    with pytest.raises(TransientNetworkError, match="connection error"):
        RunPodClient("k").get_pod("pod_abc123")


def test_get_pod_truncated_body_maps_to_transient_network_error(monkeypatch) -> None:
    """``IncompleteRead`` is an ``HTTPException``, not an ``OSError``."""
    import http.client

    def boom(req, timeout):
        raise http.client.IncompleteRead(b"partial", 100)

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", boom)
    with pytest.raises(TransientNetworkError, match="protocol error"):
        RunPodClient("k").get_pod("pod_abc123")


# ---------------------------------------------------------------------------
# create_pod: request shape, error mapping, bounded retries
# ---------------------------------------------------------------------------


class _HttpErrorFactory:
    """Callable that raises a urllib HTTPError with the given code/detail/headers."""

    def __init__(self, code, detail, headers=None):
        import urllib.error

        self._code = code
        self._detail = detail
        self._headers = dict(headers) if headers else None
        self._error_cls = urllib.error.HTTPError

    def __call__(self, req, timeout):
        body = json.dumps(
            self._detail if isinstance(self._detail, (dict, list)) else {"detail": self._detail}
        ).encode("utf-8")

        class _Body:
            # Mirrors the real http.client response object closely enough:
            # urllib wraps ``fp`` in a closer whose __del__ calls close(), so
            # the fake must expose it (GC otherwise turns the missing method
            # into an unraisable exception and fails the test).
            def __init__(self):
                self.closed = False

            def read(self):
                return body

            def close(self):
                self.closed = True

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()
                return False

        raise self._error_cls(req.full_url, self._code, "error", self._headers, _Body())


class _ScriptedUrn:
    """Scripted urlopen: returns/raises a sequence per call, records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if len(self.responses) > 1:
            response = self.responses.pop(0)
        else:
            response = self.responses[0]
        if isinstance(response, (_HttpErrorFactory, _NetworkErrorFactory)):
            response(req, timeout)
        return response


def _scripted_client(monkeypatch, *responses):
    scripted = _ScriptedUrn(*responses)
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)
    client = RunPodClient("secret_key")
    sleeps = []
    client._sleep_fn = sleeps.append
    return client, scripted, sleeps


def test_create_pod_sends_exact_body(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(monkeypatch, _FakeResponse(_pod_payload(status="PROVISIONING")))
    pod = client.create_pod("my-pod", template_id="tmpl_1", gpu_id="NVIDIA A6000")

    req = scripted.requests[0]
    assert req.get_method() == "POST"
    assert req.full_url == "https://api.runpod.io/v2/pods"
    assert json.loads(req.data) == {
        "name": "my-pod",
        "templateId": "tmpl_1",
        "gpu": {"id": "NVIDIA A6000", "count": 1},
        "startSsh": True,
        "env": {},
    }
    assert pod.status == "PROVISIONING"


def test_create_pod_includes_data_center_preferences(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(monkeypatch, _FakeResponse(_pod_payload()))
    client.create_pod(
        "my-pod", template_id="tmpl_1", gpu_id="NVIDIA A6000",
        gpu_count=2, data_center_ids=("dc-a", "dc-b"),
    )
    assert json.loads(scripted.requests[0].data) == {
        "name": "my-pod",
        "templateId": "tmpl_1",
        "gpu": {"id": "NVIDIA A6000", "count": 2},
        "dataCenterIds": ["dc-a", "dc-b"],
        "startSsh": True,
        "env": {},
    }


def test_create_pod_sends_env_overrides(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(monkeypatch, _FakeResponse(_pod_payload()))
    client.create_pod(
        "p", template_id="t", gpu_id="NVIDIA A6000",
        env={"MAX_MODEL_LEN": "262144"},
    )
    body = json.loads(scripted.requests[0].data)
    assert body["env"] == {"MAX_MODEL_LEN": "262144"}
    assert "cloudType" not in body
    assert "supportPublicIp" not in body


def test_create_pod_env_defaults_to_empty_object(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(monkeypatch, _FakeResponse(_pod_payload()))
    client.create_pod("p", template_id="t", gpu_id="NVIDIA A6000")
    body = json.loads(scripted.requests[0].data)
    assert body["env"] == {}


def test_create_pod_404_maps_to_template_not_found_no_retry(monkeypatch) -> None:
    client, scripted, sleeps = _scripted_client(monkeypatch, _HttpErrorFactory(404, "template not found"))
    with pytest.raises(TemplateNotFoundError):
        client.create_pod("p", template_id="tmpl_missing", gpu_id="NVIDIA A6000")
    assert len(scripted.requests) == 1
    assert sleeps == []


def test_create_pod_422_maps_to_contract_error_with_errors(monkeypatch) -> None:
    detail = {"detail": {"errors": [{"loc": ["body", "gpu"], "msg": "gpu.id not a valid GPU", "type": "value_error"}]}}
    client, scripted, sleeps = _scripted_client(monkeypatch, _HttpErrorFactory(422, detail))
    with pytest.raises(ContractError) as exc:
        client.create_pod("p", template_id="t", gpu_id="bogus")
    assert "gpu.id not a valid GPU" in exc.value.errors
    assert len(scripted.requests) == 1
    assert sleeps == []


def test_create_pod_400_maps_to_placement_error_no_retry(monkeypatch) -> None:
    client, scripted, sleeps = _scripted_client(monkeypatch, _HttpErrorFactory(400, "no capacity in requested data centers"))
    with pytest.raises(PlacementError) as exc:
        client.create_pod("p", template_id="t", gpu_id="NVIDIA A6000")
    assert "no capacity" in str(exc.value)
    assert len(scripted.requests) == 1
    assert sleeps == []


def test_create_pod_402_maps_to_insufficient_funds(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(monkeypatch, _HttpErrorFactory(402, "insufficient balance"))
    with pytest.raises(InsufficientFundsError):
        client.create_pod("p", template_id="t", gpu_id="NVIDIA A6000")
    assert len(scripted.requests) == 1


def test_create_pod_403_maps_to_access_denied(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(monkeypatch, _HttpErrorFactory(403, "key lacks permission"))
    with pytest.raises(AccessDeniedError):
        client.create_pod("p", template_id="t", gpu_id="NVIDIA A6000")
    assert len(scripted.requests) == 1


def test_create_pod_429_waits_retry_after_floor_then_succeeds(monkeypatch) -> None:
    ok = _FakeResponse(_pod_payload(status="PROVISIONING"))
    client, scripted, sleeps = _scripted_client(monkeypatch, _HttpErrorFactory(429, "slow down", {"Retry-After": "2"}), ok)
    pod = client.create_pod("p", template_id="t", gpu_id="NVIDIA A6000")
    assert pod.status == "PROVISIONING"
    assert len(scripted.requests) == 2
    assert sleeps == [5.0]  # max(Retry-After=2, floor=5)


def test_create_pod_429_uses_large_retry_after(monkeypatch) -> None:
    ok = _FakeResponse(_pod_payload())
    client, scripted, sleeps = _scripted_client(monkeypatch, _HttpErrorFactory(429, "slow down", {"Retry-After": "30"}), ok)
    client.create_pod("p", template_id="t", gpu_id="NVIDIA A6000")
    assert sleeps == [30.0]


def test_create_pod_429_exhausts_to_rate_limited_error(monkeypatch) -> None:
    limited = _HttpErrorFactory(429, "slow down", {"Retry-After": "1"})
    client, scripted, sleeps = _scripted_client(monkeypatch, limited, limited, limited)
    with pytest.raises(RateLimitedError) as exc:
        client.create_pod("p", template_id="t", gpu_id="NVIDIA A6000")
    assert len(scripted.requests) == 3
    assert exc.value.retry_after == 1.0
    assert sleeps == [5.0, 5.0]  # floor applied each retry


def test_create_pod_5xx_retries_with_backoff_then_succeeds(monkeypatch) -> None:
    ok = _FakeResponse(_pod_payload())
    client, scripted, sleeps = _scripted_client(
        monkeypatch,
        _HttpErrorFactory(500, "backend exploded"),
        _HttpErrorFactory(502, "bad gateway"),
        ok,
    )
    client.create_pod("p", template_id="t", gpu_id="NVIDIA A6000")
    assert len(scripted.requests) == 3
    assert sleeps == [5.0, 10.0]


def test_create_pod_5xx_exhausts_to_transient_error_after_exactly_three(monkeypatch) -> None:
    server = _HttpErrorFactory(500, "backend exploded")
    client, scripted, sleeps = _scripted_client(monkeypatch, server, server, server)
    with pytest.raises(TransientApiError):
        client.create_pod("p", template_id="t", gpu_id="NVIDIA A6000")
    assert len(scripted.requests) == 3  # exactly 3 attempts, never a 4th
    assert sleeps == [5.0, 10.0]


def test_get_pod_is_never_retried_on_5xx(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(monkeypatch, _HttpErrorFactory(500, "boom"))
    with pytest.raises(ApiError):
        client.get_pod("pod_abc123")
    assert len(scripted.requests) == 1


def test_wait_ready_survives_a_5xx_and_keeps_polling(monkeypatch) -> None:
    """One Cloudflare 502 during a 600 s poll must not abort the startup.

    ``_request`` used to map every non-404/409 status to a plain ``ApiError``,
    which the polling loops do not catch.
    """
    ok = _FakeResponse(_pod_payload(status="RUNNING"))
    client, scripted, _ = _scripted_client(
        monkeypatch,
        _HttpErrorFactory(502, "bad gateway"),
        ok,
    )
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)
    pod = client.wait_ready("pod_abc123", timeout=600, interval=1)
    assert pod.status == "RUNNING"
    assert len(scripted.requests) == 2


def test_wait_ready_is_terminal_on_terminated(monkeypatch) -> None:
    """A pod terminated concurrently must not be polled for the full timeout."""
    client, _, _ = _scripted_client(
        monkeypatch, _FakeResponse(_pod_payload(status="TERMINATED"))
    )
    monkeypatch.setattr("launcher.runpod.time.sleep", lambda s: None)
    with pytest.raises(RunPodError, match="TERMINATED"):
        client.wait_ready("pod_abc123", timeout=600, interval=1)


def test_list_pods_raises_on_an_unrecognised_shape(monkeypatch) -> None:
    """An unparsed listing must not be reported as "no pods".

    ``pod_presence`` treats an empty list as proof of absence, so a silently
    swallowed payload made the launcher clear the registry record of a pod
    that was alive and billing.
    """
    client, _, _ = _scripted_client(
        monkeypatch, _FakeResponse({"result": "weird"})
    )
    with pytest.raises(ApiError, match="unrecognised shape"):
        client.list_pods()


def test_list_pods_accepts_the_common_envelope_shapes(monkeypatch) -> None:
    for key in ("pods", "items", "data"):
        client, _, _ = _scripted_client(
            monkeypatch, _FakeResponse({key: [_pod_payload()]})
        )
        assert [p.id for p in client.list_pods()] == ["pod_abc123"]


def test_parse_pod_without_an_id_raises_api_error() -> None:
    """A 200 body without an id raised a bare KeyError, not a RunPodError."""
    with pytest.raises(ApiError, match="missing 'id'"):
        parse_pod({"name": "x", "status": "RUNNING"})


def test_parse_iso_epoch_treats_a_naive_timestamp_as_utc() -> None:
    """``astimezone`` on a naive datetime assumes LOCAL time."""
    assert _parse_iso_epoch("2026-09-22T10:00:00") == _parse_iso_epoch(
        "2026-09-22T10:00:00Z"
    )


def test_ssh_endpoint_tolerates_partial_port_entries(monkeypatch) -> None:
    """RunPod sends partial ``runtime.ports`` entries while provisioning."""
    payload = _pod_payload(
        ssh={},
        runtime={
            "ports": [
                {"private": None, "type": "tcp", "public": "auto", "ip": "1.2.3.4"},
                {"private": "22/tcp", "type": "tcp", "public": 2222, "ip": "1.2.3.4"},
                {"private": 22, "type": "tcp", "public": 3333, "ip": "5.6.7.8"},
            ]
        },
    )
    pod = parse_pod(payload)
    assert pod.tcp_ssh_endpoint == SshEndpoint("5.6.7.8", 3333, "root")


def test_create_pod_retries_transient_network_error_then_succeeds(monkeypatch) -> None:
    client, scripted, sleeps = _scripted_client(
        monkeypatch,
        _NetworkErrorFactory(),
        _NetworkErrorFactory(),
        _FakeResponse(_pod_payload(status="PROVISIONING")),
    )

    pod = client.create_pod("p", template_id="t", gpu_id="NVIDIA A6000")

    assert pod.status == "PROVISIONING"
    assert len(scripted.requests) == 3
    assert sleeps == [5.0, 10.0]


def test_create_pod_network_error_exhausts_to_transient_error(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(monkeypatch, _NetworkErrorFactory())

    with pytest.raises(TransientApiError):
        client.create_pod("p", template_id="t", gpu_id="NVIDIA A6000")

    assert len(scripted.requests) == 3  # exactly 3 attempts, never a 4th


# ---------------------------------------------------------------------------
# terminate (204 empty body) and other new endpoints
# ---------------------------------------------------------------------------


def test_terminate_204_empty_body_returns_ack_without_refetch(monkeypatch) -> None:
    class _EmptyResponse(_FakeResponse):
        def __init__(self):
            super().__init__({})

        def read(self):
            return b""

    scripted = _ScriptedUrn(_EmptyResponse())
    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", scripted)
    pod = RunPodClient("k").terminate_pod("pod_abc123")

    assert len(scripted.requests) == 1  # no re-fetch after 204 (which would 404)
    assert scripted.requests[0].get_method() == "POST"
    assert pod.id == "pod_abc123"
    assert pod.status == "TERMINATED"


def test_get_template_success_and_404(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(monkeypatch, _FakeResponse({"id": "tmpl_1", "name": "forge"}))
    assert client.get_template("tmpl_1")["id"] == "tmpl_1"
    assert scripted.requests[0].full_url == "https://api.runpod.io/v2/templates/tmpl_1"

    client2, scripted2, _ = _scripted_client(monkeypatch, _HttpErrorFactory(404, "no such template"))
    with pytest.raises(TemplateNotFoundError):
        client2.get_template("tmpl_missing")


def test_list_pods_parses_pod_list(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(
        monkeypatch, _FakeResponse([_pod_payload(), _pod_payload(id="pod_2", name="other")])
    )
    pods = client.list_pods()
    assert [p.id for p in pods] == ["pod_abc123", "pod_2"]


def test_get_gpu_types_builds_catalog_query_string(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(monkeypatch, _FakeResponse([{"id": "NVIDIA A6000"}]))
    result = client.get_gpu_types()
    assert result == [{"id": "NVIDIA A6000"}]
    url = scripted.requests[0].full_url
    assert url.startswith("https://api.runpod.io/v2/catalog/gpus?")
    assert "include=AVAILABILITY" in url
    assert "product=POD" in url


def test_list_ssh_keys_parses_list(monkeypatch) -> None:
    client, scripted, _ = _scripted_client(monkeypatch, _FakeResponse([{"id": "k1", "name": "forge-key"}]))
    keys = client.list_ssh_keys()
    assert keys == [{"id": "k1", "name": "forge-key"}]
    assert scripted.requests[0].full_url == "https://api.runpod.io/v2/account/ssh-keys"


# ---------------------------------------------------------------------------
# parse_pod null-safety for provisioning/stopped pod shapes
# ---------------------------------------------------------------------------


def test_parse_pod_populated_data_center_and_cost() -> None:
    payload = _pod_payload(dataCenterId="dc-3", cost=1.5, disk=500)
    pod = parse_pod(payload)
    assert pod.data_center_id == "dc-3"
    assert pod.cost_per_hour == 1.5
    assert pod.disk == 500


def test_parse_pod_env_loaded() -> None:
    pod = parse_pod(_pod_payload(env={"MODEL_ID": "Qwen/Qwen3.8-27B-FP8"}))
    assert pod.env == {"MODEL_ID": "Qwen/Qwen3.8-27B-FP8"}


def test_parse_pod_env_absent_is_none() -> None:
    pod = parse_pod(_pod_payload())
    assert pod.env is None


def test_update_pod_env_reads_then_full_replaces(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(req, timeout):
        captured.setdefault("calls", []).append(
            (req.get_method(), req.full_url, req.data)
        )
        # GET returns the current env; PATCH returns the updated pod.
        if req.get_method() == "GET":
            return _FakeResponse(
                _pod_payload(env={"PUBLIC_KEY": "k1", "MODEL_ID": "old/model"})
            )
        return _FakeResponse(
            _pod_payload(env={"PUBLIC_KEY": "k1", "MODEL_ID": "new/model"})
        )

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)

    client = RunPodClient("k")
    pod = client.update_pod_env("pod_abc123", {"MODEL_ID": "new/model"})

    assert pod.env["MODEL_ID"] == "new/model"
    assert pod.env["PUBLIC_KEY"] == "k1"  # preserved by read-then-full-replace
    methods = [c[0] for c in captured["calls"]]
    assert methods == ["GET", "PATCH"]
    patch_method, patch_url, patch_body = captured["calls"][1]
    assert patch_method == "PATCH"
    assert patch_url == "https://api.runpod.io/v2/pods/pod_abc123"
    sent = json.loads(patch_body.decode("utf-8"))
    # The complete env (current + override) is sent back, never a partial object.
    assert sent == {"env": {"PUBLIC_KEY": "k1", "MODEL_ID": "new/model"}}


def test_update_pod_env_noop_skips_patch(monkeypatch) -> None:
    methods = []

    def fake_urlopen(req, timeout):
        methods.append(req.get_method())
        return _FakeResponse(
            _pod_payload(env={"MODEL_ID": "same/model"})
        )

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)

    client = RunPodClient("k")
    client.update_pod_env("pod_abc123", {"MODEL_ID": "same/model"})

    assert methods == ["GET"]  # no PATCH when nothing changes


# ---------------------------------------------------------------------------
# Lifecycle timestamps + wallet (GraphQL myself query)
# ---------------------------------------------------------------------------


def test_parse_pod_parses_lifecycle_timestamps() -> None:
    from datetime import datetime, timezone

    pod = parse_pod(
        _pod_payload(
            startedAt="2026-09-10T10:30:55.253Z",
            createdAt="2026-09-10T10:30:55.259Z",
        )
    )
    expected_start = datetime(2026, 9, 10, 10, 30, 55, 253000, tzinfo=timezone.utc).timestamp()
    expected_created = datetime(2026, 9, 10, 10, 30, 55, 259000, tzinfo=timezone.utc).timestamp()
    assert pod.started_at == expected_start
    assert pod.created_at == expected_created


def test_parse_pod_missing_timestamps_are_none() -> None:
    pod = parse_pod(_pod_payload())
    assert pod.started_at is None
    assert pod.created_at is None


def test_parse_iso_epoch_rejects_garbage() -> None:
    assert _parse_iso_epoch("not-a-date") is None
    assert _parse_iso_epoch(None) is None
    assert _parse_iso_epoch("") is None
    assert _parse_iso_epoch(12345) is None


def test_get_wallet_parses_payload(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return _FakeResponse(
            {
                "data": {
                    "myself": {
                        "email": "a@b.c",
                        "clientBalance": 5.4948,
                        "currentSpendPerHr": 0.541,
                        "spendLimit": 80,
                    }
                }
            }
        )

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)
    wallet = RunPodClient("k").get_wallet()

    assert captured["url"] == "https://api.runpod.io/graphql"
    assert "myself" in json.loads(captured["body"])["query"]
    assert wallet.balance == 5.4948
    assert wallet.spend_per_hour == 0.541
    assert wallet.spend_limit == 80
    assert wallet.email == "a@b.c"


def test_get_wallet_custom_base_derives_graphql_url(monkeypatch) -> None:
    urls = []

    def fake_urlopen(req, timeout):
        urls.append(req.full_url)
        return _FakeResponse({"data": {"myself": {}}})

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)
    RunPodClient("k", base_url="https://example.test/v2").get_wallet()
    assert urls == ["https://example.test/graphql"]


def test_get_wallet_null_fields_degrade_to_none(monkeypatch) -> None:
    def fake_urlopen(req, timeout):
        return _FakeResponse(
            {"data": {"myself": {"email": None, "clientBalance": None}}}
        )

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)
    wallet = RunPodClient("k").get_wallet()
    assert wallet == Wallet(balance=None, spend_per_hour=None, spend_limit=None, email=None)


def test_get_wallet_graphql_error_raises_api_error(monkeypatch) -> None:
    def fake_urlopen(req, timeout):
        return _FakeResponse({"data": None, "errors": [{"message": "nope"}]})

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ApiError, match="nope"):
        RunPodClient("k").get_wallet()


def test_get_wallet_http_error_raises_api_error(monkeypatch) -> None:
    import urllib.error

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", None, _FakeResponse({"detail": "denied"})
        )

    monkeypatch.setattr("launcher.runpod.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ApiError):
        RunPodClient("k").get_wallet()

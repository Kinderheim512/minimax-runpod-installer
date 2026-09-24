"""Tests for the startup orchestrator (launcher.orchestrator)."""

import threading
import time

import pytest

from dataclasses import replace

from launcher import orchestrator
from launcher.config import (
    DEFAULT_COMFY_TEMPLATE_ID,
    Config,
    RunPodConfig,
    Secrets,
    load_config,
)
from launcher.health import HealthError
from launcher.pod_registry import PodRecord, PodRegistry
from launcher.runtime_state import ProcessEntry, RuntimeState
from launcher.runpod import (
    ApiError,
    InvalidActionError,
    PlacementError,
    PodNotFoundError,
    RateLimitedError,
    RunPodError,
)
from launcher.tunnel import SshEndpoint, TunnelError, TunnelTarget


@pytest.fixture(autouse=True)
def _mock_wait_service(monkeypatch):
    """Prevent the orchestrator from performing real HTTP/health polling."""
    monkeypatch.setattr(
        "launcher.orchestrator.wait_comfy", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "launcher.orchestrator.wait_train", lambda *a, **k: None
    )


@pytest.fixture(autouse=True)
def _mock_port_checks(monkeypatch):
    """Default: local ports are FREE so ownership checks don't depend on real state.

    Individual ownership/conflict tests override these to simulate occupancy.
    """
    monkeypatch.setattr("launcher.orchestrator.is_port_open", lambda port: False)
    monkeypatch.setattr("launcher.orchestrator.is_port_free", lambda port: True)


@pytest.fixture(autouse=True)
def _mock_tcp_probe_autouse(monkeypatch):
    """Stub the SSH TCP reachability probe so tests never open real sockets.

    ``_establish_tunnel`` probes ``endpoint.host:port`` before spawning ssh;
    unit-test pods use 1.2.3.4, which would otherwise trigger a real (slow)
    connect attempt on every startup test.
    """
    monkeypatch.setattr(
        "launcher.orchestrator.can_connect",
        lambda port, host="127.0.0.1", timeout=3.0: True,
    )


def _config(**overrides) -> Config:
    import dataclasses

    cfg = load_config(env={})
    return dataclasses.replace(cfg, **overrides)


def _freeze_tunnel_retry(monkeypatch):
    """Collapse the tunnel re-spawn budget so a failure resolves in one attempt.

    The real ``_establish_tunnel`` retries for ~180s; these tests assert the
    failure outcome, not the retry pacing, so the budget is zeroed and sleep
    is disabled to keep them fast and deterministic.
    """
    monkeypatch.setattr("launcher.orchestrator._TUNNEL_ESTABLISH_BUDGET", 0.0)
    monkeypatch.setattr("launcher.orchestrator._TUNNEL_ESTABLISH_INTERVAL", 0.0)
    monkeypatch.setattr("launcher.orchestrator.time.sleep", lambda s: None)


def _mock_tcp_probe(monkeypatch, reachable: bool = True):
    """Stub the SSH TCP reachability probe (no real socket in unit tests)."""
    monkeypatch.setattr(
        "launcher.orchestrator.can_connect",
        lambda port, host="127.0.0.1", timeout=3.0: reachable,
    )


class FakePod:
    def __init__(self, status="RUNNING", direct=True):
        self.status = status
        self.is_startable = status in ("EXITED", "ERROR")
        self.is_running = status == "RUNNING"
        self.direct_endpoint = (
            SshEndpoint("1.2.3.4", 2222, "root") if direct else None
        )
        self.tcp_ssh_endpoint = None
        self.proxy_endpoint = SshEndpoint("ssh.runpod.io", 22, "abc")

    def ssh_tunnel_endpoint(self):
        return self.direct_endpoint or self.tcp_ssh_endpoint


class FakeRunPod:
    def __init__(self, pod):
        self._pod = pod
        self.get_calls = 0
        self.start_calls = 0
        self.wait_calls = 0

    def get_pod(self, pod_id):
        self.get_calls += 1
        return self._pod

    def start_pod(self, pod_id):
        self.start_calls += 1
        return FakePod("RUNNING")

    def wait_ready(self, pod_id, timeout, interval):
        self.wait_calls += 1
        return FakePod("RUNNING")

    def wait_ssh_endpoint(self, pod_id, timeout, interval):
        self.wait_calls += 1
        pod = self.get_pod(pod_id)
        if pod.ssh_tunnel_endpoint() is not None:
            return pod
        raise RunPodError(
            f"Pod {pod_id} did not receive a public SSH (22/tcp) mapping "
            f"within {timeout:.0f}s of becoming RUNNING."
        )

    def wait_public_port(self, pod_id, private_port, timeout, interval):
        self.wait_calls += 1
        pod = self.get_pod(pod_id)
        if pod.public_url_for_port(private_port) is not None:
            return pod
        raise RunPodError(
            f"Pod {pod_id} did not receive a public mapping for port "
            f"{private_port} within {timeout:.0f}s of becoming RUNNING."
        )


class FakeTunnels:
    def __init__(self, alive=True):
        self.alive = alive
        self.started = []
        self.stopped = False
        self.stopped_names = []

    def start(self, name, target, endpoint, key_path, connect_timeout):
        self.started.append((name, target, endpoint))

    def is_alive(self, name, target):
        return self.alive

    def stop(self, name):
        self.stopped_names.append(name)

    def stop_all(self):
        self.stopped = True


class FakeOpenFox:
    def __init__(self, already_running=False):
        self.started = False
        self.stopped = False
        self.waited = False
        self._already = already_running

    def start(self, **kwargs):
        self.started = True
        self.kwargs = kwargs

    def wait_ready(self, port):
        self.waited = True

    def stop(self):
        self.stopped = True


def _pod_config():
    from launcher.config import RunPodConfig

    return _config(runpod=RunPodConfig(pod_id="pod_abc"))


def test_tunnel_keeps_the_endpoint_when_the_refresh_reports_the_same(
    monkeypatch,
) -> None:
    _freeze_tunnel_retry(monkeypatch)
    _mock_tcp_probe(monkeypatch, reachable=False)

    class Tunnels:
        def __init__(self):
            self.started = []

        def start(self, name, target, endpoint, key_path, connect_timeout):
            self.started.append((endpoint.host, endpoint.port))

        def wait_alive(self, name, target, timeout):
            return True

        def is_alive(self, name, target):
            return True

        def stop(self, name):
            pass

    tunnels = Tunnels()
    endpoint = SshEndpoint("1.2.3.4", 22165, "root")
    orchestrator._establish_tunnel(
        "llm",
        TunnelTarget(8000, "127.0.0.1", 8000),
        endpoint,
        tunnels,
        None,
        10,
        refresh_endpoint=lambda: SshEndpoint("1.2.3.4", 22165, "root"),
    )
    assert tunnels.started == [("1.2.3.4", 22165)]


def test_establish_tunnel_retries_until_alive(monkeypatch) -> None:
    """A tunnel whose first attempts fail but later forwards is retried to success."""
    monkeypatch.setattr("launcher.orchestrator.time.sleep", lambda s: None)
    _mock_tcp_probe(monkeypatch)

    class RetryingTunnels:
        def __init__(self):
            self.starts = 0
            self.stops = 0

        def start(self, name, target, endpoint, key_path, connect_timeout):
            self.starts += 1

        def wait_alive(self, name, target, timeout=15.0, interval=0.5):
            return self.starts >= 3

        def stop(self, name):
            self.stops += 1

        def stderr_text(self, name):
            return "Connection refused"

    tunnels = RetryingTunnels()
    target = TunnelTarget(8000, "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")

    orchestrator._establish_tunnel("llm", target, endpoint, tunnels, None, 10)

    assert tunnels.starts == 3
    assert tunnels.stops == 2  # released after each failed attempt


def test_establish_tunnel_raises_after_budget_with_stderr(monkeypatch) -> None:
    """Persistent failure exhausts the budget and surfaces captured ssh stderr."""
    clock = {"t": 0.0}
    monkeypatch.setattr("launcher.orchestrator.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        "launcher.orchestrator.time.sleep",
        lambda s: clock.__setitem__("t", clock["t"] + s),
    )
    monkeypatch.setattr("launcher.orchestrator._TUNNEL_ESTABLISH_BUDGET", 180.0)
    monkeypatch.setattr("launcher.orchestrator._TUNNEL_ESTABLISH_INTERVAL", 5.0)
    _mock_tcp_probe(monkeypatch)

    class AlwaysFail:
        def __init__(self):
            self.starts = 0
            self.stops = 0

        def start(self, name, target, endpoint, key_path, connect_timeout):
            self.starts += 1

        def wait_alive(self, name, target, timeout=15.0, interval=0.5):
            return False

        def stop(self, name):
            self.stops += 1

        def stderr_text(self, name):
            return "Connection timed out"

    tunnels = AlwaysFail()
    target = TunnelTarget(8000, "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")

    with pytest.raises(orchestrator.OrchestrationError) as exc:
        orchestrator._establish_tunnel("llm", target, endpoint, tunnels, None, 10)

    msg = str(exc.value)
    assert "did not become alive" in msg
    assert "attempt" in msg
    assert "Connection timed out" in msg  # real ssh stderr is surfaced
    assert tunnels.starts >= 2  # actually retried, not a single-shot failure
    assert tunnels.stops >= 1


def test_establish_tunnel_aborts_fast_on_auth_failure(monkeypatch) -> None:
    """A credential/host-key rejection aborts immediately instead of retrying."""
    monkeypatch.setattr("launcher.orchestrator.time.sleep", lambda s: None)
    _mock_tcp_probe(monkeypatch)

    class AuthFail:
        def __init__(self):
            self.starts = 0
            self.stops = 0

        def start(self, name, target, endpoint, key_path, connect_timeout):
            self.starts += 1

        def wait_alive(self, name, target, timeout=15.0, interval=0.5):
            return False

        def stop(self, name):
            self.stops += 1

        def stderr_text(self, name):
            return "Permission denied (publickey)."

    tunnels = AuthFail()
    target = TunnelTarget(8000, "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")

    with pytest.raises(orchestrator.OrchestrationError) as exc:
        orchestrator._establish_tunnel("llm", target, endpoint, tunnels, None, 10)

    msg = str(exc.value)
    assert "authentication" in msg
    assert "Permission denied" in msg
    assert tunnels.starts == 1  # never retried: waiting cannot fix the key
    assert tunnels.stops == 1


#: The stderr OpenSSH prints when the pod behind a recycled RunPod endpoint
#: presents a host key that does not match the one recorded for that IP:port.
_CHANGED_HOST_KEY_STDERR = (
    "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\r\n"
    "Offending ECDSA key in C:\\Users\\me\\.ssh\\known_hosts:69\r\n"
    "Host key for [1.2.3.4]:22122 has changed and you have requested"
    " strict checking.\r\n"
    "Host key verification failed."
)


def test_establish_tunnel_purges_a_stale_host_key_and_recovers(monkeypatch) -> None:
    """A recycled RunPod endpoint (new pod, same IP:port) must not block a start."""
    monkeypatch.setattr("launcher.orchestrator.time.sleep", lambda s: None)
    _mock_tcp_probe(monkeypatch)
    purged: list[str] = []
    monkeypatch.setattr(
        "launcher.orchestrator.purge_known_host_entry",
        lambda spec: purged.append(spec) or True,
    )

    class RecycledEndpoint:
        def __init__(self):
            self.starts = 0
            self.stops = 0

        def start(self, name, target, endpoint, key_path, connect_timeout):
            self.starts += 1

        def wait_alive(self, name, target, timeout=15.0, interval=0.5):
            return self.starts >= 2  # the retry after the purge succeeds

        def stop(self, name):
            self.stops += 1

        def stderr_text(self, name):
            return _CHANGED_HOST_KEY_STDERR

    tunnels = RecycledEndpoint()
    target = TunnelTarget(8000, "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22122, "root")

    orchestrator._establish_tunnel("comfy", target, endpoint, tunnels, None, 10)

    assert purged == ["[1.2.3.4]:22122"]
    assert tunnels.starts == 2
    assert tunnels.stops == 1


def test_establish_tunnel_purges_each_host_key_at_most_once(monkeypatch) -> None:
    """Purging must not become an endless loop when the tunnel still fails."""
    monkeypatch.setattr("launcher.orchestrator.time.sleep", lambda s: None)
    _mock_tcp_probe(monkeypatch)
    purged: list[str] = []
    monkeypatch.setattr(
        "launcher.orchestrator.purge_known_host_entry",
        lambda spec: purged.append(spec) or True,
    )

    class StillFailing:
        def __init__(self):
            self.starts = 0
            self.stops = 0

        def start(self, name, target, endpoint, key_path, connect_timeout):
            self.starts += 1

        def wait_alive(self, name, target, timeout=15.0, interval=0.5):
            return False

        def stop(self, name):
            self.stops += 1

        def stderr_text(self, name):
            return _CHANGED_HOST_KEY_STDERR

    tunnels = StillFailing()
    target = TunnelTarget(8000, "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22122, "root")

    with pytest.raises(orchestrator.OrchestrationError) as exc:
        orchestrator._establish_tunnel("comfy", target, endpoint, tunnels, None, 10)

    assert purged == ["[1.2.3.4]:22122"]  # once, not once per attempt
    assert tunnels.starts == 2
    assert "authentication" in str(exc.value)


def test_establish_tunnel_aborts_when_the_stale_host_key_cannot_be_purged(monkeypatch) -> None:
    """Nothing purged (e.g. a hashed known_hosts entry) => the old fast abort."""
    monkeypatch.setattr("launcher.orchestrator.time.sleep", lambda s: None)
    _mock_tcp_probe(monkeypatch)
    monkeypatch.setattr(
        "launcher.orchestrator.purge_known_host_entry", lambda spec: False
    )

    class AuthFail:
        def __init__(self):
            self.starts = 0
            self.stops = 0

        def start(self, name, target, endpoint, key_path, connect_timeout):
            self.starts += 1

        def wait_alive(self, name, target, timeout=15.0, interval=0.5):
            return False

        def stop(self, name):
            self.stops += 1

        def stderr_text(self, name):
            return _CHANGED_HOST_KEY_STDERR

    tunnels = AuthFail()
    target = TunnelTarget(8000, "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22122, "root")

    with pytest.raises(orchestrator.OrchestrationError) as exc:
        orchestrator._establish_tunnel("comfy", target, endpoint, tunnels, None, 10)

    assert "authentication" in str(exc.value)
    assert tunnels.starts == 1  # never retried: waiting cannot fix the key
    assert tunnels.stops == 1


def test_establish_tunnel_does_not_purge_while_the_pod_is_booting(monkeypatch) -> None:
    """Connection refused/timeout (image still pulling) is never treated as a stale key."""
    monkeypatch.setattr("launcher.orchestrator.time.sleep", lambda s: None)
    _mock_tcp_probe(monkeypatch)
    purged: list[str] = []
    monkeypatch.setattr(
        "launcher.orchestrator.purge_known_host_entry",
        lambda spec: purged.append(spec) or True,
    )

    class Booting:
        def __init__(self):
            self.starts = 0

        def start(self, name, target, endpoint, key_path, connect_timeout):
            self.starts += 1

        def wait_alive(self, name, target, timeout=15.0, interval=0.5):
            return self.starts >= 2

        def stop(self, name):
            pass

        def stderr_text(self, name):
            return "ssh: connect to host 1.2.3.4 port 22122: Connection refused"

    tunnels = Booting()
    target = TunnelTarget(8000, "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22122, "root")

    orchestrator._establish_tunnel("comfy", target, endpoint, tunnels, None, 10)

    assert purged == []
    assert tunnels.starts == 2


def test_establish_tunnel_retries_when_start_port_unreleased(monkeypatch) -> None:
    """A start that hits a still-held local port is retried instead of aborting."""
    monkeypatch.setattr("launcher.orchestrator.time.sleep", lambda s: None)
    _mock_tcp_probe(monkeypatch)

    class FlakyStart:
        def __init__(self):
            self.starts = 0

        def start(self, name, target, endpoint, key_path, connect_timeout):
            self.starts += 1
            if self.starts == 1:
                raise TunnelError("Local port 8000 is already in use")

        def wait_alive(self, name, target, timeout=15.0, interval=0.5):
            return True

        def stop(self, name):
            pass

    tunnels = FlakyStart()
    target = TunnelTarget(8000, "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")

    orchestrator._establish_tunnel("llm", target, endpoint, tunnels, None, 10)

    assert tunnels.starts == 2  # first start hit the stale port, second succeeded


_DEAD_PID = 999_999_999  # never alive: keeps terminate_pid a no-op in tests


def _entry(name: str, port: int, owner_stack: str = "") -> ProcessEntry:
    return ProcessEntry(
        pid=_DEAD_PID,
        label=name,
        port=port,
        marker=name,
        created_at=0.0,
        owner_stack=owner_stack,
    )


def _concurrent_state(tmp_path):
    """Runtime state of a live text agent + a live ComfyUI tunnel."""
    state = RuntimeState(tmp_path / "runtime.json")
    state.save(
        {
            "openfox": _entry("openfox", 10369, owner_stack="agent"),
            "tunnels:llm": _entry("tunnels:llm", 8000, owner_stack="agent"),
            "tunnels:searxng": _entry("tunnels:searxng", 8888, owner_stack="agent"),
            "tunnels:comfy": _entry("tunnels:comfy", 8188, owner_stack="comfy"),
        }
    )
    return state


def _no_stop_action_pod(status: str):
    class Pod:
        pass

    pod = Pod()
    pod.status = status
    pod.actions = []
    return pod


def _fake_runpod_with_pod(pod):
    class RunPod:
        def get_pod(self, pod_id):
            return pod

    return RunPod()


def test_stop_registered_pod_already_stopped_reports_success(tmp_path) -> None:
    # A stopped pod (EXITED) offers no "stop" action but is not billing:
    # re-stopping it must count as success, not as "skipped".
    registry = _registry(tmp_path)
    registry.save(_record(pod_id="pod_stopped"), "comfy")
    outcome = orchestrator._stop_one_registered_pod(
        _fake_runpod_with_pod(_no_stop_action_pod("EXITED")), registry, "comfy", False
    )
    assert outcome == "already_stopped"
    assert registry.load("comfy") is not None


def test_stop_registered_pod_terminated_clears_record(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry.save(_record(pod_id="pod_term"), "comfy")
    outcome = orchestrator._stop_one_registered_pod(
        _fake_runpod_with_pod(_no_stop_action_pod("TERMINATED")), registry, "comfy", False
    )
    assert outcome == "cleared"
    assert registry.load("comfy") is None


def test_stop_registered_pod_running_without_stop_skips(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry.save(_record(pod_id="pod_run"), "comfy")
    outcome = orchestrator._stop_one_registered_pod(
        _fake_runpod_with_pod(_no_stop_action_pod("RUNNING")), registry, "comfy", False
    )
    assert outcome == "skipped"
    assert registry.load("comfy") is not None


def _secrets_config(hf_token: str) -> Config:
    import dataclasses

    cfg = load_config(env={"HF_TOKEN": hf_token})
    return dataclasses.replace(cfg, runpod=RunPodConfig(pod_id="pod_abc"))


class _SwitchingPod:
    def __init__(self, status="RUNNING", env=None):
        self.status = status
        self.env = env
        self.is_startable = status in ("EXITED", "ERROR")
        self.is_running = status == "RUNNING"
        self.actions = (
            ("stop", "start", "terminate") if status == "RUNNING"
            else ("start", "terminate")
        )
        self.direct_endpoint = SshEndpoint("1.2.3.4", 2222, "root")
        self.tcp_ssh_endpoint = None
        self.proxy_endpoint = None

    def ssh_tunnel_endpoint(self):
        return self.direct_endpoint or self.tcp_ssh_endpoint


class _SwitchingRunPod:
    def __init__(self, pod):
        self._pod = pod
        self.stop_calls = 0
        self.start_calls = 0
        self.update_calls = []

    def get_pod(self, pod_id):
        return self._pod

    def stop_pod(self, pod_id):
        self.stop_calls += 1
        self._pod = _SwitchingPod("EXITED", env=self._pod.env)

    def update_pod_env(self, pod_id, env):
        self.update_calls.append(dict(env))
        self._pod = _SwitchingPod("EXITED", env=dict(env))
        return self._pod

    def start_pod(self, pod_id):
        self.start_calls += 1
        self._pod = _SwitchingPod("RUNNING", env=self._pod.env)
        return self._pod

    def wait_ready(self, pod_id, timeout, interval):
        return self._pod

    def wait_ssh_endpoint(self, pod_id, timeout, interval):
        return self._pod


# ---------------------------------------------------------------------------
# Automatic RunPod pod provisioning
# ---------------------------------------------------------------------------


class ProvPod:
    """Pod-shaped fake for provisioning tests (id, actions, data center)."""

    def __init__(
        self,
        id,
        status,
        actions=("stop", "terminate"),
        gpu_id=None,
        data_center_id=None,
        direct=True,
    ):
        self.id = id
        self.name = id
        self.status = status
        self.actions = tuple(actions)
        self.gpu_id = gpu_id
        self.data_center_id = data_center_id
        self.direct_endpoint = SshEndpoint("1.2.3.4", 2222, "root") if direct else None
        self.tcp_ssh_endpoint = None
        self.proxy_endpoint = None

    @property
    def is_running(self):
        return self.status == "RUNNING"

    @property
    def is_startable(self):
        return self.status in ("EXITED", "ERROR")

    def ssh_tunnel_endpoint(self):
        return self.direct_endpoint or self.tcp_ssh_endpoint


class FakeLock:
    """Records acquire/release; always acquires immediately."""

    def __init__(self):
        self.acquired = []
        self.released = 0

    def acquire(self, intent="provision", pod_name=""):
        self.acquired.append((intent, pod_name))
        return True

    def release(self):
        self.released += 1


class RacyLock(FakeLock):
    """Simulates a competing launcher that finished provisioning first.

    Records another process's pod in the registry when we acquire the lock,
    forcing the in-lock recheck to win over our own create.
    """

    def __init__(self, registry, pod_id):
        super().__init__()
        self._registry = registry
        self._pod_id = pod_id

    def acquire(self, intent="provision", pod_name=""):
        self.acquired.append((intent, pod_name))
        self._registry.save(
            PodRecord(
                pod_id=self._pod_id,
                name="other-pod",
                created_at=1.0,
                gpu_id="NVIDIA A6000",
                gpu_count=1,
            )
        )
        return True


class FakeProvisionerRunPod:
    """RunPod client fake with the full provisioning surface (no network)."""

    def __init__(
        self,
        pod=None,
        created_pod=None,
        create_error=None,
        create_error_seq=None,
        stop_error=None,
        get_error=None,
        wait_result=None,
        terminate_error=None,
    ):
        self._pod = pod
        self._created_pod = created_pod
        self._create_error = create_error
        # Errors raised in order, one per create attempt (None = that
        # attempt succeeds). Lets a placement failure recover on a later
        # attempt.
        self._create_error_seq = list(create_error_seq or [])
        self._stop_error = stop_error
        self._get_error = get_error
        self._wait_result = wait_result
        self._terminate_error = terminate_error
        self.get_ids = []
        self.get_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.terminate_calls = 0
        self.wait_calls = 0
        self.wait_timeouts = []
        self.wait_ssh_calls = 0
        self.wait_ssh_timeouts = []
        self.create_calls = []

    def get_pod(self, pod_id):
        self.get_ids.append(pod_id)
        self.get_calls += 1
        if self._get_error is not None:
            raise self._get_error
        return self._pod

    def start_pod(self, pod_id):
        self.start_calls += 1
        return ProvPod(pod_id, "RUNNING")

    def stop_pod(self, pod_id):
        self.stop_calls += 1
        if self._stop_error is not None:
            raise self._stop_error

    def terminate_pod(self, pod_id):
        self.terminate_calls += 1
        if self._terminate_error is not None:
            raise self._terminate_error

    def wait_ready(self, pod_id, timeout, interval):
        self.wait_calls += 1
        self.wait_timeouts.append(timeout)
        if self._wait_result is not None:
            return self._wait_result
        return ProvPod(pod_id, "RUNNING")

    def wait_ssh_endpoint(self, pod_id, timeout, interval):
        self.wait_ssh_calls += 1
        self.wait_ssh_timeouts.append(timeout)
        pod = self.get_pod(pod_id)
        if pod.ssh_tunnel_endpoint() is not None:
            return pod
        raise RunPodError(
            f"Pod {pod_id} did not receive a public SSH (22/tcp) mapping "
            f"within {timeout:.0f}s of becoming RUNNING."
        )

    def create_pod(self, name, *, template_id, gpu_id, gpu_count=1, data_center_ids=(),
                   env=None):
        self.create_calls.append(
            dict(
                name=name,
                template_id=template_id,
                gpu_id=gpu_id,
                gpu_count=gpu_count,
                data_center_ids=tuple(data_center_ids),
                env=dict(env or {}),
            )
        )
        if self._create_error_seq:
            error = self._create_error_seq.pop(0)
            if error is not None:
                raise error
        elif self._create_error is not None:
            raise self._create_error
        if self._created_pod is not None:
            return self._created_pod
        return ProvPod("pod_new", "PROVISIONING")


class FailWaitRunPod(FakeProvisionerRunPod):
    """create succeeds; wait_ready raises (crash window after create)."""

    def __init__(self, pod, wait_error, **kwargs):
        super().__init__(pod, created_pod=pod, **kwargs)
        self._wait_error = wait_error

    def wait_ready(self, pod_id, timeout, interval):
        self.wait_calls += 1
        self.wait_timeouts.append(timeout)
        raise self._wait_error


def _registry(tmp_path):
    return PodRegistry(path=tmp_path / "pod.json")


def _record(pod_id="pod_reg"):
    return PodRecord(
        pod_id=pod_id,
        name="openfox-forge-test",
        created_at=1.0,
        gpu_id="NVIDIA A6000",
        gpu_count=1,
    )


def _prov_config(**runpod_overrides):
    """Config with provisioning secrets and no RUNPOD_POD_ID by default.

    Accepts RunPodConfig fields as kwargs, or a complete ``runpod=`` object.
    """
    import dataclasses

    runpod = runpod_overrides.pop("runpod", None)
    if runpod is None:
        rp = dict(gpu_id="NVIDIA A6000", gpu_count=1)
        rp.update(runpod_overrides)
        runpod = RunPodConfig(**rp)
    return dataclasses.replace(
        load_config(env={}),
        runpod=runpod,
        secrets=Secrets(runpod_api_key="rp_test_key", runpod_template_id="tmpl_forge"),
    )


def _start(cfg, runpod, registry, lock, tmp_path, tunnels=None):
    from launcher.runtime_state import RuntimeState

    return orchestrator.start(
        cfg,
        runpod=runpod,
        tunnels=tunnels or FakeTunnels(),
        open_browser=lambda u: None,
        state=RuntimeState(tmp_path / "runtime.json"),
        registry=registry,
        lock=lock,
    )


# --- Lock scoping -----------------------------------------------------------


# --- Pod resolution ---------------------------------------------------------


# --- Ownership: what is recorded -------------------------------------------


# --- Crash windows ----------------------------------------------------------


class _NoCapacityStartRunPod(FakeProvisionerRunPod):
    """get_pod returns an EXITED adopted pod; start_pod raises PlacementError."""

    def __init__(self, pod):
        super().__init__(pod)
        self._created_pod = ProvPod("pod_new", "PROVISIONING")

    def start_pod(self, pod_id):
        self.start_calls += 1
        raise PlacementError(
            "There are not enough free GPUs on the host machine to start this pod."
        )

    def get_pod(self, pod_id):
        self.get_calls += 1
        # After recreate, the new pod resolves; before that, the adopted one.
        if pod_id == "pod_new":
            return ProvPod("pod_new", "RUNNING")
        return self._pod


# --- OpenFox port pre-flight check (agent stack) -----------------------------


# --- Rollback of a launcher-created pod -------------------------------------


# ---------------------------------------------------------------------------
# Pod presence: a fresh 404 is not proof of deletion
# ---------------------------------------------------------------------------


def test_pod_presence_retries_a_fresh_404() -> None:
    # RunPod indexes a freshly created pod with a delay: get_pod can 404 while
    # the pod is alive. A single 404 must not be read as "gone".
    class _Flaky:
        def __init__(self):
            self.calls = 0

        def get_pod(self, pod_id):
            self.calls += 1
            if self.calls < 3:
                raise PodNotFoundError("not indexed yet")
            return ProvPod(pod_id, "RUNNING")

        def list_pods(self):
            return []

    client = _Flaky()
    pod, presence = orchestrator.pod_presence(
        client, "pod_new", created_at=time.time()
    )
    assert presence == "found"
    assert pod.id == "pod_new"
    assert client.calls == 3


def test_pod_presence_gives_up_on_a_fresh_pod_after_the_retries() -> None:
    class _Gone:
        def __init__(self):
            self.calls = 0

        def get_pod(self, pod_id):
            self.calls += 1
            raise PodNotFoundError("gone")

        def list_pods(self):
            return []

    client = _Gone()
    pod, presence = orchestrator.pod_presence(
        client, "pod_x", created_at=time.time()
    )
    assert (pod, presence) == (None, "gone")
    assert client.calls == orchestrator._POD_PRESENCE_ATTEMPTS


def test_pod_presence_does_not_retry_an_old_record() -> None:
    # An old record that 404s really is gone: no retry, no delay.
    class _Gone:
        def __init__(self):
            self.calls = 0

        def get_pod(self, pod_id):
            self.calls += 1
            raise PodNotFoundError("gone")

        def list_pods(self):
            return []

    client = _Gone()
    assert orchestrator.pod_presence(
        client, "pod_x", created_at=1.0
    ) == (None, "gone")
    assert client.calls == 1


def test_pod_presence_falls_back_to_the_pod_list() -> None:
    # The list endpoint can expose a pod that get_pod still 404s on.
    class _Hidden:
        def get_pod(self, pod_id):
            raise PodNotFoundError("not yet")

        def list_pods(self):
            return [ProvPod("pod_x", "RUNNING")]

    pod, presence = orchestrator.pod_presence(
        _Hidden(), "pod_x", created_at=1.0
    )
    assert presence == "found"
    assert pod.id == "pod_x"


def test_pod_presence_reports_unknown_on_an_api_error() -> None:
    class _Boom:
        def get_pod(self, pod_id):
            raise ApiError("RunPod API unreachable")

        def list_pods(self):
            return []

    assert orchestrator.pod_presence(
        _Boom(), "pod_x", created_at=time.time()
    ) == (None, "unknown")


def test_pod_presence_without_a_list_endpoint_trusts_get_pod() -> None:
    class _Minimal:
        def get_pod(self, pod_id):
            raise PodNotFoundError("gone")

    assert orchestrator.pod_presence(
        _Minimal(), "pod_x", created_at=1.0
    ) == (None, "gone")


# --- operational_status pod key ----------------------------------------------


def test_operational_status_reports_env_pod_id(tmp_path) -> None:
    from launcher.runtime_state import RuntimeState

    cfg = _pod_config()
    summary = orchestrator.operational_status(
        config=cfg,
        runpod=FakeRunPod(FakePod("RUNNING")),
        state=RuntimeState(tmp_path / "runtime.json"),
        registry=_registry(tmp_path),
    )
    assert summary["pod"] == "pod_abc"


def test_operational_status_pod_key_none(tmp_path) -> None:
    from launcher.runtime_state import RuntimeState

    cfg = _config()
    summary = orchestrator.operational_status(
        config=cfg,
        runpod=None,
        state=RuntimeState(tmp_path / "runtime.json"),
        registry=_registry(tmp_path),
    )
    assert summary["pod"] == "NONE"


# --- stop: pod management -----------------------------------------------------


def _stop(
    tmp_path, runpod=None, registry=None, config=None, terminate=False,
    tunnels=None, openfox=None, state=None, stack=None,
):
    from launcher.runtime_state import RuntimeState

    return orchestrator.stop(
        tunnels=tunnels or FakeTunnels(),
        openfox=openfox or FakeOpenFox(),
        state=state or RuntimeState(tmp_path / "runtime.json"),
        runpod=runpod,
        registry=registry,
        config=config,
        terminate=terminate,
        stack=stack,
    )


# ---------------------------------------------------------------------------
# ComfyUI stack (stack="comfy")
# ---------------------------------------------------------------------------


class ComfyPod:
    """Pod-shaped fake with a launcher-managed ComfyUI env and port mapping."""

    def __init__(self, status="RUNNING", env=None, direct=True, port_mappings=(), pod_id="pod_comfy"):
        self.id = pod_id
        self.status = status
        self.env = env
        self.is_startable = status in ("EXITED", "ERROR")
        self.is_running = status == "RUNNING"
        self.actions = (
            ("stop", "start", "terminate") if status == "RUNNING"
            else ("start", "terminate")
        )
        self.direct_endpoint = SshEndpoint("1.2.3.4", 2222, "root") if direct else None
        self.tcp_ssh_endpoint = None
        self.proxy_endpoint = None
        self.port_mappings = tuple(port_mappings)

    def ssh_tunnel_endpoint(self):
        return self.direct_endpoint or self.tcp_ssh_endpoint

    def public_url_for_port(self, private_port):
        for mapping in self.port_mappings:
            if mapping.private_port == private_port:
                url = mapping.public_url
                if url is not None:
                    return url
        return None


def _comfy_env(tier="auto") -> dict:
    """A pod env matching the launcher's default managed ComfyUI keys.

    Built from the real ``resolved_comfy_pod_env()`` so it always stays in
    sync with whatever keys the launcher manages (including the per-slot
    model URLs); only the tier is overridable to simulate a drift.
    (``H3_PRESETS`` used to play that role; it is retired — the launcher no
    longer sends a preset name.)
    """
    env = _comfy_config().resolved_comfy_pod_env()
    env["H3_TIER"] = tier
    return env


def _comfy_config(**overrides) -> Config:
    import dataclasses

    from launcher.config import ComfyConfig

    base = _config(**overrides)
    return dataclasses.replace(base, stack="comfy", comfy=dataclasses.replace(ComfyConfig()))


def _comfy_start(
    cfg,
    runpod,
    monkeypatch,
    tmp_path,
    tunnels=None,
    openfox=None,
    browsed=None,
    on_browse=None,
    drift_probe=None,
):
    from launcher.runtime_state import RuntimeState

    monkeypatch.setattr(orchestrator, "wait_comfy", lambda base_url: base_url)
    # The version-drift probe is five real SSH round-trips against the pod
    # endpoint (the fake pods point at 1.2.3.4:2222, which never answers):
    # without this every comfy-start test would burn 5 × ConnectTimeout.
    # Tests that care about the probe inject their own via ``drift_probe``.
    monkeypatch.setattr(
        orchestrator,
        "_warn_comfy_version_drift",
        drift_probe or (lambda *a, **k: None),
    )

    def _open(url):
        if on_browse is not None:
            on_browse()
        if browsed is not None:
            browsed.append(url)

    return orchestrator.start(
        cfg,
        runpod=runpod,
        tunnels=tunnels or FakeTunnels(alive=True),
        openfox=openfox or FakeOpenFox(),
        open_browser=_open,
        state=RuntimeState(tmp_path / "runtime.json"),
    )


class _ComfySwitchingRunPod:
    def __init__(self, pod):
        self._pod = pod
        self.stop_calls = 0
        self.start_calls = 0
        self.update_calls = []

    def get_pod(self, pod_id):
        return self._pod

    def stop_pod(self, pod_id):
        self.stop_calls += 1
        self._pod = ComfyPod("EXITED", env=self._pod.env, port_mappings=self._pod.port_mappings)

    def update_pod_env(self, pod_id, env):
        self.update_calls.append(dict(env))
        self._pod = ComfyPod("EXITED", env=dict(env), port_mappings=self._pod.port_mappings)
        return self._pod

    def start_pod(self, pod_id):
        self.start_calls += 1
        self._pod = ComfyPod("RUNNING", env=self._pod.env, port_mappings=self._pod.port_mappings)
        return self._pod

    def wait_ready(self, pod_id, timeout, interval):
        return self._pod

    def wait_ssh_endpoint(self, pod_id, timeout, interval):
        return self._pod

    def wait_public_port(self, pod_id, private_port, timeout, interval):
        return self._pod


def dataclasses_replace(cfg, **overrides):
    import dataclasses

    return dataclasses.replace(cfg, **overrides)


# ---------------------------------------------------------------------------
# Click and go: the template a fresh install deploys
# ---------------------------------------------------------------------------


def _provisioning_config(monkeypatch, *, template_id=None):
    """A comfy config with an API key and no private template id."""
    monkeypatch.delenv("RUNPOD_POD_ID", raising=False)
    monkeypatch.delenv("RUNPOD_COMFY_TEMPLATE_ID", raising=False)
    env = {"RUNPOD_API_KEY": "rp_test_key"}
    if template_id is not None:
        env["RUNPOD_COMFY_TEMPLATE_ID"] = template_id
    cfg = load_config(env=env)
    assert cfg.stack == "comfy"
    return dataclasses_replace(
        cfg, runpod=RunPodConfig(gpu_id="NVIDIA A6000", gpu_count=1)
    )


def test_a_fresh_install_deploys_the_public_template(tmp_path, monkeypatch) -> None:
    """Nothing configured -> ``create_pod`` gets the PUBLIC template id.

    This is the whole "click and go" promise: a first run needs only a RunPod
    API key, so the template id has to come from the launcher's own default
    rather than from a private id the user would have to create and paste.
    """
    cfg = _provisioning_config(monkeypatch)
    assert cfg.secrets.comfy_template_id == DEFAULT_COMFY_TEMPLATE_ID

    runpod = FakeProvisionerRunPod(
        pod=ProvPod("pod_new", "RUNNING"),
        created_pod=ProvPod("pod_new", "PROVISIONING"),
    )
    _start(cfg, runpod, _registry(tmp_path), FakeLock(), tmp_path)

    assert len(runpod.create_calls) == 1, runpod.create_calls
    call = runpod.create_calls[0]
    assert call["template_id"] == DEFAULT_COMFY_TEMPLATE_ID
    # The public id is the one the launcher ships; a typo here would deploy
    # nothing at all, so pin the literal too.
    assert call["template_id"] == "oa2vozqbum"


def test_a_configured_private_template_still_wins(tmp_path, monkeypatch) -> None:
    """An explicit private template id overrides the public default."""
    cfg = _provisioning_config(monkeypatch, template_id="tmpl_private")
    assert cfg.secrets.comfy_template_id == "tmpl_private"

    runpod = FakeProvisionerRunPod(
        pod=ProvPod("pod_new", "RUNNING"),
        created_pod=ProvPod("pod_new", "PROVISIONING"),
    )
    _start(cfg, runpod, _registry(tmp_path), FakeLock(), tmp_path)

    assert runpod.create_calls[0]["template_id"] == "tmpl_private"

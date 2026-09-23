"""Tests for the M8 P1 infrastructure recovery layer.

Fully offline: every lifecycle dependency is injected as a fake, so no real
RunPod/SSH/vLLM/OpenFox/SearXNG is touched. Covers the authorization gate,
precondition re-check, concurrency lock, post-action verification, bounded
timeouts, secret redaction, audit, and the full action matrix.
"""

import json
import os
from dataclasses import replace

import pytest

from launcher.config import ConfigError, RecoveryConfig, load_config
from launcher.infra_recover import (
    RECOVERY_ACTIONS,
    RecoveryAuthorizer,
    RecoveryEngine,
    RecoveryResult,
)
from launcher.recovery_audit import RecoveryAudit


def _config(**overrides):
    cfg = load_config(
        env={
            "RUNPOD_POD_ID": "pod_abc",
            "RUNPOD_API_KEY": "rp_secret_key",
            "SSH_KEY_PATH": r"C:\Users\me\.ssh\id_ed25519",
        }
    )
    if "recover_mode" in overrides:
        cfg = replace(cfg, recover=RecoveryConfig(mode=overrides["recover_mode"]))
    return cfg


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePod:
    def __init__(self, status="RUNNING", with_endpoint=True):
        self.status = status
        self._with_endpoint = with_endpoint

    @property
    def is_running(self):
        return self.status == "RUNNING"

    @property
    def is_startable(self):
        return self.status in ("EXITED", "ERROR")

    def ssh_tunnel_endpoint(self):
        if not self._with_endpoint:
            return None

        class _Ep:
            host = "1.2.3.4"
            port = 22
            username = "root"

        return _Ep()


class _FakeRunPod:
    def __init__(self, status="EXITED", start_error=None, wait_error=None,
                 with_endpoint=True):
        self._status = status
        self._start_error = start_error
        self._wait_error = wait_error
        self._with_endpoint = with_endpoint
        self.started = 0
        self.gets = 0
        self.get_ids = []

    def get_pod(self, pod_id):
        self.gets += 1
        self.get_ids.append(pod_id)
        return _FakePod(self._status, with_endpoint=self._with_endpoint)

    def start_pod(self, pod_id):
        self.started += 1
        if self._start_error:
            raise self._start_error
        self._status = "RUNNING"
        return _FakePod("RUNNING", with_endpoint=self._with_endpoint)

    def wait_ready(self, pod_id, timeout=120.0, interval=5.0):
        if self._wait_error:
            raise self._wait_error
        self._status = "RUNNING"
        return _FakePod("RUNNING", with_endpoint=self._with_endpoint)


class _FakeTunnels:
    def __init__(self, alive=False, start_error=None):
        self._alive = alive
        self._start_error = start_error
        self.started = 0
        self.names = []
        #: Targets of the last start/start_many, so a multi-port stack can
        #: assert that both ports went through ONE call.
        self.targets: list = []

    def start(self, name, target, endpoint, key_path, connect_timeout):
        self.started += 1
        self.names.append(name)
        self.targets = [target]
        if self._start_error:
            raise self._start_error
        self._alive = True

    def start_many(self, name, targets, endpoint, key_path, connect_timeout):
        self.started += 1
        self.names.append(name)
        self.targets = list(targets)
        if self._start_error:
            raise self._start_error
        self._alive = True

    def is_alive(self, name, target=None):
        # target=None is the multi-port form: every forwarded port must answer,
        # which this fake models with the single _alive flag.
        return self._alive

    def wait_alive(self, name, target=None, timeout=30.0, interval=0.5):
        return self._alive


class _FakeOpenFox:
    def __init__(self, running=False, start_error=None, wait_error=None):
        self._running = running
        self._start_error = start_error
        self._wait_error = wait_error
        self.started = 0
        self.stopped = 0
        self.start_kwargs = None

    def start(self, **kwargs):
        self.started += 1
        self.start_kwargs = dict(kwargs)
        if self._start_error:
            raise self._start_error
        self._running = True
        return object()

    def stop(self):
        self.stopped += 1
        self._running = False

    def wait_ready(self, port=10369, host="127.0.0.1", timeout=60.0, interval=0.5):
        if self._wait_error:
            raise self._wait_error


class _FakeLock:
    def __init__(self, held=False):
        self._held = held
        self.acquired = 0
        self.released = 0

    def acquire(self):
        if self._held:
            return False
        self._held = True
        self.acquired += 1
        return True

    def release(self):
        self._held = False
        self.released += 1


class _FakeState:
    def __init__(self, entries=None):
        self._entries = entries or {}

    def load(self):
        return dict(self._entries)


def _authorizer(mode="confirm"):
    return RecoveryAuthorizer(mode=mode)


def _engine(config=None, mode="confirm", runpod=None, tunnels=None, openfox=None,
            lock=None, state=None, is_port_free=None, is_port_open=None, registry=None,
            audit=None):
    config = config if config is not None else _config(recover_mode=mode)
    runpod = runpod if runpod is not None else _FakeRunPod("EXITED")
    tunnels = tunnels if tunnels is not None else _FakeTunnels(False)
    openfox = openfox if openfox is not None else _FakeOpenFox(False)
    if is_port_free is None:
        is_port_free = lambda port: True
    if is_port_open is None:
        is_port_open = lambda port, host="127.0.0.1", timeout=1.0: bool(openfox._running)
    return RecoveryEngine(
        config=config,
        authorizer=_authorizer(mode),
        runpod=runpod,
        tunnels=tunnels,
        openfox=openfox,
        state=state if state is not None else _FakeState(),
        lock=lock if lock is not None else _FakeLock(),
        is_port_free=is_port_free,
        is_port_open=is_port_open,
        registry=registry,
        audit=audit,
    )


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# start_runpod
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# reconnect_ssh
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# restart_openfox
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Concurrency, stale state, verification
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Secret redaction + bounded output
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Recovery audit
# ---------------------------------------------------------------------------


def test_recovery_audit_records(tmp_path) -> None:
    audit = RecoveryAudit(path=tmp_path / "recovery-audit.jsonl")
    audit.record(
        action="start_runpod", authorized=True, confirm=True,
        started_at=1.0, ended_at=2.0, duration_ms=1000,
        before="EXITED", after="RUNNING", ok=True, error_class=None,
    )
    entries = [json.loads(l) for l in audit.path.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1
    e = entries[0]
    assert e["action"] == "start_runpod"
    assert e["authorized"] is True
    assert e["ok"] is True
    assert e["error_class"] is None


def test_recovery_audit_redacts_reason(tmp_path) -> None:
    audit = RecoveryAudit(path=tmp_path / "recovery-audit.jsonl")
    audit.record(
        action="start_runpod", authorized=True, confirm=True,
        started_at=1.0, ended_at=2.0, duration_ms=1,
        before="EXITED", after="RUNNING", ok=True, error_class=None,
        reason="api_key=sk_live_abcdefghijklmnop",
    )
    blob = (tmp_path / "recovery-audit.jsonl").read_text(encoding="utf-8")
    assert "sk_live_abcdefghijklmnop" not in blob


# ---------------------------------------------------------------------------
# Tool: schema, registration, policy
# ---------------------------------------------------------------------------


def _call_recover(tmp_path, engine):
    policy = ToolPolicy(allowed_roots=[str(tmp_path)])
    registry = build_default_registry(policy)
    tool = build_tool(policy, config=_config(recover_mode="confirm"), engine=engine)
    registry._tools["forge_infra_recover"] = tool
    audit = AuditLog(path=tmp_path / "tools-audit.jsonl")
    return mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "forge_infra_recover", "arguments": {"action": "start_runpod", "confirm": True}},
        },
        registry, policy, audit,
    ), audit


# ---------------------------------------------------------------------------
# Audit wiring + confirm strictness (audit follow-up fixes)
# ---------------------------------------------------------------------------


class _RaisingAudit:
    def record(self, *args, **kwargs):
        raise OSError("audit disk full")


# ---------------------------------------------------------------------------
# Pod registry fallback
# ---------------------------------------------------------------------------

from launcher.pod_registry import PodRecord, PodRegistry  # noqa: E402


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


# ---------------------------------------------------------------------------
# restart_comfy (comfy stack recovery)
# ---------------------------------------------------------------------------


def _comfy_config(env_extra=None, recover_mode="confirm", include_pod_id=True):
    env = {
        "RUNPOD_API_KEY": "rp_secret_key",
        "SSH_KEY_PATH": r"C:\Users\me\.ssh\id_ed25519",
    }
    if include_pod_id:
        env["RUNPOD_POD_ID"] = "pod_comfy"
    env.update(env_extra or {})
    cfg = load_config(env=env, stack="comfy")
    if recover_mode != "confirm":
        cfg = replace(cfg, recover=RecoveryConfig(mode=recover_mode))
    return cfg


def _comfy_engine(
    config=None,
    runpod=None,
    tunnels=None,
    lock=None,
    state=None,
    is_port_free=None,
    registry=None,
    check_comfy_fn=None,
):
    config = config if config is not None else _comfy_config()
    runpod = runpod if runpod is not None else _FakeRunPod("RUNNING")
    tunnels = tunnels if tunnels is not None else _FakeTunnels(False)
    if is_port_free is None:
        is_port_free = lambda port: True
    return RecoveryEngine(
        config=config,
        authorizer=_authorizer("confirm"),
        runpod=runpod,
        tunnels=tunnels,
        openfox=_FakeOpenFox(False),
        state=state if state is not None else _FakeState(),
        lock=lock if lock is not None else _FakeLock(),
        is_port_free=is_port_free,
        is_port_open=lambda port, host="127.0.0.1", timeout=1.0: False,
        registry=registry,
        check_comfy_fn=check_comfy_fn,
    )


# ---------------------------------------------------------------------------
# restart_train (train stack recovery)
# ---------------------------------------------------------------------------


def _train_config(env_extra=None, recover_mode="confirm", include_pod_id=True):
    env = {
        "RUNPOD_API_KEY": "rp_secret_key",
        "SSH_KEY_PATH": r"C:\Users\me\.ssh\id_ed25519",
        "VNC_PASSWORD": "twelvechars1",
    }
    if include_pod_id:
        env["RUNPOD_POD_ID"] = "pod_train"
    env.update(env_extra or {})
    cfg = load_config(env=env, stack="train")
    if recover_mode != "confirm":
        cfg = replace(cfg, recover=RecoveryConfig(mode=recover_mode))
    return cfg


def _train_engine(
    config=None,
    runpod=None,
    tunnels=None,
    lock=None,
    state=None,
    is_port_free=None,
    registry=None,
    check_comfy_fn=None,
):
    config = config if config is not None else _train_config()
    runpod = runpod if runpod is not None else _FakeRunPod("RUNNING")
    tunnels = tunnels if tunnels is not None else _FakeTunnels(False)
    if is_port_free is None:
        is_port_free = lambda port: True
    return RecoveryEngine(
        config=config,
        authorizer=_authorizer("confirm"),
        runpod=runpod,
        tunnels=tunnels,
        openfox=_FakeOpenFox(False),
        state=state if state is not None else _FakeState(),
        lock=lock if lock is not None else _FakeLock(),
        is_port_free=is_port_free,
        is_port_open=lambda port, host="127.0.0.1", timeout=1.0: False,
        registry=registry,
        check_comfy_fn=check_comfy_fn,
    )


# ---------------------------------------------------------------------------
# Default SSH key (no SSH_KEY_PATH): the launcher passes no -i and lets ssh
# use its own identity. That must not be a precondition failure — it is the
# launcher's normal configuration.
# ---------------------------------------------------------------------------



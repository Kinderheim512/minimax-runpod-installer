"""Tests for the forge_infra_diagnose capability (infra_diagnose + tool).

Fully offline: every probe is injected as a fake, so no RunPod/SSH/vLLM/
OpenFox/SearXNG/network is required. Covers the health-state model, aggregation
rules, cause/remediation, secret redaction, bounded output, and tool/audit/
policy behavior.
"""

import json
import os
from dataclasses import replace

import pytest

from launcher.config import RunPodConfig, Secrets, SshConfig, load_config
from launcher.infra_diagnose import (
    DEGRADED,
    FAILED,
    HEALTHY,
    STOPPED,
    UNKNOWN,
    InfraReport,
    diagnose,
)


def _full_config():
    return load_config(
        env={
            "RUNPOD_POD_ID": "pod_abc",
            "RUNPOD_API_KEY": "rp_secret_key",
            "SSH_KEY_PATH": r"C:\Users\me\.ssh\id_ed25519",
        }
    )


class _FakePod:
    def __init__(self, status):
        self.status = status


class _FakeRunPod:
    def __init__(self, status="RUNNING", error=None):
        self._status = status
        self._error = error

    def get_pod(self, pod_id):
        if self._error:
            raise self._error
        return _FakePod(self._status)


class _FakeTunnels:
    def __init__(self, alive=True):
        self._alive = alive

    def is_alive(self, name, target):
        return self._alive


def _state_with(entries):
    """Return a fake RuntimeState whose load() returns *entries*."""
    class _State:
        def load(self):
            return entries
    return _State()


def _live_entry(pid, port, label="ssh tunnel llm", marker="ssh-tunnel"):
    from launcher.runtime_state import ProcessEntry
    return ProcessEntry(pid=pid, label=label, port=port, marker=marker, created_at=0.0)


def _diagnose(config=None, runpod=None, tunnels=None, state=None,
              health=True, model=True, searxng=True):
    return diagnose(
        config=config if config is not None else _full_config(),
        runpod=runpod if runpod is not None else _FakeRunPod("RUNNING"),
        tunnels=tunnels if tunnels is not None else _FakeTunnels(True),
        state=state if state is not None else _state_with(_recorded_tunnel_and_openfox()),
        check_health_fn=lambda base, timeout=5.0: health,
        check_model_fn=lambda base, model_id, timeout=5.0: model,
        check_searxng_fn=lambda url, timeout: searxng,
    )


# ---------------------------------------------------------------------------
# Aggregation / health-state model
# ---------------------------------------------------------------------------


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


class _IdTrackingRunPod(_FakeRunPod):
    def __init__(self, status="RUNNING"):
        super().__init__(status)
        self.get_ids = []

    def get_pod(self, pod_id):
        self.get_ids.append(pod_id)
        if self._error:
            raise self._error
        return _FakePod(self._status)


# ---------------------------------------------------------------------------
# Tool: schema / registration / execution / audit / policy
# ---------------------------------------------------------------------------


def _call_with_fake_diagnose(tmp_path, diagnose_fn):
    policy = ToolPolicy(allowed_roots=[str(tmp_path)])
    registry = build_default_registry(policy)
    tool = build_tool(policy, config=_full_config(), diagnose_fn=diagnose_fn)
    registry._tools["forge_infra_diagnose"] = tool
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    response = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "forge_infra_diagnose", "arguments": {}},
        },
        registry,
        policy,
        audit,
    )
    return response, audit


# ---------------------------------------------------------------------------
# Comfy stack
# ---------------------------------------------------------------------------


def _comfy_config():
    import dataclasses

    cfg = _full_config()
    return dataclasses.replace(cfg, stack="comfy")


def _comfy_diagnose(comfy=True, searxng=True):
    from launcher.runtime_state import ProcessEntry

    state = {
        "tunnels:comfy": ProcessEntry(pid=os.getpid(), label="ssh tunnel comfy", port=8188, marker="ssh-tunnel", created_at=0.0),
    }
    return diagnose(
        config=_comfy_config(),
        runpod=_FakeRunPod("RUNNING"),
        tunnels=_FakeTunnels(True),
        state=_state_with(state),
        check_comfy_fn=lambda base, timeout=5.0: comfy,
        check_searxng_fn=lambda url, timeout: searxng,
    )



"""Tests for the launcher-created pod registry (launcher.pod_registry)."""

import json
import logging

import pytest  # noqa: F401  (kept for future parametrize use)

from launcher.pod_registry import CURRENT_VERSION, PodRecord, PodRegistry, default_home


def _record(**overrides) -> PodRecord:
    base = dict(
        pod_id="pod_abc123",
        name="openfox-forge-20260830-120000",
        created_at=1756500000.0,
        gpu_id="NVIDIA A6000",
        gpu_count=1,
        data_center_id="us-east-1",
    )
    base.update(overrides)
    return PodRecord(**base)


def test_save_load_roundtrip(tmp_path) -> None:
    reg = PodRegistry(tmp_path / "pod.json")
    record = _record()
    reg.save(record)
    assert reg.load() == record
    assert reg.load().version == CURRENT_VERSION


def test_atomic_replace_leaves_no_partial_or_temp_file(tmp_path) -> None:
    reg = PodRegistry(tmp_path / "pod.json")
    reg.save(_record())
    reg.save(_record(pod_id="pod_second", name="second-name"))
    raw = json.loads((tmp_path / "pod.json").read_text(encoding="utf-8"))
    assert raw["pods"]["agent"]["pod_id"] == "pod_second"
    assert raw["pods"]["agent"]["name"] == "second-name"
    # The directory contains exactly the registry file — no temp leftovers.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pod.json"]


def test_load_missing_returns_none(tmp_path) -> None:
    assert PodRegistry(tmp_path / "pod.json").load() is None


def test_load_missing_fields_returns_none(tmp_path) -> None:
    path = tmp_path / "pod.json"
    path.write_text(json.dumps({"version": 1, "name": "only-a-name"}), encoding="utf-8")
    assert PodRegistry(path).load() is None


def test_clear_is_idempotent(tmp_path) -> None:
    reg = PodRegistry(tmp_path / "pod.json")
    reg.clear()  # no file yet — must not raise
    reg.save(_record())
    reg.clear()
    assert not reg.path.exists()
    reg.clear()  # still no raise


def test_null_data_center_roundtrip(tmp_path) -> None:
    reg = PodRegistry(tmp_path / "pod.json")
    reg.save(_record(data_center_id=None))
    assert reg.load().data_center_id is None


def test_data_center_stored_from_response_not_request(tmp_path) -> None:
    """Regression: the scheduler's assigned data center (the 201 response
    value) is what gets stored, not the requested preference list."""
    reg = PodRegistry(tmp_path / "pod.json")
    # Request preferred ["dc-a", "dc-b"]; the API assigned "dc-b".
    reg.save(_record(data_center_id="dc-b"))
    assert reg.load().data_center_id == "dc-b"


def test_no_secret_keys_in_file(tmp_path) -> None:
    path = tmp_path / "pod.json"
    PodRegistry(path).save(_record())
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw.keys()) == {"version", "pods"}
    assert raw["version"] == CURRENT_VERSION
    record = raw["pods"]["agent"]
    assert set(record.keys()) == {
        "pod_id",
        "name",
        "created_at",
        "gpu_id",
        "gpu_count",
        "data_center_id",
        "stack",
    }
    blob = json.dumps(raw).lower()
    for forbidden in ("api_key", "apikey", "secret", "token", "template", "password"):
        assert forbidden not in blob


def test_two_stacks_coexist_independently(tmp_path) -> None:
    reg = PodRegistry(tmp_path / "pod.json")
    agent = _record(pod_id="pod_agent", name="agent-pod", stack="agent")
    comfy = _record(pod_id="pod_comfy", name="comfy-pod", stack="comfy")
    reg.save(agent)
    reg.save(comfy)
    assert reg.load("agent") == agent
    assert reg.load("comfy") == comfy
    # Re-saving one stack must not clobber the other.
    reg.save(_record(pod_id="pod_agent2", name="agent-pod-2", stack="agent"))
    assert reg.load("agent").pod_id == "pod_agent2"
    assert reg.load("comfy") == comfy


def test_all_returns_every_stack(tmp_path) -> None:
    reg = PodRegistry(tmp_path / "pod.json")
    reg.save(_record(pod_id="pod_agent", stack="agent"))
    reg.save(_record(pod_id="pod_comfy", stack="comfy"))
    result = reg.all()
    assert set(result) == {"agent", "comfy"}
    assert result["agent"].pod_id == "pod_agent"
    assert result["comfy"].pod_id == "pod_comfy"


def test_clear_one_stack_preserves_other(tmp_path) -> None:
    reg = PodRegistry(tmp_path / "pod.json")
    reg.save(_record(pod_id="pod_agent", stack="agent"))
    reg.save(_record(pod_id="pod_comfy", stack="comfy"))
    reg.clear("comfy")
    assert reg.load("comfy") is None
    assert reg.load("agent").pod_id == "pod_agent"
    # File still exists because the agent record remains.
    assert reg.path.exists()


def test_clear_last_stack_removes_file(tmp_path) -> None:
    reg = PodRegistry(tmp_path / "pod.json")
    reg.save(_record(pod_id="pod_agent", stack="agent"))
    reg.save(_record(pod_id="pod_comfy", stack="comfy"))
    reg.clear("comfy")
    reg.clear("agent")
    assert not reg.path.exists()


def test_legacy_v1_file_upgrades_on_save(tmp_path) -> None:
    """A pre-multi-stack v1 file is read as the agent stack and preserved
    when a new (comfy) record is added; the file is rewritten as v2."""
    path = tmp_path / "pod.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "pod_id": "pod_legacy",
                "name": "legacy-pod",
                "created_at": 1750000000.0,
                "gpu_id": "NVIDIA A6000",
                "gpu_count": 1,
                "data_center_id": "us-east-1",
            }
        ),
        encoding="utf-8",
    )
    reg = PodRegistry(path)
    assert reg.load("agent").pod_id == "pod_legacy"
    reg.save(_record(pod_id="pod_comfy", stack="comfy"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == CURRENT_VERSION
    assert raw["pods"]["agent"]["pod_id"] == "pod_legacy"
    assert raw["pods"]["comfy"]["pod_id"] == "pod_comfy"
    # The legacy agent pod survives the in-place upgrade.
    assert reg.load("agent").pod_id == "pod_legacy"



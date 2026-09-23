"""Tests for cross-invocation process ownership (runtime_state + orchestrator stop)."""

import json

import pytest

from launcher import orchestrator, runtime_state
from launcher.config import Config, RunPodConfig, Secrets, load_config
from launcher.runtime_state import ProcessEntry, RuntimeState, pid_is_alive
from launcher.tunnel import SshEndpoint, TunnelTarget


def _config():
    import dataclasses

    return dataclasses.replace(
        load_config(env={}),
        runpod=RunPodConfig(pod_id="pod_abc"),
        secrets=Secrets(runpod_api_key="k"),
    )


def _tmp_state(tmp_path):
    return RuntimeState(tmp_path / "runtime.json")


def test_stop_does_not_kill_unrecorded_pids(tmp_path, monkeypatch) -> None:
    state = _tmp_state(tmp_path)
    # Only record one tunnel; another unrelated pid exists but is NOT in state.
    state.save({"tunnels:llm": ProcessEntry(pid=100, label="ssh tunnel llm", port=8000, marker="ssh-tunnel", created_at=1.0)})

    terminated = []

    monkeypatch.setattr(runtime_state, "pid_is_alive", lambda pid: True)
    monkeypatch.setattr(runtime_state, "terminate_pid", lambda pid: terminated.append(pid) or True)

    orchestrator.stop(state=state)

    assert terminated == [100]  # 9999 was never recorded, never killed


def test_build_runpod_failure_uses_standard_error(monkeypatch) -> None:
    cfg = load_config(env={})  # no api key -> _build_runpod raises
    with pytest.raises(orchestrator.OrchestrationError) as exc:
        orchestrator.start(cfg)
    assert "RUNPOD_API_KEY" in str(exc.value)


def test_pid_is_alive_returns_false_for_impossible_pid() -> None:
    # A huge PID is effectively guaranteed not to exist on any normal system.
    assert pid_is_alive(2_000_000_000) is False


def test_pid_is_alive_rejects_non_positive_pids() -> None:
    """``os.kill(0, sig)`` targets the caller's own process group on POSIX."""
    assert pid_is_alive(0) is False
    assert pid_is_alive(-1) is False


def test_load_tolerates_a_non_mapping_document(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert RuntimeState(path).load() == {}


def test_terminate_recorded_pid_refuses_a_recycled_pid(tmp_path, monkeypatch) -> None:
    """A recorded PID whose process start time does not match is NOT killed.

    Windows recycles PIDs and the state file keeps entries indefinitely, so
    ``taskkill /T /F`` on a stale number could destroy an unrelated process
    tree (an editor, a build).
    """
    entry = ProcessEntry(
        pid=4242, label="ssh tunnel llm", port=8000, marker="ssh-tunnel", created_at=1000.0
    )
    monkeypatch.setattr(runtime_state, "process_created_at", lambda pid: 999_999.0)
    killed = []
    monkeypatch.setattr(runtime_state, "terminate_pid", lambda pid: killed.append(pid) or True)
    assert runtime_state.terminate_recorded_pid(entry) is False
    assert killed == []


def test_terminate_recorded_pid_accepts_a_matching_pid(monkeypatch) -> None:
    entry = ProcessEntry(
        pid=4242, label="ssh tunnel llm", port=8000, marker="ssh-tunnel", created_at=1000.0
    )
    monkeypatch.setattr(runtime_state, "process_created_at", lambda pid: 1001.0)
    killed = []
    monkeypatch.setattr(runtime_state, "terminate_pid", lambda pid: killed.append(pid) or True)
    assert runtime_state.terminate_recorded_pid(entry) is True
    assert killed == [4242]


def test_terminate_recorded_pid_proceeds_when_identity_is_unknown(monkeypatch) -> None:
    """An unverifiable identity must not block the teardown.

    Refusing to act would leave owned tunnels running for ever, which is worse
    than the risk the check guards against.
    """
    entry = ProcessEntry(
        pid=4242, label="ssh tunnel llm", port=8000, marker="ssh-tunnel", created_at=1000.0
    )
    monkeypatch.setattr(runtime_state, "process_created_at", lambda pid: None)
    killed = []
    monkeypatch.setattr(runtime_state, "terminate_pid", lambda pid: killed.append(pid) or True)
    assert runtime_state.terminate_recorded_pid(entry) is True
    assert killed == [4242]


def test_terminate_pid_reports_failure_when_the_process_survives(monkeypatch) -> None:
    """A kill that did not work must return False: the caller drops the record
    on True, and that record is the only handle on the process."""
    monkeypatch.setattr(runtime_state, "pid_is_alive", lambda pid: True)
    monkeypatch.setattr(runtime_state.os, "name", "posix")
    monkeypatch.setattr(runtime_state.time, "sleep", lambda s: None)
    monkeypatch.setattr(runtime_state, "_wait_until_dead", lambda pid, timeout: False)
    assert runtime_state.terminate_pid(4242) is False


def test_terminate_pid_returns_false_for_an_already_dead_pid(monkeypatch) -> None:
    monkeypatch.setattr(runtime_state, "pid_is_alive", lambda pid: False)
    assert runtime_state.terminate_pid(4242) is False

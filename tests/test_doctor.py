"""Tests for the doctor diagnostics module (launcher.doctor)."""

import pytest

from launcher.config import Config, RunPodConfig, Secrets, SshConfig, load_config
from launcher.doctor import Diagnostic, run_diagnostics
from launcher.runpod import RunPodError
from launcher.tunnel import SshEndpoint


def _config(**overrides):
    import dataclasses

    return dataclasses.replace(load_config(env={}), **overrides)


class FakePod:
    status = "RUNNING"
    id = "pod_abc"
    is_running = True
    is_startable = False
    direct_endpoint = SshEndpoint("1.2.3.4", 2222, "root")
    tcp_ssh_endpoint = None

    def ssh_tunnel_endpoint(self):
        return self.direct_endpoint or self.tcp_ssh_endpoint


_DEFAULT_GPUS = [
    {"id": "NVIDIA RTX A6000", "availability": {"DC1": "MEDIUM"}},
    {"id": "NVIDIA GeForce RTX 4090", "availability": "HIGH"},
]


class FakeRunPod:
    def __init__(
        self,
        pod=None,
        error=None,
        template_error=None,
        ssh_keys=None,
        gpu_types=None,
        keys_error=None,
        gpus_error=None,
    ):
        self._pod = pod
        self._error = error
        self._template_error = template_error
        self._ssh_keys = ssh_keys
        self._gpu_types = gpu_types
        self._keys_error = keys_error
        self._gpus_error = gpus_error
        self.get_ids = []

    def get_pod(self, pod_id):
        self.get_ids.append(pod_id)
        if self._error:
            raise self._error
        return self._pod or FakePod()

    def get_template(self, template_id):
        if self._template_error:
            raise self._template_error
        return {"id": template_id}

    def list_ssh_keys(self):
        if self._keys_error:
            raise self._keys_error
        return ["key-1"] if self._ssh_keys is None else self._ssh_keys

    def get_gpu_types(
        self, include_availability=True, product="POD", cloud=None, count=None
    ):
        if self._gpus_error:
            raise self._gpus_error
        return self._gpu_types if self._gpu_types is not None else _DEFAULT_GPUS


def _names(results):
    return [r.name for r in results]


def _status(results, name):
    for r in results:
        if r.name == name:
            return r.status
    return None


def test_doctor_configuration_ok() -> None:
    cfg = _config()
    results = run_diagnostics(cfg)
    assert _status(results, "configuration") == "OK"


def test_doctor_ssh_key_missing_is_warn() -> None:
    cfg = _config(ssh=SshConfig(key_path=None))
    results = run_diagnostics(cfg)
    assert _status(results, "ssh_key") == "WARN"


def test_doctor_ssh_key_exists(tmp_path, monkeypatch) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("not a real key")
    cfg = _config(ssh=SshConfig(key_path=str(key)))
    results = run_diagnostics(cfg)
    assert _status(results, "ssh_key") == "OK"


def test_doctor_ssh_key_not_found(tmp_path) -> None:
    cfg = _config(ssh=SshConfig(key_path=str(tmp_path / "missing")))
    results = run_diagnostics(cfg)
    assert _status(results, "ssh_key") == "ERROR"


def test_doctor_runpod_ok() -> None:
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc"))
    results = run_diagnostics(cfg, runpod=FakeRunPod(FakePod()))
    assert _status(results, "runpod") == "OK"
    assert _status(results, "ssh_endpoint") == "OK"


def test_doctor_runpod_error() -> None:
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc"))
    results = run_diagnostics(cfg, runpod=FakeRunPod(error=RunPodError("bad")))
    assert _status(results, "runpod") == "ERROR"


def test_doctor_runpod_skip_when_no_client() -> None:
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc"))
    results = run_diagnostics(cfg, runpod=None)
    assert _status(results, "runpod") == "SKIP"


# ---------------------------------------------------------------------------
# Secure credential store diagnostics (value-free state only)
# ---------------------------------------------------------------------------

from launcher.credentials import CredentialStatus  # noqa: E402


class _FakeCredStore:
    def __init__(self, status):
        self._status = status

    def status(self):
        return self._status


def _cred_status(**overrides):
    base = dict(
        file_present=False,
        api_key_configured=False,
        template_configured=False,
        readable=False,
    )
    base.update(overrides)
    return CredentialStatus(**base)


def test_doctor_credentials_skip_when_not_provided() -> None:
    cfg = _config()
    results = run_diagnostics(cfg)
    assert _status(results, "runpod_credentials") == "SKIP"


def test_doctor_credentials_ok() -> None:
    cfg = _config()
    store = _FakeCredStore(
        _cred_status(
            file_present=True,
            api_key_configured=True,
            template_configured=True,
            readable=True,
        )
    )
    results = run_diagnostics(cfg, credentials=store)
    check = next(r for r in results if r.name == "runpod_credentials")
    assert check.status == "OK"
    assert "key" not in check.detail.lower()


def test_doctor_credentials_not_stored_is_warn() -> None:
    cfg = _config()
    results = run_diagnostics(cfg, credentials=_FakeCredStore(_cred_status()))
    assert _status(results, "runpod_credentials") == "WARN"


def test_doctor_credentials_env_only_is_skip_not_warn() -> None:
    cfg = _config(secrets=Secrets(runpod_api_key="rp_env_only_key"))
    results = run_diagnostics(cfg, credentials=_FakeCredStore(_cred_status()))
    check = next(r for r in results if r.name == "runpod_credentials")
    assert check.status == "SKIP"
    assert "environment" in check.detail.lower()
    assert "rp_env_only_key" not in check.detail


def test_doctor_credentials_corrupt_is_error() -> None:
    cfg = _config()
    store = _FakeCredStore(_cred_status(file_present=True, readable=False))
    results = run_diagnostics(cfg, credentials=store)
    check = next(r for r in results if r.name == "runpod_credentials")
    assert check.status == "ERROR"
    assert "credentials" in check.detail


# ---------------------------------------------------------------------------
# Pod registry + provisioning catalog diagnostics
# ---------------------------------------------------------------------------

from launcher.pod_registry import PodRecord, PodRegistry  # noqa: E402
from launcher.runpod import TemplateNotFoundError  # noqa: E402


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


def test_doctor_no_pod_no_registry_warns(tmp_path) -> None:
    cfg = _config()
    results = run_diagnostics(cfg, runpod=FakeRunPod(), registry=_registry(tmp_path))
    check = next(r for r in results if r.name == "runpod")
    assert check.status == "WARN"
    assert "no registered pod" in check.detail


def test_doctor_provisioning_checks_skip_without_client() -> None:
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc"))
    results = run_diagnostics(cfg)
    for name in ("pod_template", "ssh_keys", "gpu_availability"):
        assert _status(results, name) == "SKIP"


def test_doctor_template_transient_error_is_skip() -> None:
    import dataclasses

    cfg = dataclasses.replace(
        _config(runpod=RunPodConfig(pod_id="pod_abc")),
        secrets=Secrets(runpod_api_key="rp_key", runpod_template_id="tmpl_forge"),
    )
    results = run_diagnostics(cfg, runpod=FakeRunPod(template_error=RunPodError("HTTP 503")))
    assert _status(results, "pod_template") == "SKIP"


def test_doctor_ssh_keys_ok() -> None:
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc"))
    results = run_diagnostics(cfg, runpod=FakeRunPod())
    check = next(r for r in results if r.name == "ssh_keys")
    assert check.status == "OK"
    assert "key-1" not in check.detail  # no key material, count only


def test_doctor_ssh_keys_empty_is_warn() -> None:
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc"))
    results = run_diagnostics(cfg, runpod=FakeRunPod(ssh_keys=[]))
    check = next(r for r in results if r.name == "ssh_keys")
    assert check.status == "WARN"
    assert "ssh-key add" in check.detail


def test_doctor_ssh_keys_error_is_skip() -> None:
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc"))
    results = run_diagnostics(cfg, runpod=FakeRunPod(keys_error=RunPodError("HTTP 503")))
    assert _status(results, "ssh_keys") == "SKIP"


def test_doctor_gpu_available_is_ok() -> None:
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc"))  # default gpu_id A6000
    results = run_diagnostics(cfg, runpod=FakeRunPod())
    check = next(r for r in results if r.name == "gpu_availability")
    assert check.status == "OK"
    assert "advisory" in check.detail


def test_doctor_gpu_not_found_is_warn() -> None:
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc", gpu_id="NVIDIA H100"))
    results = run_diagnostics(cfg, runpod=FakeRunPod())
    check = next(r for r in results if r.name == "gpu_availability")
    assert check.status == "WARN"
    assert "advisory" in check.detail


def test_doctor_gpu_all_none_availability_is_warn() -> None:
    gpus = [{"id": "NVIDIA RTX A6000", "availability": {"DC1": "NONE", "DC2": "NONE"}}]
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc"))
    results = run_diagnostics(cfg, runpod=FakeRunPod(gpu_types=gpus))
    check = next(r for r in results if r.name == "gpu_availability")
    assert check.status == "WARN"
    assert "advisory" in check.detail


def test_doctor_gpu_availability_not_reported_is_warn() -> None:
    gpus = [{"id": "NVIDIA RTX A6000"}]
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc"))
    results = run_diagnostics(cfg, runpod=FakeRunPod(gpu_types=gpus))
    check = next(r for r in results if r.name == "gpu_availability")
    assert check.status == "WARN"
    assert "not reported" in check.detail
    assert "advisory" in check.detail


def test_doctor_gpu_types_error_is_skip() -> None:
    cfg = _config(runpod=RunPodConfig(pod_id="pod_abc"))
    results = run_diagnostics(cfg, runpod=FakeRunPod(gpus_error=RunPodError("HTTP 503")))
    assert _status(results, "gpu_availability") == "SKIP"


# ---------------------------------------------------------------------------
# Comfy stack (stack="comfy")
# ---------------------------------------------------------------------------


def _comfy_config(**overrides):
    import dataclasses

    from launcher.config import ComfyConfig

    base = _config(**overrides)
    return dataclasses.replace(base, stack="comfy", comfy=dataclasses.replace(ComfyConfig()))


def test_doctor_comfy_not_healthy_warns(monkeypatch) -> None:
    monkeypatch.setattr("launcher.doctor.check_comfy", lambda base: False)
    monkeypatch.setattr("launcher.doctor.get_comfy_stats", lambda base: {})
    cfg = _comfy_config(runpod=RunPodConfig(pod_id="pod_abc"))
    results = run_diagnostics(cfg, runpod=FakeRunPod(FakePod()))
    assert _status(results, "comfyui") == "WARN"


def test_doctor_comfy_template_check_uses_comfy_template(monkeypatch) -> None:
    from launcher.config import ComfyConfig, Secrets

    monkeypatch.setattr("launcher.doctor.check_comfy", lambda base: False)
    cfg = _comfy_config(runpod=RunPodConfig(pod_id="pod_abc"))
    cfg = dataclasses_replace(cfg, secrets=Secrets(runpod_api_key="k", comfy_template_id="tmpl_comfy"))
    results = run_diagnostics(cfg, runpod=FakeRunPod(FakePod()))
    check = next(r for r in results if r.name == "pod_template")
    assert check.status == "OK"
    assert "RUNPOD_COMFY_TEMPLATE_ID" in check.detail


def test_doctor_comfy_local_port_is_comfy_port(monkeypatch) -> None:
    import socket

    monkeypatch.setattr("launcher.doctor.check_comfy", lambda base: False)
    monkeypatch.setattr(
        "launcher.doctor.is_port_free", lambda port: port != 8188
    )
    cfg = _comfy_config()
    results = run_diagnostics(cfg)
    check = next(r for r in results if r.name == "local_port")
    assert check.status == "WARN"  # 8188 is occupied in this fake
    assert "ComfyUI" in check.detail
    assert "8188" in check.detail


def dataclasses_replace(cfg, **overrides):
    import dataclasses

    return dataclasses.replace(cfg, **overrides)

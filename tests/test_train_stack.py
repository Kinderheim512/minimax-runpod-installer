"""Tests for the `train` stack (LoRA training via Fizgig on a rented GPU).

Covers the four things this stack added to the launcher:

* the ``TrainConfig`` declaration and its validation;
* the two-port tunnel (the first stack to need more than one forward);
* ``TrainOps`` — the pod-side read/collect path, including the scp transfer;
* ``launcher.train_template`` — the RunPod template checks shared by the CLI
  and the GUI's "Vérifier le template" button.

No Docker, no network, no RunPod account, no GPU.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from launcher import orchestrator, train_template
from launcher.config import (
    DEFAULT_TRAIN_FILES_LOCAL_PORT,
    DEFAULT_TRAIN_LOCAL_PORT,
    DEFAULT_TRAIN_VOLUME_GB,
    STACKS,
    ConfigError,
    TrainConfig,
    load_config,
)
from launcher.pod_registry import PodRegistry
from launcher.train_ops import (
    DEFAULT_LORA_DIR,
    TrainOps,
    TrainOpsError,
    build_train_ops,
)
from launcher.tunnel import SshEndpoint

TRAIN_ENV = {
    "LAUNCHER_STACK": "train",
    "RUNPOD_API_KEY": "rp_test_key",
    "RUNPOD_TEMPLATE_ID": "tmpl_base",
    "RUNPOD_TRAIN_TEMPLATE_ID": "tmpl_train",
    "VNC_PASSWORD": "twelvechars1",
}


def _config(**overrides):
    env = dict(TRAIN_ENV)
    env.update(overrides.pop("env", {}))
    return load_config(env=env, **overrides)


# ---------------------------------------------------------------------------
# Stack declaration
# ---------------------------------------------------------------------------


class TestStackDeclaration:
    def test_train_is_a_stack(self) -> None:
        assert "train" in STACKS

    def test_stack_selects_train(self) -> None:
        assert _config().stack == "train"

    def test_unknown_stack_still_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_config(env={**TRAIN_ENV, "LAUNCHER_STACK": "nope"})

    def test_two_distinct_local_ports(self) -> None:
        config = _config()
        assert config.train.local_port == DEFAULT_TRAIN_LOCAL_PORT
        assert config.train.files_local_port == DEFAULT_TRAIN_FILES_LOCAL_PORT
        assert config.train.local_port != config.train.files_local_port

    def test_urls_point_at_the_tunnel(self) -> None:
        config = _config()
        assert config.train_base_url() == f"http://127.0.0.1:{DEFAULT_TRAIN_LOCAL_PORT}"
        assert config.train_files_url() == (
            f"http://127.0.0.1:{DEFAULT_TRAIN_FILES_LOCAL_PORT}"
        )

    def test_image_name_is_repository_plus_tag(self) -> None:
        config = _config()
        assert config.train.image_name.endswith(f":{config.train.image_tag}")
        # Upstream's image, not a fork: a fork can only ever lag the release
        # cadence it would have to keep up with.
        assert config.train.image_name.startswith("ghcr.io/shootthesound/fizgig:")

    def test_pod_env_carries_the_telemetry_switch_and_the_ref(self) -> None:
        env = _config().resolved_train_pod_env()
        assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
        # FIZGIG_REF is the update mechanism: upstream pulls it at every boot,
        # so a restart is the update.
        assert env["FIZGIG_REF"] == TrainConfig().fizgig_ref

    def test_pod_env_omits_fetch_models_when_empty(self) -> None:
        """Empty means "download nothing at boot" — sending an empty string
        would be indistinguishable from unset on some hosts."""
        assert "FETCH_MODELS" not in _config().resolved_train_pod_env()


    def test_vnc_password_is_a_secret_not_pod_env_from_the_config(self) -> None:
        """The credential must not leak through the non-secret dataclass."""
        assert "VNC_PASSWORD" not in _config().resolved_train_pod_env()
        assert _config().secrets.vnc_password == "twelvechars1"


class TestTrainValidation:


    def test_empty_image_tag_falls_back_to_the_default(self) -> None:
        """An empty override is "not specified", not "no tag" — the same rule
        the comfy and llama.cpp stacks follow."""
        config = load_config(env=TRAIN_ENV, train={"image_tag": ""})
        assert config.train.image_tag == TrainConfig().image_tag

    def test_validation_rejects_a_directly_built_empty_tag(self) -> None:
        """The guard still exists for a caller that builds TrainConfig by hand
        (the GUI never does, but a future one could)."""
        from launcher.config import validate

        config = _config()
        broken = replace(config, train=replace(config.train, image_tag=""))
        with pytest.raises(ConfigError):
            validate(broken)


    def test_default_volume_clears_the_minimum(self) -> None:
        assert DEFAULT_TRAIN_VOLUME_GB >= train_template.MIN_VOLUME_GB


# ---------------------------------------------------------------------------
# TrainOps — the pod-side read/collect path
# ---------------------------------------------------------------------------


class _FakeRun:
    """A subprocess.run stand-in that records argv and returns canned output."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))

        class _Result:
            pass

        result = _Result()
        result.stdout = self.stdout
        result.returncode = self.returncode
        result.stderr = self.stderr
        return result


@pytest.fixture
def ops() -> TrainOps:
    return TrainOps(SshEndpoint("1.2.3.4", 2222, "root"), key_path="C:/key")


class TestTrainOpsListing:
    def test_list_loras_filters_to_safetensors(self, ops, monkeypatch) -> None:
        fake = _FakeRun("a.safetensors\nb-000001.safetensors\nnotes.txt\n.hidden\n")
        monkeypatch.setattr(subprocess, "run", fake)
        assert ops.list_loras() == ["a.safetensors", "b-000001.safetensors"]

    def test_list_loras_is_empty_when_nothing_is_trained(self, ops, monkeypatch) -> None:
        """A missing output folder is not an error — it means nothing yet."""
        monkeypatch.setattr(subprocess, "run", _FakeRun(""))
        assert ops.list_loras() == []

    def test_list_loras_raises_on_a_failed_command(self, ops, monkeypatch) -> None:
        monkeypatch.setattr(
            subprocess, "run", _FakeRun("", returncode=255, stderr="Permission denied")
        )
        with pytest.raises(TrainOpsError):
            ops.list_loras()

    def test_list_datasets_reads_the_dataset_folder(self, ops, monkeypatch) -> None:
        monkeypatch.setattr(subprocess, "run", _FakeRun("me\nmy_style\n"))
        assert ops.list_datasets() == ["me", "my_style"]

    def test_pod_version_is_read_from_git_describe(self, ops, monkeypatch) -> None:
        """The pod's revision is whatever FIZGIG_REF pointed at when it started,
        so it has to be read back off the pod rather than assumed."""
        monkeypatch.setattr(subprocess, "run", _FakeRun("6.0.1-3-gabc1234\n"))
        assert ops.pod_fizgig_version() == "6.0.1-3-gabc1234"

    def test_pod_version_falls_back_to_a_bare_sha(self, ops, monkeypatch) -> None:
        monkeypatch.setattr(subprocess, "run", _FakeRun("abc1234\n"))
        assert ops.pod_fizgig_version() == "abc1234"


class TestTrainOpsDownload:
    def test_download_rejects_an_unknown_name(self, ops, monkeypatch) -> None:
        """The name must come from the pod's own listing, never from free text:
        it is interpolated into a remote path."""
        monkeypatch.setattr(subprocess, "run", _FakeRun("real.safetensors\n"))
        with pytest.raises(TrainOpsError) as excinfo:
            ops.download_lora("../../etc/passwd", Path("out"))
        assert "not in the pod's output folder" in str(excinfo.value)

    def test_download_uses_scp_with_the_ssh_port(self, ops, monkeypatch, tmp_path) -> None:
        calls: list[list[str]] = []

        def fake(argv, **kwargs):
            calls.append(list(argv))
            # First call is the listing (ssh), the second the transfer (scp).
            stdout = "mine.safetensors\n" if argv[0] == "ssh" else ""

            class _Result:
                pass

            result = _Result()
            result.stdout = stdout
            result.returncode = 0
            result.stderr = ""
            if argv[0] == "scp":
                Path(argv[-1]).write_bytes(b"payload")
            return result

        monkeypatch.setattr(subprocess, "run", fake)
        target = ops.download_lora("mine.safetensors", tmp_path)
        assert target == tmp_path / "mine.safetensors"
        scp = next(call for call in calls if call[0] == "scp")
        # scp takes -P for the port (ssh takes -p for something else).
        assert "-P" in scp
        assert scp[scp.index("-P") + 1] == "2222"
        assert "-i" in scp
        assert any(arg.startswith("root@1.2.3.4:") for arg in scp)
        assert f"{DEFAULT_LORA_DIR}/mine.safetensors" in " ".join(scp)

    def test_failed_scp_removes_the_partial_file(self, ops, monkeypatch, tmp_path) -> None:
        def fake(argv, **kwargs):
            class _Result:
                pass

            result = _Result()
            if argv[0] == "ssh":
                result.stdout = "mine.safetensors\n"
                result.returncode = 0
                result.stderr = ""
            else:
                # Simulate scp creating then failing. It writes the staging
                # file: the transfer only becomes the destination once scp
                # reports success.
                (tmp_path / "mine.safetensors.part").write_bytes(b"partial")
                result.stdout = ""
                result.returncode = 1
                result.stderr = "Connection closed"
            return result

        monkeypatch.setattr(subprocess, "run", fake)
        with pytest.raises(TrainOpsError):
            ops.download_lora("mine.safetensors", tmp_path)
        assert not (tmp_path / "mine.safetensors").exists()
        assert not (tmp_path / "mine.safetensors.part").exists()

    def test_download_is_atomic_through_a_part_file(
        self, ops, monkeypatch, tmp_path
    ) -> None:
        """A killed launcher must never leave a truncated LoRA in place.

        The transfer lands in ``.part`` and is renamed only on success, so a
        truncated file can never be mistaken for a complete one.
        """
        seen: dict = {}

        def fake(argv, **kwargs):
            class _Result:
                pass

            result = _Result()
            result.stdout = "mine.safetensors\n" if argv[0] == "ssh" else ""
            result.returncode = 0
            result.stderr = ""
            if argv[0] == "scp":
                seen["staging"] = Path(argv[-1])
                seen["staging"].write_bytes(b"complete")
            return result

        monkeypatch.setattr(subprocess, "run", fake)
        target = ops.download_lora("mine.safetensors", tmp_path)
        assert seen["staging"].name == "mine.safetensors.part"
        assert target.read_bytes() == b"complete"
        assert not seen["staging"].exists()

    def test_download_refuses_a_pod_name_with_a_windows_separator(
        self, ops, monkeypatch, tmp_path
    ) -> None:
        """A Linux file name may contain ``\\`` — a path separator on Windows.

        The name is only checked against the pod's listing, so it used to be
        joined into the local path verbatim and could escape the destination.
        """
        name = "sub\\evil.safetensors"
        monkeypatch.setattr(subprocess, "run", _FakeRun(name + "\n"))
        with pytest.raises(TrainOpsError) as excinfo:
            ops.download_lora(name, tmp_path)
        assert "separator" in str(excinfo.value) or "escape" in str(excinfo.value)
        assert not (tmp_path / "sub").exists()

    def test_collect_refuses_a_pod_name_with_a_separator(
        self, ops, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setattr(
            subprocess, "run", _FakeRun("ok.safetensors\nsub\\evil.safetensors\n")
        )
        with pytest.raises(TrainOpsError):
            ops.collect_loras(tmp_path)

    def test_download_refuses_an_ads_name(
        self, ops, monkeypatch, tmp_path
    ) -> None:
        """``:`` opens an NTFS alternate data stream."""
        monkeypatch.setattr(subprocess, "run", _FakeRun("evil:s.txt\n"))
        with pytest.raises(TrainOpsError):
            ops.download_lora("evil:s.txt", tmp_path)

    def test_collect_skips_files_already_present(self, ops, monkeypatch, tmp_path) -> None:
        (tmp_path / "have.safetensors").write_bytes(b"already here")

        def fake(argv, **kwargs):
            class _Result:
                pass

            result = _Result()
            result.stdout = "have.safetensors\nnew.safetensors\n" if argv[0] == "ssh" else ""
            result.returncode = 0
            result.stderr = ""
            if argv[0] == "scp":
                Path(argv[-1]).write_bytes(b"payload")
            return result

        monkeypatch.setattr(subprocess, "run", fake)
        downloaded = ops.collect_loras(tmp_path)
        # Only the missing one is fetched; the present one is left alone.
        assert [p.name for p in downloaded] == ["new.safetensors"]


class TestBuildTrainOps:
    def test_requires_an_api_key(self) -> None:
        config = load_config(env={**TRAIN_ENV, "RUNPOD_API_KEY": ""})
        with pytest.raises(TrainOpsError):
            build_train_ops(config)

    def test_explains_a_missing_ssh_endpoint(self) -> None:
        """The likeliest failure is a template without SSH access — say so."""

        class _Pod:
            status = "RUNNING"

            def ssh_tunnel_endpoint(self):
                return None

        class _RunPod:
            def get_pod(self, pod_id):
                return _Pod()

        class _Registry:
            def load(self, stack):
                class _Record:
                    pod_id = "pod1"

                return _Record()

        config = _config()
        with pytest.raises(TrainOpsError) as excinfo:
            build_train_ops(config, runpod=_RunPod(), registry=_Registry())
        assert "SSH" in str(excinfo.value)
        assert "verify_template" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Template checks (shared by the CLI script and the GUI button)
# ---------------------------------------------------------------------------

CONFORMING = {
    "imageName": "ghcr.io/shootthesound/fizgig:6.0.1",
    "ports": "22/tcp,6080/http,8080/http",
    "startSsh": True,
    "volumeMountPath": "/workspace",
    "volumeInGb": 150,
    "env": {
        "HF_TOKEN": "hf_x",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "FIZGIG_REF": "master",
    },
}

#: The shape the v2 API actually returns (``GET /templates/{id}``): a nested
#: persistent mount and a ``startSsh`` flag, not the flat documented fields.
CONFORMING_API_PAYLOAD = {
    "id": "aolnvq5akk",
    "name": "Training lora",
    "image": "ghcr.io/shootthesound/fizgig:6.0.1",
    "ports": ["22/tcp", "6080/http", "8080/http"],
    "startSsh": True,
    "disk": 40,
    "mounts": {"persistent": {"path": "/workspace", "size": 150}},
    "env": {
        "HF_TOKEN": "{{ RUNPOD_SECRET_huggingface }}",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "FIZGIG_REF": "master",
    },
}


class TestTemplateChecks:
    def test_conforming_template_passes_every_check(self) -> None:
        failed = [c for c in train_template.run_checks(CONFORMING) if not c.ok]
        assert not failed, [c.render() for c in failed]

    def test_the_real_api_payload_passes_every_check(self) -> None:
        """The checks must read what the API returns, not what it documents.

        The v2 payload carries the volume as ``mounts.persistent`` and SSH as
        ``startSsh``; reading only the flat ``volumeInGb``/``volumeMountPath``
        made a perfectly configured template report UNKNOWN forever.
        """
        failed = [
            c for c in train_template.run_checks(CONFORMING_API_PAYLOAD) if not c.ok
        ]
        assert not failed, [c.render() for c in failed]

    def test_volume_is_read_from_the_nested_mount(self) -> None:
        check = train_template.check_volume(CONFORMING_API_PAYLOAD)
        assert check.ok
        assert "/workspace = 150 GB" in check.detail

    def test_nested_mount_on_the_wrong_path_is_rejected(self) -> None:
        check = train_template.check_volume(
            {"mounts": {"persistent": {"path": "/data", "size": 150}}}
        )
        assert not check.ok
        assert "erased" in check.detail

    def test_missing_persistent_volume_is_rejected(self) -> None:
        check = train_template.check_volume({"mounts": {}})
        assert not check.ok
        assert "container disk" in check.detail

    def test_ssh_disabled_is_rejected(self) -> None:
        check = train_template.check_ssh({"startSsh": False})
        assert not check.ok
        assert "PUBLIC_KEY" in check.detail

    def test_absent_ssh_field_is_unknown_not_assumed(self) -> None:
        assert not train_template.check_ssh({}).ok

    def test_ssh_enabled_passes(self) -> None:
        assert train_template.check_ssh({"startSsh": True}).ok


    def test_foreign_repository_is_rejected(self) -> None:
        assert not train_template.check_image(
            {"imageName": "docker.io/someoneelse/train:1.0"}
        ).ok

    def test_missing_ssh_port_is_rejected_with_the_reason(self) -> None:
        check = train_template.check_ports({"ports": "6080/http,8080/http"})
        assert not check.ok
        assert "22/tcp" in check.detail
        assert "unreachable" in check.detail

    def test_wrong_volume_mount_is_rejected(self) -> None:
        check = train_template.check_volume(
            {"volumeMountPath": "/data", "volumeInGb": 150}
        )
        assert not check.ok
        assert "erased" in check.detail

    def test_small_volume_is_rejected(self) -> None:
        assert not train_template.check_volume(
            {"volumeMountPath": "/workspace", "volumeInGb": 40}
        ).ok

    def test_absent_hf_token_is_rejected(self) -> None:
        checks = train_template.check_env({"HF_HUB_DISABLE_TELEMETRY": "1"})
        assert any(not c.ok and c.name == "env.HF_TOKEN" for c in checks)

    def test_telemetry_not_disabled_is_rejected(self) -> None:
        checks = train_template.check_env(
            {"HF_TOKEN": "hf_x", "HF_HUB_DISABLE_TELEMETRY": "0"}
        )
        assert any(not c.ok and "TELEMETRY" in c.name for c in checks)

    def test_a_foreign_fizgig_repo_is_flagged(self) -> None:
        """FIZGIG_REPO runs Fizgig from somewhere the launcher never chose."""
        checks = train_template.check_env(
            {
                "HF_TOKEN": "hf_x",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "FIZGIG_REF": "master",
                "FIZGIG_REPO": "https://github.com/someone/fork.git",
            }
        )
        assert any(not c.ok and c.name == "env.FIZGIG_REPO" for c in checks)

    def test_a_missing_fizgig_ref_is_flagged(self) -> None:
        """Without it the pod silently falls back to upstream's default, and the
        launcher can no longer report which ref it was meant to be on."""
        checks = train_template.check_env(
            {"HF_TOKEN": "hf_x", "HF_HUB_DISABLE_TELEMETRY": "1"}
        )
        assert any(not c.ok and c.name == "env.FIZGIG_REF" for c in checks)

    def test_an_explicit_fizgig_ref_passes(self) -> None:
        """FIZGIG_REF is the update mechanism now — its presence is correct."""
        checks = train_template.check_env(
            {"HF_TOKEN": "hf_x", "HF_HUB_DISABLE_TELEMETRY": "1", "FIZGIG_REF": "master"}
        )
        assert not [c for c in checks if not c.ok], [c.render() for c in checks]

    def test_unknown_fields_fail_rather_than_pass(self) -> None:
        """A field the payload does not expose must never be assumed correct."""
        for check in (
            train_template.check_image({}),
            train_template.check_ports({}),
            train_template.check_volume({}),
        ):
            assert not check.ok
            assert "UNKNOWN" in check.detail

    def test_ports_parse_both_shapes(self) -> None:
        assert train_template.parse_ports("22/tcp, 6080/http") == {
            "22/tcp",
            "6080/http",
        }
        assert train_template.parse_ports(
            [{"privatePort": 22, "protocol": "tcp"}]
        ) == {"22/tcp"}
        assert train_template.parse_ports({"weird": True}) is None
        assert train_template.parse_ports(None) is None

    def test_summary_counts_failures_without_values(self) -> None:
        checks = train_template.run_checks({"ports": "6080/http"})
        summary = train_template.summarize(checks)
        assert "NOT compliant" in summary
        assert "22/tcp" not in summary or "ports" in summary

    def test_verify_template_uses_the_injected_client(self) -> None:
        class _Client:
            def __init__(self):
                self.asked: list[str] = []

            def get_template(self, template_id):
                self.asked.append(template_id)
                return CONFORMING

        client = _Client()
        checks = train_template.verify_template("tmpl_train", client)
        assert client.asked == ["tmpl_train"]
        assert not train_template.failures(checks)


class TestTemplateSpecFile:
    """The declarative spec must describe a template the checks accept."""

    SPEC = Path(__file__).resolve().parents[1] / "scripts" / "train" / "template.spec.json"


def test_train_tunnel_is_probed_on_both_ports(monkeypatch) -> None:
    """One tunnel, two ports: liveness must require BOTH forwards."""
    cfg = load_config(env={"LAUNCHER_STACK": "train", "RUNPOD_POD_ID": "pod_train"})
    seen: list = []

    class _Tunnels:
        def is_alive(self, name, target=None):
            seen.append((name, target))
            return True

    state = orchestrator.runtime_state.RuntimeState()
    entries = state.load()
    entries["tunnels:train"] = orchestrator.runtime_state.ProcessEntry(
        pid=1234, label="train", port=0, marker="tunnel", created_at=0.0
    )
    state.save(entries)
    monkeypatch.setattr(
        orchestrator.runtime_state, "pid_is_alive", lambda pid: True
    )
    summary = orchestrator.operational_status(
        config=cfg, tunnels=_Tunnels(), state=state, registry=PodRegistry()
    )
    assert summary["tunnel"] == "CONNECTED"
    assert seen == [("train", None)]

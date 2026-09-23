"""Tests for configuration validation and missing-configuration handling."""

from dataclasses import replace

import pytest

from launcher.config import ConfigError, Config, load_config, validate


def _config(**overrides) -> Config:
    config = load_config(env={})
    return replace(config, **overrides)


def test_valid_config_passes() -> None:
    validate(load_config(env={}))


def test_non_positive_ssh_timeout_rejected() -> None:
    from launcher.config import SshConfig

    config = _config(ssh=SshConfig(connect_timeout=0))
    with pytest.raises(ConfigError):
        validate(config)


def test_tilde_in_ssh_key_path_is_expanded() -> None:
    """OpenSSH does not expand ``~`` in an ``-i`` argument."""
    import os

    config = load_config(env={"SSH_KEY_PATH": "~/.ssh/id_ed25519"})
    assert config.ssh.key_path == os.path.expanduser("~/.ssh/id_ed25519")
    assert "~" not in (config.ssh.key_path or "")


def test_tilde_in_launcher_home_is_expanded() -> None:
    import os

    config = load_config(env={"MINIMAX_LAUNCHER_HOME": "~/forge-home"})
    assert config.launcher_home == os.path.expanduser("~/forge-home")
    validate(config)


def test_hf_repo_id_accepts_every_huggingface_host_spelling() -> None:
    from launcher.config import normalize_hf_repo_id

    for raw in (
        "https://huggingface.co/datasets/foo/bar",
        "https://www.huggingface.co/datasets/foo/bar",
        "https://hf.co/datasets/foo/bar",
        "huggingface.co/foo/bar",
        "foo/bar",
    ):
        assert normalize_hf_repo_id(raw) == "foo/bar", raw


def test_hf_repo_id_rejects_a_foreign_url() -> None:
    """A non-HF URL used to become the bogus id ``example.com/a``."""
    from launcher.config import normalize_hf_repo_id

    assert normalize_hf_repo_id("https://example.com/a/b") is None


def test_recovery_mode_defaults_to_disabled() -> None:
    config = load_config(env={})
    assert config.recover.mode == "disabled"


def test_recovery_mode_loaded_from_environment() -> None:
    config = load_config(env={"LAUNCHER_INFRA_RECOVERY": "confirm"})
    assert config.recover.mode == "confirm"


def test_invalid_recovery_mode_rejected() -> None:
    from launcher.config import RecoveryConfig

    config = _config(recover=RecoveryConfig(mode="auto"))
    with pytest.raises(ConfigError):
        validate(config)


# ---------------------------------------------------------------------------
# Pod provisioning validation
# ---------------------------------------------------------------------------


def test_empty_gpu_id_rejected() -> None:
    from launcher.config import RunPodConfig

    config = _config(runpod=RunPodConfig(gpu_id=""))
    with pytest.raises(ConfigError):
        validate(config)


def test_zero_gpu_count_rejected() -> None:
    from launcher.config import RunPodConfig

    config = _config(runpod=RunPodConfig(gpu_count=0))
    with pytest.raises(ConfigError):
        validate(config)


def test_provision_timeout_below_60_rejected() -> None:
    from launcher.config import RunPodConfig

    config = _config(runpod=RunPodConfig(provision_timeout=59))
    with pytest.raises(ConfigError):
        validate(config)


def test_provision_timeout_above_7200_rejected() -> None:
    from launcher.config import RunPodConfig

    config = _config(runpod=RunPodConfig(provision_timeout=7201))
    with pytest.raises(ConfigError):
        validate(config)


def test_provision_timeout_boundaries_accepted() -> None:
    from launcher.config import RunPodConfig

    validate(_config(runpod=RunPodConfig(provision_timeout=60)))
    validate(_config(runpod=RunPodConfig(provision_timeout=7200)))


def test_empty_data_center_entry_rejected() -> None:
    from launcher.config import RunPodConfig

    config = _config(runpod=RunPodConfig(data_centers=("dc-a", "")))
    with pytest.raises(ConfigError):
        validate(config)


def test_empty_pod_name_rejected() -> None:
    from launcher.config import RunPodConfig

    config = _config(runpod=RunPodConfig(pod_name=""))
    with pytest.raises(ConfigError):
        validate(config)


def test_relative_launcher_home_rejected() -> None:
    config = _config(launcher_home="relative/forge-home")
    with pytest.raises(ConfigError):
        validate(config)


def test_absolute_launcher_home_accepted() -> None:
    import os

    config = _config(launcher_home=os.path.abspath("forge-home"))
    validate(config)

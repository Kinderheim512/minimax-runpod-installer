"""Tests for configuration loading."""

import os

import pytest

from launcher.config import (
    DEFAULT_COMFY_TEMPLATE_ID,
    DEFAULT_RUNPOD_GPU_COUNT,
    DEFAULT_RUNPOD_GPU_ID,
    DEFAULT_RUNPOD_PROVISION_TIMEOUT,
    DEFAULT_STACK,
    STACKS,
    load_config,
)
from launcher.credentials import (
    CredentialStoreCorrupt,
    CredentialStoreMissing,
    CredentialStoreUnsupported,
    RunPodCredentials,
)

STORED_KEY = "rp_stored_key_abcdef0123456789"
STORED_TEMPLATE = "tmpl_stored_abcdef0123456789"
STORED_HF = "hf_stored_token_abcdef0123456789"
STORED_CIVITAI = "civ_stored_key_abcdef0123456789"


class _FakeStore:
    """Duck-typed credential store for load_config integration tests."""

    def __init__(self, creds=None, error=None):
        self._creds = creds
        self._error = error
        self.calls = 0

    def get(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._creds


def test_blank_values_treated_as_unset() -> None:
    config = load_config(
        env={
            "RUNPOD_API_KEY": "",
            "SSH_KEY_PATH": "",
            "RUNPOD_POD_ID": "",
        }
    )

    assert config.secrets.runpod_api_key is None
    assert config.ssh.key_path is None
    assert config.runpod.pod_id is None


# ---------------------------------------------------------------------------
# Secure credential store integration (precedence: env > stored > missing)
# ---------------------------------------------------------------------------


def test_stored_credentials_used_when_env_absent() -> None:
    store = _FakeStore(
        creds=RunPodCredentials(
            runpod_api_key=STORED_KEY, runpod_template_id=STORED_TEMPLATE
        )
    )
    config = load_config(env={}, store=store)
    assert store.calls == 1
    assert config.secrets.runpod_api_key == STORED_KEY
    assert config.secrets.runpod_template_id == STORED_TEMPLATE


def test_env_api_key_overrides_stored_value() -> None:
    store = _FakeStore(
        creds=RunPodCredentials(
            runpod_api_key=STORED_KEY, runpod_template_id=STORED_TEMPLATE
        )
    )
    config = load_config(
        env={"RUNPOD_API_KEY": "rp_env_key"}, store=store
    )
    assert config.secrets.runpod_api_key == "rp_env_key"
    assert config.secrets.runpod_template_id == STORED_TEMPLATE


def test_env_template_id_overrides_stored_value() -> None:
    store = _FakeStore(
        creds=RunPodCredentials(
            runpod_api_key=STORED_KEY, runpod_template_id=STORED_TEMPLATE
        )
    )
    config = load_config(
        env={"RUNPOD_TEMPLATE_ID": "tmpl_env"}, store=store
    )
    assert config.secrets.runpod_api_key == STORED_KEY
    assert config.secrets.runpod_template_id == "tmpl_env"


def test_pod_id_never_loaded_from_store() -> None:
    store = _FakeStore(
        creds=RunPodCredentials(
            runpod_api_key=STORED_KEY, runpod_template_id=STORED_TEMPLATE
        )
    )
    config = load_config(env={}, store=store)
    assert config.runpod.pod_id is None

    config = load_config(
        env={"RUNPOD_POD_ID": "pod_abc"}, store=store
    )
    assert config.runpod.pod_id == "pod_abc"


def test_missing_store_is_silent() -> None:
    store = _FakeStore(error=CredentialStoreMissing("not stored"))
    config = load_config(env={}, store=store)
    assert config.secrets.runpod_api_key is None
    assert config.secrets.runpod_template_id is None


def test_unsupported_store_platform_is_silent() -> None:
    store = _FakeStore(error=CredentialStoreUnsupported("no DPAPI here"))
    config = load_config(env={}, store=store)
    assert config.secrets.runpod_api_key is None


def test_corrupt_store_does_not_mask_env_value() -> None:
    store = _FakeStore(error=CredentialStoreCorrupt("undecryptable"))
    config = load_config(
        env={"RUNPOD_API_KEY": "rp_env_key"}, store=store
    )
    assert config.secrets.runpod_api_key == "rp_env_key"


def test_no_credentials_arg_never_touches_store() -> None:
    config = load_config(env={})
    assert config.secrets.runpod_api_key is None
    assert config.secrets.runpod_template_id is None


def test_secrets_repr_redacts_stored_values() -> None:
    store = _FakeStore(
        creds=RunPodCredentials(
            runpod_api_key=STORED_KEY, runpod_template_id=STORED_TEMPLATE
        )
    )
    config = load_config(env={}, store=store)
    assert STORED_KEY not in repr(config.secrets)
    assert STORED_TEMPLATE not in repr(config.secrets)


def test_extras_default_to_empty() -> None:
    config = load_config(env={})
    assert config.secrets.extra == {}


def test_extras_loaded_from_environment() -> None:
    config = load_config(env={"HF_TOKEN": "hf_env_token"})
    assert config.secrets.extra == {"HF_TOKEN": "hf_env_token"}


def test_stored_extras_used_when_env_absent() -> None:
    store = _FakeStore(
        creds=RunPodCredentials(
            runpod_api_key=STORED_KEY,
            runpod_template_id=STORED_TEMPLATE,
            extra={"HF_TOKEN": STORED_HF, "CIVITAI_API_KEY": STORED_CIVITAI},
        )
    )
    config = load_config(env={}, store=store)
    assert config.secrets.extra == {
        "HF_TOKEN": STORED_HF,
        "CIVITAI_API_KEY": STORED_CIVITAI,
    }


def test_env_extra_overrides_stored_value() -> None:
    store = _FakeStore(
        creds=RunPodCredentials(
            runpod_api_key=STORED_KEY,
            runpod_template_id=STORED_TEMPLATE,
            extra={"HF_TOKEN": STORED_HF, "CIVITAI_API_KEY": STORED_CIVITAI},
        )
    )
    config = load_config(env={"HF_TOKEN": "hf_env_token"}, store=store)
    assert config.secrets.extra == {
        "HF_TOKEN": "hf_env_token",
        "CIVITAI_API_KEY": STORED_CIVITAI,
    }


def test_store_consulted_when_only_extras_missing_from_env() -> None:
    store = _FakeStore(
        creds=RunPodCredentials(
            runpod_api_key=STORED_KEY,
            runpod_template_id=STORED_TEMPLATE,
            extra={"HF_TOKEN": STORED_HF},
        )
    )
    config = load_config(
        env={"RUNPOD_API_KEY": "rp_env_key", "RUNPOD_TEMPLATE_ID": "tmpl_env"},
        store=store,
    )
    assert store.calls == 1
    assert config.secrets.runpod_api_key == "rp_env_key"
    assert config.secrets.extra == {"HF_TOKEN": STORED_HF}


def test_secrets_repr_redacts_extras() -> None:
    store = _FakeStore(
        creds=RunPodCredentials(
            runpod_api_key=STORED_KEY,
            runpod_template_id=STORED_TEMPLATE,
            extra={"HF_TOKEN": STORED_HF},
        )
    )
    config = load_config(env={}, store=store)
    assert STORED_HF not in repr(config.secrets)


# ---------------------------------------------------------------------------
# Pod provisioning settings (RUNPOD_GPU_ID, RUNPOD_GPU_COUNT, ...)
# ---------------------------------------------------------------------------


def test_provisioning_defaults_when_environment_empty() -> None:
    config = load_config(env={})
    assert config.runpod.gpu_id == DEFAULT_RUNPOD_GPU_ID == "NVIDIA RTX A6000"
    assert config.runpod.gpu_count == DEFAULT_RUNPOD_GPU_COUNT == 1
    assert config.runpod.data_centers == ()
    assert config.runpod.pod_name is None
    assert config.runpod.pod_env == {}
    assert (
        config.runpod.provision_timeout
        == DEFAULT_RUNPOD_PROVISION_TIMEOUT
        == 1800
    )
    assert config.launcher_home is None


def test_pod_env_loaded_from_environment() -> None:
    config = load_config(
        env={"RUNPOD_POD_ENV": '{"MAX_MODEL_LEN": "262144", "MAX_NUM_SEQS": "1"}'}
    )
    assert config.runpod.pod_env == {"MAX_MODEL_LEN": "262144", "MAX_NUM_SEQS": "1"}


def test_pod_env_invalid_json_rejected() -> None:
    from launcher.config import ConfigError

    with pytest.raises(ConfigError):
        load_config(env={"RUNPOD_POD_ENV": "not-json"})
    with pytest.raises(ConfigError):
        load_config(env={"RUNPOD_POD_ENV": "[1, 2, 3]"})
    with pytest.raises(ConfigError):
        load_config(env={"RUNPOD_POD_ENV": '{"MAX_MODEL_LEN": 262144}'})


def test_provisioning_values_loaded_from_environment() -> None:
    launcher_home = os.path.abspath("forge-home")
    config = load_config(
        env={
            "RUNPOD_GPU_ID": "NVIDIA GeForce RTX 4090",
            "RUNPOD_GPU_COUNT": "2",
            "RUNPOD_DATA_CENTERS": "dc-a,dc-b",
            "RUNPOD_POD_NAME": "my-forge-pod",
            "RUNPOD_PROVISION_TIMEOUT": "2400",
            "MINIMAX_LAUNCHER_HOME": launcher_home,
        }
    )
    assert config.runpod.gpu_id == "NVIDIA GeForce RTX 4090"
    assert config.runpod.gpu_count == 2
    assert config.runpod.data_centers == ("dc-a", "dc-b")
    assert config.runpod.pod_name == "my-forge-pod"
    assert config.runpod.provision_timeout == 2400
    assert config.launcher_home == launcher_home


def test_data_centers_parsing_strips_and_drops_empty() -> None:
    config = load_config(env={"RUNPOD_DATA_CENTERS": " dc-a ,, dc-b ,"})
    assert config.runpod.data_centers == ("dc-a", "dc-b")

    assert load_config(env={"RUNPOD_DATA_CENTERS": ""}).runpod.data_centers == ()
    assert load_config(env={"RUNPOD_DATA_CENTERS": ",,"}).runpod.data_centers == ()


def test_blank_provisioning_values_treated_as_unset() -> None:
    config = load_config(
        env={
            "RUNPOD_GPU_COUNT": "",
            "RUNPOD_POD_NAME": "",
            "RUNPOD_PROVISION_TIMEOUT": "",
            "MINIMAX_LAUNCHER_HOME": "",
        }
    )
    assert config.runpod.gpu_count == DEFAULT_RUNPOD_GPU_COUNT
    assert config.runpod.pod_name is None
    assert config.runpod.provision_timeout == DEFAULT_RUNPOD_PROVISION_TIMEOUT
    assert config.launcher_home is None


def test_non_integer_gpu_count_raises_config_error() -> None:
    from launcher.config import ConfigError

    with pytest.raises(ConfigError):
        load_config(env={"RUNPOD_GPU_COUNT": "not-a-number"})


def test_non_integer_provision_timeout_raises_config_error() -> None:
    from launcher.config import ConfigError

    with pytest.raises(ConfigError):
        load_config(env={"RUNPOD_PROVISION_TIMEOUT": "not-a-number"})


def test_launcher_home_not_loaded_when_unset() -> None:
    config = load_config(env={})
    assert config.launcher_home is None

# ---------------------------------------------------------------------------
# The public template is the default the launcher ships
# ---------------------------------------------------------------------------


def test_the_comfy_template_defaults_to_the_public_one() -> None:
    """Nothing configured -> the PUBLIC template id, so a first run works.

    A private template id still wins (that is asserted end to end in
    ``test_orchestrator.py``); this pins the default itself, which is what the
    "click and go" promise rests on.
    """
    config = load_config(env={})
    assert config.secrets.comfy_template_id == DEFAULT_COMFY_TEMPLATE_ID
    assert DEFAULT_COMFY_TEMPLATE_ID == "oa2vozqbum"


def test_an_explicit_comfy_template_wins_over_the_default() -> None:
    config = load_config(env={"RUNPOD_COMFY_TEMPLATE_ID": "tmpl_private"})
    assert config.secrets.comfy_template_id == "tmpl_private"

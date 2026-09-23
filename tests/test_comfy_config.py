"""Tests for the comfy stack configuration resolution (launcher.config)."""

import pytest

from launcher.config import (
    COMFY_ACCESS_MODES,
    COMFY_NO_PRESET_SENTINEL,
    COMFY_SAGE_MODES,
    COMFY_TIERS,
    COMFY_WORKFLOW_TASKS,
    ConfigError,
    load_config,
)

# ---------------------------------------------------------------------------
# Stack resolution
# ---------------------------------------------------------------------------


def test_stack_from_env() -> None:
    assert load_config(env={"LAUNCHER_STACK": "comfy"}).stack == "comfy"


def test_unknown_stack_rejected() -> None:
    with pytest.raises(ConfigError, match="stack"):
        load_config(env={"LAUNCHER_STACK": "nope"})


# ---------------------------------------------------------------------------
# Comfy defaults
# ---------------------------------------------------------------------------


def test_comfy_defaults() -> None:
    comfy = load_config(env={}, stack="comfy").comfy
    assert comfy.preset == "dasiwa_mmh3v12"
    assert comfy.tier == "auto"
    assert comfy.workflows == "all"
    assert comfy.access == "tunnel"
    assert comfy.local_port == 8188
    assert comfy.remote_port == 8188
    # The Turbo LoRA / Spectrum node are Library-managed content now: the
    # launcher never auto-installs them unless explicitly asked to.
    assert comfy.turbo_lora is False
    assert comfy.spectrum is False
    assert comfy.sage_attention == "auto"
    assert comfy.ntfy_topic is None
    assert comfy.personal_storage_repo is None


def test_comfy_reception_options_default_to_off() -> None:
    comfy = load_config(env={}, stack="comfy").comfy
    assert comfy.auto_collect is False
    assert comfy.notify_windows is False
    assert comfy.notify_ntfy is False
    assert comfy.terminate_after_generation is False
    # Empty = the user's Downloads folder (resolved by launcher.comfy_watch).
    assert comfy.outputs_dir is None


def test_comfy_terminate_settle_seconds_rejects_junk() -> None:
    with pytest.raises(ConfigError):
        load_config(env={"COMFY_TERMINATE_SETTLE_SECONDS": "soon"})
    with pytest.raises(ConfigError):
        load_config(env={"COMFY_TERMINATE_SETTLE_SECONDS": "nan"})


def test_comfy_reception_options_never_reach_the_pod() -> None:
    """They drive the launcher's watcher only — the pod must not see them."""
    comfy = load_config(
        env={"COMFY_AUTO_COLLECT": "true", "COMFY_OUTPUTS_DIR": "D:/videos"},
        stack="comfy",
    ).comfy
    env = comfy.pod_env()
    assert not any(key.startswith("COMFY_AUTO_COLLECT") for key in env)
    assert not any("OUTPUTS_DIR" in key for key in env)
    assert "auto_collect" not in env
    # No launcher-side ntfy notice requested -> the pod keeps its own.
    assert "NOTIFY_ON_GENERATION" not in env


def test_comfy_base_url_is_local_tunnel_url() -> None:
    assert load_config(env={}, stack="comfy").comfy_base_url() == "http://127.0.0.1:8188"


# ---------------------------------------------------------------------------
# Comfy env / override resolution
# ---------------------------------------------------------------------------


def test_comfy_empty_env_falls_back_to_default() -> None:
    cfg = load_config(env={"COMFY_PRESET": "", "COMFY_TIER": ""}, stack="comfy")
    assert cfg.comfy.preset == "dasiwa_mmh3v12"
    assert cfg.comfy.tier == "auto"


# ---------------------------------------------------------------------------
# Comfy pod env
# ---------------------------------------------------------------------------


def test_comfy_pod_env_defaults() -> None:
    env = load_config(env={}, stack="comfy").resolved_comfy_pod_env()
    assert env["H3_TIER"] == "auto"
    assert env["H3_WORKFLOWS"] == "all"
    assert env["COMFYUI_PORT"] == "8188"
    assert "NTFY_TOPIC" not in env
    assert "PERSONAL_STORAGE_HF_REPO" not in env
    assert "COMFYUI_COMMIT" not in env
    # Retired keys (2.0.0): presets are launcher-created only, and
    # SageAttention / Turbo / Spectrum are Library-managed content, so the
    # launcher no longer configures them on the pod at all.
    for retired in (
        "H3_PRESETS",
        "H3_DASIWA_CHECKPOINT_VARIANT",
        "INSTALL_SPECTRUM",
        "MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD",
        "SAGE_ATTENTION",
    ):
        assert retired not in env


def test_comfy_pod_env_retired_keys_never_sent_even_when_flags_set() -> None:
    """The legacy opt-in flags no longer reach the pod."""
    env = load_config(
        env={"COMFY_TURBO_LORA": "true", "COMFY_SPECTRUM": "true"}, stack="comfy"
    ).resolved_comfy_pod_env()
    assert "MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD" not in env
    assert "INSTALL_SPECTRUM" not in env


def test_comfyui_version_unset_by_default() -> None:
    cfg = load_config(env={}, stack="comfy")
    assert cfg.comfy.comfyui_version is None
    assert "COMFYUI_COMMIT" not in cfg.resolved_comfy_pod_env()


def test_normalize_hf_repo_id_bare() -> None:
    from launcher.config import normalize_hf_repo_id

    assert normalize_hf_repo_id("Kinderheim/minimax-runpod-perso") == "Kinderheim/minimax-runpod-perso"
    assert normalize_hf_repo_id("") is None
    assert normalize_hf_repo_id("justanamespace") is None


def test_normalize_hf_repo_id_full_urls() -> None:
    from launcher.config import normalize_hf_repo_id

    assert normalize_hf_repo_id("https://huggingface.co/Kinderheim/private") == "Kinderheim/private"
    assert (
        normalize_hf_repo_id("https://huggingface.co/datasets/Kinderheim/minimax-runpod-perso")
        == "Kinderheim/minimax-runpod-perso"
    )
    assert (
        normalize_hf_repo_id(
            "https://huggingface.co/datasets/Kinderheim/minimax-runpod-perso/resolve/main/loras_manifest.txt?download=true"
        )
        == "Kinderheim/minimax-runpod-perso"
    )


def test_comfy_model_slot_defaults() -> None:
    cfg = load_config(env={}, stack="comfy")
    env = cfg.resolved_comfy_pod_env()
    assert env["H3_DIFFUSION_URL"].endswith("DasiwaMinimaxH3_dasiwaREF2VAHybridV1.safetensors")
    assert env["H3_VIDEO_VAE_URL"].endswith("minimax_h3_video_vae_int8_convrot.safetensors")
    assert env["H3_AUDIO_VAE_URL"].endswith("minimax_h3_audio_vae_fp32.safetensors")
    assert env["H3_TEXT_ENCODER_URL"].endswith("qwen3vl_32b_minimax_h3_int4_convrot.safetensors")
    assert "H3_DASIWA_CHECKPOINT_VARIANT" not in env
    assert "H3_TAE_URL" in env
    assert "H3_UPSCALER_URL" in env
    assert "H3_FRAME_INTERP_URL" in env


def test_comfy_hybrid_tier_maps_to_auto_tier() -> None:
    """``hybrid`` is a launcher-side alias that maps onto the auto tier.

    The pod-side ``H3_DASIWA_CHECKPOINT_VARIANT`` key it used to drive is
    retired with the hardcoded DaSiWa preset machinery.
    """
    cfg = load_config(env={"COMFY_TIER": "hybrid"}, stack="comfy")
    env = cfg.resolved_comfy_pod_env()
    assert env["H3_TIER"] == "auto"  # hybrid is a checkpoint variant, not a weight tier
    assert "H3_DASIWA_CHECKPOINT_VARIANT" not in env


def test_comfy_pod_env_false_flags() -> None:
    """The legacy false flags are inert: those keys are no longer sent."""
    cfg = load_config(env={"COMFY_TURBO_LORA": "false", "COMFY_SPECTRUM": "0"}, stack="comfy")
    env = cfg.resolved_comfy_pod_env()
    assert "MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD" not in env
    assert "INSTALL_SPECTRUM" not in env


# ---------------------------------------------------------------------------
# C2 — three model-selection modes (auto / auto_perso / perso)
# ---------------------------------------------------------------------------


def test_gui_tier_auto_perso_maps_to_pod_auto() -> None:
    cfg = load_config(stack="comfy", comfy={"tier": "auto_perso"})
    assert cfg.comfy.tier == "auto"
    assert cfg.resolved_comfy_pod_env()["H3_TIER"] == "auto"


def test_custom_model_categories_absent_by_default() -> None:
    cfg = load_config(env={}, stack="comfy")
    assert cfg.comfy.custom_model_categories is None
    assert "H3_CUSTOM_MODEL_CATEGORIES" not in cfg.resolved_comfy_pod_env()


def test_custom_model_categories_explicit_none_not_sent() -> None:
    # GUI auto mode: the override dict explicitly carries None, so the pod
    # env must not define the variable at all (config.env default = empty).
    cfg = load_config(
        env={"H3_CUSTOM_MODEL_CATEGORIES": "diffusion"},
        stack="comfy",
        comfy={"tier": "auto", "custom_model_categories": None},
    )
    assert cfg.comfy.custom_model_categories is None
    assert "H3_CUSTOM_MODEL_CATEGORIES" not in cfg.resolved_comfy_pod_env()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Annuaire (LoRA / Workflow / Node) — launcher -> pod download lists
# ---------------------------------------------------------------------------


def test_comfy_annuaire_empty_by_default() -> None:
    cfg = load_config(env={}, stack="comfy")
    assert cfg.comfy.custom_loras == ()
    assert cfg.comfy.custom_nodes == ()
    assert cfg.comfy.custom_workflows == ()
    assert "H3_CUSTOM_LORAS" not in cfg.resolved_comfy_pod_env()
    assert "H3_CUSTOM_NODES" not in cfg.resolved_comfy_pod_env()
    assert "H3_CUSTOM_WORKFLOWS" not in cfg.resolved_comfy_pod_env()



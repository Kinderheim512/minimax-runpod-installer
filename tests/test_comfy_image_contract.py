"""The baked image contract must match what the launcher actually expects.

``comfy/image-contract.json`` is baked into the image
(``openfox-forge-comfy``) and describes its guarantees: paths, endpoints,
pod-side scripts, CUDA floor, and the environment keys it consumes. It only
has value if it is *enforced*, so this test binds it to the launcher's own
constants — a drift (e.g. the ``INSTALL_DIR`` mismatch documented in
``docs/comfy-image-analysis.md`` §9) then fails in CI instead of surfacing as
a silent pod-side misbehaviour.

See ``docs/comfy-image-analysis.md`` §7.6 for the rationale.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from launcher import comfy_ops
from launcher.config import ComfyConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "image-contract.json"

#: Keys RunPod itself injects (not sent by the launcher), so they legitimately
#: appear in the contract without being launcher-managed.
RUNPOD_INJECTED_KEYS = frozenset({"PUBLIC_KEY"})


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _launcher_sent_keys() -> set[str]:
    """Every env key the launcher can send for the comfy stack.

    Built from a fully-populated ``ComfyConfig`` so the conditional keys
    (``NTFY_TOPIC``, ``PERSONAL_STORAGE_HF_REPO``, the annuaire lists, user
    presets...) are included.
    """
    config = ComfyConfig(
        ntfy_topic="topic",
        personal_storage_repo="namespace/repo",
        comfyui_version="v0.35.0",
        custom_model_categories="diffusion,video_vae",
        custom_loras=("https://example.invalid/lora.safetensors",),
        custom_nodes=("https://example.invalid/node.git",),
        custom_workflows=("https://example.invalid/wf.json wf.json",),
        user_presets={
            "preset": {
                "models": [
                    {
                        "url": "https://example.invalid/m.safetensors",
                        "target": "diffusion_models/m.safetensors",
                    }
                ],
                "nodes": [{"url": "https://github.com/example/node.git"}],
                "workflows": [{"url": "https://example.invalid/w.json"}],
            }
        },
    )
    return set(config.pod_env())


def test_contract_file_exists_and_parses(contract: dict) -> None:
    assert contract["contract_version"] == 1
    # image_version / comfyui_release / torch_build are stamped at build time
    # (the Dockerfile writes what was actually built), so the repository copy
    # only carries placeholders — asserting a literal version here would be
    # wrong by construction.
    assert isinstance(contract["image_version"], str) and contract["image_version"]


def test_cuda_floor_is_cu130(contract: dict) -> None:
    """The image is cu130-only; the contract must state the floor."""
    assert contract["cuda_min"] == "13.0"


def test_install_dir_matches_launcher_scripts_dir(contract: dict) -> None:
    """``project_root`` is where the launcher runs its pod-side scripts."""
    assert contract["project_root"] == comfy_ops.DEFAULT_SCRIPTS_DIR


def test_output_and_workflow_paths_derive_from_install_dir(contract: dict) -> None:
    """The launcher's hardcoded paths must equal install_dir + the subpath.

    This is the assertion that would have caught the ``/opt/ComfyUI`` vs
    ``/workspace/ComfyUI`` split.
    """
    install_dir = contract["install_dir"]
    assert f"{install_dir}/output" == comfy_ops.DEFAULT_OUTPUT_DIR
    assert (
        f"{install_dir}/user/default/workflows" == comfy_ops.DEFAULT_WORKFLOWS_DIR
    )


def test_venv_dir_is_inside_install_dir(contract: dict) -> None:
    assert contract["venv_dir"] == f"{contract['install_dir']}/venv"


def test_manager_dir_is_the_lowercase_contract_name(contract: dict) -> None:
    """ComfyUI-Manager requires ``custom_nodes/comfyui-manager`` (lowercase)."""
    assert contract["manager_dir"] == (
        f"{contract['install_dir']}/custom_nodes/comfyui-manager"
    )
    assert contract["manager_flag"] == "--enable-manager"


def test_pod_side_scripts_exist_in_the_repository(contract: dict) -> None:
    for script in contract["scripts"]:
        assert (REPO_ROOT / script).is_file(), script


def test_declared_env_keys_are_actually_sent_by_the_launcher(
    contract: dict,
) -> None:
    """Every key the image claims to consume must be one the launcher sends.

    A key in the contract that the launcher never sends would mean the image
    documents a knob nobody can turn.
    """
    allowed = _launcher_sent_keys() | RUNPOD_INJECTED_KEYS
    unknown = sorted(set(contract["env_keys"]) - allowed)
    assert not unknown, f"contract declares env keys the launcher never sends: {unknown}"


def test_contract_env_keys_contain_no_wildcard(contract: dict) -> None:
    """The list must be equality-checkable, so no globs are allowed."""
    assert not [k for k in contract["env_keys"] if "*" in k]


def test_retired_env_keys_are_not_declared(contract: dict) -> None:
    """Keys retired by the 2.0.0 decisions must not reappear in the contract."""
    retired = {
        "H3_PRESETS",
        "H3_DASIWA_CHECKPOINT_VARIANT",
        "INSTALL_SPECTRUM",
        "MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD",
        "MINIMAX_H3_TURBO_NODE_AUTO_INSTALL",
        "SAGE_ATTENTION",
        "PREFER_CUDA130",
    }
    assert not retired & set(contract["env_keys"])

"""Configuration model for the MiniMax H3 Launcher.

This module defines a clean, typed configuration abstraction that separates:

* **non-secret configuration** (ports, IDs, paths, model identifier);
* **secrets** (API keys, tokens);
* **defaults**.

No real RunPod, SSH, Docker, or ComfyUI behavior is implemented here. This is a
pure data model that later milestones will populate and consume.

Secret precedence for RunPod secrets: environment variables first, then the
secure DPAPI credential store (when one is supplied), then absent. Secrets are
never hard-coded and never written back to disk or logs.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional

from . import credentials
from . import logging as launcher_logging
from .comfy_presets import build_user_presets_json, normalize_user_presets

logger = launcher_logging.get_logger("minimax-launcher.config")

# Default ports and values used when the corresponding variable is unset.
DEFAULT_SSH_CONNECT_TIMEOUT = 10
DEFAULT_RECOVERY_MODE = "disabled"
DEFAULT_RUNPOD_GPU_ID = "NVIDIA RTX A6000"
DEFAULT_RUNPOD_GPU_COUNT = 1
DEFAULT_RUNPOD_PROVISION_TIMEOUT = 1800

# Workload stacks served by one launcher.
#   "comfy"    — ComfyUI + MiniMax H3 video generation (the comfy/ installer);
#   "train"    — LoRA training (MiniMax H3 / Krea 2 / Klein 9B) on a rented GPU,
#                running Fizgig (see docs/train-stack.md). The pod is a desktop
#                streamed over KasmVNC, reached through an SSH tunnel; nothing
#                is ever published on the pod's public URL.
STACKS = ("comfy", "train")
DEFAULT_STACK = "comfy"

DEFAULT_COMFY_LOCAL_PORT = 8188
DEFAULT_COMFY_REMOTE_PORT = 8188
DEFAULT_COMFY_PRESET = "dasiwa_mmh3v12"
#: Sentinel sent as ``H3_PRESETS`` when the launcher wants NO hardcoded preset.
#: RunPod drops empty-string env values, so ``""`` never reaches the pod and
#: it fell back to the installer's config.env default (dasiwa_mmh3v12). The
#: pod's ``resolve_h3_presets()`` (comfy/lib/presets.sh) maps this sentinel
#: back to "". Reserved: never use it as a user preset name.
COMFY_NO_PRESET_SENTINEL = "none"
DEFAULT_COMFY_TIER = "auto"
DEFAULT_COMFY_WORKFLOWS = "all"
DEFAULT_COMFY_ACCESS = "tunnel"
#: Public RunPod template of the ComfyUI + MiniMax H3 image. Used when no
#: private template is configured, so a first run needs only a RunPod API key
#: — the whole point of the "click and go" path. An explicit
#: ``RUNPOD_COMFY_TEMPLATE_ID`` (or a stored private template) always wins.
DEFAULT_COMFY_TEMPLATE_ID = "rfv75gjaip"
#: Settle window (seconds) before "stop the pod once the queue is empty"
#: fires. See ``ComfyConfig.terminate_settle_seconds``.
DEFAULT_COMFY_TERMINATE_SETTLE_SECONDS = 60.0

# ---------------------------------------------------------------------------
# train stack (LoRA training) — see docs/train-stack.md
# ---------------------------------------------------------------------------
# Two ports, both reached over the SSH tunnel. The desktop (KasmVNC) and the file
# manager (filebrowser) are separate services on separate ports, so the launcher
# opens BOTH in a single `ssh -L` — that is what makes this stack the first to
# need multi-target tunnelling.
DEFAULT_TRAIN_LOCAL_PORT = 6080
DEFAULT_TRAIN_REMOTE_PORT = 6080
# The LOCAL port must not collide with another stack's local port: a second
# `ssh -L` on the same local port makes ssh abort the whole forward
# (ExitOnForwardFailure). The train file manager therefore takes 8081 locally
# while the pod-side service keeps 8080.
DEFAULT_TRAIN_FILES_LOCAL_PORT = 8081
DEFAULT_TRAIN_FILES_REMOTE_PORT = 8080

#: Which model families the pod pre-downloads at boot (~45 GB for MiniMax H3 alone).
#: Empty means "download nothing at boot" — the in-app Preferences button does it on
#: demand instead, which is upstream's default and the cheaper one when you are not
#: sure which family you want yet.
DEFAULT_TRAIN_FETCH_MODELS = ""

#: Image tag the RunPod template must reference.
#:
#: ``latest`` on purpose, and it is worth being explicit about why, because
#: this used to be forbidden.
#:
#: The rule ("never :latest: a floating tag silently moves every existing
#: template onto different code") came from the era when this was OUR image: the
#: tag was the only version axis and the audit hung off it, so pinning was a
#: security property. Both premises are gone — the image is upstream's and the
#: application already floats on ``fizgig_ref``.
#:
#: Worse, pinning only the runtime was incoherent: the app would race ahead of
#: the base on every restart while the pin sat there rotting until someone
#: edited this constant by hand — the maintenance burden the floating ref exists
#: to remove, re-introduced on the other axis. ``latest`` + ``master`` track
#: upstream together and agree at pod creation.
#:
#: What it costs: a pod created today can boot a different runtime than one
#: created yesterday, and a broken upstream image breaks the *next* pod
#: creation — which is worse than a broken app, because it fails before the
#: tunnel exists and no version check can look inside. Pin a concrete tag here
#: temporarily if that happens; that is the whole rollback story.
DEFAULT_TRAIN_IMAGE_TAG = "latest"

#: Which Fizgig the pod runs. Upstream's entrypoint clones/pulls this ref at
#: every boot, so the pod is current each time it starts and the app updates
#: itself independently of the image.
#:
#: Left on ``master`` on purpose: the launcher does not want to be the thing
#: that decides when Fizgig moves. The cost is that the code changes between two
#: restarts, which is why ``launcher.fizgig_version`` reports what the pod
#: actually ended up running. Pin it to a tag instead if a run must be
#: reproducible — it is a pod env change, not an image rebuild.
DEFAULT_TRAIN_FIZGIG_REF = "master"

#: Volume size in GB. MiniMax H3 weights are ~45 GB before any dataset, latent cache
#: or checkpoint; below this the pod fills up mid-run.
DEFAULT_TRAIN_VOLUME_GB = 150

#: Where trained LoRAs are collected on the Windows host. The user picks this in the
#: GUI; the default mirrors the comfy stack's output convention.
DEFAULT_TRAIN_LORA_DIR = ""

#: Fizgig can stop its own pod when a training run finishes. The launcher also knows
#: how to stop a pod. Both at once means two mechanisms racing over one machine, so
#: the default is the launcher's (it also knows the tunnel state) and the in-app one
#: stays off.
DEFAULT_TRAIN_ALLOW_IN_APP_AUTOSTOP = False

#: The launcher's tunnel is an `ssh -L`, which needs port 22 open on the pod. Upstream's
#: image deliberately leaves 22 closed unless PUBLIC_KEY is set, so the RunPod template
#: MUST enable SSH access. This flag exists so a mis-created template fails with a clear
#: message instead of an unexplained tunnel timeout.
DEFAULT_TRAIN_REQUIRE_SSH = True

TRAIN_IMAGE_REPOSITORY = "ghcr.io/shootthesound/fizgig"


#: Every Hugging Face host spelling a user may paste (the canonical one, the
#: ``www.`` variant, and the ``hf.co`` shortener).
_HF_HOSTS = frozenset({"huggingface.co", "www.huggingface.co", "hf.co", "www.hf.co"})
#: A valid ``namespace`` / ``repo_name`` segment.
_HF_REPO_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def normalize_hf_repo_id(value: str) -> Optional[str]:
    """Reduce a Hugging Face repo reference to a bare ``namespace/repo`` id.

    Accepts a bare id (``Kinderheim/minimax-runpod-perso``) or a full URL
    (``https://huggingface.co/datasets/Kinderheim/minimax-runpod-perso/...``,
    or a ``/resolve/``, ``/tree/``, ``/blob/`` link). ``hf download`` and the
    installer's ``PERSONAL_STORAGE_HF_REPO`` expect only the bare id; anything
    with a host/path prefix is rejected with "Repo id must be in the form
    'namespace/repo_name'". Returns None when nothing usable remains.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    raw = raw.split("?")[0].split("#")[0].rstrip("/")
    for marker in ("/resolve/", "/tree/", "/blob/", "/raw/"):
        if marker in raw:
            raw = raw.split(marker, 1)[0]
            break
    had_scheme = False
    for scheme in ("https://", "http://"):
        if raw.startswith(scheme):
            raw = raw[len(scheme):]
            had_scheme = True
            break
    # Any Hugging Face host spelling: ``huggingface.co``, ``www.huggingface.co``
    # and the ``hf.co`` shortener are all copy-pasted in practice. Only the
    # canonical one used to be stripped, so the others silently became a
    # "namespace" — ``https://hf.co/datasets/foo/bar`` produced the bogus id
    # ``hf.co/datasets``, which then failed on the pod, far from the input.
    host, sep, rest = raw.partition("/")
    if host.lower() in _HF_HOSTS:
        raw = rest if sep else ""
    elif had_scheme:
        # A URL pointing anywhere else is not a Hugging Face repo reference.
        return None
    for prefix in ("datasets/", "models/", "spaces/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    parts = [p for p in raw.split("/") if p]
    if len(parts) == 0:
        return None
    if len(parts) == 1:
        return None  # "namespace" alone is not a repo id
    if not all(_HF_REPO_SEGMENT_RE.fullmatch(part) for part in parts[:2]):
        return None
    return "/".join(parts[:2])

# Default per-slot model URLs for the dasiwa preset. The launcher injects
# these as pod env (H3_*_URL) so the installer downloads exactly this set;
# each is overridable from the GUI. Kept in sync with comfy/config.env.
DEFAULT_COMFY_DIFFUSION_URL = (
    "https://huggingface.co/Kinderheim/private/resolve/main/"
    "DasiwaMinimaxH3_dasiwaREF2VAHybridV1.safetensors"
)
DEFAULT_COMFY_VIDEO_VAE_URL = (
    "https://huggingface.co/Kijai/MiniMax-H3-experimental/resolve/main/"
    "minimax_h3_video_vae_int8_convrot.safetensors"
)
DEFAULT_COMFY_AUDIO_VAE_URL = (
    "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/"
    "minimax_h3_audio_vae_fp32.safetensors"
)
DEFAULT_COMFY_TEXT_ENCODER_URL = (
    "https://huggingface.co/Abiray/MiniMax-H3-GGUF/resolve/main/"
    "text_encoders/qwen3vl_32b_minimax_h3_int4_convrot.safetensors"
)
DEFAULT_COMFY_TAE_URL = (
    "https://huggingface.co/Kijai/MiniMax-H3-TAE/resolve/main/vae_approx/"
    "taeh3.safetensors"
)
DEFAULT_COMFY_UPSCALER_URL = (
    "https://huggingface.co/Kim2091/2x-AnimeSharpV4/resolve/main/"
    "2x-AnimeSharpV4_RCAN.safetensors"
)
DEFAULT_COMFY_FRAME_INTERP_URL = (
    "https://huggingface.co/Comfy-Org/frame_interpolation/resolve/main/"
    "frame_interpolation/rife_v4.26.safetensors"
)
DEFAULT_COMFY_CHECKPOINT_VARIANT = "dasiwa_hybrid"

COMFY_TIERS = ("auto", "light", "pruned", "pruned_scaled", "balanced", "max", "hybrid", "custom")

#: The GUI exposes three model-selection modes, two of which map onto pod
#: tier values the CLI can also send directly ("auto" / "custom"). The raw
#: GUI values never reach the pod; the mapping happens in
#: :func:`_build_comfy_config` (pod env stays CLI-compatible).
COMFY_GUI_TIER_TO_POD_TIER = {
    "auto_perso": "auto",
    "perso": "custom",
}
COMFY_WORKFLOW_TASKS = ("t2v", "i2v", "r2v")
COMFY_ACCESS_MODES = ("tunnel", "direct")
COMFY_SAGE_MODES = ("auto", "true", "false")

#: Extra API keys the launcher can manage. Each is resolved with the same
#: precedence as the RunPod pair (environment variable, then the secure
#: store), injected into the pod env on create/sync. Extend for future
#: model/workflow needs.
EXTRA_SECRET_KEYS = ("HF_TOKEN", "CIVITAI_API_KEY")


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class RunPodConfig:
    """Non-secret RunPod configuration."""

    pod_id: Optional[str] = None
    gpu_id: str = DEFAULT_RUNPOD_GPU_ID
    gpu_count: int = DEFAULT_RUNPOD_GPU_COUNT
    data_centers: tuple[str, ...] = ()
    pod_name: Optional[str] = None
    provision_timeout: int = DEFAULT_RUNPOD_PROVISION_TIMEOUT
    pod_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SshConfig:
    """SSH configuration (key path is non-secret; the key material is not)."""

    key_path: Optional[str] = None
    connect_timeout: int = DEFAULT_SSH_CONNECT_TIMEOUT


#: Asset kinds the launcher can upload from a **local** path. Mirrors the GUI
#: library's types (``launcher.gui.ANNUAIRE_TYPES``); declared here so
#: ``config`` never has to import the GUI module.
LOCAL_ASSET_KINDS = ("lora", "workflow", "node")


@dataclass(frozen=True)
class LocalAsset:
    """One library entry whose source is a **local** file or folder.

    The pod cannot read this machine, so the launcher uploads these over the
    SSH tunnel (``scp``) once ComfyUI is up — unlike URL entries, which travel
    as ``H3_CUSTOM_*`` pod env and are downloaded by the pod-side installer.

    ``kind`` is one of ``ANNUAIRE_TYPES`` (``lora`` / ``workflow`` / ``node``)
    and drives the destination folder; ``name`` is the entry's label, used for
    the journal only (the remote file name is the local basename).
    """

    kind: str
    path: str
    name: str = ""

    def as_tuple(self) -> tuple[str, str, str]:
        """Wire form, for the GUI→config overrides (hashable, JSON-safe)."""
        return (self.kind, self.path, self.name)


def _coerce_local_assets(raw) -> tuple[LocalAsset, ...]:
    """Normalize the ``local_assets`` override into a tuple of LocalAsset.

    Accepts :class:`LocalAsset` instances, ``(kind, path[, name])`` tuples, or
    ``{"kind", "path", "name"}`` mappings — the GUI builds tuples, tests may
    build any of the three. Entries with an unknown kind or an empty path are
    dropped rather than trusted.
    """
    if not raw:
        return ()
    out: list[LocalAsset] = []
    for item in raw:
        if isinstance(item, LocalAsset):
            asset = item
        elif isinstance(item, Mapping):
            asset = LocalAsset(
                kind=str(item.get("kind", "")).strip(),
                path=str(item.get("path", "")).strip(),
                name=str(item.get("name", "")).strip(),
            )
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            asset = LocalAsset(
                kind=str(item[0]).strip(),
                path=str(item[1]).strip(),
                name=str(item[2]).strip() if len(item) > 2 else "",
            )
        else:
            continue
        if asset.kind in LOCAL_ASSET_KINDS and asset.path:
            out.append(asset)
    return tuple(out)


@dataclass(frozen=True)
class ComfyConfig:
    """ComfyUI / MiniMax H3 stack configuration (non-secret).

    ``preset`` is the comma-separated ``H3_PRESETS`` value (the bash
    installer is the source of truth for what each preset installs);
    ``tier``/``workflows`` map to ``H3_TIER``/``H3_WORKFLOWS``; the booleans
    map to ``MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD`` / ``SAGE_ATTENTION`` /
    ``INSTALL_SPECTRUM``.

    ``turbo_lora`` and ``spectrum`` default to **False**: the Turbo LoRA and
    the Spectrum node are content the operator manages in the launcher's
    Library / preset (models and custom nodes), so the launcher no longer
    auto-installs them on a fresh pod. Setting ``COMFY_TURBO_LORA=true`` /
    ``COMFY_SPECTRUM=true`` (or the overrides) is an explicit opt-in.

    ``access`` selects how the ComfyUI web UI is reached:
    ``tunnel`` (default) forwards the pod's 8188 to the local machine over
    SSH — the UI runs in the local browser, all compute stays on the pod;
    ``direct`` opens the pod's public URL (no tunnel, option only — the
    ComfyUI endpoint has no authentication).

    ``ntfy_topic`` / ``personal_storage_repo`` are pass-through values for
    the pod-side notification and personal-vault features.
    """

    local_port: int = DEFAULT_COMFY_LOCAL_PORT
    remote_port: int = DEFAULT_COMFY_REMOTE_PORT
    preset: str = DEFAULT_COMFY_PRESET
    tier: str = DEFAULT_COMFY_TIER
    workflows: str = DEFAULT_COMFY_WORKFLOWS
    turbo_lora: bool = False
    sage_attention: str = COMFY_SAGE_MODES[0]
    spectrum: bool = False
    access: str = DEFAULT_COMFY_ACCESS
    #: Automatic collection of finished generations — a **launcher-side**
    #: feature: the launcher watches ComfyUI's history through the tunnel,
    #: downloads each finished output into ``outputs_dir`` and writes the
    #: ``.txt`` sidecar next to it. These values drive the launcher's watcher
    #: (``pod_env()`` does not forward them) — with one exception:
    #: ``notify_ntfy`` also sets ``NOTIFY_ON_GENERATION=false`` on the pod, so
    #: the operator gets a single ntfy message per generation instead of two.
    auto_collect: bool = False
    notify_windows: bool = False
    notify_ntfy: bool = False
    terminate_after_generation: bool = False
    #: How long the ComfyUI queue must stay EMPTY before
    #: ``terminate_after_generation`` actually stops the pod. Guards the batch
    #: case: ten generations queued, the pod must stop after the tenth, not
    #: after the first — and a prompt submitted in the gap between two
    #: generations must not be lost.
    terminate_settle_seconds: float = DEFAULT_COMFY_TERMINATE_SETTLE_SECONDS
    #: Where the collected outputs land. ``None``/"" = the user's Downloads
    #: folder (``~/Downloads``); any other value is used as-is.
    outputs_dir: Optional[str] = None
    ntfy_topic: Optional[str] = None
    #: Self-hosted ntfy base URL. Sent to the pod so both sides publish on
    #: the same server (the pod-side default is ``https://ntfy.sh``).
    ntfy_server: Optional[str] = None
    personal_storage_repo: Optional[str] = None
    comfyui_version: Optional[str] = None
    diffusion_url: str = DEFAULT_COMFY_DIFFUSION_URL
    video_vae_url: str = DEFAULT_COMFY_VIDEO_VAE_URL
    audio_vae_url: str = DEFAULT_COMFY_AUDIO_VAE_URL
    text_encoder_url: str = DEFAULT_COMFY_TEXT_ENCODER_URL
    tae_url: str = DEFAULT_COMFY_TAE_URL
    upscaler_url: str = DEFAULT_COMFY_UPSCALER_URL
    frame_interp_url: str = DEFAULT_COMFY_FRAME_INTERP_URL
    checkpoint_variant: str = DEFAULT_COMFY_CHECKPOINT_VARIANT
    custom_model_categories: Optional[str] = None
    # Launcher "annuaire" (LoRA / Workflow / Node) — declarative download
    # lists the launcher remembers and sends to the pod at startup. Each
    # entry is a single line: LoRAs and nodes are "url" (optionally "url
    # <filename/pip-flag>"), workflows are "url <filename.json>". The
    # pod-side installer processes them via H3_CUSTOM_LORAS / H3_CUSTOM_NODES
    # / H3_CUSTOM_WORKFLOWS (newline-separated). Empty by default = no-op.
    custom_loras: tuple[str, ...] = ()
    custom_nodes: tuple[str, ...] = ()
    custom_workflows: tuple[str, ...] = ()
    #: Library entries whose source is a **local** file or folder. They cannot
    #: travel through ``H3_CUSTOM_*`` (the pod cannot read this machine), so
    #: the launcher uploads them over the tunnel once ComfyUI is up — see
    #: :meth:`launcher.orchestrator._start_comfy`. Empty by default = no-op.
    local_assets: tuple["LocalAsset", ...] = ()
    #: User-defined ComfyUI presets (models + nodes + workflows), normalized
    #: to ``{name: {kind: [entry]}}`` (see :mod:`launcher.comfy_presets`).
    #: Serialized to ``H3_USER_PRESETS_JSON`` at pod create/re-sync; the
    #: pod-side installer also persists it (see ``lib/user_presets.sh`` for
    #: the location) so the presets survive a pod resync/recreation.
    user_presets: dict = field(default_factory=dict)

    def pod_env(self) -> dict[str, str]:
        """Pod env contributed by this configuration at create/re-sync.

        Every value is consumed by the comfy/ installer's ``config.env``
        (which applies its own defaults only when the variable is unset),
        so injecting here fully drives the pod-side install.
        """
        # The "hybrid" tier is a launcher-side alias for the single hybrid
        # checkpoint (does FL2VA + REF2VA). It maps to the dasiwa_hybrid
        # variant; H3_TIER itself stays "auto" (the dasiwa preset replaces the
        # standard tier download anyway).
        #
        # The "custom" tier (GUI mode "Perso", sent by the installer as
        # H3_TIER=custom) downloads NO standard stack at all — only presets
        # and the checked catalog models (H3_CUSTOM_MODEL_CATEGORIES) are
        # pulled. The GUI raw modes never reach this point: they are mapped
        # to pod values in _build_comfy_config().
        tier = self.tier
        if tier == "hybrid":
            # "hybrid" is a launcher-side alias for the single hybrid
            # checkpoint; it maps onto the auto tier. The pod-side
            # H3_DASIWA_CHECKPOINT_VARIANT key is no longer sent — the
            # hardcoded DaSiWa preset machinery it fed is retired (see
            # docs/comfy-image-analysis.md §13).
            tier = "auto"
        env = {
            # H3_PRESETS is deliberately NOT sent any more: the tier is always
            # "auto" and presets are exclusively launcher-created
            # (H3_USER_PRESETS_JSON). The installer's default is now empty as
            # well, so a pod that never receives the key installs no hardcoded
            # preset — see docs/comfy-image-analysis.md §0 and §13.
            "H3_TIER": tier,
            "H3_WORKFLOWS": self.workflows,
            "COMFYUI_PORT": str(self.remote_port),
        }
        if self.ntfy_topic:
            env["NTFY_TOPIC"] = self.ntfy_topic
        if self.ntfy_server:
            # One ``.env`` drives both sides: without this the pod keeps its
            # own default (https://ntfy.sh) while the launcher publishes on
            # the self-hosted server — same topic, two different servers.
            env["NTFY_SERVER"] = self.ntfy_server
        if self.notify_ntfy:
            # The launcher publishes its own "generation finished" notice on
            # that same topic: mute the pod-side one, otherwise the operator
            # gets two messages per generation. The pod keeps its "pod ready"
            # and inactivity notices.
            env["NOTIFY_ON_GENERATION"] = "false"
        if self.personal_storage_repo:
            env["PERSONAL_STORAGE_HF_REPO"] = self.personal_storage_repo
        if self.comfyui_version:
            # Pin ComfyUI to a specific git ref (tag or commit) via the
            # installer's COMFYUI_COMMIT. Empty/unset = follow the installer's
            # resolved target (latest release by default).
            env["COMFYUI_COMMIT"] = self.comfyui_version
        if self.diffusion_url:
            env["H3_DIFFUSION_URL"] = self.diffusion_url
        if self.video_vae_url:
            env["H3_VIDEO_VAE_URL"] = self.video_vae_url
        if self.audio_vae_url:
            env["H3_AUDIO_VAE_URL"] = self.audio_vae_url
        if self.text_encoder_url:
            env["H3_TEXT_ENCODER_URL"] = self.text_encoder_url
        if self.tae_url:
            env["H3_TAE_URL"] = self.tae_url
        if self.upscaler_url:
            env["H3_UPSCALER_URL"] = self.upscaler_url
        if self.frame_interp_url:
            env["H3_FRAME_INTERP_URL"] = self.frame_interp_url
        # None (the default) leaves the variable undefined on the pod: the
        # installer's config.env default ("") means "no custom models".
        # "" (an explicit empty) behaves the same way.
        if self.custom_model_categories is not None:
            env["H3_CUSTOM_MODEL_CATEGORIES"] = self.custom_model_categories
        if self.custom_loras:
            env["H3_CUSTOM_LORAS"] = "\n".join(self.custom_loras)
        if self.custom_nodes:
            env["H3_CUSTOM_NODES"] = "\n".join(self.custom_nodes)
        if self.custom_workflows:
            env["H3_CUSTOM_WORKFLOWS"] = "\n".join(self.custom_workflows)
        user_presets_json = build_user_presets_json(self.user_presets)
        if user_presets_json:
            env["H3_USER_PRESETS_JSON"] = user_presets_json
        return env


@dataclass(frozen=True)
class TrainConfig:
    """LoRA training stack configuration (non-secret).

    The pod runs **upstream's** Fizgig image (``ghcr.io/shootthesound/fizgig``) —
    a Tkinter desktop app on a virtual screen served by KasmVNC, plus filebrowser
    for dataset upload and LoRA download. Neither is published: the launcher
    opens an SSH tunnel to *both* and the user reaches them on ``127.0.0.1``.

    ``image_tag`` / ``fizgig_ref``
    ------------------------------
    Two version axes, both tracking upstream — and they are meant to move
    together, which is why neither is pinned by default.

    ``image_tag`` is the **runtime** — CUDA, PyTorch, KasmVNC, the system
    packages. Default ``latest``.

    ``fizgig_ref`` is the **application**. Upstream's entrypoint pulls this ref
    at every boot, so the app updates itself on a restart and needs no image
    rebuild — which is the whole reason the launcher does not ship a forked
    image any more. Default ``master``.

    ``latest`` + ``master`` is deliberate and consistent: both follow upstream,
    and they agree at pod creation. Pinning one and floating the other is the
    combination that rots — the floating side races ahead while the pin waits
    for someone to edit it. ``launcher.fizgig_version`` reports what the pod
    actually ended up running, so drift is visible instead of silent.

    Pin a concrete tag in either axis when a run must be reproducible, or when
    an upstream release turns out to be broken. It is a config/env change, never
    an image rebuild.

    Ports
    -----
    The remote ports are what the image serves and are effectively fixed by it
    (``-websocketPort 6080`` for KasmVNC, ``--port 8080`` for filebrowser), so
    they are documented here rather than treated as tunable. The *local* ports
    are the ones that can conflict on the Windows host, which is why they are
    configurable.

    ``fetch_models``
    ----------------
    Comma-separated families the pod downloads at boot (``krea2``, ``klein``,
    ``tools``). Empty — the default — downloads nothing: the in-app Preferences
    button fetches on demand, which spends no money on a family you did not
    want. MiniMax H3 weights are ~45 GB, so this is a real cost decision.

    ``lora_dir``
    ------------
    Where trained LoRAs are collected on the Windows host. The user chooses it;
    empty means "ask, and remember". Downloading locally is deliberate: the
    launcher never routes a trained LoRA through a third-party service.

    ``allow_in_app_autostop``
    -------------------------
    Fizgig can stop its own pod when a run finishes, and so can the launcher.
    Two independent stop mechanisms racing over one machine is a support burden
    with no upside, so the launcher owns it by default (it also knows the
    tunnel state) and the in-app one stays off.
    """

    local_port: int = DEFAULT_TRAIN_LOCAL_PORT
    remote_port: int = DEFAULT_TRAIN_REMOTE_PORT
    files_local_port: int = DEFAULT_TRAIN_FILES_LOCAL_PORT
    files_remote_port: int = DEFAULT_TRAIN_FILES_REMOTE_PORT
    fetch_models: str = DEFAULT_TRAIN_FETCH_MODELS
    image_tag: str = DEFAULT_TRAIN_IMAGE_TAG
    fizgig_ref: str = DEFAULT_TRAIN_FIZGIG_REF
    volume_gb: int = DEFAULT_TRAIN_VOLUME_GB
    lora_dir: str = DEFAULT_TRAIN_LORA_DIR
    allow_in_app_autostop: bool = DEFAULT_TRAIN_ALLOW_IN_APP_AUTOSTOP
    require_ssh: bool = DEFAULT_TRAIN_REQUIRE_SSH

    @property
    def image_name(self) -> str:
        """The image reference the RunPod template must carry."""
        return f"{TRAIN_IMAGE_REPOSITORY}:{self.image_tag}"

    def base_url(self) -> str:
        """Local URL of the Fizgig desktop (through the SSH tunnel)."""
        return f"http://127.0.0.1:{self.local_port}"

    def files_url(self) -> str:
        """Local URL of the file manager (through the SSH tunnel)."""
        return f"http://127.0.0.1:{self.files_local_port}"

    def pod_env(self) -> dict[str, str]:
        """Pod env contributed by this stack at pod create/re-sync.

        Secrets are NOT here: ``VNC_PASSWORD`` and ``HF_TOKEN`` are injected by
        the orchestrator from the credential store, the same way the comfy
        stack's tokens are, so they never travel through a non-secret mapping.

        ``FIZGIG_REF`` is what makes the application updateable without an image
        rebuild: upstream's entrypoint pulls this ref at every boot, so a
        restart *is* the update. It is sent explicitly rather than left to
        upstream's default, so the value the launcher shows is the value the pod
        actually uses.

        ``HF_HUB_DISABLE_TELEMETRY`` is belt-and-braces: it is sent even though
        a given runtime may not bake it, so the guarantee never depends on the
        base image.
        """
        env = {
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "FIZGIG_REF": self.fizgig_ref,
        }
        if self.fetch_models:
            env["FETCH_MODELS"] = self.fetch_models
        return env


@dataclass(frozen=True)
class RecoveryConfig:
    """Infrastructure recovery authorization mode.

    ``disabled`` (default) — recovery actions never execute.
    ``confirm`` — recovery requires an explicit ``confirm=true`` per call.
    """

    mode: str = DEFAULT_RECOVERY_MODE


@dataclass(frozen=True)
class Secrets:
    """Secret values.

    Loaded from environment variables first, then the secure DPAPI credential
    store (when one is supplied to :func:`load_config`); never logged or
    written to disk or logs.

    ``extra`` holds the resolved extra API keys (:data:`EXTRA_SECRET_KEYS`);
    keys the launcher does not manage (or that are set nowhere) are absent.
    """

    runpod_api_key: Optional[str] = None
    runpod_template_id: Optional[str] = None
    comfy_template_id: Optional[str] = None
    train_template_id: Optional[str] = None
    #: Desktop/file-manager credential for the training image. A secret like
    #: any other: it gates a browser-reachable desktop, so it belongs in the
    #: DPAPI store rather than a plain environment variable.
    vnc_password: Optional[str] = None
    extra: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - defensive, not a behavior
        return "Secrets(<redacted>)"


@dataclass(frozen=True)
class Config:
    """Top-level configuration.

    Holds non-secret configuration and secrets separately so callers can, for
    example, safely pass non-secret configuration around without risking
    accidental secret exposure.
    """

    runpod: RunPodConfig = field(default_factory=RunPodConfig)
    ssh: SshConfig = field(default_factory=SshConfig)
    comfy: ComfyConfig = field(default_factory=ComfyConfig)
    train: TrainConfig = field(default_factory=TrainConfig)
    recover: RecoveryConfig = field(default_factory=RecoveryConfig)
    secrets: Secrets = field(default_factory=Secrets)
    launcher_home: Optional[str] = None
    stack: str = DEFAULT_STACK

    def comfy_base_url(self) -> str:
        """Local base URL of the ComfyUI stack (through the SSH tunnel)."""
        return f"http://127.0.0.1:{self.comfy.local_port}"

    def resolved_comfy_pod_env(self) -> dict[str, str]:
        """Container env the comfy stack contributes at pod creation.

        The preset/tier/workflow/flags values come from :class:`ComfyConfig`
        (each overridable by its own environment variable). The caller merges
        the managed secrets and ``RUNPOD_POD_ENV`` on top so explicit user
        overrides always win.
        """
        return self.comfy.pod_env()

    def train_base_url(self) -> str:
        """Local URL of the training stack's desktop (through the SSH tunnel)."""
        return self.train.base_url()

    def train_files_url(self) -> str:
        """Local URL of the training stack's file manager (through the SSH tunnel)."""
        return self.train.files_url()


    def resolved_train_pod_env(self) -> dict[str, str]:
        """Container env the train stack contributes at pod creation.

        Comes from :class:`TrainConfig`. The caller merges the managed secrets
        (``VNC_PASSWORD``, ``HF_TOKEN``) and ``RUNPOD_POD_ENV`` on top so
        explicit user overrides always win.
        """
        return self.train.pod_env()


def _get_int(env: Mapping[str, str], key: str, default: int, section: str) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{section} setting {key!r} must be an integer, got {raw!r}"
        ) from exc


def _get_float(env: Mapping[str, str], key: str, default: float, section: str) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{section} setting {key!r} must be a number, got {raw!r}"
        ) from exc
    if not math.isfinite(value):
        # ``nan``/``inf`` parse fine and then defeat every range check written
        # against them (``nan <= 0`` is False), so a "validated" timeout could
        # still be NaN all the way into the client.
        raise ConfigError(
            f"{section} setting {key!r} must be a finite number, got {raw!r}"
        )
    return value


def _get_optional_str(env: Mapping[str, str], key: str) -> Optional[str]:
    raw = env.get(key)
    if raw is None or raw == "":
        return None
    return raw


def _expand_user(value: Optional[str]) -> Optional[str]:
    """Expand a leading ``~`` in a configured filesystem path.

    OpenSSH does not expand ``~`` in an ``-i`` argument and neither does
    ``open()``: ``SSH_KEY_PATH=~/.ssh/id_ed25519`` produced ssh's "identity
    file not accessible" warning and an authentication failure that pointed at
    ssh, not at the configuration. The rest of the repo already expands.
    """
    if value is None:
        return None
    expanded = os.path.expanduser(value)
    return expanded or value


def _stored_credentials(
    store: credentials.CredentialStore,
) -> Optional[credentials.RunPodCredentials]:
    """Load stored credentials, degrading to ``None`` on any failure.

    A missing store or an unsupported platform is a normal state and stays
    silent. Any other failure (corrupt, malformed, ...) logs a single safe
    warning and degrades to ``None`` so environment values still apply.
    """
    try:
        return store.get()
    except credentials.CredentialStoreMissing:
        return None
    except credentials.CredentialStoreUnsupported:
        return None
    except credentials.CredentialStoreError as exc:
        logger.warning("Stored RunPod credentials are unavailable: %s", exc)
        return None


def load_config(
    env: Optional[Mapping[str, str]] = None,
    *,
    store: Optional[credentials.CredentialStore] = None,
    stack: Optional[str] = None,
    comfy: Optional[Mapping[str, object]] = None,
    train: Optional[Mapping[str, object]] = None,
    gpu: Optional[str] = None,
) -> Config:
    """Build a :class:`Config` from environment variables and, optionally,
    the secure credential store.

    Parameters
    ----------
    env:
        Mapping to read variables from. Defaults to ``os.environ``.
    store:
        Optional credential store consulted only when an environment value is
        missing. Precedence per secret: environment variable, then stored
        value, then absent. ``RUNPOD_POD_ID`` is environment-only and is never
        read from the store.
    stack:
        Optional workload stack name (:data:`STACKS`). Precedence: this
        argument, then the ``LAUNCHER_STACK`` environment variable, then
        :data:`DEFAULT_STACK`.
    comfy:
        Optional mapping of explicit ComfyUI stack values (keys of
        :class:`ComfyConfig`) supplied by an interactive caller (the GUI).
        These win over the corresponding environment variables, which win
        over the built-in defaults.
    train:
        Optional mapping of explicit training stack values (keys of
        :class:`TrainConfig`) supplied by an interactive caller (the GUI).
        Same precedence as ``comfy``. Note that ``lora_dir`` is expected here:
        the local collection folder for trained LoRAs is a user choice, and
        the GUI passes it explicitly rather than relying on an environment
        variable being set.
    gpu:
        Optional RunPod GPU id overriding ``RUNPOD_GPU_ID`` for this run
        (any stack). The GUI/CLI use it to launch on a card the account can
        actually get — e.g. ``"NVIDIA A40"`` when the default A6000 has no
        capacity. Must match a RunPod catalog id.

    Returns
    -------
    Config
        A validated configuration object.
    """
    source: Mapping[str, str] = env if env is not None else os.environ

    ssh_timeout = _get_int(
        source, "SSH_CONNECT_TIMEOUT", DEFAULT_SSH_CONNECT_TIMEOUT, "SSH"
    )

    runpod_gpu_count = _get_int(
        source, "RUNPOD_GPU_COUNT", DEFAULT_RUNPOD_GPU_COUNT, "RunPod"
    )
    runpod_provision_timeout = _get_int(
        source,
        "RUNPOD_PROVISION_TIMEOUT",
        DEFAULT_RUNPOD_PROVISION_TIMEOUT,
        "RunPod",
    )
    runpod_data_centers = _parse_data_centers(
        _get_optional_str(source, "RUNPOD_DATA_CENTERS")
    )
    runpod_pod_env = _parse_pod_env(_get_optional_str(source, "RUNPOD_POD_ENV"))

    stack_name = (
        stack or _get_optional_str(source, "LAUNCHER_STACK") or DEFAULT_STACK
    ).strip().lower()
    if stack_name not in STACKS:
        raise ConfigError(
            f"Unknown stack {stack_name!r}; choose one of {list(STACKS)}"
        )

    comfy_config = _build_comfy_config(source, comfy)
    train_config = _build_train_config(source, train)

    config = Config(
        runpod=RunPodConfig(
            pod_id=_get_optional_str(source, "RUNPOD_POD_ID"),
            gpu_id=(gpu or "").strip()
            or _get_optional_str(source, "RUNPOD_GPU_ID")
            or DEFAULT_RUNPOD_GPU_ID,
            gpu_count=runpod_gpu_count,
            data_centers=runpod_data_centers,
            pod_name=_get_optional_str(source, "RUNPOD_POD_NAME"),
            provision_timeout=runpod_provision_timeout,
            pod_env=runpod_pod_env,
        ),
        ssh=SshConfig(
            key_path=_expand_user(_get_optional_str(source, "SSH_KEY_PATH")),
            connect_timeout=ssh_timeout,
        ),
        recover=RecoveryConfig(
            mode=_get_optional_str(source, "LAUNCHER_INFRA_RECOVERY") or DEFAULT_RECOVERY_MODE,
        ),
        secrets=_resolve_secrets(source, store, stack_name),
        launcher_home=_expand_user(_get_optional_str(source, "MINIMAX_LAUNCHER_HOME")),
        stack=stack_name,
    )
    validate(config)
    return config


def _comfy_str_value(
    source: Mapping[str, str],
    override: Optional[Mapping[str, object]],
    env_key: str,
    field_name: str,
    default: str,
    allow_empty_override: bool = False,
) -> str:
    """Resolve one ComfyUI stack value: explicit override > env > default.

    ``allow_empty_override`` lets an explicit empty value survive as ``""``
    (used for the preset, where ``""`` disables presets and installs the
    standard tier only). An empty *env* value still falls back to the
    default.
    """
    if override is not None and field_name in override:
        value = override.get(field_name)
        stripped = str(value).strip() if value is not None else ""
        if stripped or allow_empty_override:
            return stripped
    value = _get_optional_str(source, env_key)
    return value if value is not None else default


def _comfy_bool_value(
    source: Mapping[str, str],
    override: Optional[Mapping[str, object]],
    env_key: str,
    field_name: str,
    default: bool,
) -> bool:
    """Resolve one ComfyUI boolean: explicit override > env > default."""
    if override is not None:
        value = override.get(field_name)
        if value is not None:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
    raw = _get_optional_str(source, env_key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _comfy_list_value(
    source: Mapping[str, str],
    override: Optional[Mapping[str, object]],
    env_key: str,
    field_name: str,
) -> tuple[str, ...]:
    """Resolve one ComfyUI list value (annuaire): override > env > empty.

    The override may be a list/tuple of strings (the GUI path) or a single
    newline-separated string; the env value is newline-separated. Blank
    lines and entries are dropped. Returns a tuple of trimmed, non-empty
    entries — empty when nothing is configured.
    """
    if override is not None and field_name in override:
        value = override.get(field_name)
        if isinstance(value, (list, tuple)):
            items = [str(v) for v in value]
        elif isinstance(value, str):
            items = value.splitlines()
        else:
            items = []
        return tuple(item.strip() for item in items if item.strip())
    raw = _get_optional_str(source, env_key)
    if not raw:
        return ()
    return tuple(line.strip() for line in raw.splitlines() if line.strip())


def _build_comfy_config(
    source: Mapping[str, str],
    override: Optional[Mapping[str, object]],
) -> ComfyConfig:
    """Build the :class:`ComfyConfig` from env vars and optional overrides."""
    local_port = _get_int(source, "COMFY_LOCAL_PORT", DEFAULT_COMFY_LOCAL_PORT, "Comfy")
    remote_port = _get_int(source, "COMFY_REMOTE_PORT", DEFAULT_COMFY_REMOTE_PORT, "Comfy")
    ntfy_topic = None
    if override is not None and override.get("ntfy_topic"):
        ntfy_topic = str(override["ntfy_topic"])
    else:
        ntfy_topic = _get_optional_str(source, "NTFY_TOPIC")
    personal_storage_repo = None
    if override is not None and override.get("personal_storage_repo"):
        personal_storage_repo = normalize_hf_repo_id(str(override["personal_storage_repo"]))
    else:
        personal_storage_repo = normalize_hf_repo_id(
            _get_optional_str(source, "PERSONAL_STORAGE_HF_REPO") or ""
        )
    comfyui_version = None
    if override is not None and override.get("comfyui_version"):
        comfyui_version = str(override["comfyui_version"])
    else:
        comfyui_version = _get_optional_str(source, "COMFYUI_COMMIT")
    raw_tier = _comfy_str_value(
        source, override, "COMFY_TIER", "tier", DEFAULT_COMFY_TIER
    )
    tier = COMFY_GUI_TIER_TO_POD_TIER.get(raw_tier, raw_tier)
    custom_model_categories = None
    if override is not None and "custom_model_categories" in override:
        if override.get("custom_model_categories"):
            custom_model_categories = str(override["custom_model_categories"]).strip()
    else:
        custom_model_categories = _get_optional_str(source, "H3_CUSTOM_MODEL_CATEGORIES")
    user_presets: dict = {}
    if override is not None and "user_presets" in override:
        user_presets = normalize_user_presets(override.get("user_presets"))
    return ComfyConfig(
        local_port=local_port,
        remote_port=remote_port,
        preset=_comfy_str_value(
            source, override, "COMFY_PRESET", "preset", DEFAULT_COMFY_PRESET,
            allow_empty_override=True,
        ),
        tier=tier,
        workflows=_comfy_str_value(
            source, override, "COMFY_WORKFLOWS", "workflows", DEFAULT_COMFY_WORKFLOWS
        ),
        turbo_lora=_comfy_bool_value(
            source, override, "COMFY_TURBO_LORA", "turbo_lora", False
        ),
        sage_attention=_comfy_str_value(
            source, override, "COMFY_SAGE_ATTENTION", "sage_attention", "auto"
        ),
        spectrum=_comfy_bool_value(
            source, override, "COMFY_SPECTRUM", "spectrum", False
        ),
        access=_comfy_str_value(
            source, override, "COMFY_ACCESS", "access", DEFAULT_COMFY_ACCESS
        ),
        auto_collect=_comfy_bool_value(
            source, override, "COMFY_AUTO_COLLECT", "auto_collect", False
        ),
        notify_windows=_comfy_bool_value(
            source, override, "COMFY_NOTIFY_WINDOWS", "notify_windows", False
        ),
        notify_ntfy=_comfy_bool_value(
            source, override, "COMFY_NOTIFY_NTFY", "notify_ntfy", False
        ),
        terminate_after_generation=_comfy_bool_value(
            source, override, "COMFY_TERMINATE_AFTER_GENERATION",
            "terminate_after_generation", False,
        ),
        terminate_settle_seconds=_get_float(
            source,
            "COMFY_TERMINATE_SETTLE_SECONDS",
            DEFAULT_COMFY_TERMINATE_SETTLE_SECONDS,
            "ComfyUI",
        ),
        outputs_dir=_expand_user(
            _comfy_str_value(
                source, override, "COMFY_OUTPUTS_DIR", "outputs_dir", "",
                allow_empty_override=True,
            )
        )
        or None,
        ntfy_topic=ntfy_topic,
        ntfy_server=_comfy_str_value(
            source, override, "NTFY_SERVER", "ntfy_server", "",
            allow_empty_override=True,
        )
        or None,
        personal_storage_repo=personal_storage_repo,
        comfyui_version=comfyui_version,
        # allow_empty_override on every slot: the GUI sends "" for a
        # category the user unchecked, and that emptiness must survive the
        # resolution (pod_env() then omits the variable entirely — the
        # pod-side default is inert while no preset/custom tier consumes it).
        diffusion_url=_comfy_str_value(
            source, override, "H3_DIFFUSION_URL", "diffusion_url",
            DEFAULT_COMFY_DIFFUSION_URL, allow_empty_override=True,
        ),
        video_vae_url=_comfy_str_value(
            source, override, "H3_VIDEO_VAE_URL", "video_vae_url",
            DEFAULT_COMFY_VIDEO_VAE_URL, allow_empty_override=True,
        ),
        audio_vae_url=_comfy_str_value(
            source, override, "H3_AUDIO_VAE_URL", "audio_vae_url",
            DEFAULT_COMFY_AUDIO_VAE_URL, allow_empty_override=True,
        ),
        text_encoder_url=_comfy_str_value(
            source, override, "H3_TEXT_ENCODER_URL", "text_encoder_url",
            DEFAULT_COMFY_TEXT_ENCODER_URL, allow_empty_override=True,
        ),
        tae_url=_comfy_str_value(
            source, override, "H3_TAE_URL", "tae_url",
            DEFAULT_COMFY_TAE_URL, allow_empty_override=True,
        ),
        upscaler_url=_comfy_str_value(
            source, override, "H3_UPSCALER_URL", "upscaler_url",
            DEFAULT_COMFY_UPSCALER_URL, allow_empty_override=True,
        ),
        frame_interp_url=_comfy_str_value(
            source, override, "H3_FRAME_INTERP_URL", "frame_interp_url",
            DEFAULT_COMFY_FRAME_INTERP_URL, allow_empty_override=True,
        ),
        checkpoint_variant=_comfy_str_value(
            source, override, "H3_DASIWA_CHECKPOINT_VARIANT", "checkpoint_variant",
            DEFAULT_COMFY_CHECKPOINT_VARIANT,
        ),
        custom_model_categories=custom_model_categories,
        custom_loras=_comfy_list_value(
            source, override, "H3_CUSTOM_LORAS", "custom_loras"
        ),
        custom_nodes=_comfy_list_value(
            source, override, "H3_CUSTOM_NODES", "custom_nodes"
        ),
        custom_workflows=_comfy_list_value(
            source, override, "H3_CUSTOM_WORKFLOWS", "custom_workflows"
        ),
        # Local-path entries never travel as pod env: they are uploaded by the
        # launcher over the tunnel. They come from the GUI's library only.
        local_assets=_coerce_local_assets(
            override.get("local_assets") if override else None
        ),
        user_presets=user_presets,
    )


def _train_int_value(
    source: Mapping[str, str],
    override: Optional[Mapping[str, object]],
    env_key: str,
    field_name: str,
    default: int,
) -> int:
    """Resolve one train integer: explicit override > env > default."""
    if override is not None and override.get(field_name) is not None:
        raw = str(override[field_name]).strip()
        if raw:
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(
                    f"train setting {field_name!r} must be an integer, got {raw!r}"
                ) from exc
    return _get_int(source, env_key, default, "train")


def _build_train_config(
    source: Mapping[str, str],
    override: Optional[Mapping[str, object]],
) -> TrainConfig:
    """Build the :class:`TrainConfig` from env vars and optional overrides.

    ``_comfy_str_value`` / ``_comfy_bool_value`` are reused rather than
    duplicated: they implement exactly the precedence this stack needs
    (explicit override > environment > default), and the name is about the
    helper's origin, not its applicability.
    """
    return TrainConfig(
        local_port=_train_int_value(
            source, override, "TRAIN_LOCAL_PORT", "local_port",
            DEFAULT_TRAIN_LOCAL_PORT,
        ),
        remote_port=_train_int_value(
            source, override, "TRAIN_REMOTE_PORT", "remote_port",
            DEFAULT_TRAIN_REMOTE_PORT,
        ),
        files_local_port=_train_int_value(
            source, override, "TRAIN_FILES_LOCAL_PORT", "files_local_port",
            DEFAULT_TRAIN_FILES_LOCAL_PORT,
        ),
        files_remote_port=_train_int_value(
            source, override, "TRAIN_FILES_REMOTE_PORT", "files_remote_port",
            DEFAULT_TRAIN_FILES_REMOTE_PORT,
        ),
        fetch_models=_comfy_str_value(
            source, override, "TRAIN_FETCH_MODELS", "fetch_models",
            DEFAULT_TRAIN_FETCH_MODELS,
        ),
        image_tag=_comfy_str_value(
            source, override, "TRAIN_IMAGE_TAG", "image_tag",
            DEFAULT_TRAIN_IMAGE_TAG,
        ),
        fizgig_ref=_comfy_str_value(
            source, override, "TRAIN_FIZGIG_REF", "fizgig_ref",
            DEFAULT_TRAIN_FIZGIG_REF,
        ),
        volume_gb=_train_int_value(
            source, override, "TRAIN_VOLUME_GB", "volume_gb",
            DEFAULT_TRAIN_VOLUME_GB,
        ),
        lora_dir=_expand_user(
            _comfy_str_value(
                source, override, "TRAIN_LORA_DIR", "lora_dir",
                DEFAULT_TRAIN_LORA_DIR,
            )
        )
        or DEFAULT_TRAIN_LORA_DIR,
        allow_in_app_autostop=_comfy_bool_value(
            source, override, "TRAIN_ALLOW_IN_APP_AUTOSTOP",
            "allow_in_app_autostop", DEFAULT_TRAIN_ALLOW_IN_APP_AUTOSTOP,
        ),
        require_ssh=_comfy_bool_value(
            source, override, "TRAIN_REQUIRE_SSH", "require_ssh",
            DEFAULT_TRAIN_REQUIRE_SSH,
        ),
    )


def _parse_data_centers(raw: Optional[str]) -> tuple[str, ...]:
    """Split a comma-separated data-center list, trimming and dropping empties."""
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_pod_env(raw: Optional[str]) -> dict[str, str]:
    """Parse ``RUNPOD_POD_ENV`` as a JSON object mapping strings to strings.

    Returns an empty dict when unset. Raises :class:`ConfigError` when the
    value is not a JSON object of string->string entries.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(
            f"RUNPOD_POD_ENV must be a JSON object, got {raw!r}"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"RUNPOD_POD_ENV must be a JSON object, got {raw!r}"
        )
    result: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError(
                "RUNPOD_POD_ENV entries must map strings to strings, "
                f"got {raw!r}"
            )
        result[key] = value
    return result


def _resolve_secrets(
    source: Mapping[str, str],
    store: Optional[credentials.CredentialStore],
    stack: str = DEFAULT_STACK,
) -> Secrets:
    """Resolve all secrets with env > stored > missing precedence.

    The RunPod pair and every extra API key (:data:`EXTRA_SECRET_KEYS`) are
    resolved independently: the environment value wins, then the stored
    value, then absent. The store is consulted once whenever a secret the
    selected stack needs is missing from the environment; when every such
    secret is present in the environment the store is not touched at all.

    The comfy/train template ids are only stack-relevant: each triggers a store
    lookup only when *stack* selects it and the value is absent from the
    environment, so a fully env-configured run never touches the store.
    """
    api_key = _get_optional_str(source, "RUNPOD_API_KEY")
    template_id = _get_optional_str(source, "RUNPOD_TEMPLATE_ID")
    comfy_template_id = _get_optional_str(source, "RUNPOD_COMFY_TEMPLATE_ID")
    train_template_id = _get_optional_str(source, "RUNPOD_TRAIN_TEMPLATE_ID")
    vnc_password = _get_optional_str(source, "VNC_PASSWORD")
    extra_env = {key: _get_optional_str(source, key) for key in EXTRA_SECRET_KEYS}
    any_missing = (
        api_key is None
        or template_id is None
        or (stack == "comfy" and comfy_template_id is None)
        or (stack == "train" and train_template_id is None)
        or (stack == "train" and vnc_password is None)
        or any(value is None for value in extra_env.values())
    )
    stored_extra: dict[str, str] = {}
    if store is not None and any_missing:
        stored = _stored_credentials(store)
        if stored is not None:
            if api_key is None:
                api_key = stored.runpod_api_key
            if template_id is None:
                template_id = stored.runpod_template_id
            if comfy_template_id is None:
                comfy_template_id = stored.comfy_template_id
            if train_template_id is None:
                train_template_id = stored.train_template_id
            if vnc_password is None:
                vnc_password = stored.vnc_password
            stored_extra = stored.extra
    extra: dict[str, str] = {}
    for key in EXTRA_SECRET_KEYS:
        value = extra_env.get(key)
        if value is None:
            value = stored_extra.get(key)
        if value is not None:
            extra[key] = value
    return Secrets(
        runpod_api_key=api_key,
        runpod_template_id=template_id,
        # The comfy stack ships a PUBLIC RunPod template: a first run needs
        # nothing but the API key (see DEFAULT_COMFY_TEMPLATE_ID). An explicit
        # environment value or a stored private template still wins.
        comfy_template_id=comfy_template_id or DEFAULT_COMFY_TEMPLATE_ID,
        train_template_id=train_template_id,
        vnc_password=vnc_password,
        extra=extra,
    )


def _valid_port(port: int) -> bool:
    return 1 <= port <= 65535


def validate(config: Config) -> None:
    """Validate the structure of *config*, raising :class:`ConfigError`.

    This validates shape and ranges only — it does not require any value to be
    present, because most values are optional at the foundation stage.
    """
    if config.stack not in STACKS:
        raise ConfigError(
            f"Unknown stack {config.stack!r}; choose one of {list(STACKS)}"
        )
    if config.ssh.connect_timeout <= 0:
        raise ConfigError(
            f"SSH connect timeout must be positive, got {config.ssh.connect_timeout}"
        )
    if config.recover.mode not in ("disabled", "confirm"):
        raise ConfigError(
            f"Recovery mode must be 'disabled' or 'confirm', got {config.recover.mode!r}"
        )
    if not _valid_port(config.comfy.local_port):
        raise ConfigError(
            f"ComfyUI local port out of range: {config.comfy.local_port}"
        )
    if not _valid_port(config.comfy.remote_port):
        raise ConfigError(
            f"ComfyUI remote port out of range: {config.comfy.remote_port}"
        )
    # An empty preset is valid: it disables presets (H3_PRESETS="") and
    # installs the standard tier only.
    if config.comfy.tier not in COMFY_TIERS:
        raise ConfigError(
            f"ComfyUI tier must be one of {list(COMFY_TIERS)}, "
            f"got {config.comfy.tier!r}"
        )
    if config.comfy.access not in COMFY_ACCESS_MODES:
        raise ConfigError(
            f"ComfyUI access must be one of {list(COMFY_ACCESS_MODES)}, "
            f"got {config.comfy.access!r}"
        )
    if config.comfy.sage_attention not in COMFY_SAGE_MODES:
        raise ConfigError(
            f"ComfyUI sage_attention must be one of {list(COMFY_SAGE_MODES)}, "
            f"got {config.comfy.sage_attention!r}"
        )
    if config.comfy.workflows != "all":
        _tasks = [
            task.strip().lower()
            for task in config.comfy.workflows.split(",")
            if task.strip()
        ]
        if not _tasks or any(task not in COMFY_WORKFLOW_TASKS for task in _tasks):
            raise ConfigError(
                "ComfyUI workflows must be 'all' or a comma-separated subset "
                f"of {list(COMFY_WORKFLOW_TASKS)}, got {config.comfy.workflows!r}"
            )
    for _label, _port in (
        ("train local port", config.train.local_port),
        ("train remote port", config.train.remote_port),
        ("train files local port", config.train.files_local_port),
        ("train files remote port", config.train.files_remote_port),
    ):
        if not _valid_port(_port):
            raise ConfigError(f"{_label} out of range: {_port}")
    # The desktop and the file manager must not share a local port: the tunnel
    # would bind the first and fail on the second, and the error would point at
    # ssh rather than at the configuration that caused it.
    if config.train.local_port == config.train.files_local_port:
        raise ConfigError(
            "train desktop and file-manager local ports must differ, both are "
            f"{config.train.local_port}"
        )
    # Cross-stack collision: two `ssh -L` on one local port make ssh abort the
    # WHOLE forward (ExitOnForwardFailure), and the error is attributed to ssh
    # rather than to the configuration that caused it. Only the train pair used
    # to be checked, which is the one pair that is easy to spot.
    _local_ports: list[tuple[str, int]] = [
        ("COMFY_LOCAL_PORT (comfy tunnel)", config.comfy.local_port),
        ("TRAIN_LOCAL_PORT (desktop tunnel)", config.train.local_port),
        ("TRAIN_FILES_LOCAL_PORT (file manager tunnel)", config.train.files_local_port),
    ]
    _seen_ports: dict[int, str] = {}
    for _label, _port in _local_ports:
        if _port in _seen_ports:
            raise ConfigError(
                f"local port {_port} is used by both {_seen_ports[_port]} and "
                f"{_label}; each tunnel needs its own local port"
            )
        _seen_ports[_port] = _label
    if config.train.volume_gb <= 0:
        raise ConfigError(
            f"train volume size must be positive, got {config.train.volume_gb}"
        )
    if not config.train.image_tag:
        raise ConfigError("train image tag must not be empty")
    # NOTE: `latest` is deliberately NOT rejected here, unlike the comfy
    # stack. That one publishes our own image, where the tag is the only
    # version axis and pinning is what keeps a template honest. This stack runs
    # upstream's image and floats the application on FIZGIG_REF, so pinning only
    # the runtime would rot while the app raced ahead of it. See
    # DEFAULT_TRAIN_IMAGE_TAG for the reasoning and the rollback.
    if not config.runpod.gpu_id:
        raise ConfigError("RunPod GPU id must not be empty")
    if config.runpod.gpu_count < 1:
        raise ConfigError(
            f"RunPod GPU count must be at least 1, got {config.runpod.gpu_count}"
        )
    if not (60 <= config.runpod.provision_timeout <= 7200):
        raise ConfigError(
            "RunPod provision timeout must be between 60 and 7200 seconds, "
            f"got {config.runpod.provision_timeout}"
        )
    if any(not dc for dc in config.runpod.data_centers):
        raise ConfigError("RunPod data center entries must not be empty")
    if config.runpod.pod_name is not None and not config.runpod.pod_name:
        raise ConfigError("RunPod pod name must not be empty when set")
    if config.launcher_home is not None and not os.path.isabs(config.launcher_home):
        raise ConfigError(
            f"MINIMAX_LAUNCHER_HOME must be an absolute path, got {config.launcher_home!r}"
        )

"""MiniMax H3 Launcher — Windows GUI launcher (tkinter, stdlib).

Graphical front-end for the MiniMax H3 Launcher launcher. It reuses the exact same
primitives as the CLI and the MCP tool layer:

* dashboard state comes from :func:`launcher.infra_diagnose.diagnose`
  (single source of truth, bounded probes, degrades to ``UNKNOWN``);
* ``Start`` / ``Stop`` delegate to :func:`launcher.orchestrator.start` /
  :func:`launcher.orchestrator.stop`;
* ``Doctor`` delegates to :func:`launcher.doctor.run_diagnostics`;
* ``Repair`` delegates to :class:`launcher.infra_recover.RecoveryEngine`
  (only enabled when ``LAUNCHER_INFRA_RECOVERY=confirm``);
* credentials are read/written through the DPAPI
  :class:`launcher.credentials.CredentialStore`.

Design rules:

* stdlib only at runtime (``pystray`` is optional and degrades gracefully);
* all long-running work (pod provisioning, API probes) runs on worker
  threads; the UI thread only consumes a :class:`queue.Queue`;
* no secret ever reaches the UI: every log line is passed through
  :func:`launcher.logging.redact` before display;
* the non-UI logic (env-file loading, settings, status snapshot, actions)
  is plain functions/classes so it is testable headlessly.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import importlib.util
import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from . import alerts
from . import comfy_ops
from . import comfy_watch
from . import health
from . import orchestrator
from . import portctl
from . import runtime_state
from . import winproc
from .comfy_presets import model_target_for_url, normalize_user_presets
from .config import (
    COMFY_ACCESS_MODES,
    COMFY_SAGE_MODES,
    DEFAULT_COMFY_ACCESS,
    DEFAULT_COMFY_AUDIO_VAE_URL,
    DEFAULT_COMFY_DIFFUSION_URL,
    DEFAULT_COMFY_FRAME_INTERP_URL,
    DEFAULT_COMFY_TAE_URL,
    DEFAULT_COMFY_TEXT_ENCODER_URL,
    DEFAULT_COMFY_TIER,
    DEFAULT_COMFY_UPSCALER_URL,
    DEFAULT_COMFY_VIDEO_VAE_URL,
    DEFAULT_COMFY_WORKFLOWS,
    DEFAULT_RUNPOD_GPU_ID,
    DEFAULT_TRAIN_FETCH_MODELS,
    DEFAULT_TRAIN_FILES_LOCAL_PORT,
    DEFAULT_TRAIN_LOCAL_PORT,
    STACKS,
    Config,
    ConfigError,
    load_config,
)
from .credentials import CredentialStore, CredentialStoreError
from .doctor import run_diagnostics
from .infra_diagnose import InfraReport, diagnose
from .i18n import (
    LANGUAGE_ENV,
    LANGUAGE_LABELS,
    SUPPORTED_LANGUAGES,
    get_language,
    normalize_language,
    set_language,
    t,
)
from .infra_recover import (
    RECOVERY_ACTIONS,
    RecoveryAuthorizer,
    RecoveryEngine,
    RecoveryResult,
)
from .logging import get_logger, redact, setup_logging
from .portctl import is_port_open
from .pod_registry import PodRecord, PodRegistry, default_home
from .runpod import RunPodClient

logger = get_logger("minimax-launcher.gui")

try:
    import tkinter as tk
    import tkinter as _TKMOD
    import tkinter.font as tkfont
    from tkinter import ttk

    _HAS_TK = True
    _TK_IMPORT_ERROR: Optional[str] = None
except ImportError as exc:  # pragma: no cover - platform dependent
    tk = None  # type: ignore[assignment]
    _TKMOD = None  # type: ignore[assignment]
    tkfont = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    _HAS_TK = False
    _TK_IMPORT_ERROR = str(exc)

# ttkbootstrap is an optional styling layer: when installed it provides the
# ready-made theme (buttons, inputs, frames, notebook…) and the dark/light
# toggle switches between its two theme modes. Without it the launcher falls
# back to the hand-rolled PALETTES + clam theme (same behavior as before).
try:
    import ttkbootstrap as _ttkbootstrap

    _HAS_TTKBOOTSTRAP = True
    _TBS_IMPORT_ERROR: Optional[str] = None
except ImportError as exc:
    _ttkbootstrap = None  # type: ignore[assignment]
    _HAS_TTKBOOTSTRAP = False
    _TBS_IMPORT_ERROR = str(exc)

_HAS_PYSTRAY = importlib.util.find_spec("pystray") is not None
_HAS_PIL = importlib.util.find_spec("PIL") is not None

ENV_FILE_VARIABLE = "MINIMAX_LAUNCHER_ENV_FILE"
SETTINGS_FILENAME = "gui.json"
REFRESH_INTERVAL_MS = 5000
POLL_INTERVAL_MS = 200
MAX_LOG_LINES = 2000

# Notebook tab labels — the single source of truth. Both the notebook itself
# and the "Navigation" menu read them, so renaming a tab can never leave the
# menu pointing at a label that no longer exists (``_select_tab_by_label``
# silently does nothing on a mismatch). ``TAB_LABELS`` is the display order;
# ``NAV_TAB_LABELS`` is the (smaller) set worth a keyboard shortcut.
TAB_DASHBOARD = "Dashboard"
TAB_COMFY = "ComfyUI"
TAB_TRAIN = "LoRA training"
TAB_MODELS = "Models & presets"
TAB_LIBRARY = "Library"
TAB_SETTINGS = "Settings"

TAB_LABELS = (
    TAB_DASHBOARD,
    TAB_COMFY,
    TAB_TRAIN,
    TAB_MODELS,
    TAB_LIBRARY,
    TAB_SETTINGS,
)

NAV_TAB_LABELS = (TAB_DASHBOARD, TAB_COMFY, TAB_SETTINGS)

# One spacing scale for the whole window: sections pick a step from here
# instead of inventing their own padx/pady, so the vertical rhythm stays
# consistent from tab to tab.
PAD_XS = 2
PAD_S = 4
PAD_M = 8
PAD_L = 12

# The gap between a check/radio indicator and its label, applied once in the
# shared styles (``indicatormargin``) rather than per widget. ttk's
# ``indicatorspacing`` is ignored by the ttkbootstrap themes; the indicator
# margin is honored on every theme checked (light and dark).
INDICATOR_GAP = PAD_M

#: Left inset of the « Access » help line, so it starts under the field it
#: documents (« Workflows: » + its combobox) rather than under its label.
ACCESS_HINT_INDENT = 58

#: Icon-only action buttons: their tooltip is the only thing that says what
#: they do, so every one of them must carry one (see ``_tooltip``). This is
#: the canonical list the tooltip-coverage test walks the widget tree with.
ACTION_GLYPHS = ("✕", "＋", "🔗", "⬇", "✏️")

#: Bundled PNG assets (``launcher/assets/icons``), pre-shrunk to ~2x their
#: on-screen size by ``scripts/shrink_icons.py``.
ICON_ASSETS = (
    "logo_minimax_h3.png",
    "status_running_green.png",
    "status_retrying_amber.png",
    "status_failed_red.png",
    "status_stopped_gray.png",
    "category_diffusion_checkpoint_violet.png",
    "category_text_encoder_blue.png",
    "category_video_vae_cyan.png",
    "category_audio_vae_amber.png",
    "category_upscaler_green.png",
    "category_frame_interpolation_pink.png",
    "category_workflows_indigo.png",
    "category_custom_nodes_orange.png",
    "badge_locked_by_preset_amber.png",
    "header_cost_meter_violet.png",
)

#: Health-table tag -> status icon (the tag is the color key from
#: ``_component_color_key``).
HEALTH_STATUS_ICONS = {
    "ok": "status_running_green.png",
    "warn": "status_retrying_amber.png",
    "error": "status_failed_red.png",
    "idle": "status_stopped_gray.png",
}

#: Model category -> icon, so the preset cards read at a glance.
CATEGORY_ICONS = {
    "diffusion": "category_diffusion_checkpoint_violet.png",
    "text_encoder": "category_text_encoder_blue.png",
    "video_vae": "category_video_vae_cyan.png",
    "audio_vae": "category_audio_vae_amber.png",
    "upscaler": "category_upscaler_green.png",
    "frame_interp": "category_frame_interpolation_pink.png",
    # TAE shares the video VAE icon (both are video-side VAEs).
    "tae": "category_video_vae_cyan.png",
}

LOCK_BADGE_ICON = "badge_locked_by_preset_amber.png"
LOCK_BADGE_TOOLTIP = "Locked by the active preset"
COST_METER_ICON = "header_cost_meter_violet.png"

#: Size of the health table's status-icon column (#0): the icon plus the gap
#: before the "Component" text (a Treeview draws no padding of its own).
STATUS_ICON_COLUMN_WIDTH = 38

# Journal level -> text tag (tags are configured against the palette in
# ``_build_widgets``).
_LOG_TAG_MAP = {
    "info": "lvl_info",
    "ok": "lvl_ok",
    "warn": "lvl_warn",
    "error": "lvl_error",
}

# ComfyUI version pin for the quick-settings gear. The release list is
# fetched from the GitHub releases page at launcher startup; the local
# fallback below is only used when that fetch fails (offline / rate limit).
# The field is editable, so any git tag/commit is accepted; an empty value
# means "follow the installer's resolved target" (latest release).
# v0.34.5 re-ordered the native MiniMax H3 node signatures, which broke the
# DaSiWa Director Guide FL2VA/I2VA branch; v0.34.4 is the last pre-change tag.
COMFYUI_RELEASES_API_URL = "https://api.github.com/repos/Comfy-Org/ComfyUI/releases"
COMFYUI_RELEASES_MAX = 20
COMFYUI_RELEASES_TIMEOUT = 10.0

#: Combo entry meaning "no pin at all". Selecting it leaves COMFYUI_COMMIT
#: unset, so the installer resolves the latest stable ComfyUI release at every
#: pod boot (COMFYUI_RELEASE_MODE=release, comfy/config.env). Any concrete tag
#: selected instead is a deliberate PIN — rollback, or reproducibility of a
#: workflow that breaks on a newer release.
COMFYUI_VERSION_AUTO_LABEL = "Latest version (auto)"


def version_display(value: str) -> str:
    """Stored pin -> what the combo shows (``""`` means auto/latest)."""
    return value or COMFYUI_VERSION_AUTO_LABEL


def version_from_display(display: str) -> str:
    """Combo display -> stored pin (the auto entry stores ``""``)."""
    display = (display or "").strip()
    return "" if display == COMFYUI_VERSION_AUTO_LABEL else display


# Model cards show the download size in Go. Nothing in the config carries it
# (the pod only knows the files it already has), so it is probed once per URL
# with a HEAD request and cached in memory for the session.
MODEL_SIZE_PROBE_TIMEOUT = 6.0

#: Category -> banner color, matching the category icon family.
CATEGORY_COLORS = {
    "diffusion": "#7c5cff",
    "video_vae": "#06b6d4",
    "audio_vae": "#f59e0b",
    "text_encoder": "#3b82f6",
    "tae": "#0ea5e9",
    "upscaler": "#22c55e",
    "frame_interp": "#ec4899",
    # The two non-catalog sections of the same tab get the same treatment.
    "custom_nodes": "#f97316",
    "workflows": "#4f46e5",
}
CATEGORY_DEFAULT_COLOR = "#6b7280"

#: Banner icon per section (the two extra sections are not model categories).
SECTION_ICONS = {
    "custom_nodes": "category_custom_nodes_orange.png",
    "workflows": "category_workflows_indigo.png",
}
COMFYUI_FALLBACK_VERSIONS = (
    "v0.34.5",
    "v0.34.4",
    "v0.34.3",
    "v0.34.0",
    "v0.33.4",
    "v0.33.3",
)


def fetch_comfyui_releases(
    url: str = COMFYUI_RELEASES_API_URL,
    timeout: float = COMFYUI_RELEASES_TIMEOUT,
    max_pages: int = 2,
) -> list[str]:
    """Fetch ComfyUI release tags (newest first) from the GitHub API.

    The default ``/releases`` endpoint returns stable releases only
    (pre-releases are excluded server-side and skipped here as well).
    Raises on any HTTP, timeout, or parse failure; the caller decides the
    fallback.
    """
    tags: list[str] = []
    page = 1
    while page <= max_pages:
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(
            f"{url}{sep}per_page=10&page={page}",
            headers={"User-Agent": "minimax-launcher-launcher"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            if not isinstance(item, dict) or item.get("prerelease"):
                continue
            tag = (item.get("tag_name") or "").strip()
            if tag:
                tags.append(tag)
        if len(payload) < 10:
            break
        page += 1
    if not tags:
        raise RuntimeError("no ComfyUI releases returned")
    return tags


def format_model_size(size_bytes: Optional[int]) -> str:
    """A download size for the model cards ("" when unknown).

    Gigabytes for the models that matter (``3.0 Go``), megabytes for the
    small ones (a 45 MB TAE would read ``0.0 Go`` otherwise).
    """
    if not size_bytes or size_bytes <= 0:
        return ""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.1f} Go"
    return f"{size_bytes / (1024 ** 2):.0f} Mo"


def probe_model_size(
    url: str, timeout: float = MODEL_SIZE_PROBE_TIMEOUT
) -> Optional[int]:
    """Content-Length of a model URL (HEAD), or None when unknown.

    Best effort by design: hosts that refuse HEAD, private repos and offline
    sessions all return None, and the card then simply shows no size.
    """
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": "minimax-launcher-launcher"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
        size = int(length) if length else 0
        return size if size > 0 else None
    except Exception:  # noqa: BLE001 - a size is a nicety, never a failure
        return None


def _contrast_fg(background: str) -> str:
    """Black or white text for *background* (a #rrggbb color)."""
    try:
        value = background.lstrip("#")
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:  # noqa: BLE001 - defensive: fall back to white
        return "#ffffff"
    # Rec. 601 luma: light banners (amber, green) take dark text.
    luma = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return "#1b1b1b" if luma > 0.6 else "#ffffff"


# ---------------------------------------------------------------------------
# ComfyUI model catalog & presets (100% modulable).
# ---------------------------------------------------------------------------
# Each category holds a list of {name, url, enabled}. The enabled set is what
# the launcher sends to the installer (comma-separated per category). Users
# can add models by URL, toggle them on/off, and snapshot the current
# selection into a named preset (save / reload / delete).
COMFY_MODEL_CATEGORIES = (
    "diffusion",
    "video_vae",
    "audio_vae",
    "text_encoder",
    "tae",
    "upscaler",
    "frame_interp",
)

COMFY_MODEL_CATEGORY_LABELS = {
    "diffusion": "Diffusion (checkpoint)",
    "video_vae": "Video VAE",
    "audio_vae": "Audio VAE",
    "text_encoder": "Text encoder",
    "tae": "TAE (preview)",
    "upscaler": "Upscaler",
    "frame_interp": "Frame interpolation",
}

# Built-in catalog (name, url, enabled-by-default). Only the enabled ones are
# downloaded unless the user checks more. The dasiwa-validated set is the
# default enabled selection; alternates ship disabled so a checkbox is all it
# takes to pull them in.
_DEFAULT_COMFY_CATALOG = (
    ("diffusion", "DaSiWa Hybrid (FL2VA+REF2VA)", DEFAULT_COMFY_DIFFUSION_URL, True),
    ("video_vae", "Video VAE int8 ConvRot", DEFAULT_COMFY_VIDEO_VAE_URL, True),
    ("video_vae", "Video VAE fp16", "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_fp16.safetensors", False),
    ("audio_vae", "Audio VAE fp32", DEFAULT_COMFY_AUDIO_VAE_URL, True),
    ("text_encoder", "Text encoder INT4 ConvRot", DEFAULT_COMFY_TEXT_ENCODER_URL, True),
    ("text_encoder", "Text encoder NVFP4 AWQ", "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", False),
    ("tae", "TAE (taeh3)", DEFAULT_COMFY_TAE_URL, True),
    ("upscaler", "2x-AnimeSharpV4_RCAN", DEFAULT_COMFY_UPSCALER_URL, True),
    ("frame_interp", "RIFE v4.26", DEFAULT_COMFY_FRAME_INTERP_URL, True),
)


def _default_comfy_catalog() -> dict:
    """Return the built-in catalog as {category: [{name,url,enabled}]}."""
    catalog: dict = {c: [] for c in COMFY_MODEL_CATEGORIES}
    for category, name, url, enabled in _DEFAULT_COMFY_CATALOG:
        catalog[category].append({"name": name, "url": url, "enabled": enabled})
    return catalog


def _normalize_catalog(data) -> dict:
    """Normalize a persisted catalog (dict or None) into a safe catalog.

    Unknown/malformed entries are dropped; missing categories fall back to the
    built-in list. URLs are kept as-is (full Hugging Face resolve URLs).
    """
    base = _default_comfy_catalog()
    if not isinstance(data, dict):
        return base
    for category in COMFY_MODEL_CATEGORIES:
        entries = data.get(category)
        if not isinstance(entries, list):
            continue
        cleaned = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            url = str(entry.get("url", "")).strip()
            if not name or not url:
                continue
            cleaned.append(
                {"name": name, "url": url, "enabled": bool(entry.get("enabled"))}
            )
        if cleaned:
            base[category] = cleaned
    return base


def _normalize_presets(data) -> dict:
    """Normalize persisted presets into ``{name: {models/nodes/workflows}}``.

    Delegates to :func:`launcher.comfy_presets.normalize_user_presets`, which
    also migrates the legacy ``{name: {category: [url, ...]}}`` shape.
    """
    return normalize_user_presets(data)


def _display_name_for_url(url: str) -> str:
    """Derive a short display name (repo name) from a git/file URL."""
    if not url:
        return ""
    base = url.split("#")[0].rstrip("/").split("/")[-1]
    return base[:-4] if base.endswith(".git") else base


def _file_stem(value: str) -> str:
    """The bare file name (no query string, no path, no extension)."""
    if not value:
        return ""
    name = value.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    if name.lower().endswith(".safetensors"):
        name = name[: -len(".safetensors")]
    return name.strip().lower()


#: ``(needle in the target path, category)`` — first match wins, so the
#: specific directories come before the generic ``vae`` one (the audio VAE
#: lives in ``vae/`` too, and its file name carries "audio").
_MODEL_TARGET_CATEGORY_HINTS = (
    ("audio", "audio_vae"),
    ("vae_approx", "tae"),
    ("text_encoder", "text_encoder"),
    ("frame_interp", "frame_interp"),
    ("upscale", "upscaler"),
    ("diffusion", "diffusion"),
    ("checkpoint", "diffusion"),
    ("vae", "video_vae"),
)


def _category_for_target(target: str, name: str = "") -> str:
    """The catalog category a preset model's ``target`` path belongs to.

    Used to file the models that have no catalog counterpart under the right
    category card (they used to sit in a separate list at the bottom of the
    tab). Anything unrecognisable falls back to ``diffusion``, the checkpoint
    category — a bare file name is most likely a checkpoint.
    """
    haystack = f"{target} {name}".lower()
    for needle, category in _MODEL_TARGET_CATEGORY_HINTS:
        if needle in haystack:
            return category
    return "diffusion"


def _enabled_models_from_catalog(catalog: dict) -> list:
    """Extract the checked catalog models as ``[{url, target}, ...]``.

    ``target`` (path under ComfyUI's ``models/``) is derived from the
    category and the URL basename — the same derivation the pod-side
    installer would apply for a user-preset model entry.
    """
    out = []
    for category, entries in catalog.items():
        for entry in entries:
            if not entry.get("enabled"):
                continue
            url = entry.get("url", "")
            if not url:
                continue
            out.append({"url": url, "target": model_target_for_url(category, url)})
    return out


def _comfy_terminate_settle_seconds() -> float:
    """Settle window (seconds) before "stop the pod" fires.

    Read straight from the environment rather than through a full
    ``load_config``: this runs on the UI thread during the periodic refresh,
    and decrypting the credential store there would be both wasteful and
    needless. A missing or malformed value falls back to the default — a typo
    in the ``.env`` must never break the refresh loop.
    """
    raw = (os.environ.get("COMFY_TERMINATE_SETTLE_SECONDS") or "").strip()
    if not raw:
        return comfy_watch.DEFAULT_QUEUE_SETTLE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return comfy_watch.DEFAULT_QUEUE_SETTLE_SECONDS
    if value != value or value < 0:  # NaN or negative
        return comfy_watch.DEFAULT_QUEUE_SETTLE_SECONDS
    return value


#: Entry types of the launcher "annuaire" (directory of LoRAs / workflows /
#: custom nodes to download at pod startup). ``type`` drives which pod env
#: variable receives the entry's download link.
ANNUAIRE_TYPES = ("lora", "workflow", "node")
ANNUAIRE_TYPE_LABELS = {
    "lora": "LoRA",
    "workflow": "Workflow",
    "node": "Node",
}

#: Where a library entry's payload comes from. ``url`` is downloaded by the pod
#: at startup (``H3_CUSTOM_*``); ``local`` is a file/folder on this machine,
#: uploaded by the launcher over the SSH tunnel once ComfyUI is up — the pod
#: cannot read the user's disk, and its installer rejects non-http(s) sources.
ANNUAIRE_SOURCE_URL = "url"
ANNUAIRE_SOURCE_LOCAL = "local"

#: Compatibility mode of a LoRA (local metadata only — never sent to the pod).
#: "" means "not specified"; the names follow MiniMax H3's own vocabulary
#: (T2VA / I2VA / FL2VA / L2VA / REF2VA) plus the "Turbo" LoRA family, and
#: ``fl2va_ref2va`` covers a LoRA that works in both first-last-frame and
#: reference mode.
ANNUAIRE_MODES = (
    "",
    "turbo",
    "t2va",
    "i2va",
    "fl2va",
    "l2va",
    "ref2va",
    "fl2va_ref2va",
)
ANNUAIRE_MODE_LABELS = {
    "": "—",
    "turbo": "Turbo",
    "t2va": "T2VA",
    "i2va": "I2VA",
    "fl2va": "FL2VA",
    "l2va": "L2VA",
    "ref2va": "REF2VA",
    "fl2va_ref2va": "FL2VA + REF2VA",
}
#: Legacy raw values (pre-extension) → canonical. Applied when loading a
#: saved library, so existing entries keep a meaningful badge.
ANNUAIRE_MODE_ALIASES = {
    "fl2v": "fl2va",
    "ref2v": "ref2va",
    "both": "fl2va_ref2va",
}
#: Banner colour per mode: one distinct colour per mode so a growing library
#: stays scannable at a glance. Fixed values (theme-independent), like the
#: model-category banners of the "Models & presets" tab; the banner text
#: uses a contrasting black/white (see ``_contrast_fg``).
ANNUAIRE_MODE_COLORS = {
    "": "#6b7280",
    "turbo": "#f97316",
    "t2va": "#06b6d4",
    "i2va": "#3b82f6",
    "fl2va": "#7c5cff",
    "l2va": "#14b8a6",
    "ref2va": "#22c55e",
    "fl2va_ref2va": "#eab308",
}
ANNUAIRE_MODE_RAW_BY_LABEL = {
    label: raw for raw, label in ANNUAIRE_MODE_LABELS.items()
}


def _canonical_annuaire_mode(mode: str) -> str:
    """Normalize a saved mode: legacy alias → canonical, unknown → ""."""
    raw = str(mode or "").strip()
    raw = ANNUAIRE_MODE_ALIASES.get(raw, raw)
    return raw if raw in ANNUAIRE_MODES else ""

#: Default target models an annuaire entry can be tagged with (local metadata
#: only). The editable combobox lets a user type additional models; any model
#: already used by an entry is appended to the dropdown automatically.
DEFAULT_ANNUAIRE_MODELS = (
    "MinimaxH3",
    "Wan2.2",
    "Flux",
    "Qwen Image",
    "Z Image",
    "Krea2",
)


def _annuaire_entry_enabled(entry) -> bool:
    """Whether an annuaire entry is downloaded automatically at pod startup.

    Absent (a library saved before the flag existed) means **enabled**: an
    existing ``gui.json`` keeps downloading everything it used to.
    """
    if not isinstance(entry, dict):
        return True
    raw = entry.get("enabled", True)
    return raw if isinstance(raw, bool) else True


def _annuaire_entry_source(entry) -> str:
    """Where an entry's payload comes from: ``"url"`` or ``"local"``.

    A local path wins when both are set: the user picked a file on this
    machine, and silently downloading a same-named URL instead would be a
    surprise. Absent both, the entry is treated as a URL (legacy shape).
    """
    if not isinstance(entry, dict):
        return "url"
    if str(entry.get("local_path", "")).strip():
        return "local"
    return "url"


def _annuaire_entry_payload(entry) -> str:
    """The entry's source (URL or local path), stripped."""
    if not isinstance(entry, dict):
        return ""
    if _annuaire_entry_source(entry) == "local":
        return str(entry.get("local_path", "")).strip()
    return str(entry.get("dl_url", "")).strip()


def _normalize_annuaire(data) -> list:
    """Normalize a persisted annuaire (list or None) into safe entries.

    Each entry is ``{"type", "name", "page_url", "dl_url", "local_path",
    "note", "enabled"}``. An entry is kept when its ``type`` is known and it
    has **either** a ``dl_url`` (downloaded by the pod at startup) **or** a
    ``local_path`` (a file/folder on this machine, uploaded by the launcher
    over the tunnel). The page link and the note are free-form (local memory
    aid, never sent to the pod). ``enabled`` is the "put this on the pod when
    it starts" switch. Unknown/malformed entries are dropped.
    """
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        entry_type = str(entry.get("type", "")).strip()
        if entry_type not in ANNUAIRE_TYPES:
            continue
        dl_url = str(entry.get("dl_url", "")).strip()
        local_path = str(entry.get("local_path", "")).strip()
        if not dl_url and not local_path:
            continue
        mode = _canonical_annuaire_mode(entry.get("mode", ""))
        out.append(
            {
                "type": entry_type,
                "name": str(entry.get("name", "")).strip(),
                "page_url": str(entry.get("page_url", "")).strip(),
                "dl_url": dl_url,
                "local_path": local_path,
                "note": str(entry.get("note", "")).strip(),
                "mode": mode,
                "trigger_words": str(entry.get("trigger_words", "")).strip(),
                "model": str(entry.get("model", "")).strip(),
                "enabled": _annuaire_entry_enabled(entry),
            }
        )
    return out


def _annuaire_workflow_filename(name: str) -> str:
    """Derive a safe workflow filename from an annuaire entry name."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._-")
    if not cleaned:
        return ""
    if not cleaned.lower().endswith(".json"):
        cleaned += ".json"
    return cleaned


def _annuaire_download_lists(entries) -> tuple[list[str], list[str], list[str]]:
    """Extract (loras, nodes, workflows) download lines from annuaire entries.

    Only the download link (plus, for workflows, a derived filename) leaves
    the machine; the page link and the note are local-only. LoRAs and nodes
    keep a bare URL (the pod resolves the filename / clones the repo).

    **Unchecked entries are skipped**: the checkbox on each card is the "put
    this on the pod when it starts" switch, so a disabled entry never reaches
    the pod's startup download list (it stays manually installable with the ⬇
    button).

    **Local entries are skipped too**: they are not URLs, and the pod-side
    installer rejects anything that is not http(s). They are uploaded by the
    launcher instead (see :meth:`ForgeApp._comfy_local_assets`).
    """
    loras: list[str] = []
    nodes: list[str] = []
    workflows: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not _annuaire_entry_enabled(entry):
            continue
        if _annuaire_entry_source(entry) != "url":
            continue
        dl_url = str(entry.get("dl_url", "")).strip()
        if not dl_url:
            continue
        entry_type = entry.get("type")
        if entry_type == "lora":
            loras.append(dl_url)
        elif entry_type == "node":
            nodes.append(dl_url)
        elif entry_type == "workflow":
            filename = _annuaire_workflow_filename(str(entry.get("name", "")))
            workflows.append(f"{dl_url} {filename}".strip())
    return loras, nodes, workflows


def _annuaire_to_markdown(entries) -> str:
    """Render the annuaire as a Markdown file (name, note, trigger words).

    Grouped by target model. Download links are intentionally omitted — this
    file is meant to be handed to an AI agent for prompt writing, not for
    re-downloading the LoRAs.
    """
    grouped: dict = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model = str(entry.get("model", "")).strip() or "No model"
        grouped.setdefault(model, []).append(entry)
    lines = ["# LoRA / Workflow / Node library", ""]
    for model in sorted(grouped, key=str.lower):
        lines.append(f"## {model}")
        lines.append("")
        for entry in grouped[model]:
            name = str(entry.get("name", "")).strip() or "(unnamed)"
            lines.append(f"### {name}")
            if not _annuaire_entry_enabled(entry):
                lines.append(
                    "- **Disabled**: not sent when the pod starts"
                )
            if _annuaire_entry_source(entry) == "local":
                # The absolute path is deliberately not written here: this
                # file is handed to an AI agent for prompt writing, and a
                # machine-specific path is noise (the JSON backup keeps it).
                lines.append("- **Source**: local file")
            mode = ANNUAIRE_MODE_LABELS.get(entry.get("mode", ""), "")
            if mode and mode != "—":
                lines.append(f"- **Mode** : {mode}")
            tw = str(entry.get("trigger_words", "")).strip()
            if tw:
                lines.append(f"- **Trigger words** : {tw.replace(chr(10), ' ')}")
            note = str(entry.get("note", "")).strip()
            if note:
                lines.append(f"- **Note** : {note.replace(chr(10), ' ')}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


ANNUAIRE_BACKUP_FORMAT = "minimax-launcher-annuaire"
ANNUAIRE_BACKUP_VERSION = 1


def annuaire_backup_payload(entries) -> dict:
    """Build a machine-readable backup document of the library.

    Unlike the Markdown export (human / AI facing, download links omitted),
    this keeps every field — including the download URLs — so the file can
    be imported back into any launcher unchanged.
    """
    return {
        "format": ANNUAIRE_BACKUP_FORMAT,
        "version": ANNUAIRE_BACKUP_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "entries": _normalize_annuaire(entries),
    }


def annuaire_from_backup(data) -> list:
    """Extract and normalize entries from a backup document.

    Accepts the wrapper form ``{"format", "version", "entries": [...]}`` or a
    bare list of entry dicts. Returns a normalized (safe) list.
    """
    if isinstance(data, dict):
        raw = data.get("entries")
    elif isinstance(data, list):
        raw = data
    else:
        raw = None
    return _normalize_annuaire(raw)


def annuaire_merge(current, incoming) -> tuple:
    """Merge ``incoming`` entries into ``current`` without duplicates.

    An incoming entry is a duplicate (and is skipped) when an existing entry
    has the same ``type`` and the same ``name`` (case-insensitive). Returns
    ``(merged, added, skipped)``.
    """
    merged = [dict(e) for e in current]
    seen = {
        (str(e.get("type", "")).strip(), str(e.get("name", "")).strip().lower())
        for e in merged
    }
    added = 0
    skipped = 0
    for entry in incoming:
        key = (
            str(entry.get("type", "")).strip(),
            str(entry.get("name", "")).strip().lower(),
        )
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        merged.append(dict(entry))
        added += 1
    return merged, added, skipped


def _scroll_canvas_clamped(canvas, delta: int) -> None:
    """Scroll a canvas by one wheel notch, clamped to the content bounds.

    ``Canvas.yview_scroll`` overscrolls by itself (showing empty space past
    the top/bottom). This stops at ``y0 == 0`` (top) and ``y1 == 1``
    (bottom), and never scrolls when the content fits the viewport.
    """
    y0, y1 = canvas.yview()
    units = int(-delta / 120)
    if units < 0 and y0 <= 0.0:
        return
    if units > 0 and y1 >= 1.0:
        return
    canvas.yview_scroll(units, "units")


def truncate_to_width(text: str, max_width: int, font) -> str:
    """Truncate *text* to *max_width* pixels with a real ellipsis.

    Character counts lie: two names of the same length can differ by half
    the widget width. This measures the actual font and binary-searches the
    longest prefix that still fits, so a long name never gets cut at the
    widget border (the full value stays available through the tooltip).
    """
    if not text or max_width <= 12:
        return text
    try:
        if font.measure(text) <= max_width:
            return text
        ellipsis = "…"
        low, high, best = 0, len(text), 0
        while low <= high:
            middle = (low + high) // 2
            if font.measure(text[:middle] + ellipsis) <= max_width:
                best, low = middle, middle + 1
            else:
                high = middle - 1
        return (text[:best] + ellipsis) if best else ellipsis
    except tk.TclError:  # pragma: no cover - defensive
        return text


def comfy_summary_text(
    preset: str,
    tier: str,
    workflows: str,
    catalog: dict,
    preset_content: Optional[dict] = None,
) -> str:
    """One-line summary of the selected ComfyUI configuration."""
    count = sum(
        1 for entries in catalog.values() for entry in entries if entry.get("enabled")
    )
    tier_label = COMFY_TIER_LABELS.get(tier, tier)
    if tier == "auto":
        preset_label = "ignored (Auto mode)"
    else:
        preset_label = preset.strip() or "none"
        if preset_content:
            n_models = len(preset_content.get("models") or [])
            n_nodes = len(preset_content.get("nodes") or [])
            n_wf = len(preset_content.get("workflows") or [])
            preset_label = (
                f"{preset.strip()} ({n_models} models, {n_nodes} nodes, "
                f"{n_wf} workflows)"
            )
    text = (
        f"{count} models · preset {preset_label} · "
        f"tier {tier_label} · workflows {workflows}"
    )
    if tier == "perso" and not preset.strip() and count == 0:
        text += " · ⚠ nothing to download: ComfyUI will not be able to generate"
    return text


def _enabled_urls_from_catalog(catalog: dict) -> dict:
    """Extract {category: [enabled urls]} from a catalog."""
    out = {}
    for category, entries in catalog.items():
        urls = [e["url"] for e in entries if e.get("enabled")]
        if urls:
            out[category] = urls
    return out

POWER_ACTIONS = ("none", "shutdown", "sleep")
POWER_ACTION_LABELS = {
    "none": "No action",
    "shutdown": "Shut the machine down",
    "sleep": "Put the machine to sleep",
}
ENV_TEMPLATE = (
    "# MiniMax H3 Launcher — configuration (optional)\n"
    "# Variables already present in the environment take precedence.\n\n"
    "# RUNPOD_API_KEY=\n"
    "# RUNPOD_TEMPLATE_ID=\n"
    "# MODEL_ID=Qwen/Qwen3.8-27B-FP8\n"
    "# LLM_PROFILE=fp8\n"
    "# LAUNCHER_INFRA_RECOVERY=confirm\n"
    "# Optional API keys (preferably stored via \"Credentials\"/DPAPI)\n"
    "# HF_TOKEN=\n"
    "# CIVITAI_API_KEY=\n"
)

COMPONENT_LABELS = {
    "runpod": "Pod",
    "ssh": "SSH tunnel",
    "vllm": "Service",
    "model": "Preset / image",
    "config": "Configuration",
}

COMPONENT_ORDER = ("runpod", "ssh", "vllm", "model", "config")

RECOVERY_LABELS = {
    "start_runpod": "Start the pod",
    "reconnect_ssh": "Reconnect SSH",
    "restart_comfy": "Restart ComfyUI",
    "restart_train": "Restart the training",
}

#: Serving statuses the diagnose maps to a FAILED severity. ``UNAVAILABLE``
#: is deliberately absent: ``_probe_service`` classifies a refused/timed-out
#: connection as UNKNOWN so a legitimate service-loading window raises no
#: alarm (no cause, no remediation) — offering a repair there would show a
#: button next to an empty hint and could interrupt a start in flight.
_FAILED_SERVING_STATES = ("UNHEALTHY", "NOT READY")
_STOPPED_POD_STATES = ("STOPPED", "EXITED", "TERMINATED", "ERROR", "FAILED")


def suggest_recovery_action(
    components: Mapping[str, str], stack: str = "comfy"
) -> Optional[str]:
    """Map the failing component onto a RecoveryEngine action name.

    Follows the diagnostic priority order (``infra_diagnose._REMEDIATION``),
    so the shortcut matches the cause/remediation actually shown: the
    serving probe first (ComfyUI for the comfy stack, the KasmVNC desktop
    for the train stack), then the pod, then the tunnel.

    Only states the diagnose itself grades as a failure are mapped, so the
    button never contradicts the displayed cause (see
    ``_FAILED_SERVING_STATES``). Returns ``None`` when nothing maps —
    configuration problems have no automated repair, and the remediation
    text stays the only guidance. Pure: no I/O, no app state.
    """
    def _status(key: str) -> str:
        return str(components.get(key) or "").upper()

    if _status("vllm") in _FAILED_SERVING_STATES:
        return "restart_comfy" if stack == "comfy" else "reconnect_ssh"
    if _status("runpod") in _STOPPED_POD_STATES:
        return "start_runpod"
    if _status("ssh") == "STOPPED":
        return "reconnect_ssh"
    return None


#: Outcomes of the tunnel check « Ouvrir … ↗ » performs before opening a
#: tunneled surface. Only :data:`OPEN_TUNNEL_REPAIR` leads to a mutation, and
#: that mutation is limited to starting the launcher's own SSH tunnel: a pod
#: that is not RUNNING, a pod RunPod no longer has, a local port held by a
#: foreign process and an unreachable RunPod API are all reported instead of
#: acted upon.
OPEN_TUNNEL_OK = "ok"
OPEN_TUNNEL_REPAIR = "repair"
OPEN_TUNNEL_PORT_BUSY = "port_busy"
OPEN_TUNNEL_POD_STOPPED = "pod_stopped"
OPEN_TUNNEL_POD_GONE = "pod_gone"
OPEN_TUNNEL_POD_UNKNOWN = "pod_unknown"

#: Pod statuses meaning "this pod is not coming back on its own".
_GONE_POD_STATES = ("TERMINATED", "MISSING", "NOT_FOUND")


def open_tunnel_outcome(
    *, tunnel_ok: bool, pod_state: Optional[str], port_free: bool
) -> str:
    """Decide what « Ouvrir … » must do about the stack's tunnel.

    Pure, so the whole decision table is unit-tested: a live tunnel needs no
    check at all, and a dead one is only repaired when the pod is RUNNING and
    the local port is ours to take.
    """
    if tunnel_ok:
        return OPEN_TUNNEL_OK
    state = (pod_state or "").strip().upper()
    if state in _GONE_POD_STATES:
        return OPEN_TUNNEL_POD_GONE
    if not state:
        return OPEN_TUNNEL_POD_UNKNOWN
    if state != "RUNNING":
        return OPEN_TUNNEL_POD_STOPPED
    if not port_free:
        return OPEN_TUNNEL_PORT_BUSY
    return OPEN_TUNNEL_REPAIR


def open_tunnel_message(
    outcome: str, *, state: Optional[str] = None, label: str = "", port: int = 0
) -> str:
    """The journal line explaining a refused outcome ("" when there is none).

    Every message names the next gesture ("Start") so the button never
    fails silently, and the port case states explicitly that nothing was
    killed: an external process is never the launcher's to reap.
    """
    if outcome == OPEN_TUNNEL_POD_GONE:
        return (
            f"Tunnel {label} not restored: pod deleted (record cleared) "
            "— press Start to create a new one."
        )
    if outcome == OPEN_TUNNEL_POD_STOPPED:
        return (
            f"Tunnel {label} not restored: pod {(state or '?').upper()} "
            "— press Start to relaunch it."
        )
    if outcome == OPEN_TUNNEL_POD_UNKNOWN:
        return (
            f"Tunnel {label}: pod state unavailable (RunPod API) "
            "— retrying shortly."
        )
    if outcome == OPEN_TUNNEL_PORT_BUSY:
        return (
            f"Tunnel {label} not restored: port {port} held by an external "
            "process — nothing was killed."
        )
    return ""


def open_url_in_browser(url: str) -> Optional[str]:
    """Open *url* in the default browser; return an error message on failure.

    Kept free of Tk and of the journal so the action thread can call it: the
    caller decides how to report (``_append_log`` on the main thread, the
    queue from a worker).
    """
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, never raised
        return str(exc) or exc.__class__.__name__
    return None


STACK_LABELS = {
    "comfy": "ComfyUI video",
    "train": "LoRA training",
}

#: Wording of the destructive preset action (label + tooltip): it must always
#: say that it deletes the *selected* preset, wherever the button sits.
PRESET_DELETE_LABEL = "🗑 Delete the preset"
PRESET_DELETE_TOOLTIP = "Deletes the preset selected in the list."

# Per-secret fields of the credential store, in display order. ``runpod`` /
# ``template`` are the required RunPod pair, ``comfy_template`` /
# ``train_template`` are the optional stack template IDs, ``vnc_password`` is
# the training desktop credential, and ``hf``/``civitai`` are the optional
# extra API keys.
CREDENTIAL_FIELD_ORDER = (
    "runpod", "template", "comfy_template", "train_template",
    "vnc_password", "hf", "civitai",
)

CREDENTIAL_FIELD_LABELS = {
    "runpod": "RunPod API key",
    "template": "RunPod template (private)",
    "comfy_template": "ComfyUI template (private)",
    "train_template": "LoRA training template (private)",
    "vnc_password": "Training desktop password",
    "hf": "Hugging Face token",
    "civitai": "Civitai API key",
}

CREDENTIAL_STATE = {
    "CONFIGURED": "Credentials configured",
    "NOT CONFIGURED": "No stored credential",
    "CORRUPT": "Credentials unreadable (recreate them)",
    "UNKNOWN": "Credential state unknown",
}

# Mirrors H3_PRESET_NAMES in comfy/config.env (the installer is the source
# of truth); the combobox stays editable so any installer-known preset works.
COMFY_PRESET_CHOICES = ("dasiwa_mmh3v12", "muse_director_seedhunt")

# An empty H3_PRESETS disables presets and installs the standard tier only.
# Exposed in the GUI as an explicit "no preset" choice (never a silent
# fallback to the default preset).
COMFY_PRESET_NONE_LABEL = "None (standard tier)"
COMFY_PRESET_DISPLAY_CHOICES = (COMFY_PRESET_NONE_LABEL,) + COMFY_PRESET_CHOICES

# C2: the three model-selection modes shown in the GUI. Raw values are
# persisted in gui.json; launcher.config maps them to the pod tier
# ("auto_perso" -> auto + custom models, "perso" -> custom, standard stack
# not downloaded). All three modes always download the LoRAs (turbo auto +
# personal vault).
COMFY_TIER_CHOICES = ("auto", "auto_perso", "perso")
COMFY_TIER_LABELS = {
    "auto": "Auto",
    "auto_perso": "Auto + Perso",
    "perso": "Perso",
}
COMFY_TIER_RAW_BY_LABEL = {label: raw for raw, label in COMFY_TIER_LABELS.items()}

#: gui.json values written before C2 (they picked a standard-tier flavor with
#: the personal extras on top): the closest new mode is "Auto + Perso".
_LEGACY_COMFY_TIER_MIGRATION = {
    "light": "auto_perso",
    "pruned": "auto_perso",
    "pruned_scaled": "auto_perso",
    "balanced": "auto_perso",
    "max": "auto_perso",
    "hybrid": "auto_perso",
    "custom": "perso",
}


def _migrate_comfy_tier(raw: str) -> str:
    """Migrate a stored tier value to the three-mode scheme (or default)."""
    if raw in COMFY_TIER_CHOICES:
        return raw
    return _LEGACY_COMFY_TIER_MIGRATION.get(raw, "auto")


def _preset_to_display(value: str) -> str:
    """Raw preset value -> combobox display (empty means "no preset")."""
    return COMFY_PRESET_NONE_LABEL if not value else value


def _display_to_preset(value: str) -> str:
    """Combobox display -> raw preset value (the "none" label maps to "")."""
    value = value.strip()
    return "" if value == COMFY_PRESET_NONE_LABEL else value

COMFY_WORKFLOW_CHOICES = (
    "all",
    "t2v",
    "i2v",
    "r2v",
    "t2v,i2v",
    "i2v,r2v",
    "t2v,i2v,r2v",
)

COMPONENT_LABELS_COMFY = {
    "vllm": "ComfyUI",
    "model": "Preset",
}

THEMES = ("dark", "light")

PALETTES = {
    "dark": {
        "bg": "#10131a",
        "bg_alt": "#1a202c",
        "surface": "#171d27",
        "fg": "#f1f5fb",
        "muted": "#9aa8bd",
        "border": "#2b3547",
        "accent": "#7c6cff",
        "accent_active": "#978bff",
        "accent_fg": "#ffffff",
        "ok": "#48c78e",
        "warn": "#f0b35b",
        "error": "#ff6b78",
        "log_bg": "#0c0f15",
        "log_fg": "#c8d2e0",
        "button_bg": "#242c3a",
        "button_fg": "#e6edf7",
        "button_active": "#354158",
        "input_bg": "#1a202c",
        "input_fg": "#f1f5fb",
    },
    "light": {
        "bg": "#f5f7fb",
        "bg_alt": "#ffffff",
        "surface": "#ffffff",
        "fg": "#172033",
        "muted": "#64748b",
        "border": "#dbe2ec",
        "accent": "#5d4ee8",
        "accent_active": "#4939d2",
        "accent_fg": "#ffffff",
        "ok": "#16875a",
        "warn": "#af6a08",
        "error": "#d83b4a",
        "log_bg": "#ffffff",
        "log_fg": "#243044",
        "button_bg": "#e9edf4",
        "button_fg": "#243044",
        "button_active": "#dbe2ec",
        "input_bg": "#ffffff",
        "input_fg": "#172033",
    },
}

# ---------------------------------------------------------------------------
# ttkbootstrap theme family («forge»).
#
# The hand-rolled palette anchors double as the semantic color anchors of a
# ttkbootstrap theme family: one registration generates the ready-made
# ``forge-light`` / ``forge-dark`` themes (ramps, borders, input colors…)
# from the same hex values the fallback palette uses. When ttkbootstrap is
# installed the whole window is themed from that family and the dark/light
# toggle simply switches its theme mode.
# ---------------------------------------------------------------------------

TB_THEME_FAMILY = "forge"
_TB_THEME_NEVER_REGISTERED = object()
_TB_THEME_REGISTERED_ON = _TB_THEME_NEVER_REGISTERED
_TB_STYLE_ACTIVE = False


def _hex_mix(color_a: str, color_b: str, ratio: float) -> str:
    """Linear blend of two ``#rrggbb`` colors (``ratio`` = share of *b*)."""

    def _channel(value: int) -> int:
        return max(0, min(255, round(value)))

    a = color_a.lstrip("#")
    b = color_b.lstrip("#")
    out = []
    for i in (0, 2, 4):
        channel_a = int(a[i : i + 2], 16)
        channel_b = int(b[i : i + 2], 16)
        out.append(f"{_channel(channel_a + (channel_b - channel_a) * ratio):02x}")
    return "#" + "".join(out)


# The fallback palettes gain the same derived key the theme palette gets:
# a panel border lifted toward the foreground so framed sections read as
# raised above the page in both modes.
for _pal in PALETTES.values():
    _pal["panel_border"] = _hex_mix(_pal["border"], _pal["fg"], 0.25)


def ensure_forge_theme() -> bool:
    """Register the ``forge`` ttkbootstrap family (idempotent per Style).

    The ttkbootstrap ``Style`` is a process singleton, but its Tcl state
    belongs to one Tk root / interpreter. The registration is therefore
    keyed on the *live Style instance*: before any root exists it is
    queued (ttkbootstrap applies it when the first Style comes up), and a
    later call against a different live Style (a fresh root — tests, or a
    relaunch in the same process) registers again for that interpreter.

    Returns True when the family is available."""
    global _TB_THEME_REGISTERED_ON
    if not _HAS_TTKBOOTSTRAP:
        return False
    live = _ttkbootstrap.Style.get_instance()
    if _TB_THEME_REGISTERED_ON is live:
        return True
    dark = PALETTES["dark"]
    light = PALETTES["light"]
    _ttkbootstrap.Theme(
        name=TB_THEME_FAMILY,
        primary=dark["accent"],
        success=dark["ok"],
        info="#3d9bff",
        warning=dark["warn"],
        danger=dark["error"],
        secondary=dark["muted"],
        light={"background": light["bg"], "foreground": light["fg"]},
        dark={"background": dark["bg"], "foreground": dark["fg"]},
    ).register()
    _TB_THEME_REGISTERED_ON = live
    return True


def _reset_stale_style() -> None:
    """Drop the process-singleton Style when its Tk root is gone.

    The ttkbootstrap ``Style`` is a singleton, but its Tcl state belongs to
    the Tk root / interpreter that created it. A second root in the same
    process (tests, a relaunch after tray quit) gets a fresh interpreter;
    the stale singleton would only raise "application has been destroyed"
    — so it is cleared and a new Style is built against the current
    default root."""
    if _ttkbootstrap is None:
        return
    try:
        stale = _ttkbootstrap.Style.get_instance()
        if stale is None:
            return
        try:
            alive = bool(stale.master.winfo_exists())
        except tk.TclError:
            alive = False
        if not alive:
            _ttkbootstrap.Style.instance = None
    except Exception as exc:  # noqa: BLE001 - an unexpected library shape is
        # not worth failing the launch over; the caller falls back.
        logger.debug("Style reset skipped: %s", exc)


def make_forge_style(theme: str):
    """Create (or reuse) the forge ``Style`` for the current root.

    The ttkbootstrap ``Style`` is a process singleton, so within one root
    successive app instances re-activate the same one; across roots (a
    stale singleton) it is reset and rebuilt. Returns None when
    ttkbootstrap is unavailable or fails — the app then runs on PALETTES.
    ``_TB_STYLE_ACTIVE`` mirrors the outcome so module-level dialogs (which
    do not hold an app reference) know which mode the process runs in."""
    global _TB_STYLE_ACTIVE, _TB_THEME_REGISTERED_ON
    if not _HAS_TTKBOOTSTRAP:
        _TB_STYLE_ACTIVE = False
        return None
    _reset_stale_style()
    mode = "dark" if theme == "dark" else "light"
    try:
        ensure_forge_theme()
        style = _ttkbootstrap.Style(
            f"{TB_THEME_FAMILY}-{mode}",
            themename=TB_THEME_FAMILY,
            light_theme=f"{TB_THEME_FAMILY}-light",
            dark_theme=f"{TB_THEME_FAMILY}-dark",
        )
        style.theme_mode = mode
    except Exception as exc:  # noqa: BLE001 - the theme is optional: any
        # failure (Tcl or an unexpected library API) must degrade to the
        # hand-rolled palettes instead of aborting startup.
        logger.warning("ttkbootstrap theme unavailable: %s", exc)
        _TB_STYLE_ACTIVE = False
        return None
    _TB_THEME_REGISTERED_ON = style
    _TB_STYLE_ACTIVE = True
    return style


def palette_from_theme(style, theme: str) -> dict:
    """Build the recoloring palette for *theme*.

    With a live ttkbootstrap style the values are derived from the theme's
    color set (so hand-rolled tk widgets and themed ttk widgets stay
    visually identical); without it the hand-rolled PALETTES entry is
    returned unchanged."""
    pal = dict(PALETTES[theme])
    if style is None:
        return pal
    colors = style.colors
    pal.update(
        {
            "bg": colors.bg,
            "fg": colors.fg,
            "bg_alt": colors.active,
            "surface": colors.active,
            "muted": colors.secondary,
            "border": colors.border,
            "panel_border": _hex_mix(colors.border, colors.fg, 0.25),
            "accent": colors.primary,
            "accent_active": _hex_mix(colors.primary, colors.fg, 0.25),
            "ok": colors.success,
            "warn": colors.warning,
            "error": colors.danger,
            "button_bg": colors.secondary,
            "button_fg": colors.fg,
            "button_active": colors.active,
            "input_bg": colors.inputbg,
            "input_fg": colors.inputfg,
        }
    )
    return pal


_OVERALL_COLOR_KEY = {
    "HEALTHY": "ok",
    "DEGRADED": "warn",
    "FAILED": "error",
    "STOPPED": "muted",
    "UNKNOWN": "muted",
}

# One-sentence hover explanations for the technical parameters (point 11:
# no in-interface paragraphs, a tooltip is enough).
TECHNICAL_TOOLTIPS = {
    "sage": "Approximate attention (Sage): speeds generation up without hurting quality.",
    "ntfy": "ntfy topic to receive generation notifications (optional).",
    "vault": "Private Hugging Face repo your LoRAs, presets and outputs are synced to.",
    "tae": "TAE (taeh3): a light, fast VAE for previews (less accurate than the full VAE).",
    "video_vae": "Video VAE: encodes/decodes the video (int8 ConvRot = lighter, fp16 = more faithful).",
    "text_encoder": "Text encoder: the INT4 ConvRot quantisation cuts VRAM at equal quality.",
    "comfy_mode_only": "Only available in ComfyUI video mode.",
    "gpu_choice": (
        "GPU card requested from RunPod for the launch. "
        "The A6000 is the project target; the A40 (48 GB, same compute "
        "capability 8.6) is the fallback when the A6000 has no capacity — "
        "the stack was validated live on it."
    ),
    "tunnel_restart_recovery": (
        "Requires infrastructure recovery to be enabled: "
        "LAUNCHER_INFRA_RECOVERY=confirm in .env."
    ),
    "comfy_direct_no_tunnel": (
        "Direct mode: no local process to close, access goes through the "
        "pod's public URL."
    ),
    "tier_modes": (
        "Auto: standard stack (GPU auto-detection) + LoRA; the preset and "
        "the ticked models are ignored. Auto + Perso: standard stack + "
        "preset + ticked models + LoRA. Perso: preset + ticked models + "
        "LoRA only (the standard stack is not downloaded)."
    ),
    "perso_tiers_only": (
        "Active with the \"Auto + Perso\" or \"Perso\" tier — \"Auto\" only "
        "uses the standard stack."
    ),
    "auto_collect": (
        "Watches the ComfyUI history through the tunnel: every finished "
        "generation is downloaded into the reception folder, with a .txt "
        "file of the same name (prompt, settings, resources, workflow). "
        "Generations already finished before you enable this are ignored; "
        "the one in flight is collected when it ends."
    ),
    "notify_windows": (
        "Shows a Windows notification at the end of each generation "
        "(taskbar balloon, or a system notification when the tray icon "
        "is unavailable)."
    ),
    "notify_ntfy": (
        "Publishes an ntfy notification at the end of each generation, on the "
        "\"ntfy topic\" above (the same one the pod uses). Self-hosted "
        "server: set NTFY_SERVER in .env — it is forwarded to the pod, so "
        "both sides publish on the same server. On a pod that is already "
        "running, the pod's own notification is only muted at its next "
        "start: you will get two messages until then."
    ),
    "terminate_after": (
        "One-shot option: the ComfyUI pod is stopped once the queue is EMPTY "
        "(not after the first generation) — a batch of ten finishes, then the "
        "pod goes. The box unticks itself. Tick it before going to bed: "
        "nothing runs (or bills) once the last generation is done. "
        "COMFY_TERMINATE_SETTLE_SECONDS (default 60) is how long the queue "
        "must stay empty first."
    ),
    "outputs_dir": (
        "Folder the received files are written to (videos and their .txt). "
        "Leave empty to use your Downloads folder. An existing file is never "
        "overwritten: the new one gets a \"(2)\", \"(3)\", … suffix."
    ),
    "outputs_dir_off": (
        "Has no effect until \"Receive the generations\" is ticked."
    ),
}


# ---------------------------------------------------------------------------
# .env loading (the CLI reads os.environ; the GUI additionally loads a local
# .env file so a double-clicked executable works without manual exports).
# ---------------------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` file (comments, blank lines and ``export`` ok).

    Surrounding matching quotes are stripped; an unquoted trailing ``# comment``
    is removed. Values are returned verbatim otherwise (no interpolation).
    """
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match is None:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        else:
            raw = raw.split(" #", 1)[0].rstrip()
        if raw:
            values[key] = raw
    return values


def find_env_file(
    cwd: Optional[Path] = None,
    candidates: Optional[list[Path]] = None,
) -> Optional[Path]:
    """Locate the ``.env`` file to load, or None.

    Order: ``MINIMAX_LAUNCHER_ENV_FILE`` override, then the executable/app
    directory (frozen build: the exe's folder; dev: the repository root),
    then the current working directory. First match wins.
    """
    override = os.environ.get(ENV_FILE_VARIABLE)
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None
    if candidates is None:
        candidates = []
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent)
        else:
            candidates.append(Path(__file__).resolve().parents[1])
        if cwd is not None:
            candidates.append(Path(cwd))
    for candidate in candidates:
        env_file = candidate / ".env"
        if env_file.is_file():
            return env_file
    return None


def load_env_file(
    path: Path,
    env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Load *path* into *env* (default ``os.environ``) without overriding.

    Existing variables always win (same precedence principle as secrets).
    Returns the mapping of keys actually loaded.
    """
    target = os.environ if env is None else env
    loaded: dict[str, str] = {}
    for key, value in parse_env_file(path).items():
        if key not in target:
            target[key] = value
            loaded[key] = value
    return loaded


# ---------------------------------------------------------------------------
# Persisted GUI settings (theme, close behavior).
# ---------------------------------------------------------------------------


@dataclass
class GuiSettings:
    theme: str = "dark"
    #: UI language code (see launcher.i18n); "en_US" unless changed.
    language: str = "en_US"
    auto_refresh: bool = True
    minimize_on_close: bool = True
    stack: str = "comfy"
    comfy_preset: str = ""
    comfy_tier: str = DEFAULT_COMFY_TIER
    comfy_workflows: str = DEFAULT_COMFY_WORKFLOWS
    comfy_sage_attention: str = COMFY_SAGE_MODES[0]
    comfy_access: str = DEFAULT_COMFY_ACCESS
    comfy_ntfy_topic: str = ""
    comfy_personal_repo: str = ""
    comfyui_version: str = ""
    #: Automatic collection of finished ComfyUI generations (launcher-side).
    #: ``comfy_outputs_dir`` empty = the user's Downloads folder; the other
    #: three are opt-in extras on top of the collection itself.
    comfy_auto_collect: bool = False
    comfy_notify_windows: bool = False
    comfy_notify_ntfy: bool = False
    comfy_terminate_after: bool = False
    comfy_outputs_dir: str = ""
    diffusion_url: str = DEFAULT_COMFY_DIFFUSION_URL
    video_vae_url: str = DEFAULT_COMFY_VIDEO_VAE_URL
    audio_vae_url: str = DEFAULT_COMFY_AUDIO_VAE_URL
    text_encoder_url: str = DEFAULT_COMFY_TEXT_ENCODER_URL
    tae_url: str = DEFAULT_COMFY_TAE_URL
    upscaler_url: str = DEFAULT_COMFY_UPSCALER_URL
    frame_interp_url: str = DEFAULT_COMFY_FRAME_INTERP_URL
    comfy_models: dict = field(default_factory=_default_comfy_catalog)
    comfy_presets: dict = field(default_factory=dict)
    comfy_annuaire: list = field(default_factory=list)
    #: Which model families the training pod pre-downloads at boot. Empty
    #: (the default) downloads nothing and lets the in-app Preferences button
    #: fetch on demand — MiniMax H3 weights are ~45 GB, so this is a real
    #: cost decision rather than a convenience toggle.
    train_fetch_models: str = DEFAULT_TRAIN_FETCH_MODELS
    #: Where trained LoRAs are collected on this machine. The user picks it;
    #: empty means "ask". Collected locally on purpose: the launcher never
    #: routes a trained LoRA through a third-party service.
    train_lora_dir: str = ""
    #: RunPod GPU id used for the next launch (any stack). Persisted so the
    #: operator does not have to retype the fallback card every session.
    runpod_gpu: str = DEFAULT_RUNPOD_GPU_ID


def load_settings(home: Optional[Path] = None) -> GuiSettings:
    path = (home or default_home()) / SETTINGS_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return GuiSettings()
    if not isinstance(data, dict):
        return GuiSettings()
    settings = GuiSettings()
    if data.get("theme") in THEMES:
        settings.theme = data["theme"]
    if isinstance(data.get("auto_refresh"), bool):
        settings.auto_refresh = data["auto_refresh"]
    if isinstance(data.get("minimize_on_close"), bool):
        settings.minimize_on_close = data["minimize_on_close"]
    if data.get("stack") in STACKS:
        settings.stack = data["stack"]
    if isinstance(data.get("comfy_preset"), str):
        settings.comfy_preset = data["comfy_preset"]
    if isinstance(data.get("comfy_tier"), str):
        settings.comfy_tier = _migrate_comfy_tier(data["comfy_tier"])
    if isinstance(data.get("comfy_workflows"), str):
        settings.comfy_workflows = data["comfy_workflows"]
    if data.get("comfy_sage_attention") in COMFY_SAGE_MODES:
        settings.comfy_sage_attention = data["comfy_sage_attention"]
    if data.get("comfy_access") in COMFY_ACCESS_MODES:
        settings.comfy_access = data["comfy_access"]
    if isinstance(data.get("comfy_ntfy_topic"), str):
        settings.comfy_ntfy_topic = data["comfy_ntfy_topic"]
    if isinstance(data.get("comfy_personal_repo"), str):
        settings.comfy_personal_repo = data["comfy_personal_repo"]
    if isinstance(data.get("comfyui_version"), str):
        settings.comfyui_version = data["comfyui_version"]
    for key in (
        "comfy_auto_collect",
        "comfy_notify_windows",
        "comfy_notify_ntfy",
        "comfy_terminate_after",
    ):
        if isinstance(data.get(key), bool):
            setattr(settings, key, data[key])
    if isinstance(data.get("comfy_outputs_dir"), str):
        settings.comfy_outputs_dir = data["comfy_outputs_dir"]
    if isinstance(data.get("diffusion_url"), str):
        settings.diffusion_url = data["diffusion_url"]
    if isinstance(data.get("video_vae_url"), str):
        settings.video_vae_url = data["video_vae_url"]
    if isinstance(data.get("audio_vae_url"), str):
        settings.audio_vae_url = data["audio_vae_url"]
    if isinstance(data.get("text_encoder_url"), str):
        settings.text_encoder_url = data["text_encoder_url"]
    if isinstance(data.get("tae_url"), str):
        settings.tae_url = data["tae_url"]
    if isinstance(data.get("upscaler_url"), str):
        settings.upscaler_url = data["upscaler_url"]
    if isinstance(data.get("frame_interp_url"), str):
        settings.frame_interp_url = data["frame_interp_url"]
    settings.comfy_models = _normalize_catalog(data.get("comfy_models"))
    settings.comfy_presets = _normalize_presets(data.get("comfy_presets"))
    settings.comfy_annuaire = _normalize_annuaire(data.get("comfy_annuaire"))
    if isinstance(data.get("runpod_gpu"), str) and data["runpod_gpu"].strip():
        settings.runpod_gpu = data["runpod_gpu"].strip()
    if isinstance(data.get("train_fetch_models"), str):
        settings.train_fetch_models = data["train_fetch_models"]
    if isinstance(data.get("train_lora_dir"), str):
        settings.train_lora_dir = data["train_lora_dir"]
    language = data.get("language")
    if isinstance(language, str) and language.strip():
        settings.language = normalize_language(language)
        # LAUNCHER_LANG wins for the launch: it is an explicit, one-shot
        # choice, and a persisted setting silently overriding it would make
        # the environment variable useless.
        if not os.environ.get(LANGUAGE_ENV):
            set_language(settings.language)
    return settings


def save_settings(settings: GuiSettings, home: Optional[Path] = None) -> bool:
    """Persist *settings* atomically; returns whether the write succeeded.

    ``gui.json`` holds the whole model catalog, the presets and the library,
    and it is rewritten on every close/tray-minimise and on every tier/preset
    change. A direct ``write_text`` truncated by a kill or a power loss left a
    file that no longer parsed — and the next save then overwrote it with the
    defaults. The write goes through a temp file + ``os.replace`` (the same
    pattern ``credentials._atomic_write`` uses), and an unparseable file is
    moved aside instead of being dropped.

    The return value lets the caller tell the user the truth: it used to log
    "Settings saved." even when the write had failed.
    """
    path = (home or default_home()) / SETTINGS_FILENAME
    payload = json.dumps(
        {
            "theme": settings.theme,
            "language": settings.language,
            "auto_refresh": settings.auto_refresh,
            "minimize_on_close": settings.minimize_on_close,
            "stack": settings.stack,
            "comfy_preset": settings.comfy_preset,
            "comfy_tier": settings.comfy_tier,
            "comfy_workflows": settings.comfy_workflows,
            "comfy_sage_attention": settings.comfy_sage_attention,
            "comfy_access": settings.comfy_access,
            "comfy_ntfy_topic": settings.comfy_ntfy_topic,
            "comfy_personal_repo": settings.comfy_personal_repo,
            "comfyui_version": settings.comfyui_version,
            "comfy_auto_collect": settings.comfy_auto_collect,
            "comfy_notify_windows": settings.comfy_notify_windows,
            "comfy_notify_ntfy": settings.comfy_notify_ntfy,
            "comfy_terminate_after": settings.comfy_terminate_after,
            "comfy_outputs_dir": settings.comfy_outputs_dir,
            "diffusion_url": settings.diffusion_url,
            "video_vae_url": settings.video_vae_url,
            "audio_vae_url": settings.audio_vae_url,
            "text_encoder_url": settings.text_encoder_url,
            "tae_url": settings.tae_url,
            "upscaler_url": settings.upscaler_url,
            "frame_interp_url": settings.frame_interp_url,
            "comfy_models": settings.comfy_models,
            "comfy_presets": settings.comfy_presets,
            "comfy_annuaire": settings.comfy_annuaire,
            "train_fetch_models": settings.train_fetch_models,
            "train_lora_dir": settings.train_lora_dir,
            "runpod_gpu": settings.runpod_gpu,
        },
        indent=2,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _preserve_unreadable_settings(path)
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Could not save GUI settings: %s", exc)
        return False
    return True


def _preserve_unreadable_settings(path: Path) -> None:
    """Move a corrupt ``gui.json`` aside before overwriting it.

    ``load_settings`` silently returns defaults for a file it cannot parse, so
    the next save would destroy a partially-recoverable catalog.
    """
    if not path.exists():
        return
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        pass
    else:
        return
    backup = path.with_name(f"{path.name}.bak")
    try:
        os.replace(path, backup)
        logger.warning("Unreadable GUI settings moved to %s", backup)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Per-stack launch values from the persisted settings (no tkinter).
#
# These are the exact values the GUI's « Start » passes to `load_config`.
# They are pure functions of `GuiSettings` so a non-GUI caller can start a
# stack with the same configuration: the `--gui-settings` CLI flag and the
# MCP `forge_stack_start` tool both go through here.
# ---------------------------------------------------------------------------


def _annuaire_local_assets(entries) -> list:
    """Enabled **local** annuaire entries, as ``(kind, path, name)`` triples.

    These are the assets the launcher uploads itself once ComfyUI is up — the
    pod-side installer only understands URLs, and the pod cannot read this
    machine.
    """
    out: list = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        if not _annuaire_entry_enabled(entry):
            continue
        if _annuaire_entry_source(entry) != ANNUAIRE_SOURCE_LOCAL:
            continue
        path = str(entry.get("local_path", "")).strip()
        if not path:
            continue
        out.append(
            (
                str(entry.get("type", "lora")),
                path,
                str(entry.get("name", "")).strip(),
            )
        )
    return out


def comfy_overrides_from_settings(settings: GuiSettings) -> dict:
    """Explicit ComfyUI values a GUI-configured launch would use.

    The tier mode drives what personal material reaches the pod: "Auto"
    forces an empty preset and clears every catalog URL / category
    (standard stack + LoRAs only); "Auto + Perso" / "Perso" send the active
    user preset plus the checked catalog.
    """
    tier = settings.comfy_tier
    enabled = _enabled_urls_from_catalog(settings.comfy_models)
    overrides: dict = {
        "tier": tier,
        "workflows": settings.comfy_workflows,
        "sage_attention": settings.comfy_sage_attention,
        "access": settings.comfy_access,
        "ntfy_topic": settings.comfy_ntfy_topic.strip() or None,
        "personal_storage_repo": settings.comfy_personal_repo.strip() or None,
        "comfyui_version": settings.comfyui_version.strip() or None,
        # Launcher-side collection (never sent to the pod).
        "auto_collect": bool(settings.comfy_auto_collect),
        "notify_windows": bool(settings.comfy_notify_windows),
        "notify_ntfy": bool(settings.comfy_notify_ntfy),
        "terminate_after_generation": bool(settings.comfy_terminate_after),
        "outputs_dir": settings.comfy_outputs_dir.strip() or None,
    }
    # The active preset is a *user* preset (created in "Models & presets"):
    # H3_PRESETS stays empty and only the active user preset is sent.
    active = settings.comfy_preset.strip()
    if tier == "auto":
        overrides["preset"] = ""
        overrides["user_presets"] = {}
        overrides["custom_model_categories"] = None
        for category in COMFY_MODEL_CATEGORIES:
            overrides[f"{category}_url"] = ""
    else:
        overrides["preset"] = ""
        overrides["user_presets"] = (
            {active: settings.comfy_presets[active]}
            if active and active in settings.comfy_presets
            else {}
        )
        overrides["custom_model_categories"] = ",".join(
            category
            for category in COMFY_MODEL_CATEGORIES
            if enabled.get(category)
        )
        for category in COMFY_MODEL_CATEGORIES:
            overrides[f"{category}_url"] = ",".join(enabled.get(category, []))
    loras, nodes, workflows = _annuaire_download_lists(settings.comfy_annuaire)
    overrides["custom_loras"] = loras
    overrides["custom_nodes"] = nodes
    overrides["custom_workflows"] = workflows
    overrides["local_assets"] = _annuaire_local_assets(settings.comfy_annuaire)
    return overrides


def train_overrides_from_settings(settings: GuiSettings) -> dict:
    """Explicit training values a GUI-configured launch would use."""
    return {
        "fetch_models": settings.train_fetch_models,
        "lora_dir": settings.train_lora_dir.strip(),
    }


def stack_overrides_from_settings(settings: GuiSettings, stack: str) -> dict:
    """Per-stack explicit launch values for *stack* (``{}`` when none apply)."""
    if stack == "comfy":
        return comfy_overrides_from_settings(settings)
    if stack == "train":
        return train_overrides_from_settings(settings)
    return {}


# ---------------------------------------------------------------------------
# Log capture (thread-safe, redacted).
# ---------------------------------------------------------------------------


class GuiLogHandler(logging.Handler):
    """Forwards log records to a thread-safe queue, secrets redacted.

    The UI thread drains the queue; this handler never raises (a logging
    failure must not take the launcher down) and applies
    :func:`launcher.logging.redact` so no secret ever reaches the window.
    """

    def __init__(self, put: Callable[[tuple], None]) -> None:
        super().__init__(level=logging.INFO)
        self._put = put
        self._formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact(self.format(record))
            self._put(("log", record.levelname, message))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Status snapshot (dashboard data, read-only, degrading on errors).
# ---------------------------------------------------------------------------


def _credentials_state(store: CredentialStore) -> str:
    try:
        status = store.status()
    except CredentialStoreError:
        return "UNKNOWN"
    if not status.file_present:
        return "NOT CONFIGURED"
    return "CONFIGURED" if status.readable else "CORRUPT"


def _credentials_details(store: CredentialStore) -> dict:
    """Value-free per-secret view of the credential store (never raises).

    ``state`` is the same aggregate label as :func:`_credentials_state`; the
    per-key booleans tell the UI which of the secrets are actually set
    (``hf``/``civitai`` are the optional extra keys; ``runpod``/``template``
    are the required pair; ``comfy_template``/``train_template`` are the
    optional per-stack template IDs). No value ever leaves this function.
    """
    empty = {
        "state": "NOT CONFIGURED",
        "runpod": False,
        "template": False,
        "comfy_template": False,
        "train_template": False,
        "vnc_password": False,
        "hf": False,
        "civitai": False,
    }
    try:
        status = store.status()
    except CredentialStoreError:
        empty["state"] = "UNKNOWN"
        return empty
    if not status.file_present:
        return empty
    extra = set(status.extra_keys)
    return {
        "state": "CONFIGURED" if status.readable else "CORRUPT",
        "runpod": status.api_key_configured,
        "template": status.template_configured,
        "comfy_template": status.comfy_template_configured,
        "train_template": status.train_template_configured,
        "vnc_password": status.vnc_password_configured,
        "hf": "HF_TOKEN" in extra,
        "civitai": "CIVITAI_API_KEY" in extra,
    }


def credential_field_labels(
    details: Mapping[str, object], keys: Sequence[str]
) -> dict[str, tuple[str, str]]:
    """"set" / "not set" per field, shared by the two dialogs.

    *details* is a :func:`_credentials_details` view (or an empty mapping);
    returns ``{key: (label, color)}`` where color is "ok" for a defined
    field, "muted" otherwise, and the label reads "unknown" when the
    store state is unknown or corrupt. "Quick settings" and the
    "Credentials / API keys" dialog both render from this single
    computation so the two can never diverge."""
    state = details.get("state") if details else None
    out: dict[str, tuple[str, str]] = {}
    for key in keys:
        if not state or state in ("UNKNOWN", "CORRUPT"):
            out[key] = ("unknown", "muted")
        else:
            out[key] = ("set", "ok") if details.get(key) else ("not set", "muted")
    return out


def _pod_id_resolvable(config: Optional[Config], registry: PodRegistry) -> bool:
    if config is not None and config.runpod.pod_id:
        return True
    # Stack-aware: a comfy-only machine has no agent record — the registry
    # lookup must target the stack actually in use.
    stack = config.stack if config is not None else "agent"
    try:
        return registry.load(stack) is not None
    except Exception:
        return False


def _resolve_pod_id(config: Optional[Config], registry: PodRegistry) -> Optional[str]:
    """Return the pod id in use (environment first, then the registry)."""
    if config is not None and config.runpod.pod_id:
        return config.runpod.pod_id
    stack = config.stack if config is not None else "agent"
    try:
        record = registry.load(stack)
    except Exception:
        return None
    return record.pod_id if record is not None else None


def build_status_snapshot(
    config: Optional[Config] = None,
    *,
    config_error: Optional[str] = None,
    runpod: Optional[RunPodClient] = None,
    registry: Optional[PodRegistry] = None,
    diagnose_fn: Optional[Callable[..., InfraReport]] = None,
    credentials_store: Optional[CredentialStore] = None,
) -> dict:
    """Aggregate dashboard state for one refresh cycle.

    Mirrors the MCP ``forge_infra_diagnose`` wiring: a RunPod client is only
    built when credentials and a resolvable pod exist, and every probe degrades
    to ``UNKNOWN`` instead of raising. Never raises for a missing probe.
    """
    registry = registry or PodRegistry()
    # A key alone justifies a client: the wallet balance is account-level
    # (pod-less setups still show it); the pod probe needs a resolvable id.
    if runpod is None and (
        config is not None and config.secrets.runpod_api_key
    ):
        try:
            runpod = RunPodClient(config.secrets.runpod_api_key, timeout=10.0)
        except Exception:
            runpod = None
    report = (diagnose_fn or diagnose)(
        config,
        runpod=runpod,
        check_comfy_fn=health.check_comfy,
        check_train_fn=health.check_train,
        registry=registry,
    )
    components = {}
    for key, value in report.components.items():
        components[key] = value.status
    store = credentials_store or CredentialStore()
    return {
        "components": components,
        "overall": report.overall,
        "cause": report.cause,
        "remediation": report.remediation,
        "url": report.url,
        "credentials": _credentials_state(store),
        "credentials_detail": _credentials_details(store),
        "recovery_enabled": bool(
            config is not None and config.recover.mode == "confirm"
        ),
        "preset": config.comfy.preset if config is not None else None,
        "tier": config.comfy.tier if config is not None else None,
        "image": config.train.image_name if config is not None else None,
        "billing": _pod_billing_info(config, runpod, registry),
        "config_error": config_error,
        "stack": config.stack if config is not None else "comfy",
        "comfy": _comfy_dashboard_info(config, runpod, registry)
        if config is not None and config.stack == "comfy"
        else None,
    }


def build_active_stacks_snapshot(
    config: Optional[Config] = None,
    runpod: Optional[RunPodClient] = None,
    registry: Optional[PodRegistry] = None,
    state=None,
) -> dict:
    """Per-stack dashboard rows for every **active** workload stack.

    Several stacks can run at once, each on its own pod, tunnel and lifecycle
    (a text agent plus ComfyUI and/or LoRA training). This returns one row per
    active stack so the dashboard can show and drive them independently.

    Bounded by design: at most one ``list_pods()`` call (to read every pod's
    status and hourly rate in one request) and no per-stack ``get_pod`` — a
    probe failure degrades to ``UNKNOWN`` rather than raising.
    """
    registry = registry or PodRegistry()
    try:
        stacks = orchestrator.active_stacks(registry=registry, state=state)
    except Exception:  # noqa: BLE001 - a broken registry means "no rows"
        return {"stacks": [], "cost_per_hour_total": None}

    pods: dict = {}
    if runpod is not None:
        try:
            pods = {pod.id: pod for pod in runpod.list_pods()}
        except Exception:  # noqa: BLE001 - status/rate are best-effort
            pods = {}

    rows: list = []
    total = 0.0
    have_rate = False
    for stack in stacks:
        try:
            cfg = (
                config
                if config is not None and config.stack == stack
                else load_config(stack=stack, store=CredentialStore())
            )
        except Exception:  # noqa: BLE001 - one bad stack must not hide the rest
            cfg = None
        summary: dict = {}
        if cfg is not None:
            try:
                summary = orchestrator.operational_status(
                    config=cfg,
                    runpod=None,
                    tunnels=None,
                    state=state,
                    registry=registry,
                )
            except Exception:  # noqa: BLE001
                summary = {}
        pod_id = summary.get("pod") or "NONE"
        pod = pods.get(pod_id)
        runpod_status = "NONE"
        rate = None
        if pod_id != "NONE":
            runpod_status = getattr(pod, "status", None) or "UNKNOWN"
            rate = getattr(pod, "cost_per_hour", None)
        if rate:
            total += float(rate)
            have_rate = True
        rows.append(
            {
                "stack": stack,
                "label": STACK_LABELS.get(stack, stack),
                "pod": pod_id,
                "runpod": runpod_status,
                "tunnel": summary.get("tunnel", "UNKNOWN"),
                "ready": summary.get("vllm", "UNKNOWN"),
                "url": summary.get("url", "—"),
                "cost_per_hour": f"{float(rate):.2f} $/h" if rate else None,
                "running": str(runpod_status).upper() == "RUNNING",
            }
        )
    return {
        "stacks": rows,
        "cost_per_hour_total": f"{total:.2f} $/h" if have_rate else None,
    }


def _comfy_dashboard_info(
    config: Config, runpod: Optional[RunPodClient], registry: PodRegistry
) -> dict:
    """Queue / VRAM / cost-per-hour / URL for the ComfyUI pod (all degrading)."""
    info = {
        "preset": config.comfy.preset,
        "access": config.comfy.access,
        "queue": None,
        "vram": None,
        "cost_per_hour": None,
        "url": None,
    }
    base_url = config.comfy_base_url()
    if config.comfy.access == "tunnel":
        info["url"] = base_url
    else:
        try:
            record = registry.load("comfy")
        except Exception:
            record = None
        public = None
        if runpod is not None:
            pod_id = None
            if config.runpod.pod_id:
                pod_id = config.runpod.pod_id
            elif record is not None:
                pod_id = record.pod_id
            if pod_id is not None:
                try:
                    pod = runpod.get_pod(pod_id)
                except Exception:
                    pod = None
                public = getattr(pod, "public_ip_address", None) if pod else None
        if public:
            info["url"] = f"https://{public}"
    try:
        if health.check_comfy(base_url, timeout=3.0):
            stats = health.get_comfy_stats(base_url, timeout=3.0)
            queue = health.get_comfy_queue(base_url, timeout=3.0)
            if isinstance(queue, dict) and queue.get("running") is not None:
                info["queue"] = (
                    f"{queue['running']} en cours / "
                    f"{queue.get('pending', 0)} en attente"
                )
            if isinstance(stats, dict):
                info["vram"] = health.comfy_vram_text(stats) or None
    except Exception:
        pass
    if runpod is not None:
        try:
            record = registry.load("comfy")
            if record is not None and record.gpu_id:
                for item in runpod.get_gpu_types():
                    if not isinstance(item, dict):
                        continue
                    gpu = item.get("gpu") or item
                    if gpu.get("id") == record.gpu_id:
                        price = gpu.get("price") or {}
                        value = price.get("secure") or price.get("community")
                        if value:
                            info["cost_per_hour"] = f"{value:.2f} $/h"
                        break
        except Exception:
            pass
    return info


def _fmt_money(value: float) -> str:
    return f"${value:.2f}"


def _fmt_duration(total_seconds: float) -> str:
    if total_seconds < 3600:
        return f"{max(1, int(total_seconds // 60))} min"
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    return f"{hours} h {minutes:02d} min"


def _pod_billing_info(
    config: Optional[Config],
    runpod: Optional[RunPodClient],
    registry: PodRegistry,
    now: Optional[float] = None,
) -> Optional[dict]:
    """Pod spend so far + RunPod wallet balance (all degrading, never raises).

    Returns a display-ready dict, or ``None`` when nothing could be fetched.
    Cost is an estimate: elapsed time (pod ``startedAt``/``createdAt``, then
    the registry record) times the hourly rate (pod ``cost``, then the
    wallet's current spend/hour).
    """
    if runpod is None:
        return None
    now = now if now is not None else time.time()

    pod_id = _resolve_pod_id(config, registry)
    pod = None
    if pod_id is not None:
        try:
            pod = runpod.get_pod(pod_id)
        except Exception:
            pod = None

    wallet = None
    try:
        wallet = runpod.get_wallet()
    except Exception:
        wallet = None

    if pod is None and wallet is None:
        return None

    start = None
    if pod is not None:
        start = pod.started_at or pod.created_at
    if start is None:
        stack = config.stack if config is not None else "agent"
        try:
            record = registry.load(stack)
        except Exception:
            record = None
        if record is not None:
            start = record.created_at

    running = pod is not None and pod.is_running
    rate = None
    if pod is not None:
        rate = pod.cost_per_hour
    if rate is None and wallet is not None:
        rate = wallet.spend_per_hour

    elapsed_s: Optional[float] = None
    cost: Optional[float] = None
    if start is not None:
        elapsed_s = max(0.0, now - start)
        if rate is not None and rate > 0:
            cost = (elapsed_s / 3600.0) * rate

    balance = wallet.balance if wallet is not None else None
    hours_left: Optional[float] = None
    if balance is not None and rate is not None and rate > 0:
        hours_left = balance / rate

    hours_left_text = None
    if hours_left is not None:
        hours_left_text = (
            f"~{int(hours_left)} h"
            if hours_left >= 1
            else f"~{max(1, int(hours_left * 60))} min"
        )

    return {
        "running": running,
        "has_pod": pod is not None,
        "has_wallet": wallet is not None,
        "elapsed": _fmt_duration(elapsed_s) if elapsed_s is not None else None,
        "cost": _fmt_money(cost) if cost is not None else None,
        "rate": f"${rate:.2f}/h" if rate is not None and rate > 0 else None,
        "balance": _fmt_money(balance) if balance is not None else None,
        "hours_left": hours_left,
        "hours_left_text": hours_left_text,
    }


# ---------------------------------------------------------------------------
# Actions (each returns a human-readable French summary; long-running work).
# ---------------------------------------------------------------------------


#: Delay before the first-run wizard opens: long enough for the window to be
#: painted and the first status refresh to land, short enough that a brand-new
#: user is not left staring at a grey dashboard.
FIRST_RUN_WIZARD_DELAY_MS = 1200


def first_run_needs_credentials(config: Optional[Config]) -> bool:
    """True when nothing is configured yet and the wizard should offer to help.

    "Nothing" means: no RunPod API key from the environment *and* none in the
    secure store — the two places :func:`launcher.config.load_config` looks.
    A user who set ``RUNPOD_API_KEY`` is never interrupted.
    """
    if config is not None and config.secrets.runpod_api_key:
        return False
    if os.environ.get("RUNPOD_API_KEY"):
        return False
    try:
        status = CredentialStore().status()
    except CredentialStoreError:
        # An unreadable store is exactly when the wizard is useful.
        return True
    return not status.api_key_configured


def action_start(
    stack: str = "comfy",
    comfy: Optional[Mapping[str, object]] = None,
    train: Optional[Mapping[str, object]] = None,
    gpu: Optional[str] = None,
    on_no_capacity: Optional[Callable[[str], bool]] = None,
) -> str:
    """Run the full startup sequence for *stack* (comfy or train).

    ``comfy`` (comfy stack only) carries the explicit ComfyUI values from the
    GUI; ``train`` (train stack only) carries the model families to
    pre-download and the local LoRA collection folder. ``on_no_capacity``
    (optional) is called with the pod id when starting an adopted pod fails
    for lack of a free GPU; returning True terminates that pod and provisions
    a fresh one.
    """
    if stack == "train":
        config = load_config(
            stack="train", train=dict(train or {}), gpu=gpu,
            store=CredentialStore(),
        )
        orchestrator.start(config, on_no_capacity=on_no_capacity)
        return (
            "Start finished: training pod and SSH tunnel "
            f"up (image {config.train.image_tag}) — desktop: "
            f"{config.train_base_url()} (local tunnel), files: "
            f"{config.train_files_url()}."
        )
    config = load_config(
        stack="comfy", comfy=dict(comfy or {}), gpu=gpu,
        store=CredentialStore(),
    )
    orchestrator.start(config, on_no_capacity=on_no_capacity)
    if config.comfy.access == "direct":
        where = (
            "Pod public URL (direct mode — ComfyUI without "
            "authentication, explicit option)"
        )
    else:
        where = f"{config.comfy_base_url()} (local tunnel)"
    return (
        f"Start finished: ComfyUI pod, SSH tunnel and ComfyUI "
        f"up (preset {config.comfy.preset}, "
        f"tier {config.comfy.tier}) — interface: {where}."
    )


POD_OUTCOME_LABELS = {
    "stopped": "RunPod pod stopped (disk kept).",
    "terminated": "RunPod pod terminated (deleted).",
    "cleared": "Pod already gone; record cleared.",
    "already_stopped": "Pod already stopped (not billing).",
    "skipped": "Pod: no stop action available.",
    "unavailable": "Pod: RunPod API unreachable.",
}

#: Human wording for the orchestrator's English stop/provision failures.
#: Matched on the first fragment found in the text; anything unmatched keeps
#: its technical detail behind a plain sentence.
_STOP_ERRORS = (
    (
        "No registered pod to terminate",
        (
            "No active pod to terminate: nothing was deleted. Check the "
            "RunPod console if a pod is still running, or press Start to "
            "provision one."
        ),
    ),
    (
        "No registered",
        (
            "No pod registered for this stack: nothing to stop. If a pod is "
            "still running, terminate it from the RunPod console."
        ),
    ),
    (
        "RUNPOD_POD_ID is set",
        (
            "RUNPOD_POD_ID is set: the launcher never terminates an "
            "explicitly configured pod. Clear RUNPOD_POD_ID, or terminate "
            "the pod from the RunPod console."
        ),
    ),
    (
        "RUNPOD_API_KEY is not set",
        (
            "RunPod API key missing: cannot terminate the pod. "
            "Set the key in Settings, then try again."
        ),
    ),
    ("Failed to terminate pod", "Failed to terminate the pod"),
    ("Failed to stop pod", "Failed to stop the pod"),
)

#: Same idea for the messages a start attempt can surface.
_START_ERRORS = (
    (
        "Pod not found (HTTP 404)",
        (
            "Pod not found on RunPod (HTTP 404): it was probably deleted. "
            "Press Start to provision a new one."
        ),
    ),
    (
        "Pod has no usable SSH endpoint",
        (
            "The pod exposes no usable SSH endpoint for port forwarding. "
            "Configure it to expose port 22/tcp (SSH) or a direct SSH "
            "endpoint, and check that it is RUNNING."
        ),
    ),
    (
        "SSH tunnel failed authentication",
        (
            "SSH authentication failed: the pod rejects the SSH key (or the "
            "host key). Check the configured key and the endpoint."
        ),
    ),
    (
        "SSH tunnel did not become alive",
        (
            "The SSH tunnel never answered: the pod is probably still pulling "
            "its Docker image. Try again in a few minutes."
        ),
    ),
    (
        "did not become RUNNING within",
        "The pod did not reach RUNNING within the time limit.",
    ),
    ("entered ERROR state", "The pod entered ERROR state on RunPod."),
    ("entered EXITED state", "The pod entered EXITED state on RunPod."),
    (
        "did not receive a public SSH",
        "The pod never received a public SSH mapping (22/tcp) in time.",
    ),
    (
        "is occupied by a process not started by the launcher",
        (
            "The port is held by a process the launcher did not start. Close "
            "that process or change the configured port."
        ),
    ),
    (
        "RunPod API key must not be empty",
        "RunPod API key is empty: set it in Settings.",
    ),
    (
        "is unreachable",
        (
            "The registered pod cannot be reached through the RunPod API: "
            "retry when the API is available."
        ),
    ),
)


#: The sentences above, for the idempotency guard in
#: :func:`translate_action_error`.
_ERROR_TEXTS = frozenset(
    text for _fragment, text in _STOP_ERRORS + _START_ERRORS
)
#: Messages our own stop path raises already translated.
_ALREADY_TRANSLATED_PREFIXES = (
    "No pod ",
    "No active ",
    # "Open …" tunnel check (see _open_comfy_checked): already translated.
    "Tunnel ",
    "Cannot open the browser",
    "ComfyUI configuration not found",
)


def translate_action_error(text: str) -> str:
    """Human wording for an orchestrator/API error message.

    The launcher's internal errors are written in English (they are also
    logged and used by the CLI). Known messages are matched and returned as
    the sentence below; anything else keeps its technical detail appended to
    a plain lead-in.

    Idempotent: text that is already one of the sentences below (or that our
    own stop path produced) is returned unchanged, so a message can be
    translated at several layers without being wrapped twice.
    """
    message = (text or "").strip()
    for fragment, english in _STOP_ERRORS + _START_ERRORS:
        if fragment.lower() in message.lower():
            return english
    if message in _ERROR_TEXTS:
        return message
    if message.startswith(_ALREADY_TRANSLATED_PREFIXES):
        return message
    if not message:
        return "The operation failed (no detail provided)."
    return f"The operation failed. Technical detail: {message}"


def resync_registered_pod(
    config: Optional[Config],
    runpod,
    registry: PodRegistry,
    stack: str,
) -> tuple[Optional[str], Optional[str]]:
    """Re-align the registry record for *stack* with the account's real pod.

    The registry is local state and it drifts: a retry sequence can clear the
    record on a stale-id 404 while the pod is actually RUNNING (or a pod can
    be created outside this session). "Terminate the pod" used to trust that
    record blindly — with no record it reported nothing to do, and with a
    stale id it terminated a pod that no longer existed and reported success
    while the real pod kept billing.

    Returns ``(pod_id, note)``: the id the registry now holds (None when no
    pod could be resolved) and a French journal line, or ``(id, None)`` when
    the record was already correct.
    """
    if runpod is None:
        return None, None
    note: Optional[str] = None
    record = registry.load(stack)
    if record is not None:
        # Never trust a single 404: a pod created seconds ago can still be
        # missing from the API while it is alive and billing (that 404 used to
        # clear the record and orphan the pod — the "impossible to terminate"
        # bug). ``pod_presence`` retries for a fresh record and falls back to
        # the list endpoint.
        pod, presence = orchestrator.pod_presence(
            runpod, record.pod_id, created_at=record.created_at
        )
        if presence == "unknown":
            # Unverifiable (network/auth): keep the record and let the normal
            # path report the failure.
            return record.pod_id, None
        if presence == "found":
            status = str(getattr(pod, "status", "") or "").upper()
            if status not in ("", "TERMINATED"):
                return record.pod_id, None
            registry.clear(stack)
            note = (
                f"Pod registered {record.pod_id} already finished: looking for "
                "the actually active pod…"
            )
        else:
            registry.clear(stack)
            note = (
                f"Pod registered {record.pod_id} not found on RunPod: "
                "looking for the actually active pod…"
            )
    else:
        note = (
            "No pod registered for this stack: looking for the actually "
            "active pod…"
        )

    try:
        pods = runpod.list_pods()
    except Exception as exc:  # noqa: BLE001 - inventory is best effort
        return None, f"{note} RunPod inventory unavailable ({exc})."
    candidates = [
        pod for pod in pods
        if _detect_pod_stack(pod) == stack
        and str(getattr(pod, "status", "") or "").upper() != "TERMINATED"
    ]
    if not candidates:
        return None, f"{note} no active pod found for this stack."
    pod = max(
        candidates, key=lambda p: getattr(p, "created_at", 0.0) or 0.0
    )
    try:
        registry.save(
            PodRecord(
                pod_id=pod.id,
                name=getattr(pod, "name", "") or "",
                created_at=getattr(pod, "created_at", None) or time.time(),
                gpu_id=getattr(pod, "gpu_id", None),
                gpu_count=1,
                data_center_id=getattr(pod, "data_center_id", None),
                stack=stack,
            ),
            stack=stack,
        )
    except Exception as exc:  # noqa: BLE001 - the id is what matters
        return pod.id, f"{note} pod {pod.id} found (record failed: {exc})."
    return pod.id, f"{note} pod {pod.id} (\"{getattr(pod, 'name', '')}\") registered."

_GOOD_STOP_OUTCOMES = ("stopped", "terminated", "cleared", "already_stopped")


def action_stop_outcome(
    terminate: bool = False, stack: Optional[str] = None
) -> tuple:
    """Clean shutdown (local processes + registered pod(s)) with a verdict.

    Returns ``(ok, message)``. ``ok`` is False whenever a registered pod may
    still be running and billing: missing API key, unreachable API, or a pod
    offering no stop action while not stopped.
    """
    config: Optional[Config] = None
    try:
        config = load_config(stack=stack, store=CredentialStore())
    except ConfigError:
        if terminate:
            raise
    registry = PodRegistry()
    runpod = None
    if config is not None and config.secrets.runpod_api_key:
        try:
            runpod = RunPodClient(config.secrets.runpod_api_key, timeout=10.0)
        except Exception:
            runpod = None
    parts = [
        "Local processes stopped (SSH tunnel)."
        if stack is None
        else (
            f"{STACK_LABELS.get(stack, stack)} stack stopped "
            "(its local processes and its pod only)."
        )
    ]
    # Before terminating, re-align the local record with the account's real
    # pod: a stale/cleared record used to make "Terminate the pod" either a
    # no-op or a silent success while the pod kept running (and billing).
    if terminate and stack is not None and runpod is not None:
        pod_id, note = resync_registered_pod(config, runpod, registry, stack)
        if note:
            logger.info("%s", note)
            parts.append(note)
        if pod_id is None:
            raise orchestrator.OrchestrationError(
                f"No active {STACK_LABELS.get(stack, stack)} pod to "
                "terminate: nothing was deleted. Check the RunPod console "
                "if a pod is still running."
            )
    # Read the records AFTER the resync: it may have just written one.
    records = registry.all()
    if stack is not None:
        targets = [stack] if stack in records else []
    else:
        targets = [s for s in STACKS if s in records]
    try:
        phase = orchestrator.stop(
            runpod=runpod,
            registry=registry,
            config=config,
            terminate=terminate,
            stack=stack,
        )
    except orchestrator.OrchestrationError as exc:
        raise orchestrator.OrchestrationError(translate_action_error(str(exc))) from exc
    ok = True
    if targets and runpod is None:
        ok = False
        parts.append(
            "⚠ pod(s) registered but the RunPod API key is missing: "
            "the pod(s) were NOT stopped."
        )
    if phase:
        for name, outcome in phase.items():
            parts.append(
                f"Pod {name}: "
                + POD_OUTCOME_LABELS.get(outcome, f"{outcome}")
            )
            if outcome not in _GOOD_STOP_OUTCOMES:
                ok = False
        if not ok:
            parts.append("⚠ a pod may still be billing.")
    elif targets and runpod is not None:
        ok = False
        parts.append("⚠ the pod could not be stopped.")
    return ok, " ".join(parts)


def action_stop(terminate: bool = False, stack: Optional[str] = None) -> str:
    """Clean shutdown (local processes + the stack's registered pod).

    ``stack`` (``"agent"``/``"comfy"``) stops only that stack's pod; when
    omitted, every registered pod is stopped (mirrors the CLI).
    """
    return action_stop_outcome(terminate=terminate, stack=stack)[1]


def action_doctor(config: Optional[Config]) -> str:
    """Run the non-destructive diagnostics and format the report."""
    if config is None:
        return "Doctor unavailable: the configuration is invalid."
    results = run_diagnostics(
        config,
        runpod=_build_runpod(config),
        credentials=CredentialStore(),
        registry=PodRegistry(),
    )
    lines = ["MiniMax H3 Launcher — diagnostics", "-------------------------"]
    for result in results:
        lines.append(f"[{result.status:<5}] {result.name:<20} {result.detail}")
    return "\n".join(lines)


def _build_runpod(config: Optional[Config]) -> Optional[RunPodClient]:
    if config is None or not config.secrets.runpod_api_key:
        return None
    try:
        return RunPodClient(config.secrets.runpod_api_key, timeout=10.0)
    except Exception:
        return None


def build_recovery_engine(
    config: Config, authorizer: Optional[RecoveryAuthorizer] = None
) -> RecoveryEngine:
    """Production wiring for the recovery engine (same lifecycle managers).

    The pod registry is supplied so launcher-created pods (without
    ``RUNPOD_POD_ID``) are recoverable from the GUI. The shared
    :class:`FileRecoveryLock` (same file as the MCP wiring) keeps a
    GUI-triggered and a tool-triggered recovery from running simultaneously.

    *authorizer* defaults to the operator gate (``LAUNCHER_INFRA_RECOVERY``);
    callers that run the one non-mutating action (see
    :func:`action_reconnect_tunnel`) pass their own.
    """
    from .infra_recover import FileRecoveryLock
    from .portctl import is_port_open
    from .recovery_audit import RecoveryAudit
    from .runtime_state import RuntimeState
    from .tunnel import TunnelManager, is_port_free

    registry = PodRegistry()
    runpod = None
    if config.secrets.runpod_api_key and _pod_id_resolvable(config, registry):
        try:
            runpod = RunPodClient(config.secrets.runpod_api_key, timeout=10.0)
        except Exception:
            runpod = None
    state = RuntimeState()
    return RecoveryEngine(
        config=config,
        authorizer=authorizer or RecoveryAuthorizer(mode=config.recover.mode),
        runpod=runpod,
        tunnels=TunnelManager(state=state),
        state=state,
        lock=FileRecoveryLock(),
        is_port_free=is_port_free,
        is_port_open=is_port_open,
        audit=RecoveryAudit(),
        registry=registry,
        check_comfy_fn=health.check_comfy,
        check_train_fn=health.check_train,
    )


def action_recover(config: Config, action_name: str) -> RecoveryResult:
    """Perform one recovery action with per-call confirmation (the GUI asks)."""
    engine = build_recovery_engine(config)
    return engine.recover(action=action_name, confirm=True, reason="gui")


def action_reconnect_tunnel(config: Config) -> RecoveryResult:
    """(Re)connect the stack's SSH tunnel — the one non-mutating action.

    ``reconnect_ssh`` requires the pod to be RUNNING and only re-resolves the
    pod's *current* SSH endpoint before (re)starting the local tunnel: no pod
    is ever created, started or stopped. The operator gate exists to protect
    those pod lifecycle actions, so it must not block the repair the GUI runs
    on its own before opening a tunneled page (« Ouvrir ComfyUI »).
    """
    engine = build_recovery_engine(
        config, authorizer=RecoveryAuthorizer(mode="confirm")
    )
    return engine.recover(action="reconnect_ssh", confirm=True, reason="open-surface")


def format_recovery_result(result: RecoveryResult) -> str:
    outcome = "succeeded" if result.ok else "failed"
    return f"Repair {result.action}: {outcome}.\n{result.render()}"


def action_comfy_tunnel_stop(config: Config, state=None) -> str:
    """Stop only the local ComfyUI SSH tunnel (comfy stack, tunnel access).

    The pod and the ComfyUI server running on it are left untouched: only
    the local ssh process recorded in the runtime state is terminated and
    its record cleared. ``state`` is injectable for tests; in production
    the default runtime registry is used.
    """
    from . import runtime_state

    state = state or runtime_state.RuntimeState()
    entry = state.load().get("tunnels:comfy")
    if entry is None or not runtime_state.pid_is_alive(entry.pid):
        return "The local ComfyUI tunnel is not running."
    try:
        runtime_state.terminate_pid(entry.pid)
    except OSError:
        pass
    entries = state.load()
    entries.pop("tunnels:comfy", None)
    state.save(entries)
    return "Local ComfyUI tunnel stopped (pod and ComfyUI on the pod unchanged)."


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def format_countdown(total_seconds: int) -> str:
    """Format a non-negative second count as ``HH:MM:SS``."""
    total_seconds = max(0, int(total_seconds))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _first_line(text: str) -> str:
    """First line of *text* — the status line element must stay single-line."""
    lines = text.splitlines()
    return lines[0] if lines else ""


def power_action_command(mode: str) -> Optional[list[str]]:
    """Return the Windows command for *mode*, or None for ``none``."""
    if mode == "shutdown":
        return ["shutdown", "/s", "/t", "0"]
    if mode == "sleep":
        return ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
    return None


def run_power_action(mode: str) -> str:
    """Run the Windows power action; returns a French summary (never raises)."""
    cmd = power_action_command(mode)
    if cmd is None:
        return "No Windows action."
    try:
        winproc.run(cmd, check=False, timeout=15)
    except Exception as exc:  # noqa: BLE001 - surfaced in the journal
        return f"Windows action failed ({mode}): {exc}"
    return "The machine is shutting down." if mode == "shutdown" else "The machine is going to sleep."


def env_target_dir() -> Path:
    """Directory where a new ``.env`` should be created (exe dir or repo root)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _component_color_key(status: str) -> str:
    normalized = status.strip().upper()
    if normalized in ("RUNNING", "CONNECTED", "READY", "VALID", "CONFIGURED"):
        return "ok"
    if normalized in ("STARTING", "PROVISIONING", "DEGRADED"):
        return "warn"
    if (
        normalized.startswith("MISSING")
        or normalized in ("ERROR", "FAILED", "NOT READY", "UNAVAILABLE")
        or "(MISSING)" in normalized
    ):
        return "error"
    return "muted"


# ---------------------------------------------------------------------------
# Startup progress: the start flow is a chain of network steps (pod create,
# RunPod RUNNING, SSH mapping, the stack's service). The orchestrator logs
# each phase; these tables map those existing lines onto step states — pure
# presentation, no orchestrator change.
# ---------------------------------------------------------------------------
PROGRESS_STEPS = {
    "comfy": (
        "Creating the pod",
        "ComfyUI ready",
    ),
    "train": (
        "Creating the pod",
        "Desktop ready",
    ),
}

PROGRESS_SYMBOLS = {"pending": "○", "active": "⟳", "done": "✓"}

# (substring(s), step index/indices, completed?) — first match wins.
_PROGRESS_PATTERNS = {
    "comfy": (
        (("Creating RunPod",), (0,), False),
        (("already provisioned pod", "Checking RunPod", "Pod is RUNNING"), (0,), True),
        (("Waiting for ComfyUI readiness", "Establishing SSH tunnel"), (1,), False),
        (("ComfyUI ready",), (1,), True),
    ),
    "train": (
        (("Creating RunPod",), (0,), False),
        (("already provisioned pod", "Checking RunPod", "Pod is RUNNING"), (0,), True),
        (("Waiting for the training desktop", "Establishing SSH tunnel"), (1,), False),
        (("Training desktop ready",), (1,), True),
    ),
}


def map_progress_event(
    stack: str, line: str
) -> Optional[tuple[tuple[int, ...], bool]]:
    """Map one journal line onto a progress event: (step indices, done?)."""
    for substrings, indices, done in _PROGRESS_PATTERNS.get(stack, ()):
        if any(sub in line for sub in substrings):
            return indices, done
    return None


def apply_progress_event(states: list[str], indices, done: bool) -> None:
    """Update step states in place. A completed step cascades done to every
    earlier step; a done step is never demoted back to active."""
    if done:
        for index in range(max(indices) + 1):
            if states[index] != "done":
                states[index] = "done"
    else:
        for index in indices:
            if states[index] != "done":
                states[index] = "active"


def overall_popover_lines(snapshot: dict) -> list[str]:
    """Lines shown in the global-status popover (state + cause + remediation)."""
    if not isinstance(snapshot, dict):
        return ["State: UNKNOWN"]
    if snapshot.get("config_error"):
        return [
            "State: INVALID CONFIGURATION",
            f"Configuration : {snapshot['config_error']}",
        ]
    lines = [f"State: {snapshot.get('overall', 'UNKNOWN')}"]
    if snapshot.get("cause"):
        lines.append(f"Cause: {snapshot['cause']}")
    if snapshot.get("remediation"):
        lines.append(f"Remediation: {snapshot['remediation']}")
    return lines


_STACK_POD_NAME_PREFIX = {
    "comfy": "minimax-launcher-comfy-",
    "train": "minimax-launcher-train-",
}


def _detect_pod_stack(pod) -> Optional[str]:
    """Best-effort stack of a RunPod pod (launcher naming, then its env)."""
    name = getattr(pod, "name", "") or ""
    for stack in STACKS:
        prefix = _STACK_POD_NAME_PREFIX.get(stack)
        if prefix and name.startswith(prefix):
            return stack
    env = getattr(pod, "env", None) or {}
    if "H3_PRESETS" in env or "COMFYUI_PORT" in env:
        return "comfy"
    if "FIZGIG_REF" in env or "TRAIN_LOCAL_PORT" in env:
        return "train"
    return None


def scan_open_stacks(
    config,
    runpod,
    registry: PodRegistry,
    current_stack: str,
) -> tuple[list[dict], Optional[str]]:
    """Find RUNNING Forge-managed pods on the account and re-adopt the
    current stack's when this session has no registered pod for it.

    "Another session" case: the pod may have been opened by an earlier
    launcher instance, the CLI, or another machine sharing the account.
    Adoption is bookkeeping only (writes the stack's registry record); it
    performs no pod action. Returns ``(found, adopted_pod_id)`` where each
    found entry is ``{stack, pod_id, name, created_at, adopted}``; both
    degrade to ``([], None)`` on any failure.
    """
    if runpod is None or config is None:
        return [], None
    stack = current_stack or "comfy"
    try:
        if registry.load(stack) is not None:
            return [], None
    except Exception:
        return [], None
    try:
        pods = runpod.list_pods()
    except Exception:
        return [], None
    found: list[dict] = []
    adopt = None
    for pod in pods:
        if str(getattr(pod, "status", "")).upper() != "RUNNING":
            continue
        pod_stack = _detect_pod_stack(pod)
        if pod_stack is None:
            continue
        created = getattr(pod, "created_at", None)
        found.append(
            {
                "stack": pod_stack,
                "pod_id": pod.id,
                "name": pod.name,
                "created_at": created,
            }
        )
        if pod_stack == stack and (
            adopt is None
            or (created or 0.0) > (getattr(adopt, "created_at", None) or 0.0)
        ):
            adopt = pod
    if adopt is not None:
        try:
            registry.save(
                PodRecord(
                    pod_id=adopt.id,
                    name=adopt.name,
                    created_at=getattr(adopt, "created_at", None) or time.time(),
                    gpu_id=getattr(adopt, "gpu_id", None),
                    gpu_count=1,
                    data_center_id=getattr(adopt, "data_center_id", None),
                    stack=stack,
                ),
                stack=stack,
            )
        except Exception:
            for entry in found:
                entry["adopted"] = False
            return found, None
        for entry in found:
            entry["adopted"] = entry["pod_id"] == adopt.id
        return found, adopt.id
    for entry in found:
        entry["adopted"] = False
    return found, None


def is_idle_state(snapshot: dict) -> bool:
    """True when no instance is active: nothing to show row by row, so the
    dashboard displays an explicit empty state instead of UNKNOWN/STOPPED
    rows that mean nothing to someone discovering the screen."""
    if not isinstance(snapshot, dict) or snapshot.get("config_error"):
        return False
    components = snapshot.get("components") or {}
    runpod_status = str(components.get("runpod", "UNKNOWN")).upper()
    if runpod_status in ("RUNNING", "STARTING", "PROVISIONING"):
        return False
    comfy = snapshot.get("comfy")
    if isinstance(comfy, dict) and (comfy.get("queue") or comfy.get("vram")):
        return False
    return True


def build_health_table_rows(
    snapshot: dict, *, active_preset: str = "",
) -> list[tuple[str, str, str, str]]:
    """(name, state, info, color_key) rows for the stack health table.

    One row per component, for the ACTIVE stack only: the comfy stack shows
    the ComfyUI row and its preset, the train stack the desktop row and the
    image. Runtime facts of the pod (queue, VRAM, cost) live in the "Pod &
    GPU" card, not here.

    *active_preset* is the preset the launcher will actually send (a GUI
    setting): it wins over the pod config's own ``preset``, and the row is
    dropped entirely when neither is known — an empty "—" row tells nothing.
    """
    if not isinstance(snapshot, dict):
        return []
    components = snapshot.get("components") or {}
    stack = snapshot.get("stack") or "comfy"
    rows: list[tuple[str, str, str, str]] = []
    for key in COMPONENT_ORDER:
        status = str(components.get(key, "UNKNOWN"))
        color = _component_color_key(status)
        if stack == "train" and key == "model":
            # The "model" row carries the image name for this stack: it is
            # descriptive, not a health signal (same treatment as the comfy
            # preset row).
            rows.append(("Image", status, "—", "ok"))
            continue
        if stack == "comfy" and key == "model":
            preset = active_preset.strip() or str(
                (snapshot.get("comfy") or {}).get("preset") or ""
            ).strip()
            if preset:
                rows.append(("Preset", preset, "—", "ok"))
            continue
        name = COMPONENT_LABELS[key]
        info = ""
        if key == "vllm" and stack == "comfy":
            name = "ComfyUI"
            info = (snapshot.get("comfy") or {}).get("url") or ""
        if key == "vllm" and stack == "train":
            name = "Desktop"
            info = snapshot.get("url") or ""
        rows.append((name, status, info, color))
    return rows


class ForgeApp:
    """The MiniMax H3 Launcher tkinter application."""

    def __init__(
        self,
        root: "tk.Tk",
        *,
        settings: Optional[GuiSettings] = None,
        env_file: Optional[Path] = None,
        refresh_interval_ms: int = REFRESH_INTERVAL_MS,
    ) -> None:
        self.root = root
        self.settings = settings or load_settings()
        self.env_file = env_file
        self._queue: queue.Queue = queue.Queue()
        self._log_handler: Optional[GuiLogHandler] = None
        self._busy = False
        self._refresh_in_progress = False
        self._last_refresh = 0.0
        self._refresh_interval_ms = refresh_interval_ms
        self._last_snapshot: Optional[dict] = None
        self._last_config: Optional[Config] = None
        self._first_run_wizard_shown = False
        self._refresh_thread = None
        self._release_thread = None
        self._action_thread = None
        self._timer_thread = None
        self._power_thread = None
        self._size_thread = None
        self._tray = None
        self._tray_thread: Optional[threading.Thread] = None
        self._action_buttons: list = []
        self._recovery_buttons: list = []
        self._recolor: list = []
        self._scrollables: list = []
        # Bundled icons: (name, size) -> PhotoImage. Tk drops an image the
        # moment its last Python reference goes away, so the dict doubles as
        # the keep-alive store.
        self._icon_refs: dict = {}
        # Model URL -> size in bytes (probed once per session, see
        # ``_start_model_size_check``); unknown URLs are simply absent.
        self._model_sizes: dict = {}
        self._size_rebuild_after: Optional[str] = None
        self._tb_panel_widgets: set = set()
        self._fonts: dict = {}
        self._tooltip_texts: dict = {}
        self._tooltip_popover = None
        self._tooltip_popover_label = None
        self._notify_after: Optional[str] = None
        self._timer_remaining = 0
        self._timer_deadline = 0.0
        self._timer_after_id: Optional[str] = None
        self._icon_image = None

        self._theme_choice = tk.StringVar(root, value="Dark")
        self._auto_refresh = tk.BooleanVar(root, value=self.settings.auto_refresh)
        self._minimize_on_close = tk.BooleanVar(
            root, value=self.settings.minimize_on_close
        )
        self._stack = tk.StringVar(root, value=self.settings.stack)
        # The footer "Mode" picks the workload stack. ``_stack`` stays the
        # effective stack everything else reads, and is recomputed from
        # ``_mode`` so no caller can leave the two out of step.
        self._mode = tk.StringVar(
            root,
            value=(
                self.settings.stack
                if self.settings.stack in ("comfy", "train")
                else "comfy"
            ),
        )
        self._mode.trace_add("write", lambda *_: self._refresh_effective_stack())
        self._comfy_preset = tk.StringVar(root, value=self.settings.comfy_preset)
        self._comfy_preset_ui = tk.StringVar(
            root, value=_preset_to_display(self.settings.comfy_preset)
        )
        self._comfy_tier = tk.StringVar(root, value=self.settings.comfy_tier)
        self._comfy_tier_ui = tk.StringVar(
            root,
            value=COMFY_TIER_LABELS.get(
                self.settings.comfy_tier, COMFY_TIER_LABELS["auto"]
            ),
        )
        self._comfy_workflows = tk.StringVar(
            root, value=self.settings.comfy_workflows
        )
        self._comfy_sage = tk.StringVar(
            root, value=self.settings.comfy_sage_attention
        )
        self._comfy_access = tk.StringVar(root, value=self.settings.comfy_access)
        self._comfy_ntfy = tk.StringVar(root, value=self.settings.comfy_ntfy_topic)
        self._comfy_repo = tk.StringVar(root, value=self.settings.comfy_personal_repo)
        self._comfy_version = tk.StringVar(root, value=self.settings.comfyui_version)
        # Automatic collection of finished generations (launcher-side).
        self._comfy_auto_collect = tk.BooleanVar(
            root, value=self.settings.comfy_auto_collect
        )
        self._comfy_notify_windows = tk.BooleanVar(
            root, value=self.settings.comfy_notify_windows
        )
        self._comfy_notify_ntfy = tk.BooleanVar(
            root, value=self.settings.comfy_notify_ntfy
        )
        self._comfy_terminate_after = tk.BooleanVar(
            root, value=self.settings.comfy_terminate_after
        )
        self._comfy_outputs_dir = tk.StringVar(
            root, value=self.settings.comfy_outputs_dir
        )
        # Automatic reception of finished generations: the watcher thread
        # reads a plain dict (never a Tk variable — Tcl is not thread-safe),
        # refreshed on the Tk thread by _sync_comfy_watcher().
        self._comfy_watcher: Optional[comfy_watch.ComfyOutputWatcher] = None
        self._comfy_watcher_suppressed = False
        self._comfy_watch_state: dict = {"enabled": False}
        self._gpu_choice = tk.StringVar(
            root, value=self.settings.runpod_gpu or DEFAULT_RUNPOD_GPU_ID
        )
        # The combobox starts on the local fallback list; the startup release
        # check replaces it with the fresh GitHub list once the fetch lands.
        self._comfy_releases: list = list(COMFYUI_FALLBACK_VERSIONS)
        self._log_entries: list = []
        # Declared before the widget exists: the log handler can deliver a line
        # before the panel is built. Annotated (not Optional) so the rest of the
        # module keeps its non-Optional widget type.
        self._log_text: "tk.Text" = None  # type: ignore[assignment]
        #: Result of the last ``save_settings`` call, so the settings dialog can
        #: report the truth instead of always claiming success.
        self.settings_saved_ok = True
        self._progress_active = False
        self._progress_steps: list = []
        self._progress_states: list = []
        self._progress_labels: list = []
        self._progress_hide_after: Optional[str] = None
        self._log_filter_info = tk.BooleanVar(root, value=True)
        self._log_filter_warn = tk.BooleanVar(root, value=True)
        self._log_filter_error = tk.BooleanVar(root, value=True)
        self._log_autoscroll = tk.BooleanVar(root, value=True)
        # The API keys section lives in the Settings dialog — these refs are
        # only valid while the dialog is open (rebuilt on each open, cleared
        # on close so the periodic status refresh never touches dead
        # widgets).
        self._settings_dialog = None
        self._creds_label = None
        self._cred_row_labels: dict[str, tk.Label] = {}
        self._cred_blocks: dict[str, tk.LabelFrame] = {}
        self._btn_creds_set = None
        self._btn_creds_clear = None

        # Model catalog + presets (mutable copies; persisted on save).
        self._comfy_models: dict = _normalize_catalog(self.settings.comfy_models)
        self._comfy_presets: dict = _normalize_presets(self.settings.comfy_presets)
        # Working set for the selected preset's models / nodes / workflows
        # (synced with _comfy_presets on save/load). The model list is
        # explicit (url + target): the category cards drive it through their
        # checkboxes, and the models it holds that the catalog does NOT offer
        # (a CivitAI checkpoint, a custom target path…) get their own short
        # "hors catalogue" editor.
        self._comfy_edit_models: list = []
        self._comfy_edit_nodes: list = []
        self._comfy_edit_workflows: list = []
        # Annuaire (LoRA / Workflow / Node) — mutable copy, persisted on save.
        self._annuaire: list = _normalize_annuaire(self.settings.comfy_annuaire)
        self._annuaire_editing_index: Optional[int] = None
        self._annuaire_grid_columns: Optional[int] = None
        # Card reuse cache: ``id(entry) -> (entry, signature, card, name
        # label, trigger label, card width)``. Rebuilding the grid reuses
        # every card whose content did not change, so filtering and resizing
        # cost a handful of Tcl calls instead of recreating ~480 widgets.
        # The entry object is kept in the tuple so its ``id()`` cannot be
        # reused by a later dict while a card still points at it.
        self._annuaire_cards: dict = {}
        self._annuaire_grid_width: int = 0
        self._annuaire_card_width: int = 180
        self._annuaire_rebuild_after: Optional[str] = None
        self._annuaire_trunc_cache: dict = {}

        # Keep the stable native title: automation and the tray use it to
        # locate the window; the richer product name lives in the header.
        self.root.title("MiniMax H3 Launcher")
        self.root.geometry("980x760")
        self.root.minsize(800, 600)
        self._apply_window_icon()

        # Style layer: the forge ttkbootstrap theme (when the library is
        # installed) drives every ttk widget; the recoloring palette used
        # by the hand-rolled tk widgets is derived from the same theme so
        # both layers stay visually coherent. Without ttkbootstrap the
        # hand-rolled PALETTES are used as-is (previous behavior).
        self._tb_style = None
        if _HAS_TTKBOOTSTRAP:
            try:
                self._tb_style = make_forge_style(self.settings.theme)
            except Exception as exc:  # noqa: BLE001 - defensive: the theme is
                # optional, never a reason to fail the launch.
                logger.warning("ttkbootstrap theme unavailable: %s", exc)
                self._tb_style = None
        self._pal = self._derive_palette(self.settings.theme)

        self._build_widgets()
        self.root.bind("<Control-comma>", lambda _event: self._open_settings())
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self._on_stack_change()
        self._apply_theme()
        self._install_log_handler()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._append_log(
            "info", "Interface started. Press Start to launch the stack."
        )
        if env_file is not None:
            self._append_log("info", f".env file loaded: {env_file}")
        else:
            self._append_log(
                "info",
                "No .env found (optional): press Create .env "
                "to enter your RunPod API key.",
            )
        self._start_tray()
        self._load_pending_stop()
        self._start_comfy_release_check()
        self._start_model_size_check()
        # Defer the first refresh until after mainloop() is entered: the
        # background refresh thread holds references to tkinter Variables,
        # and if they are GC'd on that thread before the main loop starts,
        # Variable.__del__ -> globalunsetvar() raises "main thread is not
        # in main loop" (a Tcl error). after_idle guarantees the main loop
        # is running by the time the refresh thread is spawned.
        self.root.after_idle(lambda: self._refresh(force=True))
        self.root.after(POLL_INTERVAL_MS, self._poll)
        self.root.after(FIRST_RUN_WIZARD_DELAY_MS, self._maybe_first_run_wizard)


    # -- construction ------------------------------------------------------

    def _themed(self, widget, kind: str) -> None:
        """Register a widget for recoloring on theme switches.

        The factories register the creation-time kind and the call sites
        then register the refined role (e.g. a label built with
        ``fg=pal["muted"]`` also gets ``"muted"``): both modes replay the
        entries in order, so the last registration wins — which is exactly
        the order the colors were applied at creation time."""
        self._recolor.append((widget, kind))

    def _prune_dead_refs(self) -> None:
        """Drop recolor/panel entries whose widget has been destroyed.

        Both collections are append-only during a session while some rows
        (model checklists, Library cards) are rebuilt on every interaction —
        without this they would keep every discarded widget alive."""
        self._recolor = [
            (widget, kind)
            for widget, kind in self._recolor
            if self._widget_exists(widget)
        ]
        self._tb_panel_widgets = {
            widget
            for widget in self._tb_panel_widgets
            if self._widget_exists(widget)
        }

    def _derive_palette(self, theme: str) -> dict:
        """The recoloring palette for *theme*, from the live theme if any.

        The derivation reads a handful of ttkbootstrap color fields; an
        unexpected library shape must not abort startup, so it falls back to
        the hand-rolled palette (the ttk widgets keep the theme's own look,
        only the tk-painted ones use the fallback colors)."""
        if self._tb_style is None:
            return dict(PALETTES[theme])
        try:
            return palette_from_theme(self._tb_style, theme)
        except Exception as exc:  # noqa: BLE001 - defensive
            logger.warning("Theme palette unavailable: %s", exc)
            return dict(PALETTES[theme])

    def _ui_font(self, size: int = 9, weight: str = "normal"):
        """A cached font object, for pixel-accurate width measurements."""
        key = (size, weight)
        font = self._fonts.get(key)
        if font is None:
            font = tkfont.Font(
                self.root, family="Segoe UI", size=size, weight=weight
            )
            self._fonts[key] = font
        return font

    # -- widget factories (ttkbootstrap-aware) -----------------------------
    # With ttkbootstrap installed the interactive controls are the library's
    # themed widgets (buttons, inputs, indicators, frames, notebook…); the
    # hand-rolled palette below is only a fallback. The forge theme's
    # widgets expose a reduced option set, so tk-only options (background,
    # padding, anchor, relief, masking…) are dropped in that mode — the
    # theme provides the equivalent styling.

    # ttkbootstrap widgets do not take the tk color/padding options, so in
    # that mode the roles are carried by Forge* ttk styles (defined and
    # re-applied by ``_configure_ttk_style`` on every theme switch):
    # the label styles set foreground/background, the panel styles paint
    # the widget on the raised panel surface.
    _TB_PAGE_LABEL_STYLES = {
        "bg": "TLabel",
        "muted": "ForgeMuted.TLabel",
        "status:muted": "ForgeMuted.TLabel",
        "status:ok": "ForgeOk.TLabel",
        "status:warn": "ForgeWarn.TLabel",
        "status:error": "ForgeError.TLabel",
    }
    _TB_PANEL_LABEL_STYLES = {
        "bg": "ForgePanel.TLabel",
        "muted": "ForgePanelMuted.TLabel",
        "status:muted": "ForgePanelMuted.TLabel",
        "status:ok": "ForgePanelOk.TLabel",
        "status:warn": "ForgePanelWarn.TLabel",
        "status:error": "ForgePanelError.TLabel",
    }

    _TK_ONLY_OPTIONS = (
        "bg",
        "fg",
        "activebackground",
        "activeforeground",
        "selectcolor",
        "insertbackground",
        "relief",
        "bd",
        "highlightthickness",
        "highlightbackground",
        "exportselection",
    )

    def _ttk_options(self, kwargs: dict, extra_drop: tuple = ()) -> dict:
        """Strip the options the current mode's widgets do not support."""
        drop = self._TK_ONLY_OPTIONS + extra_drop
        if self._tb_style is not None:
            for option in drop:
                kwargs.pop(option, None)
        return kwargs

    def _label_kind_for(self, kind: str, kwargs: dict) -> str:
        """Resolve the intended color role from the call's explicit fg.

        Most call sites pass ``fg=pal[...]`` explicitly; in the ttkbootstrap
        mode that value is the only way to carry the role (no per-widget
        foreground exists), so it takes precedence over the generic kind."""
        pal = self._pal
        fg = kwargs.get("fg")
        if fg == pal["muted"]:
            return "muted"
        if fg == pal["ok"]:
            return "status:ok"
        if fg == pal["warn"]:
            return "status:warn"
        if fg == pal["error"]:
            return "status:error"
        return kind

    def _photo_icon(self, name: str, size: int, *, by_height: bool = False):
        """A Tk image for a bundled icon (cached, kept alive), or None.

        Every image is stored in ``self._icon_refs`` for the lifetime of the
        app: a PhotoImage that no longer has a Python reference paints
        nothing at all. Returns None when PIL is absent or the asset is
        missing, which lets each call site fall back to its text rendering.

        ``by_height=True`` keeps the art's own width (see
        :func:`_load_asset_icon`).
        """
        key = (name, size, by_height)
        cached = self._icon_refs.get(key)
        if cached is not None:
            return cached
        pil_image = _load_asset_icon(name, size, by_height=by_height)
        if pil_image is None:
            return None
        try:
            from PIL import ImageTk

            photo = ImageTk.PhotoImage(pil_image, master=self.root)
        except Exception as exc:  # noqa: BLE001 - icons are best effort
            logger.debug("Icon %s unavailable: %s", name, exc)
            return None
        self._icon_refs[key] = photo
        return photo

    def _status_icon(self, color_key: str, *, size: int = 16):
        """A health icon for a color key (``ok``/``warn``/``error``/``muted``).

        Returns None when the asset (or PIL) is unavailable: every call site
        then keeps its colored text/glyph, so the state stays readable.
        """
        key = "idle" if color_key in ("muted", "idle", "fg") else color_key
        name = HEALTH_STATUS_ICONS.get(key)
        if name is None:
            return None
        return self._photo_icon(name, size)

    def _set_overall(self, text: str, color_key: str) -> None:
        """Global state badge: text, color and (when available) its icon."""
        kwargs = {"text": text, "fg": self._pal[color_key]}
        icon = self._status_icon(color_key)
        if icon is not None:
            kwargs["image"] = icon
            kwargs["compound"] = "left"
        self._overall_label.configure(**kwargs)

    def _apply_cost_meter(self, billing: Optional[dict]) -> None:
        """Header cost meter: the pod's hourly rate, next to the state.

        Hidden when nothing is known (no pod, probe failed): an empty chip
        would read as "free"."""
        rate = (billing or {}).get("rate")
        if not rate:
            self._cost_label.pack_forget()
            return
        self._cost_label.configure(text=f"  {rate}")
        # Inserted before the buttons so the chip stays right next to the
        # global state badge (pack order, not call order, decides the slot).
        self._cost_label.pack(
            side="right", padx=(PAD_M, 0), before=self._btn_settings
        )
        self._set_fg(self._cost_label, "muted")

    def _label(self, parent, text="", *, kind: str = "bg", panel: bool = False, **kwargs):
        text = t(text)
        """A label: themed ttkbootstrap in that mode, hand-rolled otherwise.

        ``panel=True`` paints it on the raised panel surface (the theme's
        elevated tone) instead of the page background."""
        pal = self._pal
        if self._tb_style is not None:
            role = self._label_kind_for(kind, kwargs)
            style_map = self._TB_PANEL_LABEL_STYLES if panel else self._TB_PAGE_LABEL_STYLES
            widget = ttk.Label(
                parent, text=text,
                **self._ttk_options(kwargs, ("padx", "pady", "width")),
            )
            style_name = style_map.get(role)
            widget._tb_role = role  # type: ignore[attr-defined]
            if style_name is not None:
                widget.configure(style=style_name)
                if panel:
                    self._tb_panel_widgets.add(widget)
        else:
            if panel:
                kwargs = dict(kwargs, bg=pal["surface"])
            widget = _TKMOD.Label(parent, text=text, **kwargs)
        self._themed(widget, kind)
        return widget

    def _radio(self, parent, text, *, variable=None, value=None, panel: bool = False, **kwargs):
        pal = self._pal
        if self._tb_style is not None:
            widget = ttk.Radiobutton(
                parent,
                text=text,
                variable=variable,
                value=value,
                style="ForgePanel.TRadiobutton" if panel else "Forge.TRadiobutton",
                **self._ttk_options(kwargs, ("anchor",)),
            )
            if panel:
                self._tb_panel_widgets.add(widget)
        else:
            if panel:
                kwargs = dict(kwargs, bg=pal["surface"])
            widget = _TKMOD.Radiobutton(
                parent, text=text, variable=variable, value=value, **kwargs
            )
        self._themed(widget, "check")
        return widget

    def _check(self, parent, text, *, variable=None, panel: bool = False, **kwargs):
        text = t(text)
        pal = self._pal
        if self._tb_style is not None:
            widget = ttk.Checkbutton(
                parent,
                text=text,
                variable=variable,
                style="ForgePanel.TCheckbutton" if panel else "Forge.TCheckbutton",
                **self._ttk_options(kwargs, ("anchor",)),
            )
            if panel:
                self._tb_panel_widgets.add(widget)
        else:
            if panel:
                kwargs = dict(kwargs, bg=pal["surface"])
            widget = _TKMOD.Checkbutton(
                parent, text=text, variable=variable, **kwargs
            )
        self._themed(widget, "check")
        return widget

    def _entry(self, parent, *, kind: str = "input", **kwargs):
        if self._tb_style is not None:
            widget = ttk.Entry(parent, **self._ttk_options(kwargs))
        else:
            widget = _TKMOD.Entry(parent, **kwargs)
        self._themed(widget, kind)
        return widget

    def _lframe(self, parent, text="", *, kind: str = "panelbg", **kwargs):
        """A framed section: on the raised surface with a visible border."""
        text = t(text)
        pal = self._pal
        if self._tb_style is not None:
            widget = ttk.LabelFrame(
                parent,
                text=text,
                style="Forge.TLabelframe",
                **self._ttk_options(kwargs, ("font", "padx", "pady")),
            )
            self._tb_panel_widgets.add(widget)
        else:
            kwargs = dict(kwargs)
            kwargs["bg"] = pal["surface"]
            kwargs["highlightthickness"] = 1
            kwargs["highlightbackground"] = pal["panel_border"]
            widget = _TKMOD.LabelFrame(parent, text=text, **kwargs)
        self._themed(widget, kind)
        return widget

    def _build_widgets(self) -> None:
        pal = self._pal
        pad = {"padx": 8, "pady": 4}

        container = tk.Frame(self.root, bg=pal["bg"])
        container.pack(fill="both", expand=True)
        self._themed(container, "bg")

        # Native menus remain available for keyboard and Windows users, but
        # everyday controls live in the product header below.
        menubar = tk.Menu(self.root)
        app_menu = tk.Menu(menubar, tearoff=0)
        app_menu.add_command(label="Preferences…", command=self._open_settings)
        app_menu.add_command(label="Refresh now", command=lambda: self._refresh(force=True))
        app_menu.add_separator()
        app_menu.add_command(label="Quit", command=self._quit)
        menubar.add_cascade(label="Application", menu=app_menu)
        navigation_menu = tk.Menu(menubar, tearoff=0)
        for tab_name in NAV_TAB_LABELS:
            navigation_menu.add_command(
                label=tab_name,
                command=lambda name=tab_name: self._select_tab_by_label(name),
            )
        menubar.add_cascade(label="Navigation", menu=navigation_menu)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menubar)
        self._menu = menubar
        self._file_menu = app_menu
        self._navigation_menu = navigation_menu
        self._help_menu = help_menu

        self._ttk = ttk
        if self._tb_style is None:
            # Fallback mode only: the hand-rolled palettes are paired with
            # the plain clam ttk theme. Under ttkbootstrap the forge theme
            # (and its light/dark modes) drives the ttk widgets — forcing
            # clam here would silently discard it.
            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass

        # Product header: clear identity, live status, then only the two
        # routine controls. This keeps the primary task visible at all times.
        header = tk.Frame(container, bg=pal["bg"], pady=4)
        header.pack(fill="x", padx=12, pady=(10, 4))
        self._themed(header, "bg")
        # The brand badge: the bundled logo when it can be loaded, otherwise
        # the accent-filled "OF" monogram (raw tk widget — its accent fill is
        # not part of the theme's widget palette).
        brand_logo = self._photo_icon("logo_minimax_h3.png", 40)
        if brand_logo is not None:
            brand_mark = _TKMOD.Label(header, image=brand_logo, bd=0, bg=pal["bg"])
            brand_mark.image = brand_logo  # type: ignore[attr-defined]
            brand_mark.pack(side="left", padx=(0, 9))
            self._themed(brand_mark, "bg")
        else:
            brand_mark = _TKMOD.Label(
                header,
                text="OF",
                font=("Segoe UI", 11, "bold"),
                bg=pal["accent"],
                fg=pal["accent_fg"],
                padx=9,
                pady=6,
            )
            brand_mark.pack(side="left", padx=(0, 9))
            self._themed(brand_mark, "accent")
        brand_copy = tk.Frame(header, bg=pal["bg"])
        brand_copy.pack(side="left", fill="x", expand=True)
        self._themed(brand_copy, "bg")
        title = self._label(
            brand_copy,
            text="MiniMax H3 Launcher",
            font=("Segoe UI", 16, "bold"),
            bg=pal["bg"],
            fg=pal["fg"],
            anchor="w",
        )
        title.pack(anchor="w")
        self._themed(title, "bg")
        subtitle = self._label(
            brand_copy,
            text="Control centre for your AI environment",
            font=("Segoe UI", 9),
            bg=pal["bg"],
            fg=pal["muted"],
            anchor="w",
        )
        subtitle.pack(anchor="w")
        self._themed(subtitle, "muted")
        # Surface-filled, bordered, clickable badge: raw tk (the theme has
        # no per-widget background for this one-off shape).
        self._overall_label = _TKMOD.Label(
            header,
            text="State: …",
            font=("Segoe UI", 10, "bold"),
            bg=pal["surface"],
            fg=pal["muted"],
            anchor="w",
            padx=10,
            pady=6,
            highlightthickness=1,
            highlightbackground=pal["border"],
            cursor="hand2",
        )
        self._overall_label.pack(side="right", padx=(8, 0))
        self._themed(self._overall_label, "overall")
        # Clickable from any tab: reveals cause + remediation in a popover.
        self._overall_label.bind("<Button-1>", self._show_overall_popover)
        self._overall_popover = None
        self._overall_popover_label = None
        self._overall_popover_after: Optional[str] = None
        # Cost meter: the pod's hourly rate, right next to the global state
        # (both are "what is running right now, and what it costs").
        cost_icon = self._photo_icon(COST_METER_ICON, 20)
        if cost_icon is not None:
            self._cost_meter_icon = cost_icon
            self._cost_label = _TKMOD.Label(
                header, text="", image=cost_icon, compound="left",
                font=("Segoe UI", 10), bg=pal["bg"], fg=pal["muted"],
                anchor="e", padx=6, pady=4,
            )
        else:
            self._cost_meter_icon = None
            self._cost_label = _TKMOD.Label(
                header, text="", font=("Segoe UI", 10),
                bg=pal["bg"], fg=pal["muted"], anchor="e", padx=6, pady=4,
            )
        # Left unpacked until a rate is known (``_apply_cost_meter``): an
        # icon with no value would read as "free".
        self._themed(self._cost_label, "muted")
        self._btn_settings = self._make_button(
            header, "⚙  Settings", self._open_settings, variant="quiet"
        )
        self._btn_settings.pack(side="right", padx=(6, 0))
        self._btn_refresh = self._make_button(
            header, "↻  Refresh", lambda: self._refresh(force=True), variant="quiet"
        )
        self._btn_refresh.pack(side="right")
        # Kept as an attribute for integrations that used the former control.
        self._theme_combo = None

        # Transient action banner (hidden until an action wants a visible
        # acknowledgement on top of the journal).
        self._notify_label = self._label(
            container, text="", bg=pal["bg"], fg=pal["muted"],
            anchor="w", font=("Segoe UI", 9),
        )

        # Notebook with one tab per concern (each scrollable, so nothing
        # overflows on a small window).
        self._notebook = ttk.Notebook(container, style="Forge.TNotebook")
        self._notebook.pack(fill="both", expand=True, padx=8, pady=(4, 0))

        # ------------------------------------------------------------------
        # Tab 1 — Dashboard (mode, status, actions, repair).
        # ------------------------------------------------------------------
        dash_outer, dash_inner = self._make_scrollable(self._notebook)
        self._notebook.add(dash_outer, text=t(TAB_DASHBOARD))
        self._dash_inner = dash_inner

        # The workload-stack selector lives only in the persistent footer
        # bar (« Mode : », see ``_bar_stack_radios``): the duplicate row
        # that used to sit here was removed.
        # Dashboard. The health table is content-sized and the pod/GPU card
        # takes the width it does not need: a full-width table with its three
        # narrow columns left a large empty area on the right.
        self._dash_row = tk.Frame(dash_inner, bg=pal["bg"])
        self._dash_row.pack(fill="x", **pad)
        self._themed(self._dash_row, "bg")
        self._dash_frame = self._lframe(
            self._dash_row, text="  Stack health  ", bg=pal["bg"],
            fg=pal["fg"], font=("Segoe UI", 10, "bold"), padx=6, pady=6,
            highlightthickness=1, highlightbackground=pal["border"],
        )
        self._dash_frame.pack(side="left", fill="x")
        self._themed(self._dash_frame, "panelbg")
        # 3-column table (Composant / État / Info) with a colored badge per
        # row: green=ok, orange=starting/warn, red=error, grey=unknown. The
        # badge itself is the tree column (#0), which is the only Treeview
        # column that can hold an image.
        self._dash_table = ttk.Treeview(
            self._dash_frame,
            columns=("component", "status", "info"),
            show=("tree", "headings"),
            height=7,
            selectmode="none",
        )
        self._dash_table.heading("#0", text="")
        self._dash_table.heading("component", text="Component")
        self._dash_table.heading("status", text="State")
        self._dash_table.heading("info", text="Info")
        # The status icon lives in the tree column (#0), which is also the
        # only place a Treeview can draw one. Its width sets the gap between
        # the icon and the « Composant » text: 26px left the icon almost
        # touching the first letter, so the column is a little wider.
        self._dash_table.column("#0", width=STATUS_ICON_COLUMN_WIDTH,
                                minwidth=STATUS_ICON_COLUMN_WIDTH,
                                anchor="w", stretch=False)
        # Every column is content-sized at each redraw (see
        # ``_fit_health_columns``) — none of them stretches to fill the
        # panel, so a nearly empty « Info » column cannot dominate it.
        self._dash_table.column("component", width=150, anchor="w", stretch=False)
        self._dash_table.column("status", width=200, anchor="w", stretch=False)
        self._dash_table.column("info", width=200, anchor="w", stretch=False)
        for color_key in ("ok", "warn", "error", "muted"):
            self._dash_table.tag_configure(color_key, foreground=pal[color_key])
        self._dash_table.pack(fill="x", padx=6, pady=(2, 4))
        # Explicit empty state: shown instead of the table when nothing is
        # active ("No active instance"; the start button lives in the
        # persistent footer bar).
        self._dash_idle = tk.Frame(self._dash_frame, bg=pal["surface"])
        self._themed(self._dash_idle, "panelbg")
        self._idle_label = self._label(
            self._dash_idle,
            text="No active instance",
            bg=pal["surface"],
            fg=pal["muted"],
            font=("Segoe UI", 10),
            panel=True,
        )
        self._idle_label.pack(side="left", padx=(4, 12))
        self._themed(self._idle_label, "muted")
        # Shown only when a forced refresh found an open stack on the
        # account that does not belong to the current workspace.
        self._idle_adopt_btn = self._make_button(
            self._dash_idle, "", lambda: self._adopt_idle_found(), variant="quiet"
        )
        self._idle_adopt_btn.pack_forget()
        self._idle_found: list[dict] = []
        self._dash_idle.pack_forget()
        # Billing row (pod elapsed time / spend so far / wallet balance),
        # bottom-pinned so the table re-packing above can never reorder it.
        self._billing_label = self._label(
            self._dash_frame,
            text="",
            bg=pal["surface"],
            fg=pal["muted"],
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            padx=6,
            panel=True,
        )
        self._themed(self._billing_label, "muted")
        self._billing_label.pack_forget()
        self._last_billing: Optional[dict] = None

        # Secondary card: the pod identity and GPU the table never showed,
        # in the width the health table does not use.
        self._pod_card = tk.Frame(
            self._dash_row, bg=pal["surface"], highlightthickness=1,
            highlightbackground=pal["panel_border"], bd=0,
        )
        self._themed(self._pod_card, "card")
        self._pod_card.pack(side="left", fill="both", expand=True, padx=(PAD_M, 0))
        pod_title = self._label(
            self._pod_card, text="Pod & GPU", bg=pal["surface"], fg=pal["fg"],
            anchor="w", font=("Segoe UI", 10, "bold"), panel=True,
        )
        pod_title.grid(row=0, column=0, columnspan=2, sticky="w",
                       padx=PAD_M, pady=(PAD_S, PAD_XS))
        self._themed(pod_title, "bg")
        self._pod_card_values: dict = {}
        self._pod_card_key_labels: dict = {}
        #: Rows that only appear when the pod actually reports a value (queue /
        #: VRAM are unknown until ComfyUI answers): a permanent "—" row tells
        #: nothing, so these are hidden instead.
        self._pod_card_optional: set = set()
        for row, (key, label_text) in enumerate(
            (
                ("pod", "Pod"),
                ("name", "Name"),
                ("gpu", "GPU"),
                ("zone", "Zone"),
                ("elapsed", "Running for"),
                ("cost", "Cumulative cost"),
                ("queue", "Queue"),
                ("vram", "VRAM"),
            ),
            start=1,
        ):
            key_label = self._label(
                self._pod_card, text=f"{label_text} :", bg=pal["surface"],
                fg=pal["muted"], anchor="w", font=("Segoe UI", 9), panel=True,
            )
            key_label.grid(row=row, column=0, sticky="w",
                           padx=(PAD_M, PAD_S), pady=(0, PAD_XS))
            self._themed(key_label, "muted")
            self._pod_card_key_labels[key] = key_label
            value_label = self._label(
                self._pod_card, text="—", bg=pal["surface"], fg=pal["fg"],
                anchor="w", font=("Segoe UI", 9), panel=True,
            )
            value_label.grid(row=row, column=1, sticky="ew",
                             padx=(0, PAD_M), pady=(0, PAD_XS))
            self._themed(value_label, "bg")
            self._pod_card_values[key] = value_label
            if key in ("queue", "vram"):
                self._pod_card_optional.add(key)
                key_label.grid_remove()
                value_label.grid_remove()
        self._pod_card.columnconfigure(1, weight=1)
        self._refresh_pod_card()

        # ------------------------------------------------------------------
        # Piles actives — one row per workload stack, each with its own
        # Start / Stop / Terminate buttons. Several stacks run at once
        # (a text agent plus ComfyUI and/or LoRA training, each on its own
        # pod); the footer's mode radios only pick which tab is detailed.
        # ------------------------------------------------------------------
        self._stacks_frame = self._lframe(
            dash_inner, text="  Active stacks  ", bg=pal["bg"], fg=pal["fg"],
            font=("Segoe UI", 10, "bold"), padx=6, pady=6,
            highlightthickness=1, highlightbackground=pal["border"],
        )
        self._stacks_frame.pack(fill="x", padx=12, pady=(PAD_S, 0))
        self._themed(self._stacks_frame, "panelbg")
        self._stack_rows: dict = {}
        header = tk.Frame(self._stacks_frame, bg=pal["surface"])
        header.pack(fill="x")
        self._themed(header, "panelbg")
        for text, width in (("Stack", 12), ("State", 30), ("Cost", 10)):
            cell = self._label(
                header, text=text, bg=pal["surface"], fg=pal["muted"],
                anchor="w", font=("Segoe UI", 9, "bold"), panel=True, width=width,
            )
            cell.pack(side="left")
            self._themed(cell, "muted")
        for stack in STACKS:
            row = tk.Frame(self._stacks_frame, bg=pal["surface"])
            row.pack(fill="x", pady=(2, 0))
            self._themed(row, "panelbg")
            name = self._label(
                row, text=STACK_LABELS.get(stack, stack), bg=pal["surface"],
                fg=pal["fg"], anchor="w", font=("Segoe UI", 9), panel=True,
                width=12,
            )
            name.pack(side="left")
            self._themed(name, "bg")
            status = self._label(
                row, text="—", bg=pal["surface"], fg=pal["muted"], anchor="w",
                font=("Segoe UI", 9), panel=True, width=30,
            )
            status.pack(side="left")
            self._themed(status, "muted")
            cost = self._label(
                row, text="—", bg=pal["surface"], fg=pal["muted"], anchor="w",
                font=("Segoe UI", 9), panel=True, width=10,
            )
            cost.pack(side="left")
            self._themed(cost, "muted")
            term = self._make_button(
                row, "Terminate", lambda s=stack: self._stack_action(s, True),
                variant="danger",
            )
            term.pack(side="right", padx=(PAD_S, 0))
            stop = self._make_button(
                row, "Stop", lambda s=stack: self._stack_action(s, False)
            )
            stop.pack(side="right", padx=(PAD_S, 0))
            start = self._make_button(
                row, "Start", lambda s=stack: self._stack_start(s),
                variant="primary",
            )
            start.pack(side="right")
            self._stack_rows[stack] = {
                "status": status,
                "cost": cost,
                "start": start,
                "stop": stop,
                "term": term,
            }
        self._stacks_total = self._label(
            self._stacks_frame, text="", bg=pal["surface"], fg=pal["muted"],
            anchor="w", font=("Segoe UI", 9), panel=True,
        )
        self._stacks_total.pack(fill="x", pady=(PAD_S, 0))
        self._themed(self._stacks_total, "muted")
        self._stacks_snapshot: dict = {"stacks": [], "cost_per_hour_total": None}

        # Cause / remediation hint, with its one-click repair shortcut on
        # the same line (the button is packed first so it keeps the right
        # edge while the hint text stretches).
        self._hint_row = tk.Frame(dash_inner, bg=pal["bg"])
        self._hint_row.pack(fill="x", padx=12)
        self._themed(self._hint_row, "bg")
        self._btn_repair = self._make_button(
            self._hint_row, "▶ Repair", self._repair_action_clicked,
            variant="primary",
        )
        self._btn_repair.pack(side="right")
        self._btn_repair.pack_forget()
        self._repair_action: Optional[str] = None
        self._hint_label = self._label(
            self._hint_row, text="", bg=pal["bg"], fg=pal["warn"], anchor="w",
            justify="left", font=("Segoe UI", 9),
        )
        self._hint_label.pack(side="left", fill="x", expand=True)
        self._themed(self._hint_label, "status:warn")

        # Progress panel: one framed chip per startup step (✓/⟳/○), shown
        # while a multi-step start runs (replaces the former single status
        # text). It is a bordered panel so the running sequence is not just
        # another grey line among the dashboard hints.
        self._progress_frame = tk.Frame(
            dash_inner, bg=pal["bg"], padx=8, pady=6,
            highlightthickness=1, highlightbackground=pal["panel_border"],
        )
        self._themed(self._progress_frame, "progresspanel")

        self._status_line = self._label(
            dash_inner, text="", bg=pal["bg"], fg=pal["muted"], anchor="w",
            font=("Segoe UI", 9),
        )
        self._status_line.pack(fill="x", padx=12)
        self._themed(self._status_line, "muted")

        # Recovery row (shown only when LAUNCHER_INFRA_RECOVERY=confirm).
        self._recovery_frame = tk.Frame(dash_inner, bg=pal["bg"])
        self._recovery_frame.pack(fill="x", **pad)
        self._themed(self._recovery_frame, "bg")
        self._label(
            self._recovery_frame,
            text="Repair:",
            bg=pal["bg"],
            fg=pal["fg"],
        ).pack(side="left")
        self._themed(self._recovery_frame.winfo_children()[-1], "bg")
        for name in RECOVERY_ACTIONS:
            btn = self._make_button(
                self._recovery_frame,
                RECOVERY_LABELS[name],
                (lambda action_name=name: self._recover_action(action_name)),
            )
            btn.pack(side="left", padx=(6, 0))
            self._recovery_buttons.append(btn)
        self._recovery_frame.pack_forget()

        # ------------------------------------------------------------------
        # Tab 2 — ComfyUI (MiniMax H3) options, plus the LoRA &
        # personal-storage management. The llama.cpp (GGUF) options live in
        # the Qwen tab, next to the model choice they configure.
        # ------------------------------------------------------------------
        comfy_outer, comfy_inner = self._make_scrollable(self._notebook)
        self._notebook.add(comfy_outer, text=t(TAB_COMFY))
        self._comfy_tab_inner = comfy_inner

        self._comfy_tab_hint = self._label(
            comfy_inner,
            text="ComfyUI video stack options (MiniMax H3). The llama.cpp "
            "(GGUF) options live in the Qwen tab, next to the model choice. "
            "The bottom bar or the Dashboard pick the active stack; the "
            "inactive section is greyed out, never hidden.",
            bg=pal["bg"],
            fg=pal["muted"],
            anchor="w",
            justify="left",
            font=("Segoe UI", 9),
        )
        self._comfy_tab_hint.pack(fill="x", padx=12, pady=8)
        self._themed(self._comfy_tab_hint, "muted")

        # ComfyUI options (always visible; enabled only in ComfyUI video
        # mode — point 4: disable, never hide). The values are persisted in
        # gui.json and injected as pod env at creation.
        comfy_frame = self._lframe(
            comfy_inner, text="ComfyUI / MiniMax H3 options", bg=pal["bg"],
            fg=pal["fg"], font=("Segoe UI", 10, "bold"),
        )
        self._themed(comfy_frame, "panelbg")
        self._comfy_frame = comfy_frame
        self._comfy_frame.pack(fill="x", padx=8, pady=4)
        # Row1 wraps onto two sub-rows so it never overflows on a narrow
        # window: Preset/Tier on the first line, Workflows/Access on the next.
        row1 = tk.Frame(comfy_frame, bg=pal["bg"])
        row1.pack(fill="x", padx=8, pady=(4, 2))
        self._themed(row1, "bg")
        r1a = tk.Frame(row1, bg=pal["bg"])
        r1a.pack(fill="x", side="top")
        self._themed(r1a, "bg")
        r1b = tk.Frame(row1, bg=pal["bg"])
        r1b.pack(fill="x", side="top")
        self._themed(r1b, "bg")
        self._label(r1a, text="Preset:", bg=pal["bg"], fg=pal["fg"]).pack(side="left")
        self._themed(r1a.winfo_children()[-1], "bg")
        self._preset_combo = ttk.Combobox(
            r1a, textvariable=self._comfy_preset_ui,
            values=[], width=22,
            state="readonly", style="Forge.TCombobox",
        )
        self._preset_combo.pack(side="left", padx=(4, 8))
        self._preset_combo.bind("<<ComboboxSelected>>", self._on_preset_change)
        self._preset_combo.bind("<FocusOut>", self._on_preset_change)
        # The options preset selector lists the user presets created in the
        # "Models & presets" tab (plus "Aucun"), not hardcoded names.
        self._refresh_comfy_options_preset_combo()
        self._label(r1a, text="Tier:", bg=pal["bg"], fg=pal["fg"]).pack(side="left")
        self._themed(r1a.winfo_children()[-1], "bg")
        self._comfy_tier_combo = ttk.Combobox(
            r1a, textvariable=self._comfy_tier_ui,
            values=list(COMFY_TIER_LABELS.values()),
            state="readonly", width=12, style="Forge.TCombobox",
        )
        self._comfy_tier_combo.pack(side="left", padx=(4, 8))
        self._comfy_tier_combo.bind("<<ComboboxSelected>>", self._on_tier_change)
        self._comfy_tier_combo.bind("<FocusOut>", self._on_tier_change)
        self._tooltip(self._comfy_tier_combo, TECHNICAL_TOOLTIPS["tier_modes"])
        self._label(r1b, text="Workflows:", bg=pal["bg"], fg=pal["fg"]).pack(side="left")
        self._themed(r1b.winfo_children()[-1], "bg")
        self._comfy_workflows_combo = ttk.Combobox(
            r1b, textvariable=self._comfy_workflows,
            values=list(COMFY_WORKFLOW_CHOICES), state="readonly", width=12,
            style="Forge.TCombobox",
        )
        self._comfy_workflows_combo.pack(side="left", padx=(4, 8))
        self._comfy_workflows_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._refresh_comfy_summary()
        )
        self._comfy_workflows_combo.bind(
            "<FocusOut>", lambda _e: self._refresh_comfy_summary()
        )
        self._label(r1b, text="Access:", bg=pal["bg"], fg=pal["fg"]).pack(side="left")
        self._themed(r1b.winfo_children()[-1], "bg")
        ttk.Combobox(
            r1b, textvariable=self._comfy_access,
            values=list(COMFY_ACCESS_MODES),
            state="readonly", width=8, style="Forge.TCombobox",
        ).pack(side="left", padx=(4, 10))
        # The hint explains the « Access » field: it sits on its own line,
        # directly under the field it documents (a trailing label on the same
        # line read as an unrelated caption).
        self._comfy_access_row = tk.Frame(row1, bg=pal["bg"])
        self._comfy_access_row.pack(fill="x", side="top")
        self._themed(self._comfy_access_row, "bg")
        self._comfy_access_hint = self._label(
            self._comfy_access_row,
            text="Access — local tunnel: private UI on 127.0.0.1; "
            "direct: public URL (no authentication).",
            bg=pal["bg"],
            fg=pal["muted"],
            font=("Segoe UI", 8),
            anchor="w",
        )
        self._comfy_access_hint.pack(side="left", padx=(ACCESS_HINT_INDENT, 0))
        self._themed(self._comfy_access_hint, "muted")
        # Warning shown when the active preset is what the pod receives: the
        # catalog checkboxes only reach it through « Sauver ».
        self._comfy_preset_warning = self._label(
            row1,
            text="",
            bg=pal["bg"],
            fg=pal["warn"],
            font=("Segoe UI", 8),
            anchor="w",
            justify="left",
        )
        self._themed(self._comfy_preset_warning, "status:warn")
        # Row2 wraps onto two sub-rows: Turbo LoRA/Sage/Spectrum on the
        # first line, the ntfy topic and Vault repo on the next.
        row2 = tk.Frame(comfy_frame, bg=pal["bg"])
        row2.pack(fill="x", padx=8, pady=(2, 4))
        self._themed(row2, "bg")
        r2a = tk.Frame(row2, bg=pal["bg"])
        r2a.pack(fill="x", side="top")
        self._themed(r2a, "bg")
        r2b = tk.Frame(row2, bg=pal["bg"])
        r2b.pack(fill="x", side="top")
        self._themed(r2b, "bg")
        # The Turbo LoRA and the Spectrum node used to have their own
        # checkboxes here. They are Library-managed content (models + custom
        # nodes of the preset), so the launcher no longer exposes — nor
        # auto-installs — them.
        self._label(r2a, text="Sage attention:", bg=pal["bg"], fg=pal["fg"]).pack(side="left")
        self._themed(r2a.winfo_children()[-1], "bg")
        self._comfy_sage_combo = ttk.Combobox(
            r2a, textvariable=self._comfy_sage, values=list(COMFY_SAGE_MODES),
            state="readonly", width=5, style="Forge.TCombobox",
        )
        self._comfy_sage_combo.pack(side="left", padx=(4, 8))
        self._tooltip(self._comfy_sage_combo, TECHNICAL_TOOLTIPS["sage"])
        self._label(r2b, text="ntfy topic:", bg=pal["bg"], fg=pal["muted"]).pack(side="left")
        self._themed(r2b.winfo_children()[-1], "muted")
        self._comfy_ntfy_entry = self._entry(
            r2b, textvariable=self._comfy_ntfy, width=14,
            bg=pal["input_bg"], fg=pal["input_fg"],
            insertbackground=pal["fg"], relief="flat",
        )
        self._comfy_ntfy_entry.pack(side="left", padx=(4, 8))
        self._themed(self._comfy_ntfy_entry, "input")
        self._tooltip(self._comfy_ntfy_entry, TECHNICAL_TOOLTIPS["ntfy"])
        self._label(r2b, text="HF vault repo:", bg=pal["bg"], fg=pal["muted"]).pack(side="left")
        self._themed(r2b.winfo_children()[-1], "muted")
        self._comfy_repo_entry = self._entry(
            r2b, textvariable=self._comfy_repo, width=24,
            bg=pal["input_bg"], fg=pal["input_fg"],
            insertbackground=pal["fg"], relief="flat",
        )
        self._comfy_repo_entry.pack(side="left", padx=(4, 0))
        self._themed(self._comfy_repo_entry, "input")
        self._tooltip(self._comfy_repo_entry, TECHNICAL_TOOLTIPS["vault"])
        # The ComfyUI version pin lives with the options (not with the
        # models): it pins the software itself and stays active in every
        # tier mode.
        version_row = tk.Frame(comfy_frame, bg=pal["bg"])
        version_row.pack(fill="x", padx=8, pady=(2, 4))
        self._themed(version_row, "bg")
        self._label(
            version_row, text="ComfyUI version:", bg=pal["bg"], fg=pal["fg"]
        ).pack(side="left")
        self._themed(version_row.winfo_children()[-1], "bg")
        self._comfy_version_display = tk.StringVar(
            self.root, value=version_display(self._comfy_version.get())
        )
        self._comfy_version_combo = ttk.Combobox(
            version_row,
            textvariable=self._comfy_version_display,
            values=[COMFYUI_VERSION_AUTO_LABEL] + list(self._comfy_releases),
            state="readonly", width=24, style="Forge.TCombobox",
        )
        self._comfy_version_combo.pack(side="left", padx=(4, 8))
        self._comfy_version_combo.bind("<<ComboboxSelected>>", self._on_version_change)
        self._comfy_version_combo.bind("<FocusOut>", self._on_version_change)
        self._comfy_version_hint = self._label(
            version_row,
            text="\"auto\" follows the latest release; a tag pins the version",
            bg=pal["bg"], fg=pal["muted"], font=("Segoe UI", 8),
        )
        self._comfy_version_hint.pack(side="left")
        self._themed(self._comfy_version_hint, "muted")

        # Automatic collection of finished generations. The launcher watches
        # the ComfyUI history through the tunnel: every finished generation is
        # downloaded into the chosen folder, together with a .txt (prompt,
        # settings, resources, workflow). The checkboxes are independent
        # options; the watcher only starts when at least one is ticked, and
        # the pod is only polled while it answers on the tunnel.
        receive_row = tk.Frame(comfy_frame, bg=pal["bg"])
        receive_row.pack(fill="x", padx=8, pady=(2, 4))
        self._themed(receive_row, "bg")
        self._comfy_auto_collect_check = self._check(
            receive_row,
            text="Receive the generations",
            variable=self._comfy_auto_collect,
            command=self._on_comfy_receive_change,
            anchor="w",
        )
        self._comfy_auto_collect_check.pack(side="left")
        self._tooltip(
            self._comfy_auto_collect_check, TECHNICAL_TOOLTIPS["auto_collect"]
        )
        self._comfy_notify_windows_check = self._check(
            receive_row,
            text="Windows notification",
            variable=self._comfy_notify_windows,
            command=self._on_comfy_receive_change,
            anchor="w",
        )
        self._comfy_notify_windows_check.pack(side="left", padx=(12, 0))
        self._tooltip(
            self._comfy_notify_windows_check, TECHNICAL_TOOLTIPS["notify_windows"]
        )
        self._comfy_notify_ntfy_check = self._check(
            receive_row,
            text="ntfy notification",
            variable=self._comfy_notify_ntfy,
            command=self._on_comfy_receive_change,
            anchor="w",
        )
        self._comfy_notify_ntfy_check.pack(side="left", padx=(12, 0))
        self._tooltip(
            self._comfy_notify_ntfy_check, TECHNICAL_TOOLTIPS["notify_ntfy"]
        )
        self._comfy_terminate_after_check = self._check(
            receive_row,
            text="Stop the pod once the queue is empty",
            variable=self._comfy_terminate_after,
            command=self._on_comfy_receive_change,
            anchor="w",
        )
        self._comfy_terminate_after_check.pack(side="left", padx=(12, 0))
        self._tooltip(
            self._comfy_terminate_after_check, TECHNICAL_TOOLTIPS["terminate_after"]
        )
        receive_dir_row = tk.Frame(comfy_frame, bg=pal["bg"])
        receive_dir_row.pack(fill="x", padx=8, pady=(0, 4))
        self._themed(receive_dir_row, "bg")
        self._label(
            receive_dir_row, text="Reception folder:", bg=pal["bg"],
            fg=pal["muted"],
        ).pack(side="left")
        self._themed(receive_dir_row.winfo_children()[-1], "muted")
        self._comfy_outputs_dir_entry = self._entry(
            receive_dir_row, textvariable=self._comfy_outputs_dir, width=40,
            bg=pal["input_bg"], fg=pal["input_fg"],
            insertbackground=pal["fg"], relief="flat",
        )
        self._comfy_outputs_dir_entry.pack(side="left", padx=(4, 8))
        self._themed(self._comfy_outputs_dir_entry, "input")
        self._tooltip(
            self._comfy_outputs_dir_entry, TECHNICAL_TOOLTIPS["outputs_dir"]
        )
        self._comfy_outputs_dir_entry.bind(
            "<FocusOut>", lambda _e: self._save_comfy_settings()
        )
        self._comfy_receive_hint = self._label(
            receive_dir_row,
            text="empty = Downloads; the files arrive with a .txt "
            "of the same name",
            bg=pal["bg"], fg=pal["muted"], font=("Segoe UI", 8),
        )
        self._comfy_receive_hint.pack(side="left")
        self._themed(self._comfy_receive_hint, "muted")

        # ------------------------------------------------------------------
        # Tab 1c — LoRA training: the Fizgig pod (MiniMax H3 / Krea 2 /
        # Klein 9B). The launcher trains nothing itself: it rents, tunnels and
        # collects. All the training work stays inside Fizgig.
        # ------------------------------------------------------------------
        train_outer, train_inner = self._make_scrollable(self._notebook)
        self._notebook.add(train_outer, text=t(TAB_TRAIN))
        self._train_tab_inner = train_inner

        train_hint = self._label(
            train_inner,
            text="Trains a LoRA (MiniMax H3, Krea 2, Klein 9B) on a rented "
            "pod, with Fizgig. The pod exposes NOTHING publicly: the desktop "
            "and the file manager are only reachable through the launcher's "
            "SSH tunnel.",
            bg=pal["bg"], fg=pal["muted"], anchor="w", justify="left",
            font=("Segoe UI", 9),
        )
        train_hint.pack(fill="x", padx=12, pady=(4, 6))
        self._themed(train_hint, "muted")

        # -- Pod access ------------------------------------------------------
        access_frame = self._lframe(
            train_inner, text="  Pod access  ", bg=pal["bg"], fg=pal["fg"],
            font=("Segoe UI", 10, "bold"), padx=6, pady=6,
            highlightthickness=1, highlightbackground=pal["border"],
        )
        self._themed(access_frame, "panelbg")
        self._train_access_frame = access_frame
        access_frame.pack(fill="x", padx=8, pady=4)

        self._train_access_rows: dict = {}
        for key, label_text in (
            ("image", "Image"),
            ("fizgig", "Fizgig"),
            ("desktop", "Bureau"),
            ("files", "Files"),
            ("pod", "Pod"),
        ):
            row = tk.Frame(access_frame, bg=pal["surface"])
            row.pack(fill="x", padx=8, pady=1)
            self._themed(row, "panelbg")
            key_label = self._label(
                row, text=f"{label_text} :", bg=pal["surface"], fg=pal["muted"],
                anchor="w", font=("Segoe UI", 9), panel=True, width=9,
            )
            key_label.pack(side="left")
            self._themed(key_label, "muted")
            value_label = self._label(
                row, text="—", bg=pal["surface"], fg=pal["fg"], anchor="w",
                font=("Segoe UI", 9), panel=True,
            )
            value_label.pack(side="left", fill="x", expand=True)
            self._themed(value_label, "panelbg")
            self._train_access_rows[key] = value_label

        train_btn_row = tk.Frame(access_frame, bg=pal["surface"])
        train_btn_row.pack(fill="x", padx=8, pady=(6, 2))
        self._themed(train_btn_row, "panelbg")
        self._btn_train_desktop = self._make_button(
            train_btn_row, "Open Fizgig ↗", self._open_train_desktop,
            variant="primary",
        )
        self._btn_train_desktop.pack(side="left")
        self._btn_train_files = self._make_button(
            train_btn_row, "Files ↗", self._open_train_files, variant="quiet"
        )
        self._btn_train_files.pack(side="left", padx=(6, 0))
        self._btn_train_verify = self._make_button(
            train_btn_row, "Check the template", self._verify_train_template,
            variant="quiet",
        )
        self._btn_train_verify.pack(side="left", padx=(6, 0))
        self._btn_train_version = self._make_button(
            train_btn_row, "Version Fizgig", self._check_fizgig_version,
            variant="quiet",
        )
        self._btn_train_version.pack(side="left", padx=(6, 0))

        # Fizgig updates itself on a pod restart (upstream pulls FIZGIG_REF at
        # every boot), so the drift line ends in an action the user already has:
        # stop, start. It is a separate label from the template hint above
        # because the two answers have different lifetimes — one is about the
        # template, one about the run currently on the pod.
        self._train_version_hint = self._label(
            access_frame, text="", bg=pal["surface"], fg=pal["muted"], anchor="w",
            justify="left", font=("Segoe UI", 8), panel=True,
        )
        self._train_version_hint.pack(fill="x", padx=8, pady=(0, 6))
        self._themed(self._train_version_hint, "muted")

        self._train_hint = self._label(
            access_frame, text="", bg=pal["surface"], fg=pal["muted"], anchor="w",
            justify="left", font=("Segoe UI", 8), panel=True,
        )
        self._train_hint.pack(fill="x", padx=8, pady=(2, 6))
        self._themed(self._train_hint, "muted")

        # -- Models pre-downloaded when the pod boots ------------------------
        fetch_frame = self._lframe(
            train_inner, text="  Models pre-downloaded when the pod starts  ",
            bg=pal["bg"], fg=pal["fg"], font=("Segoe UI", 10, "bold"),
            padx=6, pady=6, highlightthickness=1,
            highlightbackground=pal["border"],
        )
        self._themed(fetch_frame, "panelbg")
        self._train_fetch_frame = fetch_frame
        fetch_frame.pack(fill="x", padx=8, pady=4)

        current_fetch = {
            part.strip()
            for part in (self.settings.train_fetch_models or "").split(",")
            if part.strip()
        }
        fetch_row = tk.Frame(fetch_frame, bg=pal["surface"])
        fetch_row.pack(fill="x", padx=8, pady=(2, 0))
        self._themed(fetch_row, "panelbg")
        self._train_fetch_vars: dict = {}
        for key, label in (
            ("krea2", "Krea 2"),
            ("klein", "Klein 9B"),
            ("minimax", "MiniMax H3"),
            ("tools", "Outils"),
        ):
            var = tk.BooleanVar(fetch_row, value=key in current_fetch)
            self._train_fetch_vars[key] = var
            self._check(
                fetch_row,
                text=label,
                variable=var,
                panel=True,
                command=self._on_train_fetch_change,
            ).pack(side="left", padx=(0, 14), pady=2)
        self._train_fetch_hint = self._label(
            fetch_frame, text="", bg=pal["surface"], fg=pal["muted"],
            anchor="w", justify="left", font=("Segoe UI", 8), panel=True,
        )
        self._train_fetch_hint.pack(fill="x", padx=8, pady=(2, 6))
        self._themed(self._train_fetch_hint, "muted")
        self._refresh_train_fetch_hint()

        # -- Collecte des LoRA ----------------------------------------------
        collect_frame = self._lframe(
            train_inner, text="  LoRA collection  ", bg=pal["bg"], fg=pal["fg"],
            font=("Segoe UI", 10, "bold"), padx=6, pady=6,
            highlightthickness=1, highlightbackground=pal["border"],
        )
        self._themed(collect_frame, "panelbg")
        self._train_collect_frame = collect_frame
        collect_frame.pack(fill="x", padx=8, pady=4)

        self._train_lora_dir = tk.StringVar(value=self.settings.train_lora_dir)
        dir_row = tk.Frame(collect_frame, bg=pal["surface"])
        dir_row.pack(fill="x", padx=8, pady=2)
        self._themed(dir_row, "panelbg")
        dir_label = self._label(
            dir_row, text="Local folder:", bg=pal["surface"], fg=pal["muted"],
            anchor="w", font=("Segoe UI", 9), panel=True,
        )
        dir_label.pack(side="left")
        self._themed(dir_label, "muted")
        self._train_lora_dir_entry = self._entry(
            dir_row, textvariable=self._train_lora_dir, width=48
        )
        self._train_lora_dir_entry.pack(side="left", padx=(6, 4), fill="x", expand=True)
        self._btn_train_lora_dir = self._make_button(
            dir_row, "Browse…", self._choose_train_lora_dir, variant="quiet"
        )
        self._btn_train_lora_dir.pack(side="left")

        collect_btn_row = tk.Frame(collect_frame, bg=pal["surface"])
        collect_btn_row.pack(fill="x", padx=8, pady=(6, 2))
        self._themed(collect_btn_row, "panelbg")
        self._btn_train_collect = self._make_button(
            collect_btn_row, "Collect the LoRAs", self._collect_train_loras,
            variant="primary",
        )
        self._btn_train_collect.pack(side="left")
        self._btn_train_refresh = self._make_button(
            collect_btn_row, "Lister sur le pod", self._list_train_loras,
            variant="quiet",
        )
        self._btn_train_refresh.pack(side="left", padx=(6, 0))
        self._train_collect_status = self._label(
            collect_frame, text="", bg=pal["surface"], fg=pal["muted"], anchor="w",
            justify="left", font=("Segoe UI", 8), panel=True,
        )
        self._train_collect_status.pack(fill="x", padx=8, pady=(2, 6))
        self._themed(self._train_collect_status, "muted")

        # -- Privacy ---------------------------------------------------------
        privacy_frame = self._lframe(
            train_inner, text="  Privacy  ", bg=pal["bg"], fg=pal["fg"],
            font=("Segoe UI", 10, "bold"), padx=6, pady=6,
            highlightthickness=1, highlightbackground=pal["border"],
        )
        self._themed(privacy_frame, "panelbg")
        privacy_frame.pack(fill="x", padx=8, pady=4)
        for line in (
            "• SSH tunnel access only — ports 6080/8080 are never "
            "published on the pod's public URL.",
            "• The image runs a pinned, audited Fizgig commit; the pod cannot "
            "fetch any other source code.",
            "• Models and datasets stay on the pod's volume; the LoRAs come "
            "back locally, never through a third-party service.",
            "• Telemetry off (HF_HUB_DISABLE_TELEMETRY=1). The application's "
            "only outbound request is the pod stop, and only if you enable "
            "it.",
        ):
            bullet = self._label(
                privacy_frame, text=line, bg=pal["surface"], fg=pal["muted"],
                anchor="w", justify="left", font=("Segoe UI", 8), panel=True,
            )
            bullet.pack(fill="x", padx=8, pady=1)
            self._themed(bullet, "muted")

        # ------------------------------------------------------------------
        # Tab 1b — Models & presets: model catalog (checkboxes) +
        # preset editor (models/nodes/workflows), in its own tab
        # pour lui donner plus d'espace.
        # ------------------------------------------------------------------
        models_outer, models_inner = self._make_scrollable(self._notebook)
        self._notebook.add(models_outer, text=t(TAB_MODELS))
        self._comfy_models_tab_inner = models_inner

        # Models & presets (fully modular) — selection by category with
        # checkboxes, adding a model by URL, and named presets.
        models_frame = self._lframe(
            models_inner, text="Models & presets (ComfyUI pod)", bg=pal["bg"],
            fg=pal["fg"], font=("Segoe UI", 10, "bold"),
        )
        self._themed(models_frame, "panelbg")
        self._comfy_models_frame = models_frame
        # The model checklist is the tab's main surface: it absorbs the
        # spare vertical space (the summary line stays just below it).
        self._comfy_models_frame.pack(fill="both", expand=True, padx=8, pady=4)

        # preset_row wraps onto two sub-rows: the selector + actions on the
        # first line, the explanatory hint on the next (it is the wide part).
        preset_row = tk.Frame(models_frame, bg=pal["surface"])
        preset_row.pack(fill="x", padx=8, pady=(2, 2))
        self._themed(preset_row, "panelbg")
        preset_a = tk.Frame(preset_row, bg=pal["surface"])
        preset_a.pack(fill="x", side="top")
        self._themed(preset_a, "panelbg")
        preset_b = tk.Frame(preset_row, bg=pal["surface"])
        preset_b.pack(fill="x", side="top")
        self._themed(preset_b, "panelbg")
        self._label(
            preset_a, text="Preset:", bg=pal["surface"], fg=pal["fg"], panel=True
        ).pack(side="left")
        self._themed(preset_a.winfo_children()[-1], "bg")
        self._comfy_preset_combo = ttk.Combobox(
            preset_a, values=[], state="readonly", width=26, style="Forge.TCombobox",
        )
        self._comfy_preset_combo.pack(side="left", padx=(4, 8))
        self._comfy_preset_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._comfy_load_preset()
        )
        self._btn_save_preset = self._make_button(
            preset_a, "💾 Save", self._comfy_save_preset, variant="quiet"
        )
        self._btn_save_preset.pack(side="left", padx=(0, 6))
        self._btn_preset_duplicate = self._make_button(
            preset_a, "⧉ Duplicate", self._comfy_duplicate_preset, variant="quiet"
        )
        self._btn_preset_duplicate.pack(side="left", padx=(0, 6))
        self._tooltip(
            self._btn_preset_duplicate,
            "Duplicates the selected preset under a new name (a starting point).",
        )
        # The destructive preset action sits right next to the combo it
        # acts on (not isolated in a corner), and says so in its tooltip.
        self._btn_preset_delete = self._make_button(
            preset_a, PRESET_DELETE_LABEL, self._comfy_delete_preset,
            variant="danger",
        )
        self._btn_preset_delete.pack(side="left", padx=(0, 6))
        self._tooltip(self._btn_preset_delete, PRESET_DELETE_TOOLTIP)
        self._label(
            preset_b,
            text="Models (ticked boxes) + custom nodes + workflows below; Save freezes all of it under one name.",
            bg=pal["surface"], fg=pal["muted"], font=("Segoe UI", 8), panel=True,
        ).pack(side="left")
        self._themed(preset_b.winfo_children()[-1], "muted")

        self._comfy_model_rows = tk.Frame(models_frame, bg=pal["surface"])
        self._comfy_model_rows.pack(fill="x", padx=8, pady=(2, 6))
        self._themed(self._comfy_model_rows, "panelbg")
        # The checkbox labels are truncated to a pixel budget: re-derive it
        # when the panel is resized (the rows are built before the widgets
        # are mapped, so the first pass has no real width yet).
        self._comfy_rows_width = 0
        models_frame.bind("<Configure>", self._on_comfy_rows_configure)
        # Start the editor on the ACTIVE preset: otherwise the models the pod
        # will actually receive — the "extra" ones in particular — are not
        # visible until the user re-selects the preset in the dropdown.
        self._apply_preset_to_editor(self._comfy_preset.get().strip())
        self._rebuild_comfy_model_rows()

        # Custom nodes & workflows of the preset (dedicated editor, below the
        # model list). The entries of the loaded preset are edited here; they
        # are frozen into _comfy_presets by « Save ».
        self._comfy_extra_rows = tk.Frame(models_frame, bg=pal["surface"])
        self._comfy_extra_rows.pack(fill="x", padx=8, pady=(2, 6))
        self._themed(self._comfy_extra_rows, "panelbg")
        self._rebuild_comfy_extra_rows()

        self._comfy_summary_card = tk.Frame(
            comfy_inner, bg=pal["surface"], highlightthickness=1,
            highlightbackground=pal["panel_border"], bd=0,
        )
        self._themed(self._comfy_summary_card, "card")
        self._comfy_summary_card.pack(fill="x", padx=PAD_L, pady=(0, PAD_M))
        self._comfy_summary_label = self._label(
            self._comfy_summary_card,
            text=comfy_summary_text(
                self._comfy_preset.get(), self._comfy_tier.get(),
                self._comfy_workflows.get(), self._comfy_models,
            ),
            bg=pal["surface"], fg=pal["muted"], anchor="w", font=("Segoe UI", 9),
            justify="left", panel=True,
        )
        self._comfy_summary_label.pack(fill="x", padx=PAD_M, pady=(PAD_S, PAD_S))
        self._themed(self._comfy_summary_label, "muted")

        # ------------------------------------------------------------------
        # Tab 2b — Library (LoRA / Workflow / Node directory): create/edit
        # form + a grid of compact Steam-like cards, per-model filter, and pod
        # operations. Everything is gated on comfy, like the tab
        # ComfyUI.
        # ------------------------------------------------------------------
        biblio_outer, biblio_inner = self._make_scrollable(self._notebook)
        self._notebook.add(biblio_outer, text=t(TAB_LIBRARY))
        self._biblio_inner = biblio_inner

        self._annuaire_frame = tk.Frame(biblio_inner, bg=pal["bg"])
        self._annuaire_frame.pack(fill="both", expand=True)
        self._themed(self._annuaire_frame, "bg")

        # Header: « ➕ New » + per-model filter.
        biblio_header = tk.Frame(self._annuaire_frame, bg=pal["bg"])
        biblio_header.pack(fill="x", padx=8, pady=(8, 4))
        self._themed(biblio_header, "bg")
        self._btn_annuaire_add = self._make_button(
            biblio_header, "➕ New", self._annuaire_new
        )
        self._btn_annuaire_add.pack(side="left", padx=(0, 8))
        self._make_button(
            biblio_header, "📄 Exporter .md", self._annuaire_export_md,
            variant="quiet",
        ).pack(side="left", padx=(0, 8))
        self._make_button(
            biblio_header, "💾 Exporter .json", self._annuaire_export_json,
            variant="quiet",
        ).pack(side="left", padx=(0, 8))
        self._make_button(
            biblio_header, "📥 Importer", self._annuaire_import_json,
            variant="quiet",
        ).pack(side="left", padx=(0, 8))
        # The userscript's counterpart: the entry it builds is on the
        # clipboard, one click away from the library.
        self._make_button(
            biblio_header, "📋 Paste", self._annuaire_paste_json,
            variant="quiet",
        ).pack(side="left", padx=(0, 12))
        # The "download automatically at pod startup" switch: one checkbox per
        # card, plus a bulk pair and a live counter here.
        self._make_button(
            biblio_header, "☑ Tout cocher",
            lambda: self._annuaire_set_all(True), variant="quiet",
        ).pack(side="left", padx=(0, 6))
        self._make_button(
            biblio_header, "☐ Clear all",
            lambda: self._annuaire_set_all(False), variant="quiet",
        ).pack(side="left", padx=(0, 8))
        self._annuaire_counter = self._label(
            biblio_header, text="", bg=pal["bg"], fg=pal["muted"],
            font=("Segoe UI", 9),
        )
        self._annuaire_counter.pack(side="left", padx=(0, 12))
        self._themed(self._annuaire_counter, "muted")
        self._label(
            biblio_header, text="Filter by model:", bg=pal["bg"],
            fg=pal["muted"], font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 4))
        self._themed(biblio_header.winfo_children()[-1], "muted")
        self._annuaire_filter = tk.StringVar(self.root, value="")
        filter_entry = self._entry(
            biblio_header, textvariable=self._annuaire_filter, width=20,
            bg=pal["input_bg"], fg=pal["input_fg"],
            insertbackground=pal["fg"], relief="flat",
        )
        filter_entry.pack(side="left", padx=(0, 4))
        self._themed(filter_entry, "input")
        # Typing filters live, but the grid is synced once the user pauses:
        # a rebuild per keystroke cost ~400 ms with a few dozen entries.
        filter_entry.bind(
            "<KeyRelease>", lambda _e: self._schedule_annuaire_rebuild(250)
        )

        # Form (create/edit) — hidden until « ➕ Nouveau » / « ✏️ Éditer ».
        self._annuaire_form = self._lframe(
            self._annuaire_frame, text="New entry", bg=pal["bg"],
            fg=pal["fg"], font=("Segoe UI", 10, "bold"),
        )
        self._themed(self._annuaire_form, "panelbg")
        form = self._annuaire_form
        form.columnconfigure(1, weight=1)

        form_head = tk.Frame(form, bg=pal["bg"])
        form_head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(6, 2))
        self._themed(form_head, "bg")
        self._label(
            form_head, text="Type:", bg=pal["bg"], fg=pal["muted"], font=("Segoe UI", 9)
        ).pack(side="left")
        self._themed(form_head.winfo_children()[-1], "muted")
        self._form_type = ttk.Combobox(
            form_head, values=list(ANNUAIRE_TYPES), state="readonly",
            width=8, style="Forge.TCombobox",
        )
        self._form_type.set("lora")
        self._form_type.pack(side="left", padx=(4, 12))
        self._form_type.bind(
            "<<ComboboxSelected>>", lambda _e: self._refresh_form_local_hint()
        )
        self._label(
            form_head, text="Mode:", bg=pal["bg"], fg=pal["muted"], font=("Segoe UI", 9)
        ).pack(side="left")
        self._themed(form_head.winfo_children()[-1], "muted")
        self._form_mode = ttk.Combobox(
            form_head, values=list(ANNUAIRE_MODE_LABELS.values()),
            state="readonly", width=11, style="Forge.TCombobox",
        )
        self._form_mode.set("—")
        self._form_mode.pack(side="left", padx=(4, 0))

        self._form_vars: dict = {}
        for row, (key, label_text) in enumerate(
            (
                ("name", "Name:"),
                ("page_url", "Lien page :"),
            ),
            start=1,
        ):
            lbl = self._label(
                form, text=label_text, bg=pal["bg"], fg=pal["muted"],
                font=("Segoe UI", 9),
            )
            lbl.grid(row=row, column=0, sticky="w", padx=(6, 4), pady=2)
            self._themed(lbl, "muted")
            var = tk.StringVar(form, value="")
            entry = self._entry(
                form, textvariable=var,
                bg=pal["input_bg"], fg=pal["input_fg"],
                insertbackground=pal["fg"], relief="flat",
            )
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 6), pady=2)
            self._themed(entry, "input")
            self._form_vars[key] = var

        # -- Source: a download URL, or a file/folder on THIS machine --------
        # The pod can only download URLs (its installer rejects anything that
        # is not http(s)), so a local path is uploaded by the launcher over the
        # tunnel instead. The radio makes which one is in play unambiguous.
        source_lbl = self._label(
            form, text="Source:", bg=pal["bg"], fg=pal["muted"],
            font=("Segoe UI", 9),
        )
        source_lbl.grid(row=3, column=0, sticky="w", padx=(6, 4), pady=2)
        self._themed(source_lbl, "muted")
        self._form_source = tk.StringVar(form, value=ANNUAIRE_SOURCE_URL)
        source_row = tk.Frame(form, bg=pal["bg"])
        source_row.grid(row=3, column=1, sticky="ew", padx=(0, 6), pady=2)
        self._themed(source_row, "bg")
        self._form_source_radios = {}
        for value, label in (
            (ANNUAIRE_SOURCE_URL, "URL (the pod downloads it)"),
            (ANNUAIRE_SOURCE_LOCAL, "Local file (the launcher uploads it)"),
        ):
            rb = self._radio(
                source_row, text=label, variable=self._form_source, value=value,
                bg=pal["bg"], fg=pal["fg"], activebackground=pal["bg"],
                activeforeground=pal["fg"], selectcolor=pal["bg_alt"],
                anchor="w", command=self._on_form_source_change,
            )
            rb.pack(side="left", padx=(0, 12))
            self._themed(rb, "check")
            self._form_source_radios[value] = rb

        dl_lbl = self._label(
            form, text="Lien DL :", bg=pal["bg"], fg=pal["muted"],
            font=("Segoe UI", 9),
        )
        dl_lbl.grid(row=4, column=0, sticky="w", padx=(6, 4), pady=2)
        self._themed(dl_lbl, "muted")
        self._form_dl_url = tk.StringVar(form, value="")
        self._form_dl_entry = self._entry(
            form, textvariable=self._form_dl_url,
            bg=pal["input_bg"], fg=pal["input_fg"],
            insertbackground=pal["fg"], relief="flat",
        )
        self._form_dl_entry.grid(row=4, column=1, sticky="ew", padx=(0, 6), pady=2)
        self._themed(self._form_dl_entry, "input")

        local_lbl = self._label(
            form, text="Chemin local :", bg=pal["bg"], fg=pal["muted"],
            font=("Segoe UI", 9),
        )
        local_lbl.grid(row=5, column=0, sticky="w", padx=(6, 4), pady=2)
        self._themed(local_lbl, "muted")
        local_row = tk.Frame(form, bg=pal["bg"])
        local_row.grid(row=5, column=1, sticky="ew", padx=(0, 6), pady=2)
        self._themed(local_row, "bg")
        local_row.columnconfigure(0, weight=1)
        self._form_local_path = tk.StringVar(form, value="")
        self._form_local_entry = self._entry(
            local_row, textvariable=self._form_local_path,
            bg=pal["input_bg"], fg=pal["input_fg"],
            insertbackground=pal["fg"], relief="flat",
        )
        self._form_local_entry.grid(row=0, column=0, sticky="ew")
        self._themed(self._form_local_entry, "input")
        self._btn_form_browse = self._make_button(
            local_row, "📂 Browse…", self._annuaire_pick_local, variant="quiet"
        )
        self._btn_form_browse.grid(row=0, column=1, padx=(6, 0))
        self._form_local_hint = self._label(
            form, text="", bg=pal["bg"], fg=pal["muted"], anchor="w",
            justify="left", font=("Segoe UI", 8),
        )
        self._form_local_hint.grid(
            row=6, column=0, columnspan=2, sticky="w", padx=(6, 6), pady=(0, 2)
        )
        self._themed(self._form_local_hint, "muted")

        self._form_vars["trigger_words"] = tk.StringVar(form, value="")
        tw_lbl = self._label(
            form, text="Trigger words:", bg=pal["bg"], fg=pal["muted"],
            font=("Segoe UI", 9),
        )
        tw_lbl.grid(row=7, column=0, sticky="w", padx=(6, 4), pady=2)
        self._themed(tw_lbl, "muted")
        tw_entry = self._entry(
            form, textvariable=self._form_vars["trigger_words"],
            bg=pal["input_bg"], fg=pal["input_fg"],
            insertbackground=pal["fg"], relief="flat",
        )
        tw_entry.grid(row=7, column=1, sticky="ew", padx=(0, 6), pady=2)
        self._themed(tw_entry, "input")

        model_lbl = self._label(
            form, text="Model:", bg=pal["bg"], fg=pal["muted"], font=("Segoe UI", 9)
        )
        model_lbl.grid(row=8, column=0, sticky="w", padx=(6, 4), pady=2)
        self._themed(model_lbl, "muted")
        self._form_model = ttk.Combobox(
            form, values=[], state="normal", style="Forge.TCombobox",
        )
        self._form_model.grid(row=8, column=1, sticky="ew", padx=(0, 6), pady=2)

        note_lbl = self._label(
            form, text="Note:", bg=pal["bg"], fg=pal["muted"], font=("Segoe UI", 9)
        )
        note_lbl.grid(row=9, column=0, sticky="nw", padx=(6, 4), pady=(2, 6))
        self._themed(note_lbl, "muted")
        self._form_note = tk.Text(
            form, height=2, wrap="word",
            bg=pal["log_bg"], fg=pal["log_fg"],
            insertbackground=pal["fg"], relief="flat", font=("Segoe UI", 9),
        )
        self._form_note.grid(row=9, column=1, sticky="ew", padx=(0, 6), pady=(2, 6))
        self._themed(self._form_note, "logtext")

        form_actions = tk.Frame(form, bg=pal["bg"])
        form_actions.grid(row=10, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        self._themed(form_actions, "bg")
        self._make_button(
            form_actions, "💾 Save", self._annuaire_save_form, variant="primary"
        ).pack(side="left", padx=(0, 6))
        self._make_button(
            form_actions, "Cancel", self._annuaire_cancel_form, variant="quiet"
        ).pack(side="left")

        # Pod operations toolbar (Lister / Sync vault / Outputs / Retirer).
        self._pod_actions_frame = tk.Frame(self._annuaire_frame, bg=pal["bg"])
        self._pod_actions_frame.pack(fill="x", padx=8, pady=(4, 4))
        self._themed(self._pod_actions_frame, "bg")
        self._make_button(
            self._pod_actions_frame, "List the pod", self._pod_list, variant="quiet"
        ).pack(side="left", padx=(0, 6))
        self._make_button(
            self._pod_actions_frame, "Sync vault", self._vault_sync, variant="quiet"
        ).pack(side="left", padx=(0, 6))
        self._make_button(
            self._pod_actions_frame, "Download the outputs",
            self._outputs_download, variant="quiet",
        ).pack(side="left", padx=(0, 12))
        self._label(
            self._pod_actions_frame, text="Remove from the pod:", bg=pal["bg"],
            fg=pal["muted"], font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 4))
        self._themed(self._pod_actions_frame.winfo_children()[-1], "muted")
        self._pod_remove_name = tk.StringVar(self.root, value="")
        remove_entry = self._entry(
            self._pod_actions_frame, textvariable=self._pod_remove_name, width=16,
            bg=pal["input_bg"], fg=pal["input_fg"],
            insertbackground=pal["fg"], relief="flat",
        )
        remove_entry.pack(side="left", padx=(0, 4))
        self._themed(remove_entry, "input")
        self._make_button(
            self._pod_actions_frame, "Remove", self._pod_remove, variant="quiet"
        ).pack(side="left")

        # Grid of compact cards.
        self._annuaire_grid = tk.Frame(self._annuaire_frame, bg=pal["bg"])
        self._annuaire_grid.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._themed(self._annuaire_grid, "bg")
        self._annuaire_grid.bind("<Configure>", self._on_biblio_grid_configure)

        # The cards are built **once the window is up** (idle time), never
        # here: this code runs before the frame has a width, so the cards
        # would be created at a guessed width and thrown away one frame later
        # (a full rebuild for nothing). Building them while the dashboard is
        # on screen also keeps the ~300 ms of widget creation off the
        # Library tab's first-open path.
        self._update_annuaire_counter()
        self.root.after_idle(self._warm_annuaire_grid)

        # ------------------------------------------------------------------
        # Tab 6 — Settings: .env, the timer and the configuration recap.
        # Credentials are not a tab: they live in the Settings dialog,
        # rebuilt on every open.
        # ------------------------------------------------------------------
        config_outer, config_inner = self._make_scrollable(self._notebook)
        self._notebook.add(config_outer, text=t(TAB_SETTINGS))
        self._config_tab_inner = config_inner

        # This tab holds the model choice and its llama.cpp options, then the
        # .env and timer blocks — all anchored at the top — plus a recap card
        # that absorbs the spare height (the blocks used to be spread over the
        # whole tab, which read as unrelated rows floating apart).

        # .env row.
        env_region = tk.Frame(config_inner, bg=pal["bg"])
        env_region.pack(fill="x")
        self._themed(env_region, "bg")
        env_row = tk.Frame(env_region, bg=pal["bg"])
        env_row.pack(fill="x", **pad)
        self._themed(env_row, "bg")
        self._label(
            env_row, text=".env :", bg=pal["bg"], fg=pal["fg"]
        ).pack(side="left")
        self._themed(env_row.winfo_children()[-1], "bg")
        self._env_label = self._label(
            env_row,
            text=self._env_status_text(),
            bg=pal["bg"],
            fg=pal["muted"],
        )
        self._env_label.pack(side="left", padx=(4, 8))
        self._themed(self._env_label, "muted")
        self._make_button(env_row, "Create .env", self._create_env).pack(side="left", padx=(0, 6))
        self._make_button(env_row, "Edit .env", self._edit_env).pack(side="left")

        # Timer: stop the pod after a delay, then optionally shutdown/sleep.
        timer_region = tk.Frame(config_inner, bg=pal["bg"])
        timer_region.pack(fill="x")
        self._themed(timer_region, "bg")
        timer_frame = self._lframe(
            timer_region, text="Timer", bg=pal["bg"], fg=pal["fg"],
            font=("Segoe UI", 10, "bold"),
        )
        timer_frame.pack(fill="x", **pad)
        self._themed(timer_frame, "panelbg")
        self._label(
            timer_frame, text="Duration (minutes):", bg=pal["bg"], fg=pal["fg"]
        ).pack(side="left", padx=(8, 4))
        self._themed(timer_frame.winfo_children()[-1], "bg")
        self._timer_minutes_entry = self._entry(
            timer_frame, width=6, bg=pal["input_bg"], fg=pal["input_fg"],
            insertbackground=pal["fg"], relief="flat", justify="center",
        )
        self._timer_minutes_entry.insert(0, "60")
        self._timer_minutes_entry.pack(side="left")
        self._themed(self._timer_minutes_entry, "input")
        self._timer_start_btn = self._make_button(
            timer_frame, "Start the timer", self._timer_start
        )
        self._timer_start_btn.pack(side="left", padx=(6, 6))
        self._timer_cancel_btn = self._make_button(
            timer_frame, "Cancel", self._timer_cancel
        )
        self._timer_cancel_btn.pack(side="left")
        self._timer_label = self._label(
            timer_frame, text="—", bg=pal["bg"], fg=pal["fg"],
            font=("Consolas", 11, "bold"),
        )
        self._timer_label.pack(side="left", padx=(10, 6))
        self._themed(self._timer_label, "bg")

        self._power_mode = tk.StringVar(self.root, value="none")
        power_row = tk.Frame(timer_frame, bg=pal["bg"])
        power_row.pack(fill="x", padx=PAD_M, pady=(PAD_XS, PAD_S))
        self._themed(power_row, "bg")
        self._label(
            power_row, text="After the pod stops:", bg=pal["bg"], fg=pal["muted"]
        ).pack(side="left")
        self._themed(power_row.winfo_children()[-1], "muted")
        for mode in POWER_ACTIONS:
            rb = self._radio(
                power_row,
                text=POWER_ACTION_LABELS[mode],
                variable=self._power_mode,
                value=mode,
                bg=pal["bg"],
                fg=pal["fg"],
                activebackground=pal["bg"],
                activeforeground=pal["fg"],
                selectcolor=pal["bg_alt"],
                anchor="w",
            )
            rb.pack(side="left", padx=(6, 0))
            self._themed(rb, "check")

        # Recap card: what this tab is currently set to run. It absorbs the
        # spare height so the three blocks above stay anchored at the top
        # without leaving a dead zone below them.
        self._config_recap = tk.Frame(
            config_inner, bg=pal["surface"], highlightthickness=1,
            highlightbackground=pal["panel_border"], bd=0,
        )
        self._themed(self._config_recap, "card")
        self._config_recap.pack(fill="x", padx=PAD_M, pady=PAD_S)
        recap_title = self._label(
            self._config_recap, text="Summary", bg=pal["surface"],
            fg=pal["fg"], font=("Segoe UI", 10, "bold"), anchor="w", panel=True,
        )
        recap_title.grid(row=0, column=0, columnspan=2, sticky="w",
                         padx=PAD_M, pady=(PAD_S, PAD_XS))
        self._themed(recap_title, "bg")
        self._config_recap_values: dict = {}
        self._config_recap_rows: dict = {}
        for row, (key, label_text) in enumerate(
            (
                ("stack", "Stack"),
                ("model", "Preset / image"),
                ("url", "Local URL"),
                ("env", ".env"),
                ("timer", "Timer"),
            ),
            start=1,
        ):
            key_label = self._label(
                self._config_recap, text=f"{label_text} :", bg=pal["surface"],
                fg=pal["muted"], font=("Segoe UI", 9), anchor="w", panel=True,
            )
            key_label.grid(row=row, column=0, sticky="w",
                           padx=(PAD_M, PAD_S), pady=(0, PAD_XS))
            self._themed(key_label, "muted")
            value_label = self._label(
                self._config_recap, text="—", bg=pal["surface"], fg=pal["fg"],
                font=("Segoe UI", 9), anchor="w", panel=True,
            )
            value_label.grid(row=row, column=1, sticky="ew",
                             padx=(0, PAD_M), pady=(0, PAD_XS))
            self._themed(value_label, "bg")
            self._config_recap_values[key] = value_label
            self._config_recap_rows[key] = (key_label, value_label)
        self._config_recap.columnconfigure(1, weight=1)
        self._refresh_config_recap()
        # Spare height goes to a plain filler rather than to the recap card:
        # the blocks stay anchored at the top and the tab is still painted
        # edge to edge (no dead canvas strip, no oversized empty card).
        self._config_filler = tk.Frame(config_inner, bg=pal["bg"])
        self._themed(self._config_filler, "bg")
        self._config_filler.pack(fill="both", expand=True)
        # ------------------------------------------------------------------
        # Journal — merged into the dashboard (always visible, no tab
        # switching) with its minimal controls: level filters, clear, copy,
        # auto-scroll.
        # ------------------------------------------------------------------
        self._log_frame = self._lframe(
            dash_inner, text="Journal", bg=pal["bg"], fg=pal["fg"],
            font=("Segoe UI", 10, "bold"),
        )
        # The journal is the dashboard's main surface: it absorbs all the
        # spare vertical space so the tab never ends on a dead zone.
        self._log_frame.pack(fill="both", expand=True, **pad)
        self._themed(self._log_frame, "panelbg")
        log_controls = tk.Frame(self._log_frame, bg=pal["surface"])
        log_controls.pack(fill="x", padx=8, pady=(4, 2))
        self._themed(log_controls, "panelbg")
        for filter_var, label in (
            (self._log_filter_info, "INFO"),
            (self._log_filter_warn, "WARN"),
            (self._log_filter_error, "ERROR"),
        ):
            cb = self._check(
                log_controls, text=label, variable=filter_var,
                bg=pal["surface"], fg=pal["fg"],
                activebackground=pal["surface"], activeforeground=pal["fg"],
                selectcolor=pal["bg_alt"], anchor="w",
                command=self._log_filter_changed,
                panel=True,
            )
            cb.pack(side="left", padx=(0, 8))
            self._themed(cb, "check")
        self._log_autoscroll_btn = self._check(
            log_controls, text="Auto-scroll", variable=self._log_autoscroll,
            bg=pal["surface"], fg=pal["fg"],
            activebackground=pal["surface"], activeforeground=pal["fg"],
            selectcolor=pal["bg_alt"], anchor="w",
            panel=True,
        )
        self._log_autoscroll_btn.pack(side="left", padx=(0, 8))
        self._themed(self._log_autoscroll_btn, "check")
        self._make_button(
            log_controls, "Copy", self._log_copy, variant="quiet"
        ).pack(side="left", padx=(8, 0))
        self._make_button(
            log_controls, "Clear", self._log_clear, variant="quiet"
        ).pack(side="left")
        log_inner = tk.Frame(self._log_frame, bg=pal["log_bg"])
        log_inner.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self._themed(log_inner, "log_bg_frame")
        self._log_inner = log_inner
        scrollbar = tk.Scrollbar(log_inner)
        scrollbar.pack(side="right", fill="y")
        self._log_text = tk.Text(
            log_inner,
            wrap="word",
            state="disabled",
            bg=pal["log_bg"],
            fg=pal["log_fg"],
            insertbackground=pal["fg"],
            relief="flat",
            font=("Consolas", 9),
            height=10,
            yscrollcommand=scrollbar.set,
        )
        self._log_text.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self._log_text.yview)
        self._themed(self._log_text, "logtext")
        self._log_tag_color = {
            "lvl_info": "log_fg",
            "lvl_ok": "ok",
            "lvl_warn": "warn",
            "lvl_error": "error",
        }
        for tag, color_key in self._log_tag_color.items():
            self._log_text.tag_config(tag, foreground=pal[color_key])

        # Footer: the single persistent control bar — mode, the two stack
        # toggles and start/stop/diagnostic/open/terminate/quit stay
        # reachable from every tab without going back to the dashboard.
        # (The "minimize on close" setting lives in the Parametres dialog
        # only.)
        footer = tk.Frame(container, bg=pal["bg"])
        footer.pack(fill="x", **pad)
        self._themed(footer, "bg")
        self._footer = footer
        # The persistent bar wraps onto three sub-rows so it never overflows
        # on a narrow window: the mode radios + stack toggles on the first
        # line, the start/stop/diagnostic/open/terminate/quit actions on the
        # next, and the agent-CLI selector on the last.
        footer_a = tk.Frame(footer, bg=pal["bg"])
        footer_a.pack(fill="x", side="top")
        self._themed(footer_a, "bg")
        footer_b = tk.Frame(footer, bg=pal["bg"])
        footer_b.pack(fill="x", side="top")
        self._themed(footer_b, "bg")
        footer_c = tk.Frame(footer, bg=pal["bg"])
        footer_c.pack(fill="x", side="top")
        self._themed(footer_c, "bg")
        self._footer_agent_row = footer_c
        self._label(
            footer_a, text="Mode:", font=("Segoe UI", 10, "bold"),
            bg=pal["bg"], fg=pal["fg"],
        ).pack(side="left", padx=(0, 4))
        self._themed(footer_a.winfo_children()[-1], "bg")
        self._bar_stack_radios = {}
        for mode in ("comfy", "train"):
            rb = self._radio(
                footer_a,
                text=STACK_LABELS[mode],
                variable=self._mode,
                value=mode,
                bg=pal["bg"],
                fg=pal["fg"],
                activebackground=pal["bg"],
                activeforeground=pal["fg"],
                selectcolor=pal["bg_alt"],
                anchor="w",
                command=self._on_mode_change,
            )
            rb.pack(side="left", padx=(6, 0))
            self._themed(rb, "check")
            self._bar_stack_radios[mode] = rb
        # Stack toggle: a single green/red status button for the comfy
        # stack's local SSH tunnel. A click starts or stops that process
        # only — never the pod.
        self._toggle_comfy = self._make_button(
            footer_a, "ComfyUI", self._toggle_comfy_action, variant="quiet"
        )
        self._toggle_comfy.pack(side="left")
        self._bar_start_btn = self._make_button(
            footer_b, "▶ Start", self._start_action, variant="primary"
        )
        self._bar_start_btn.pack(side="left", padx=(12, 6))
        self._bar_stop_btn = self._make_button(
            footer_b, "■ Stop", lambda: self._stop_action(False)
        )
        self._bar_stop_btn.pack(side="left", padx=(0, 8))
        # Destructive action: full danger fill, grouped with the pod
        # lifecycle actions it belongs to (it is the radical variant of
        # « Stop ») and kept well away from the routine « Quit »
        # button on the far right — a click on the red one always asks for
        # explicit confirmation.
        self._btn_stop_term = self._make_button(
            footer_b, "Terminate the pod", lambda: self._stop_action(True),
            variant="danger",
        )
        self._btn_stop_term.pack(side="left", padx=(0, 8))
        self._btn_doctor = self._make_button(
            footer_b, "Diagnostics", self._doctor_action, variant="quiet"
        )
        self._btn_doctor.pack(side="left", padx=(0, 6))
        self._btn_open = self._make_button(
            footer_b, "Open ComfyUI ↗", self._open_comfy_button, variant="quiet"
        )
        self._btn_open.pack(side="left")
        for btn in (
            self._bar_start_btn, self._bar_stop_btn, self._btn_stop_term,
            self._btn_doctor, self._btn_open, self._toggle_comfy,
        ):
            self._action_buttons.append(btn)
        # « Quitter » stands alone at the far right: the only button with
        # nothing next to it, so the destructive red one can never be its
        # accidental neighbour.
        self._make_button(footer_b, "Quit", self._quit).pack(side="right")

        self._update_toggle_buttons()

    def _make_scrollable(self, parent: "tk.Widget") -> tuple["tk.Frame", "tk.Frame"]:
        """A scrollable content area (canvas + vertical scrollbar).

        Returns ``(outer, inner)``: *outer* is the tab body to hand to the
        notebook, *inner* is the frame children are packed into. The inner
        frame tracks the canvas width (so rows stretch to the window) and
        mouse-wheel scrolling is active while the pointer is over the area.
        """
        pal = self._pal
        outer = tk.Frame(parent, bg=pal["bg"])
        canvas = tk.Canvas(outer, bg=pal["bg"], highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=pal["bg"])
        self._themed(outer, "bg")
        self._themed(canvas, "bg")
        self._themed(vsb, "bg")
        self._themed(inner, "bg")
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        # The canvas window item is kept at least as tall as the viewport,
        # so a tab shorter than the window never leaves a dead canvas strip
        # below its content: the inner frame's background fills the visible
        # area and any child packed with expand=True absorbs the spare
        # height. When the content outgrows the viewport the item height
        # follows the content's *requested* height (winfo_reqheight keeps
        # reporting the children's extent even while the item clamps the
        # frame), so scrolling on small windows stays intact.
        state = {"forced": 0, "width": 0}

        def _sync(_event=None) -> None:
            try:
                if not canvas.winfo_exists():
                    return
            except tk.TclError:
                return
            natural = inner.winfo_reqheight()
            viewport = canvas.winfo_height()
            width = canvas.winfo_width()
            forced = max(natural, viewport)
            if forced != state["forced"] or width != state["width"]:
                state["forced"] = forced
                state["width"] = width
                canvas.itemconfigure(window_id, width=width, height=forced)
            canvas.configure(scrollregion=canvas.bbox("all"))

        inner.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync)
        self._scrollables.append((canvas, inner, state, _sync))

        # Tag the canvas so the global wheel handler (see ``_on_mousewheel``)
        # can find it when the pointer is over any descendant widget. The old
        # Enter/Leave ``bind_all`` approach stopped scrolling as soon as the
        # pointer moved onto a child widget (tkinter does not propagate
        # <Enter>/<Leave> to parents), which is why the wheel only worked on
        # the empty margins.
        canvas._forge_scroll = lambda delta: _scroll_canvas_clamped(canvas, delta)

        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        return outer, inner

    def _on_mousewheel(self, event) -> None:
        """Route a mouse-wheel event to the scrollable canvas under the pointer.

        Bound once, globally (``bind_all``). Walks up the widget tree from the
        widget under the pointer until it finds a canvas tagged with
        ``_forge_scroll`` (set by :meth:`_make_scrollable`), then scrolls it.
        Works over child widgets (labels, entries, cards) unlike the old
        Enter/Leave approach.
        """
        widget = event.widget
        while widget is not None:
            scroll = getattr(widget, "_forge_scroll", None)
            if scroll is not None:
                scroll(event.delta)
                return
            widget = getattr(widget, "master", None)

    def _make_button(
        self,
        parent,
        text: str,
        command: Callable[[], None],
        *,
        variant: str = "default",
    ) -> "tk.Button":
        text = t(text)
        # ttkbootstrap mode: the variants map onto the ready-made bootstyles
        # (neutral default, outlined secondary for quiet, filled primary /
        # danger) — the theme owns the fills and hover states.
        if self._tb_style is not None:
            bootstyle = {
                "default": None,
                "quiet": "outline-secondary",
                "primary": "primary",
                "danger": "danger",
            }.get(variant)
            if bootstyle:
                return _ttkbootstrap.Button(
                    parent, text=text, command=command, bootstyle=bootstyle
                )
            return _ttkbootstrap.Button(parent, text=text, command=command)
        pal = self._pal
        colors = {
            "default": (pal["button_bg"], pal["button_fg"], pal["button_active"]),
            "quiet": (pal["bg_alt"], pal["button_fg"], pal["button_active"]),
            "primary": (pal["accent"], pal["accent_fg"], pal["accent_active"]),
            "danger": (pal["error"], pal["accent_fg"], pal["error"]),
        }
        bg, fg, active_bg = colors[variant]
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=fg,
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=10,
            pady=6,
            font=("Segoe UI", 9, "bold" if variant == "primary" else "normal"),
            cursor="hand2",
        )
        self._themed(btn, f"button:{variant}")
        return btn

    # -- tooltips -----------------------------------------------------------

    def _tooltip(self, widget, text, delay_ms: int = 400) -> None:
        """Attach a hover tooltip to *widget* (delayed, cancel-safe).

        *text* may be a callable: it is resolved when the popover is about to
        appear, so a tooltip can report live state (e.g. the pod's spend
        window) without rebinding the widget on every refresh.
        """
        self._tooltip_texts[widget] = text() if callable(text) else text
        state: dict = {"after": None}

        def _schedule(_event=None) -> None:
            if state["after"] is None and self._widget_exists(widget):
                state["after"] = self.root.after(
                    delay_ms, lambda: self._show_tooltip_popover(widget, text)
                )

        def _cancel(_event=None) -> None:
            if state["after"] is not None:
                try:
                    self.root.after_cancel(state["after"])
                except tk.TclError:
                    pass
                state["after"] = None
            self._hide_tooltip_popover()

        widget.bind("<Enter>", _schedule)
        widget.bind("<Leave>", _cancel)
        widget.bind("<Button-1>", _cancel)

    def _show_tooltip_popover(self, widget, text: str) -> None:
        self._hide_tooltip_popover()
        if not self._widget_exists(widget):
            return
        if callable(text):
            try:
                text = text()
            except Exception:  # noqa: BLE001 - a tooltip must never raise
                return
        pal = self._pal
        pop = tk.Toplevel(self.root)
        pop.overrideredirect(True)
        pop.configure(bg=pal["bg_alt"], highlightthickness=1,
                     highlightbackground=pal["border"])
        label = _TKMOD.Label(
            pop, text=text, bg=pal["bg_alt"], fg=pal["fg"],
            font=("Segoe UI", 8), justify="left", padx=8, pady=5, anchor="nw",
        )
        label.pack()
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height() + 2
        pop.geometry(f"+{max(0, x)}+{max(0, y)}")
        self._tooltip_popover = pop
        self._tooltip_popover_label = label

    def _hide_tooltip_popover(self) -> None:
        if self._tooltip_popover is not None:
            try:
                if self._tooltip_popover.winfo_exists():
                    self._tooltip_popover.destroy()
            except tk.TclError:
                pass
            self._tooltip_popover = None
            self._tooltip_popover_label = None

    # -- theming -----------------------------------------------------------

    def _apply_theme(self) -> None:
        # The ttkbootstrap theme mode drives every ttk widget at once;
        # the derived palette then re-colors the hand-rolled tk widgets.
        if self._tb_style is not None:
            try:
                self._tb_style.theme_mode = (
                    "dark" if self.settings.theme == "dark" else "light"
                )
            except Exception as exc:  # noqa: BLE001 - defensive: keep the
                # current theme rather than losing the whole repaint.
                logger.warning("Cannot switch theme: %s", exc)
        pal = self._derive_palette(self.settings.theme)
        self._pal = pal
        self._theme_choice.set("Dark" if self.settings.theme == "dark" else "Light")
        self.root.configure(bg=pal["bg"])
        for widget, kind in self._recolor:
            if not self._widget_exists(widget):
                continue
            try:
                self._colorize(widget, kind, pal)
            except tk.TclError:
                pass
        self._colorize(self._log_text, "logtext", pal)
        for tag, color_key in self._log_tag_color.items():
            self._log_text.tag_config(tag, foreground=pal[color_key])
        self._configure_ttk_style(pal)
        try:
            for menu in (
                self._menu,
                self._file_menu,
                self._navigation_menu,
                self._help_menu,
            ):
                menu.configure(
                    bg=pal["bg_alt"],
                    fg=pal["fg"],
                    activebackground=pal["button_active"],
                    activeforeground=pal["fg"],
                )
        except tk.TclError:
            pass
        if self._last_snapshot is not None:
            self._apply_status(self._last_snapshot)

    def _configure_ttk_style(self, pal: Mapping[str, str]) -> None:
        style = self._ttk.Style(self.root)
        try:
            # With a ttkbootstrap theme the Forge styles derive from the
            # theme's base styles (parent) so their layout/elements come
            # from the theme; on the plain clam fallback they are standalone.
            themed = self._tb_style is not None
            parent = {"parent": "TNotebook"} if themed else {}
            parent_tab = {"parent": "TNotebook.Tab"} if themed else {}
            parent_combo = {"parent": "TCombobox"} if themed else {}
            # Label roles: the theme carries the base look; the role styles
            # only set the foreground (+ the surface the label sits on).
            style.configure(
                "ForgeMuted.TLabel", **{"parent": "TLabel"} if themed else {},
                foreground=pal["muted"], background=pal["bg"],
            )
            style.configure(
                "ForgeOk.TLabel", **{"parent": "TLabel"} if themed else {},
                foreground=pal["ok"], background=pal["bg"],
            )
            style.configure(
                "ForgeWarn.TLabel", **{"parent": "TLabel"} if themed else {},
                foreground=pal["warn"], background=pal["bg"],
            )
            style.configure(
                "ForgeError.TLabel", **{"parent": "TLabel"} if themed else {},
                foreground=pal["error"], background=pal["bg"],
            )
            for name, fg in (
                ("ForgePanel.TLabel", pal["fg"]),
                ("ForgePanelMuted.TLabel", pal["muted"]),
                ("ForgePanelOk.TLabel", pal["ok"]),
                ("ForgePanelWarn.TLabel", pal["warn"]),
                ("ForgePanelError.TLabel", pal["error"]),
            ):
                style.configure(
                    name, **{"parent": "TLabel"} if themed else {},
                    foreground=fg, background=pal["surface"],
                )
            style.configure(
                "ForgePanel.TCheckbutton",
                **{"parent": "TCheckbutton"} if themed else {},
                foreground=pal["fg"], background=pal["surface"],
                indicatormargin=(0, 0, INDICATOR_GAP, 0),
            )
            style.configure(
                "ForgePanel.TRadiobutton",
                **{"parent": "TRadiobutton"} if themed else {},
                foreground=pal["fg"], background=pal["surface"],
                indicatormargin=(0, 0, INDICATOR_GAP, 0),
            )
            # Page-level variants: only the indicator gap is set here, so the
            # theme keeps owning the colors (a hard-coded background would be
            # wrong wherever the widget does not sit on the page background).
            style.configure(
                "Forge.TCheckbutton",
                **{"parent": "TCheckbutton"} if themed else {},
                indicatormargin=(0, 0, INDICATOR_GAP, 0),
            )
            style.configure(
                "Forge.TRadiobutton",
                **{"parent": "TRadiobutton"} if themed else {},
                indicatormargin=(0, 0, INDICATOR_GAP, 0),
            )
            style.configure(
                "Forge.TNotebook",
                **parent,
                background=pal["bg"],
                borderwidth=0,
                tabmargins=(0, 8, 0, 0),
            )
            # The selected tab is the accent fill; the inactive tabs sit on
            # a raised tone that is clearly distinct from the page
            # background so the active tab reads at a glance.
            style.configure(
                "Forge.TNotebook.Tab",
                **parent_tab,
                background=pal["bg_alt"],
                foreground=pal["muted"],
                padding=(16, 8),
                borderwidth=0,
            )
            style.map(
                "Forge.TNotebook.Tab",
                background=[
                    ("selected", pal["accent"]),
                    ("active", pal["bg_alt"]),
                ],
                foreground=[
                    ("selected", pal["accent_fg"]),
                    ("active", pal["fg"]),
                ],
            )
            # Framed sections: raised surface with a border lifted toward
            # the foreground, so panels read as cards above the page.
            style.configure(
                "Forge.TLabelframe",
                **({"parent": "TLabelframe"} if themed else {}),
                background=pal["surface"],
                foreground=pal["fg"],
                borderwidth=1,
                relief="raised",
                bordercolor=pal["panel_border"],
                lightcolor=pal["surface"],
                darkcolor=pal["surface"],
            )
            style.configure(
                "Forge.TLabelframe.Label",
                **({"parent": "TLabelframe.Label"} if themed else {}),
                background=pal["surface"],
                foreground=pal["fg"],
                font=("Segoe UI", 10, "bold"),
            )
            style.configure(
                "Forge.TCombobox",
                **parent_combo,
                fieldbackground=pal["input_bg"],
                background=pal["button_bg"],
                foreground=pal["input_fg"],
                arrowcolor=pal["fg"],
                selectbackground=pal["bg_alt"],
                selectforeground=pal["fg"],
                bordercolor=pal["muted"],
                lightcolor=pal["button_bg"],
                darkcolor=pal["button_bg"],
            )
            style.map(
                "Forge.TCombobox",
                fieldbackground=[
                    ("readonly", pal["input_bg"]),
                    ("disabled", pal["bg"]),
                ],
                foreground=[("readonly", pal["input_fg"])],
                selectbackground=[("readonly", pal["bg_alt"])],
                selectforeground=[("readonly", pal["fg"])],
            )
            self.root.option_add("*TCombobox*Listbox.background", pal["input_bg"])
            self.root.option_add("*TCombobox*Listbox.foreground", pal["input_fg"])
            self.root.option_add(
                "*TCombobox*Listbox.selectBackground", pal["bg_alt"]
            )
            self.root.option_add("*TCombobox*Listbox.selectForeground", pal["fg"])
        except tk.TclError:
            pass

    def _widget_exists(self, widget) -> bool:
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except (tk.TclError, AttributeError):
            return False

    @staticmethod
    def _apply_colors(widget, bg: str, fg: Optional[str]) -> None:
        """Set ``bg`` (and ``fg`` when the widget supports it) without raising.

        ``tk.Frame`` has no ``-fg`` option, so passing it would raise and
        silently drop the background too. Try both, then fall back to bg-only.
        """
        if fg is None:
            try:
                widget.configure(bg=bg)
            except tk.TclError:
                pass
            return
        try:
            widget.configure(bg=bg, fg=fg)
        except tk.TclError:
            try:
                widget.configure(bg=bg)
            except tk.TclError:
                pass

    def _colorize(self, widget, kind: str, pal: Mapping[str, str]) -> None:
        if self._tb_style is not None and isinstance(widget, tk.ttk.Widget):
            self._colorize_tb(widget, kind)
            return
        bg_map = {
            "bg": pal["bg"],
            "muted": pal["bg"],
            "log_bg_frame": pal["log_bg"],
            "logtext": pal["log_bg"],
            "input": pal["input_bg"],
        }
        fg_map = {
            "bg": pal["fg"],
            "muted": pal["muted"],
            "logtext": pal["log_fg"],
            "log_bg_frame": pal["log_fg"],
            "input": pal["input_fg"],
        }
        if kind == "check":
            widget.configure(
                bg=pal["bg"],
                fg=pal["fg"],
                activebackground=pal["bg"],
                activeforeground=pal["fg"],
                selectcolor=pal["bg_alt"],
            )
            return
        if kind.startswith("status:"):
            color_key = kind.split(":", 1)[1]
            fg = {
                "ok": pal["ok"],
                "warn": pal["warn"],
                "error": pal["error"],
                "muted": pal["muted"],
            }[color_key]
            self._apply_colors(widget, pal["bg"], fg)
            return
        if kind == "accent":
            self._apply_colors(widget, pal["accent"], pal["accent_fg"])
            return
        if kind == "progresspanel":
            self._apply_colors(widget, pal["bg"], None)
            try:
                widget.configure(highlightbackground=pal["panel_border"])
            except tk.TclError:
                pass
            return
        if kind == "chip":
            self._apply_colors(widget, pal["surface"], pal["muted"])
            return
        if kind == "panelbg":
            self._apply_colors(widget, pal["surface"], None)
            return
        if kind == "card":
            # A card: raised surface + the (slightly lifted) panel border.
            self._apply_colors(widget, pal["surface"], None)
            try:
                widget.configure(highlightbackground=pal["panel_border"])
            except tk.TclError:
                pass
            return
        if kind == "overall":
            self._apply_colors(widget, pal["surface"], pal["muted"])
            try:
                widget.configure(highlightbackground=pal["border"])
            except tk.TclError:
                pass
            return
        if kind.startswith("button:"):
            variant = kind.split(":", 1)[1]
            colors = {
                "default": (pal["button_bg"], pal["button_fg"], pal["button_active"]),
                "quiet": (pal["bg_alt"], pal["button_fg"], pal["button_active"]),
                "primary": (pal["accent"], pal["accent_fg"], pal["accent_active"]),
                "danger": (pal["error"], pal["accent_fg"], pal["error"]),
            }
            bg, fg, active_bg = colors.get(variant, colors["default"])
            self._apply_colors(widget, bg, fg)
            try:
                widget.configure(
                    activebackground=active_bg,
                    activeforeground=fg,
                    disabledforeground=pal["muted"],
                )
            except tk.TclError:
                pass
            return
        if kind in bg_map:
            self._apply_colors(widget, bg_map[kind], fg_map[kind])

    def _colorize_tb(self, widget, kind: str) -> None:
        """Re-apply the ttkbootstrap-mode role for one registered widget.

        Themed controls (checks, inputs, buttons, comboboxes) own their
        colors through the theme; only the labels switch between the
        Forge* styles. The role recorded at creation (or last dynamic
        update) wins over the generic registration kind."""
        if isinstance(widget, tk.ttk.Label):
            role = getattr(widget, "_tb_role", None) or kind
            style_map = (
                self._TB_PANEL_LABEL_STYLES
                if widget in self._tb_panel_widgets
                else self._TB_PAGE_LABEL_STYLES
            )
            style_name = style_map.get(role)
            if style_name is not None:
                widget.configure(style=style_name)

    def _set_fg(self, widget, color_key: str) -> None:
        """Dynamic foreground for a label: per-widget fg on the hand-rolled
        palette, Forge* style switch under the ttkbootstrap theme."""
        pal = self._pal
        if self._tb_style is not None and isinstance(widget, tk.ttk.Label):
            role = {
                "fg": "bg",
                "muted": "muted",
                "ok": "status:ok",
                "warn": "status:warn",
                "error": "status:error",
            }.get(color_key, "muted")
            widget._tb_role = role  # type: ignore[attr-defined]
            style_map = (
                self._TB_PANEL_LABEL_STYLES
                if widget in self._tb_panel_widgets
                else self._TB_PAGE_LABEL_STYLES
            )
            style_name = style_map.get(role)
            if style_name is not None:
                widget.configure(style=style_name)
                return
        try:
            widget.configure(fg=pal[color_key])
        except tk.TclError:
            pass

    def _on_theme_change(self, _event=None) -> None:
        self.settings.theme = (
            "dark" if self._theme_choice.get() == "Dark" else "light"
        )
        self.settings.auto_refresh = bool(self._auto_refresh.get())
        self.settings.minimize_on_close = bool(self._minimize_on_close.get())
        save_settings(self.settings)
        self._apply_theme()

    def _open_settings(self) -> None:
        """Open the small, task-focused preferences panel from the gear."""
        from tkinter import ttk

        dialog = tk.Toplevel(self.root)
        self._settings_dialog = dialog
        dialog.title("Quick settings")
        dialog.configure(bg=self._pal["bg"])
        dialog.resizable(True, True)
        dialog.minsize(480, 540)
        dialog.transient(self.root)
        dialog.grab_set()

        theme_var = tk.StringVar(dialog, value=self.settings.theme)
        auto_var = tk.BooleanVar(dialog, value=bool(self._auto_refresh.get()))
        minimize_var = tk.BooleanVar(
            dialog, value=bool(self._minimize_on_close.get())
        )
        auto_update_var = tk.BooleanVar(
            dialog, value=bool(self.settings.auto_update_agents)
        )
        body = tk.Frame(dialog, bg=self._pal["bg"], padx=20, pady=18)
        body.pack(fill="both", expand=True)
        heading = self._label(
            body,
            text="Quick settings",
            font=("Segoe UI", 14, "bold"),
            bg=self._pal["bg"],
            fg=self._pal["fg"],
            anchor="w",
        )
        heading.pack(fill="x")
        description = self._label(
            body,
            text="Tune the interface without leaving your dashboard.",
            font=("Segoe UI", 9),
            bg=self._pal["bg"],
            fg=self._pal["muted"],
            anchor="w",
        )
        description.pack(fill="x", pady=(2, 14))

        section = self._lframe(
            body,
            text="  Interface  ",
            font=("Segoe UI", 10, "bold"),
            bg=self._pal["bg"],
            fg=self._pal["fg"],
            padx=10,
            pady=8,
            highlightthickness=1,
            highlightbackground=self._pal["border"],
        )
        section.pack(fill="x")
        self._label(
            section, text="Appearance", bg=self._pal["bg"], fg=self._pal["fg"],
        ).grid(row=0, column=0, sticky="w", padx=(0, 12), pady=3)
        for column, (label, value) in enumerate((("Dark", "dark"), ("Light", "light")), start=1):
            self._radio(
                section,
                text=label,
                value=value,
                variable=theme_var,
                bg=self._pal["bg"],
                fg=self._pal["fg"],
                activebackground=self._pal["bg"],
                activeforeground=self._pal["fg"],
                selectcolor=self._pal["bg_alt"],
            ).grid(row=0, column=column, sticky="w", padx=(0, 8), pady=3)
        self._check(
            section,
            text="Refresh the stack state automatically",
            variable=auto_var,
            bg=self._pal["bg"],
            fg=self._pal["fg"],
            activebackground=self._pal["bg"],
            activeforeground=self._pal["fg"],
            selectcolor=self._pal["bg_alt"],
            anchor="w",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(7, 2))
        self._check(
            section,
            text="Minimise to the notification area on close",
            variable=minimize_var,
            bg=self._pal["bg"],
            fg=self._pal["fg"],
            activebackground=self._pal["bg"],
            activeforeground=self._pal["fg"],
            selectcolor=self._pal["bg_alt"],
            anchor="w",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 3))
        self._check(
            section,
            text="Check the SSH key and the RunPod account at start",
            variable=auto_update_var,
            bg=self._pal["bg"],
            fg=self._pal["fg"],
            activebackground=self._pal["bg"],
            activeforeground=self._pal["fg"],
            selectcolor=self._pal["bg_alt"],
            anchor="w",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 3))

        # Language: US English by default, French available. Applied on the
        # next dialog open (the labels already on screen keep their text until
        # the widget tree is rebuilt), and persisted in gui.json.
        self._label(
            section, text="Language", bg=self._pal["bg"], fg=self._pal["fg"],
        ).grid(row=4, column=0, sticky="w", padx=(0, 12), pady=(7, 3))
        language_var = tk.StringVar(value=LANGUAGE_LABELS.get(get_language(), "English (US)"))
        language_combo = ttk.Combobox(
            section,
            textvariable=language_var,
            values=[LANGUAGE_LABELS[code] for code in SUPPORTED_LANGUAGES],
            state="readonly",
            width=16,
            style="Forge.TCombobox",
        )
        language_combo.grid(row=4, column=1, columnspan=2, sticky="w", pady=(7, 3))
        self._tooltip(
            language_combo,
            "Interface language. US English is the default; French is "
            "available. Takes effect when you reopen this dialog.",
        )

        def apply_language(_event=None) -> None:
            chosen = language_var.get()
            for code in SUPPORTED_LANGUAGES:
                if LANGUAGE_LABELS[code] == chosen:
                    set_language(code)
                    self.settings.language = code
                    self._save_comfy_settings()
                    self._append_log(
                        "info",
                        f"Language set to {LANGUAGE_LABELS[code]}.",
                    )
                    return

        language_combo.bind("<<ComboboxSelected>>", apply_language)

        # The API keys section used to be its own tab; it now lives here
        # (rebuilt on every open — the widget refs are cleared on close so
        # the 5 s status refresh never touches dead widgets).
        creds_section = self._lframe(
            body,
            text="  API keys  ",
            font=("Segoe UI", 10, "bold"),
            bg=self._pal["bg"],
            fg=self._pal["fg"],
            padx=10,
            pady=8,
            highlightthickness=1,
            highlightbackground=self._pal["border"],
        )
        creds_section.pack(fill="x", pady=(14, 0))
        self._themed(creds_section, "panelbg")
        self._label(
            creds_section,
            text="Your credentials are encrypted locally (Windows DPAPI) "
            "and never leave this machine in clear text. A field left "
            "empty keeps the existing value; the HF/Civitai "
            "keys feed the pod at start.",
            bg=self._pal["bg"], fg=self._pal["muted"], anchor="w",
            justify="left", font=("Segoe UI", 9),
        ).pack(fill="x", pady=(0, 4))
        self._themed(creds_section.winfo_children()[-1], "muted")
        self._creds_label = self._label(
            creds_section, text="State: …", bg=self._pal["bg"],
            fg=self._pal["muted"], anchor="w", font=("Segoe UI", 11, "bold"),
        )
        self._creds_label.pack(fill="x", pady=(0, 4))
        self._themed(self._creds_label, "status:muted")
        self._cred_row_labels = {}
        self._cred_blocks = {}
        for block_key, block_title, fields in (
            ("runpod", "RunPod", ("runpod",)),
            ("templates", "Templates", ("template", "comfy_template", "train_template")),
            ("train_desktop", "Training desktop", ("vnc_password",)),
            ("hf", "Hugging Face", ("hf",)),
            ("civitai", "Civitai", ("civitai",)),
        ):
            block = self._lframe(
                creds_section, text=block_title, bg=self._pal["bg"],
                fg=self._pal["fg"], font=("Segoe UI", 10, "bold"),
            )
            block.pack(fill="x", padx=4, pady=3)
            self._themed(block, "panelbg")
            self._cred_blocks[block_key] = block
            for key in fields:
                row = tk.Frame(block, bg=self._pal["bg"])
                row.pack(fill="x", padx=10, pady=2)
                self._themed(row, "bg")
                name_lbl = self._label(
                    row, text=CREDENTIAL_FIELD_LABELS[key] + " :",
                    anchor="w", font=("Segoe UI", 10),
                    bg=self._pal["bg"], fg=self._pal["fg"],
                )
                name_lbl.pack(side="left")
                self._themed(name_lbl, "bg")
                value_lbl = self._label(
                    row, text="…", anchor="w", font=("Segoe UI", 10),
                    bg=self._pal["bg"], fg=self._pal["muted"],
                )
                value_lbl.pack(side="left", padx=(8, 0))
                self._themed(value_lbl, "status:muted")
                self._cred_row_labels[key] = value_lbl
        # Seed the whole section synchronously from the credential store:
        # the periodic 5 s refresh keeps it up to date afterwards, but the
        # dialog never shows a « … » placeholder — not even when that
        # refresh fails or is slow.
        self._seed_credentials_state()
        creds_actions = tk.Frame(creds_section, bg=self._pal["bg"])
        creds_actions.pack(fill="x", padx=4, pady=(4, 0))
        self._themed(creds_actions, "bg")
        self._btn_creds_set = self._make_button(
            creds_actions, "Configurer / Modifier",
            lambda: self._credentials_set(parent=dialog),
        )
        self._btn_creds_set.pack(side="left", padx=(0, 6))
        self._btn_creds_clear = self._make_button(
            creds_actions, "Effacer tout",
            lambda: self._credentials_clear(parent=dialog),
            variant="danger",
        )
        self._btn_creds_clear.pack(side="right", padx=(12, 0))

        actions = tk.Frame(body, bg=self._pal["bg"])
        actions.pack(fill="x", pady=(16, 0))

        def apply() -> None:
            self._theme_choice.set("Dark" if theme_var.get() == "dark" else "Light")
            self._auto_refresh.set(bool(auto_var.get()))
            self._minimize_on_close.set(bool(minimize_var.get()))
            self.settings.auto_update_agents = bool(auto_update_var.get())
            self._save_comfy_settings()
            self._on_theme_change()
            if getattr(self, "settings_saved_ok", True):
                self._append_log("info", "Settings saved.")
            else:
                self._append_log(
                    "error",
                    "Settings NOT saved (write failed) — "
                    "check the disk space and the folder permissions.",
                )
            self._close_settings_dialog()

        self._make_button(
            actions, "Cancel", self._close_settings_dialog, variant="quiet"
        ).pack(side="right")
        self._make_button(actions, "Save", apply, variant="primary").pack(
            side="right", padx=(0, 6)
        )
        dialog.bind("<Escape>", lambda _event: self._close_settings_dialog())
        dialog.bind("<Return>", lambda _event: apply())
        dialog.wait_visibility()
        dialog.focus_set()

    def _close_settings_dialog(self) -> None:
        """Close the Settings dialog, dropping its widget refs (C3)."""
        dialog = self._settings_dialog
        self._settings_dialog = None
        self._creds_label = None
        self._cred_row_labels = {}
        self._cred_blocks = {}
        self._btn_creds_set = None
        self._btn_creds_clear = None
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass

    def _select_tab_by_label(self, label: str) -> None:
        for tab_id in self._notebook.tabs():
            if self._notebook.tab(tab_id, "text") == label:
                self._notebook.select(tab_id)
                return

    def _show_about(self) -> None:
        try:
            from tkinter import messagebox

            messagebox.showinfo(
                "About MiniMax H3 Launcher",
                "MiniMax H3 Launcher\nLocal control centre for your AI stack.\n\n"
                "Shortcut: Ctrl + , opens the settings.",
                parent=self.root,
            )
        except tk.TclError:
            pass


    def _on_mode_change(self) -> None:
        """Footer mode changed (ComfyUI video vs LoRA training)."""
        self._refresh_effective_stack()
        self._on_stack_change()


    def _refresh_effective_stack(self) -> None:
        """Recompute ``_stack`` (what runs) from the footer mode."""
        self._stack.set(
            self._mode.get() if self._mode.get() in STACKS else "comfy"
        )


    def _set_widget_enabled(
        self, widget, enabled: bool, tooltip_text: Optional[str] = None
    ) -> None:
        """Enable/disable one interactive widget (recursing into frames).

        A readonly combobox remembers its original state so re-enabling
        never turns it into a free-text field. When disabling, *tooltip_text*
        is attached (so a greyed control explains why); when re-enabling it
        is removed again.
        """
        cls = type(widget).__name__
        if cls in (
            "Combobox", "Button", "Entry", "Checkbutton",
            "Radiobutton", "Text", "Spinbox", "Listbox",
        ):
            if not enabled:
                if not hasattr(widget, "_forge_state_before_disable"):
                    try:
                        widget._forge_state_before_disable = str(
                            widget.cget("state")
                        )
                    except tk.TclError:
                        widget._forge_state_before_disable = "normal"
                try:
                    widget.configure(state="disabled")
                except tk.TclError:
                    pass
                if tooltip_text is not None:
                    # Keep the technical tooltip (if any) so it can be
                    # restored once the control is enabled again. Never
                    # treat the mode-tooltip itself as the "previous" one
                    # (repeated disables must not accumulate it).
                    previous = self._tooltip_texts.get(widget)
                    if (
                        previous is not None
                        and previous != tooltip_text
                        and not hasattr(widget, "_forge_tooltip_before_disable")
                    ):
                        widget._forge_tooltip_before_disable = previous
                    self._tooltip(widget, tooltip_text)
            else:
                try:
                    widget.configure(
                        state=getattr(
                            widget, "_forge_state_before_disable", "normal"
                        )
                    )
                except tk.TclError:
                    pass
                if tooltip_text is not None:
                    widget.unbind("<Enter>")
                    widget.unbind("<Leave>")
                    widget.unbind("<Button-1>")
                    self._tooltip_texts.pop(widget, None)
                    previous = getattr(
                        widget, "_forge_tooltip_before_disable", None
                    )
                    if previous is not None:
                        self._tooltip(widget, previous)
                    # Delete (not set to None) so the next disable re-captures
                    # the *current* tooltip as the new "previous": with the
                    # attribute merely cleared to None, ``hasattr`` stays True
                    # in the disable guard and a technical tooltip restored by
                    # this enable would be lost on the following disable.
                    try:
                        del widget._forge_tooltip_before_disable
                    except AttributeError:
                        pass
            return
        if cls in ("Frame", "LabelFrame"):
            for child in widget.winfo_children():
                self._set_widget_enabled(child, enabled, tooltip_text)
            return
        # Display-only widgets (labels, …) have no state: they stay visible.

    def _set_comfy_frames_enabled(self, enabled: bool) -> None:
        # Passed both ways: attached while disabled, removed (and any
        # technical tooltip restored) once re-enabled.
        tooltip = TECHNICAL_TOOLTIPS["comfy_mode_only"]
        for frame in (
            self._comfy_frame,
            self._annuaire_frame,
            self._comfy_models_frame,
        ):
            if not self._widget_exists(frame):
                continue
            for child in frame.winfo_children():
                self._set_widget_enabled(child, enabled, tooltip)
        # The reception folder only has an effect while the collection is on;
        # the mode gate runs first so this never re-enables it in another mode.
        self._update_receive_gating(enabled)

    def _update_receive_gating(self, comfy_active: Optional[bool] = None) -> None:
        """Grey the reception folder when the collection itself is off."""
        entry = getattr(self, "_comfy_outputs_dir_entry", None)
        if entry is None or not self._widget_exists(entry):
            return
        if comfy_active is None:
            comfy_active = self._mode.get() == "comfy"
        if not comfy_active:
            return
        self._set_widget_enabled(
            entry,
            bool(self._comfy_auto_collect.get()),
            TECHNICAL_TOOLTIPS["outputs_dir_off"],
        )


    # -- log ---------------------------------------------------------------

    def _install_log_handler(self) -> None:
        self._log_handler = GuiLogHandler(self._queue.put)
        logging.getLogger().addHandler(self._log_handler)

    def _log_filter_visible(self, level: str) -> bool:
        var = {
            "info": self._log_filter_info,
            "ok": self._log_filter_info,
            "warn": self._log_filter_warn,
            "error": self._log_filter_error,
        }.get(level)
        return var.get() if var is not None else True

    def _append_log(self, level: str, message: str) -> None:
        self._log_entries.append((level, message))
        if self._progress_active:
            event = map_progress_event(self._stack.get(), message)
            if event is not None:
                indices, done = event
                apply_progress_event(self._progress_states, indices, done)
                self._redraw_progress()
        if len(self._log_entries) > MAX_LOG_LINES:
            # Drop the oldest WIDGET line instead of rebuilding the whole
            # widget. Once the journal reached MAX_LOG_LINES (fast during a
            # start with RunPod retries), _rebuild_log_text() ran a delete +
            # MAX_LOG_LINES inserts on the UI thread for EVERY new line, at up
            # to hundreds of lines/second from a child-process sink — a hard
            # UI freeze. A trim is only needed when the widget actually holds
            # more lines than the entry cap, and one delete is enough for the
            # single entry just added.
            trimmed = len(self._log_entries) - MAX_LOG_LINES
            del self._log_entries[:trimmed]
            if self._log_text is not None:
                self._log_text.configure(state="normal")
                for _ in range(trimmed):
                    self._log_text.delete("1.0", "2.0")
                self._log_text.configure(state="disabled")
        if not self._log_filter_visible(level):
            return
        self._log_text.configure(state="normal")
        self._log_text.insert(
            "end", f"[{level.upper()}] {message}\n",
            (_LOG_TAG_MAP[level],),
        )
        if self._log_autoscroll.get():
            self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _rebuild_log_text(self) -> None:
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        for level, message in self._log_entries:
            if not self._log_filter_visible(level):
                continue
            self._log_text.insert(
                "end", f"[{level.upper()}] {message}\n",
                (_LOG_TAG_MAP[level],),
            )
        if self._log_autoscroll.get():
            self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _log_filter_changed(self, _event=None) -> None:
        self._rebuild_log_text()

    def _log_clear(self) -> None:
        self._log_entries = []
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    def _log_copy(self) -> None:
        lines = [
            f"[{level.upper()}] {message}"
            for level, message in self._log_entries
            if self._log_filter_visible(level)
        ]
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(lines))
        except tk.TclError:
            return
        self._notify("Journal copied to the clipboard.", "ok")

    # -- notifications ------------------------------------------------------

    def _notify(self, message: str, kind: str = "info", after_ms: int = 4000) -> None:
        """Show a transient acknowledgement banner above the tabs."""
        self._hide_notify()
        self._notify_label.configure(text=message)
        self._set_fg(self._notify_label, {"info": "muted", "ok": "ok"}.get(kind, kind))
        self._notify_label.pack(fill="x", before=self._notebook, padx=12, pady=(0, 2))
        self._notify_after = self.root.after(after_ms, self._hide_notify)

    def _hide_notify(self) -> None:
        if self._notify_after is not None:
            try:
                self.root.after_cancel(self._notify_after)
            except tk.TclError:
                pass
            self._notify_after = None
        if (
            self._notify_label is not None
            and self._widget_exists(self._notify_label)
            and self._notify_label.winfo_viewable()
        ):
            self._notify_label.pack_forget()

    # -- progress -----------------------------------------------------------

    def _start_progress_panel(self) -> None:
        pal = self._pal
        self._progress_steps = list(
            PROGRESS_STEPS.get(self._stack.get(), PROGRESS_STEPS["agent"])
        )
        self._progress_states = ["pending"] * len(self._progress_steps)
        for lbl in self._progress_labels:
            if self._widget_exists(lbl):
                lbl.destroy()
        self._progress_labels = []
        for step in self._progress_steps:
            # One framed chip per step: bigger type and its own border make
            # the startup sequence readable at a glance instead of a row of
            # same-weight grey words.
            lbl = _TKMOD.Label(
                self._progress_frame,
                text=step,
                bg=pal["surface"],
                fg=pal["muted"],
                anchor="w",
                font=("Segoe UI", 10, "bold"),
                padx=8,
                pady=3,
                highlightthickness=1,
                highlightbackground=pal["border"],
            )
            lbl.pack(side="left", padx=(0, 8))
            self._themed(lbl, "chip")
            self._progress_labels.append(lbl)
        self._progress_active = True
        if self._progress_hide_after is not None:
            try:
                self.root.after_cancel(self._progress_hide_after)
            except tk.TclError:
                pass
            self._progress_hide_after = None
        self._redraw_progress()
        # Above the journal: the chips belong to the status block at the top
        # of the tab, and the journal (packed with expand=True) then absorbs
        # all the spare height below them instead of pushing them off-screen.
        self._progress_frame.pack(fill="x", padx=PAD_L, before=self._log_frame)

    def _redraw_progress(self) -> None:
        pal = self._pal
        colors = {
            "pending": pal["muted"],
            "active": pal["warn"],
            "done": pal["ok"],
        }
        for index, lbl in enumerate(self._progress_labels):
            if not self._widget_exists(lbl):
                continue
            state = self._progress_states[index]
            lbl.configure(
                text=f"{PROGRESS_SYMBOLS[state]} {self._progress_steps[index]}",
                fg=colors[state],
                # The chip's frame carries the same state colour as its
                # glyph, so a glance is enough to see where the start is.
                highlightbackground=(
                    pal["border"] if state == "pending" else colors[state]
                ),
            )

    def _hide_progress_panel(self) -> None:
        if self._progress_hide_after is not None:
            try:
                self.root.after_cancel(self._progress_hide_after)
            except tk.TclError:
                pass
            self._progress_hide_after = None
        self._progress_active = False
        if self._widget_exists(self._progress_frame):
            self._progress_frame.pack_forget()

    # -- workers -----------------------------------------------------------

    def _run(self, label: str, fn: Callable[[], object]) -> None:
        if self._busy:
            self._append_log("warn", f"An operation is already running; « {label} » ignored.")
            return
        self._busy = True
        self._set_buttons_state("disabled")
        if label == "Start":
            # Multi-step start: the step panel replaces the plain text.
            self._start_progress_panel()
            self._status_line.configure(text="")
        else:
            self._hide_progress_panel()
            self._status_line.configure(text=f"Operation in progress: {label}…")

        def worker() -> None:
            ok, payload = True, None
            try:
                payload = fn()
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                ok, payload = False, str(exc) or exc.__class__.__name__
            self._queue.put(("action", label, ok, payload))

        self._action_thread = threading.Thread(
            target=worker, daemon=True, name=f"gui-{label}"
        )
        self._action_thread.start()

    def _set_buttons_state(self, state: str) -> None:
        for btn in self._action_buttons + self._recovery_buttons:
            if self._widget_exists(btn):
                btn.configure(state=state)

    def _finish_action(self, label: str, ok: bool, payload) -> None:
        self._busy = False
        self._set_buttons_state("normal")
        # Re-apply mode/recovery gating the bulk enable just released.
        self._update_toggle_buttons()
        if self._progress_active:
            if ok:
                for index in range(len(self._progress_states)):
                    self._progress_states[index] = "done"
                self._redraw_progress()
            # Keep the final step picture briefly, then let it go.
            self._progress_hide_after = self.root.after(
                8000, self._hide_progress_panel
            )
        if label in ("Doctor", "doctor"):
            for line in str(payload).splitlines():
                self._append_log("info", line)
            self._status_line.configure(text="Diagnostics finished (see the journal).")
            self._notify("Diagnostics finished (see the journal).", "info")
        elif label.startswith("recover:"):
            action_name = label.split(":", 1)[1]
            if not isinstance(payload, RecoveryResult):
                # ``_run`` turns a raised exception into ``str(exc)``: the
                # annotation alone did not check, so any failure inside
                # action_recover produced "AttributeError: 'str' object has no
                # attribute 'ok'" — the fatal popup instead of the real error,
                # no journal line, and (before the _poll guard) a dead pump.
                message = str(payload)
                self._append_log("error", message)
                self._show_error(f"Repair {action_name}", message)
                self._notify(f"Repair {action_name}: failed.", "error")
                self._status_line.configure(text=f"Repair {action_name}: failed.")
                return
            text = format_recovery_result(payload)
            for line in text.splitlines():
                self._append_log("ok" if payload.ok else "error", line)
            if not payload.ok:
                self._show_error(f"Repair {action_name}", text)
                self._notify(f"Repair {action_name}: failed.", "error")
            else:
                self._notify(f"Repair {action_name}: succeeded.", "ok")
            self._status_line.configure(
                text=f"Repair {action_name}: "
                f"{'succeeded' if payload.ok else 'failed'}."
            )
        else:
            if ok:
                self._append_log("ok", str(payload))
                # The status line is a single-line element: never let a
                # multi-line payload balloon it and break the layout.
                self._status_line.configure(text=_first_line(str(payload)))
                self._notify(_first_line(str(payload)), "ok")
            elif orchestrator.is_pod_unavailable_error(str(payload)):
                # Pod unavailability is an expected, transient RunPod
                # condition already narrated in the journal during the
                # retries: keep the final failure discreet — warning line,
                # muted status, no red popup, no red banner.
                self._append_log(
                    "warn",
                    "Pod unavailable: no free GPU at RunPod. "
                    "You can press Start again at any time.",
                )
                self._status_line.configure(
                    text="Pod unavailable — no free GPU at RunPod."
                )
                self._notify(
                    "Pod unavailable — press Start again whenever you like.",
                    "warn",
                )
            else:
                message = translate_action_error(str(payload))
                self._append_log("error", f"« {label} » failed: {message}")
                self._show_error(label, message)
                self._status_line.configure(text=f"« {label} » failed.")
                self._notify(f"« {label} » failed.", "error")
        self._refresh(force=True)

    def _show_error(self, title: str, message: str) -> None:
        """Forge-themed error box (the native messagebox ignores the theme).

        A plain Tk messagebox paints a light, unthemed window with the
        system font — jarring next to the dark Forge palette. The text is
        translated first (see :func:`translate_action_error`).
        """
        text = translate_action_error(message)
        try:
            dialog = self._message_dialog(
                title, text, kind="error", buttons=(("Close", None),)
            )
        except tk.TclError:
            return
        if dialog is not None:
            dialog.focus_set()

    def _message_dialog(
        self, title: str, message: str, *, kind: str = "info",
        buttons=(("OK", None),),
    ):
        """A themed modal box: ``(title, message, buttons)``.

        ``buttons`` is a sequence of ``(label, value)``: clicking one stores
        its value in ``dialog.result`` and closes the dialog. Returns the
        dialog (non-blocking: no ``wait_window``, so tests and the refresh
        loop keep running); callers that need an answer read
        ``dialog.result`` from their own button callback.
        """
        pal = self._pal
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.configure(bg=pal["bg"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.result = None
        body = tk.Frame(dialog, bg=pal["bg"], padx=PAD_L + PAD_S, pady=PAD_L)
        body.pack(fill="both", expand=True)

        heading = self._label(
            body, text=title, bg=pal["bg"], fg=pal["fg"], anchor="w",
            font=("Segoe UI", 12, "bold"),
        )
        heading.pack(fill="x")
        self._themed(heading, "bg")
        color_key = {"error": "error", "warn": "warn"}.get(kind, "muted")
        text = self._label(
            body, text=message, bg=pal["bg"], fg=pal[color_key], anchor="w",
            justify="left", wraplength=460, font=("Segoe UI", 10),
        )
        text.pack(fill="x", pady=(PAD_M, PAD_L))
        self._themed(text, f"status:{color_key}" if color_key != "muted" else "muted")

        actions = tk.Frame(body, bg=pal["bg"])
        actions.pack(fill="x")
        self._themed(actions, "bg")

        def _choose(value) -> None:
            dialog.result = value
            dialog.destroy()

        for label, value in reversed(list(buttons)):
            variant = "primary" if value is not None else "quiet"
            self._make_button(
                actions, label, (lambda v=value: _choose(v)), variant=variant
            ).pack(side="right", padx=(PAD_M, 0))
        try:
            dialog.grab_set()
        except tk.TclError:
            pass
        dialog.bind("<Escape>", lambda _e: _choose(None))
        return dialog

    def _ask_confirm(self, title: str, message: str, *, kind: str = "warn") -> bool:
        """Themed yes/no confirmation (destructive actions)."""
        try:
            dialog = self._message_dialog(
                title, message, kind=kind,
                buttons=(("Cancel", False), ("Confirm", True)),
            )
        except tk.TclError:
            return False
        if dialog is None:
            return False
        # Modal and blocking for this one: the caller must not run the
        # destructive action before the answer exists.
        self.root.wait_window(dialog)
        return bool(dialog.result)

    def _confirm_pod_terminate(self) -> bool:
        return self._ask_confirm(
            "Terminate the pod",
            "This will permanently terminate the pod (deleted, GPU "
            "released) and interrupt the active services (the stack's "
            "service and the SSH tunnel).\n\nA plain Stop keeps the pod and its "
            "disk.\n\nTerminate the pod?",
        )

    def _prompt_recreate(self, pod_id: str, holder: dict, event: threading.Event) -> None:
        """Show the no-free-GPU recreate question and release the worker."""
        try:
            from tkinter import messagebox

            approved = messagebox.askyesno(
                "No free GPU",
                f"Pod {pod_id} cannot start: no free GPU "
                "at its current provider.\n\n"
                "Delete this pod and create a new one (placed elsewhere, "
                "possibly re-downloading the weights)?",
                parent=self.root,
            )
        except tk.TclError:
            approved = False
        holder["approved"] = approved
        event.set()


    # -- status ------------------------------------------------------------

    def _load_config_safe(self):
        try:
            if self._mode.get() == "comfy":
                return (
                    load_config(
                        stack="comfy",
                        comfy=self._comfy_overrides(),
                        store=CredentialStore(),
                    ),
                    None,
                )
            return (
                load_config(
                    stack="train",
                    # Same train values Start would use, so the dashboard and
                    # the pod-env drift check never describe a different
                    # configuration than the one that will be launched.
                    train=self._train_overrides(),
                    gpu=self._selected_gpu(),
                    store=CredentialStore(),
                ),
                None,
            )
        except ConfigError as exc:
            return None, exc

    def _refresh(self, force: bool = False) -> None:
        if self._refresh_in_progress:
            return
        now = time.monotonic()
        if not force and (now - self._last_refresh) * 1000 < self._refresh_interval_ms:
            return
        self._refresh_in_progress = True

        def worker() -> None:
            try:
                config, err = self._load_config_safe()
                if config is not None:
                    self._last_config = config
                config_error = str(err) if err is not None else None
                runpod = None
                if config is not None and config.secrets.runpod_api_key:
                    try:
                        runpod = RunPodClient(
                            config.secrets.runpod_api_key, timeout=10.0
                        )
                    except Exception:
                        runpod = None
                snapshot = build_status_snapshot(
                    config, config_error=config_error, runpod=runpod
                )
                # A forced refresh (the ↻ button, and the startup refresh)
                # also re-adopts a stack opened from another session: if this
                # session has no registered pod for the active stack, any
                # RUNNING launcher-managed pod of that stack on the account
                # becomes the stack's pod again (bookkeeping only).
                if force and config is not None:
                    found, adopted = scan_open_stacks(
                        config, runpod, PodRegistry(), config.stack
                    )
                    snapshot["open_pods_found"] = found
                    if adopted is not None:
                        label = STACK_LABELS.get(config.stack, config.stack)
                        self._queue.put(
                            (
                                "log",
                                "ok",
                                f"{label} stack found again: pod {adopted} "
                                "(opened in another session) is followed by "
                                "the launcher again.",
                            )
                        )
                        snapshot = build_status_snapshot(
                            config, config_error=config_error, runpod=runpod
                        )
                # One bounded extra request (list_pods) feeds the « Piles
                # actives » panel: every active stack, its pod status and its
                # hourly rate — so concurrent stacks are visible and
                # independently controllable.
                try:
                    snapshot["active_stacks"] = build_active_stacks_snapshot(
                        config, runpod=runpod, registry=PodRegistry()
                    )
                except Exception:  # noqa: BLE001 - a panel row is never fatal
                    snapshot["active_stacks"] = {"stacks": [], "cost_per_hour_total": None}
                self._queue.put(("status", snapshot))
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                self._queue.put(("refresh_error", str(exc)))

        self._refresh_thread = threading.Thread(
            target=worker, daemon=True, name="gui-refresh"
        )
        self._refresh_thread.start()

    def _show_overall_popover(self, _event=None) -> None:
        self._hide_overall_popover()
        pal = self._pal
        pop = tk.Toplevel(self.root)
        pop.overrideredirect(True)
        pop.configure(
            bg=pal["bg_alt"],
            highlightthickness=1,
            highlightbackground=pal["border"],
        )
        label = _TKMOD.Label(
            pop,
            text="\n".join(overall_popover_lines(self._last_snapshot or {})),
            bg=pal["bg_alt"],
            fg=pal["fg"],
            font=("Segoe UI", 9),
            justify="left",
            padx=10,
            pady=8,
            anchor="nw",
        )
        label.pack()
        x = self._overall_label.winfo_rootx()
        y = self._overall_label.winfo_rooty() + self._overall_label.winfo_height() + 4
        pop.geometry(f"+{max(0, x - 160)}+{max(0, y)}")
        pop.bind("<Escape>", lambda _e: self._hide_overall_popover())
        pop.bind("<Button-1>", lambda _e: self._hide_overall_popover())
        pop.protocol("WM_DELETE_WINDOW", self._hide_overall_popover)
        self._overall_popover = pop
        self._overall_popover_label = label
        self._overall_popover_after = self.root.after(8000, self._hide_overall_popover)

    def _hide_overall_popover(self) -> None:
        if self._overall_popover_after is not None:
            try:
                self.root.after_cancel(self._overall_popover_after)
            except tk.TclError:
                pass
            self._overall_popover_after = None
        if self._overall_popover is not None:
            try:
                if self._overall_popover.winfo_exists():
                    self._overall_popover.destroy()
            except tk.TclError:
                pass
            self._overall_popover = None
            self._overall_popover_label = None

    def _fit_health_columns(self, rows) -> None:
        """Size the three columns to their content (capped), instead of
        letting « Info » stretch across the whole panel when almost every
        row reads « — »."""
        try:
            font = tkfont.nametofont("TkDefaultFont")
        except tk.TclError:  # pragma: no cover - defensive
            return
        widths = [font.measure(header) for header in ("Component", "State", "Info")]
        for name, state, info, color in rows:
            # The status icon lives in the tree column, so the glyph is only
            # measured when it is the actual fallback badge.
            badge = "" if self._status_icon(color) is not None else (
                "● " if color != "muted" else "○ "
            )
            cells = (str(name), f"{badge}{state}", str(info or "—"))
            for index, cell in enumerate(cells):
                widths[index] = max(widths[index], font.measure(cell))
        for column, width, cap in zip(
            ("component", "status", "info"), widths, (240, 280, 460)
        ):
            self._dash_table.column(
                column, width=min(width + 26, cap), stretch=False
            )

    def _redraw_health_table(self, snapshot: dict) -> None:
        rows = build_health_table_rows(
            snapshot,
            active_preset=self._comfy_preset.get(),
        )
        self._dash_table.configure(height=max(3, len(rows)))
        self._dash_table.delete(*self._dash_table.get_children())
        for name, state, info, color in rows:
            icon = self._status_icon(color)
            badge = "" if icon is not None else (
                "● " if color != "muted" else "○ "
            )
            options = {"tags": (color,)}
            if icon is not None:
                options["image"] = icon
            self._dash_table.insert(
                "", "end",
                values=(name, f"{badge}{state}", info or "—"),
                **options,
            )
        self._fit_health_columns(rows)
        if is_idle_state(snapshot):
            self._dash_table.pack_forget()
            self._dash_idle.pack(fill="x", padx=6, pady=(2, 4))
            self._render_idle_found(snapshot)
        else:
            self._dash_idle.pack_forget()
            self._idle_adopt_btn.pack_forget()
            self._dash_table.pack(fill="x", padx=6, pady=(2, 4))

    def _render_idle_found(self, snapshot: dict) -> None:
        """In the empty state, offer to adopt an open stack found on the
        account that does not belong to the current workspace."""
        current = snapshot.get("stack") or "agent"
        found = [
            entry
            for entry in (snapshot.get("open_pods_found") or [])
            if not entry.get("adopted") and entry.get("stack") != current
        ]
        self._idle_found = found
        if not found:
            self._idle_label.configure(text="No active instance")
            self._idle_adopt_btn.pack_forget()
            return
        entry = found[0]
        label = STACK_LABELS.get(entry["stack"], entry["stack"])
        self._idle_label.configure(
            text=f"No active {label} stack"
            if len(found) == 1
            else f"No active {label} stack (+{len(found) - 1} other(s))"
        )
        self._idle_adopt_btn.configure(
            text=f"Adopt the {label} stack ({entry['pod_id']})"
        )
        self._idle_adopt_btn.pack(side="left", padx=8)

    def _adopt_idle_found(self) -> None:
        entry = (self._idle_found or [None])[0]
        if entry is None:
            return
        self._adopt_open_pod(entry)

    def _adopt_open_pod(self, entry: dict) -> None:
        from tkinter import messagebox

        stack = entry["stack"]
        label = STACK_LABELS.get(stack, stack)
        approved = messagebox.askyesno(
            "Adopt this stack?",
            f"An open {label} stack was detected on your RunPod account\n"
            f"(pod {entry['pod_id']}, \"{entry.get('name', '')}\").\n\n"
            f"Adopt it? The workspace will switch to \"{label}\" and the "
            f"launcher will follow that pod again (state, billing, timer, "
            f"stop).",
            parent=self.root,
        )
        if not approved:
            return
        try:
            PodRegistry().save(
                PodRecord(
                    pod_id=entry["pod_id"],
                    name=entry.get("name") or "",
                    created_at=entry.get("created_at") or time.time(),
                    gpu_id=None,
                    gpu_count=1,
                    data_center_id=None,
                    stack=stack,
                ),
                stack=stack,
            )
        except Exception as exc:
            self._append_log("error", f"Cannot adopt the pod: {exc}")
            return
        self._append_log("ok", f"{label} stack adopted: pod {entry['pod_id']}")
        # Adopting a pod means switching to that workload.
        self._mode.set(stack if stack in STACKS else "comfy")
        self._on_stack_change()
        self._save_comfy_settings()
        self._refresh(force=True)

    def _apply_billing(self, snapshot: dict) -> None:
        """Render the pod-spend / wallet-balance row (keeps the last good
        values while a probe transiently fails; hides when there is none)."""
        billing = snapshot.get("billing")
        if billing is None:
            billing = self._last_billing
        self._apply_cost_meter(billing)
        if billing is None:
            self._billing_label.pack_forget()
            return
        self._last_billing = billing
        pal = self._pal

        parts = []
        if billing.get("has_pod"):
            if billing.get("running"):
                head = (
                    f"Pod : {billing['elapsed']}"
                    if billing.get("elapsed")
                    else "Pod : en cours"
                )
            else:
                head = "Pod stopped"
            spend = []
            if billing.get("cost"):
                spend.append(f"≈ {billing['cost']}")
            if billing.get("rate"):
                spend.append(f"({billing['rate']})")
            if spend:
                head += "  ·  " + " ".join(spend)
            parts.append(head)
        if billing.get("has_wallet"):
            balance = (
                f"Solde RunPod : {billing['balance']}"
                if billing.get("balance")
                else "RunPod balance: unavailable"
            )
            if billing.get("hours_left_text"):
                balance += f"  ({billing['hours_left_text']})"
            parts.append(balance)
        if not parts:
            self._billing_label.pack_forget()
            return

        hours_left = billing.get("hours_left")
        if billing.get("running") and hours_left is not None:
            color = "error" if hours_left < 2 else ("warn" if hours_left < 6 else "fg")
        else:
            color = "muted"
        self._billing_label.configure(text="💰  " + "   —   ".join(parts))
        self._set_fg(self._billing_label, color)
        self._billing_label.pack(fill="x", side="bottom", pady=(2, 0))

    def _apply_status(self, snapshot: dict) -> None:
        self._last_snapshot = snapshot
        self._last_refresh = time.monotonic()
        self._refresh_in_progress = False

        if snapshot.get("config_error"):
            self._set_overall("State: INVALID CONFIGURATION", "error")
            self._hint_label.configure(text=f"Configuration : {snapshot['config_error']}")
            self._update_repair_button(None)
        else:
            overall = snapshot.get("overall", "UNKNOWN")
            color_key = _OVERALL_COLOR_KEY.get(overall, "muted")
            self._set_overall(f"State: {overall}", color_key)
            # One message at a time: while an action runs (the start retries
            # in particular) the status line owns the wording, and the stale
            # cause from the last snapshot is suppressed instead of sitting
            # next to a contradictory "new attempt" line.
            hint = ""
            if not self._busy:
                if snapshot.get("cause"):
                    hint = f"Cause : {snapshot['cause']}"
                if snapshot.get("remediation"):
                    hint = (
                        hint + "  →  " if hint else ""
                    ) + f"Remediation: {snapshot['remediation']}"
            self._hint_label.configure(text=hint)
            self._update_repair_button(snapshot)

        self._redraw_health_table(snapshot)
        self._apply_billing(snapshot)
        self._refresh_pod_card()
        self._refresh_stack_rows(snapshot)
        # Reception of finished generations: idempotent, and the watcher
        # itself probes ComfyUI before doing anything.
        self._sync_comfy_watcher()
        # Offline by design: it reads the config and the pod registry only, so
        # the tab renders even with no pod and no network.
        self._refresh_train_tab()
        self._refresh_config_recap()

        # The credentials widgets only exist while the Settings
        # dialog is open — the refresh must survive a closed dialog.
        creds_state = snapshot.get("credentials", "UNKNOWN")
        if self._widget_exists(self._creds_label):
            self._creds_label.configure(
                text=f"State: {CREDENTIAL_STATE.get(creds_state, creds_state)}"
            )
            self._set_fg(
                self._creds_label, "ok" if creds_state == "CONFIGURED" else "muted"
            )
        detail = snapshot.get("credentials_detail") or {}
        for key, (text, color) in credential_field_labels(detail, self._cred_row_labels).items():
            value_label = self._cred_row_labels[key]
            if not self._widget_exists(value_label):
                continue
            value_label.configure(text=text)
            self._set_fg(value_label, color)

        if snapshot.get("recovery_enabled"):
            self._recovery_frame.pack(fill="x", padx=8, pady=4)
        else:
            self._recovery_frame.pack_forget()
        self._update_toggle_buttons()

    # -- actions -----------------------------------------------------------


    def _selected_gpu(self) -> str:
        """GPU id the next launch should request (any stack)."""
        return self._gpu_choice.get().strip() or DEFAULT_RUNPOD_GPU_ID


    def _comfy_overrides(self) -> dict:
        """Explicit ComfyUI values from the GUI (gui.json-backed variables).

        The tier mode drives what personal material reaches the pod:
        "Auto" forces an empty preset and clears every catalog URL /
        category (standard stack + LoRAs only); "Auto + Perso" / "Perso"
        send the *active* user preset (picked next to the tier) plus the
        checked catalog (per-category URLs, and the list of categories with
        at least one checked model — launcher.config maps "" to "don't
        download").
        """
        return comfy_overrides_from_settings(self._comfy_settings_snapshot())

    def _comfy_settings_snapshot(self) -> GuiSettings:
        """Current ComfyUI-related widget values as a :class:`GuiSettings`.

        Feeds the pure :func:`comfy_overrides_from_settings` so the GUI and a
        non-GUI caller (CLI ``--gui-settings``, the MCP start tool) build the
        same overrides from one implementation.
        """
        snapshot = GuiSettings()
        snapshot.comfy_tier = self._comfy_tier.get()
        snapshot.comfy_workflows = self._comfy_workflows.get()
        snapshot.comfy_sage_attention = self._comfy_sage.get()
        snapshot.comfy_access = self._comfy_access.get()
        snapshot.comfy_ntfy_topic = self._comfy_ntfy.get()
        snapshot.comfy_personal_repo = self._comfy_repo.get()
        snapshot.comfyui_version = self._comfy_version.get()
        snapshot.comfy_auto_collect = bool(self._comfy_auto_collect.get())
        snapshot.comfy_notify_windows = bool(self._comfy_notify_windows.get())
        snapshot.comfy_notify_ntfy = bool(self._comfy_notify_ntfy.get())
        snapshot.comfy_terminate_after = bool(self._comfy_terminate_after.get())
        snapshot.comfy_outputs_dir = self._comfy_outputs_dir.get()
        snapshot.comfy_preset = self._comfy_preset.get()
        snapshot.comfy_models = self._comfy_models
        snapshot.comfy_presets = self._comfy_presets
        snapshot.comfy_annuaire = self._annuaire
        return snapshot

    def _on_tier_change(self, _event=None) -> None:
        """Sync the raw tier from its display label and persist at once.

        Same pattern as the preset: written to gui.json immediately (the
        window may close to the tray), the personal controls re-gated and
        the summary refreshed.
        """
        raw = COMFY_TIER_RAW_BY_LABEL.get(self._comfy_tier_ui.get(), "auto")
        self._comfy_tier.set(raw)
        self._update_perso_gating()
        self._refresh_comfy_summary()
        self._save_comfy_settings()

    def _update_perso_gating(self) -> None:
        """Gate the *active preset* selector on the tier mode (comfy mode only).

        "Auto" ignores the active preset, so the options preset selector is
        greyed (never hidden) with a tooltip explaining which modes activate
        it. "Auto + Perso" / "Perso" re-enable it.

        The preset EDITOR (model catalog + nodes/workflows in the "Models &
        presets" tab) is deliberately NOT gated by the tier: a preset is a
        reusable definition, so it must stay editable whatever the active
        tier — gating it here disabled the checkboxes in "Auto" mode, which
        silently dropped the user's edits (they "came back" on reload).
        """
        if self._mode.get() != "comfy":
            return
        enabled = self._comfy_tier.get() != "auto"
        tooltip = TECHNICAL_TOOLTIPS["perso_tiers_only"]
        if self._widget_exists(self._preset_combo):
            self._set_widget_enabled(self._preset_combo, enabled, tooltip)
        # Re-enabling drops the gating tooltip; the destructive preset
        # action keeps its own wording so it always says what it deletes.
        if enabled and self._widget_exists(self._btn_preset_delete):
            self._tooltip(self._btn_preset_delete, PRESET_DELETE_TOOLTIP)

    def _on_version_change(self, _event=None) -> None:
        """Sync the raw version pin from the combobox and persist at once.

        The choice is written to gui.json immediately (not only on
        start/quit): the window may be closed to the tray, and a later
        fresh launch must find the remembered version.
        """
        self._comfy_version.set(version_from_display(self._comfy_version_combo.get()))
        self._save_comfy_settings()

    def _start_comfy_release_check(self) -> None:
        """Fetch the latest ComfyUI releases from GitHub at startup.

        Runs off the Tk thread; the result is pushed through the app queue
        and applied on the main thread (newest release selected +
        persisted). On failure the local fallback list and the persisted
        choice are kept, with a warning.
        """

        def _worker() -> None:
            releases: Optional[list] = None
            try:
                releases = fetch_comfyui_releases()
            except Exception as exc:
                logger.warning(
                    "ComfyUI releases unavailable (%s): keeping the local list",
                    exc,
                )
            self._queue.put(("comfy_releases", releases))

        self._release_thread = threading.Thread(
            target=_worker, daemon=True, name="gui-comfy-releases"
        )
        self._release_thread.start()

    def _start_model_size_check(self) -> None:
        """Probe the download size of every known model URL (off-thread).

        Sizes are not part of the config (the pod only knows the files it
        already has), so each URL is probed once per session with a HEAD
        request and cached in ``self._model_sizes``. Failures are silent: an
        unknown size is simply not displayed on the card.
        """
        urls = set()
        for entries in self._comfy_models.values():
            for entry in entries:
                if entry.get("url"):
                    urls.add(entry["url"])
        for preset in self._comfy_presets.values():
            for model in preset.get("models", []):
                if model.get("url"):
                    urls.add(model["url"])
        pending = sorted(url for url in urls if url not in self._model_sizes)
        if not pending:
            return

        def _worker() -> None:
            # Report each URL as it is probed: waiting for all 13 before
            # showing anything left the cards blank for the whole sweep.
            for url in pending:
                size = probe_model_size(url)
                if size:
                    self._queue.put(("model_sizes", {url: size}))

        self._size_thread = threading.Thread(
            target=_worker, daemon=True, name="gui-model-sizes"
        )
        self._size_thread.start()

    def _apply_model_sizes(self, sizes: Mapping[str, int]) -> None:
        """Merge probed sizes and schedule one card rebuild.

        Sizes arrive one URL at a time; rebuilding the whole card list on
        each would flicker, so the rebuilds are coalesced.
        """
        if not sizes:
            return
        self._model_sizes.update(sizes)
        if self._size_rebuild_after is not None:
            try:
                self.root.after_cancel(self._size_rebuild_after)
            except tk.TclError:
                pass
        self._size_rebuild_after = self.root.after(250, self._rebuild_after_sizes)

    def _rebuild_after_sizes(self) -> None:
        self._size_rebuild_after = None
        if self._widget_exists(self._comfy_model_rows):
            self._rebuild_comfy_model_rows()

    def _cancel_size_rebuild(self) -> None:
        """Drop the pending card rebuild (called while shutting down)."""
        if self._size_rebuild_after is None:
            return
        try:
            self.root.after_cancel(self._size_rebuild_after)
        except tk.TclError:
            pass
        self._size_rebuild_after = None

    def _apply_comfy_releases(self, releases: Optional[list]) -> None:
        """Apply the fetched release list (or flag the offline fallback).

        The dropdown choices are refreshed, but the user's CURRENT pin is
        PRESERVED: it used to be silently reset to the newest release on
        every launch, so a deliberate pin (rollback / reproducibility of a
        workflow that breaks on a newer release) never survived a restart.
        The newest release is only selected when nothing is pinned yet.
        """
        if not self._widget_exists(self._comfy_version_combo):
            return
        if releases:
            releases = [r for r in releases if r][:COMFYUI_RELEASES_MAX]
        if releases:
            self._comfy_releases = list(releases)
            current = self._comfy_version_combo.get().strip()
            values = [COMFYUI_VERSION_AUTO_LABEL] + list(releases)
            # Keep an off-list pin (older tag or commit SHA) selectable.
            if current and current not in values:
                values.insert(1, current)
            self._comfy_version_combo.configure(values=values)
            if not current or current == COMFYUI_VERSION_AUTO_LABEL:
                # Nothing pinned: follow the latest release, i.e. leave
                # COMFYUI_COMMIT unset so the INSTALLER resolves the newest
                # stable tag at every pod boot. Auto-selecting releases[0]
                # instead (the old behaviour) silently turned "no choice" into
                # a PIN on whatever was newest the day the GUI first ran — the
                # pod then never followed a new release again.
                self._comfy_version_combo.set(COMFYUI_VERSION_AUTO_LABEL)
                self._comfy_version.set("")
                self._save_comfy_settings()
            if self._widget_exists(self._comfy_version_hint):
                self._comfy_version_hint.configure(
                    text="\"auto\" follows the latest release; a tag pins the version"
                )
        elif self._widget_exists(self._comfy_version_hint):
            self._comfy_version_hint.configure(
                text="GitHub list unavailable — local fallback"
            )

    def _on_preset_change(self, _event=None) -> None:
        """Sync the raw active-preset value from the combobox display.

        Bound to both ``<<ComboboxSelected>>`` and ``<FocusOut>``; the
        "None" label maps back to an empty preset (standard tier only).
        Persisted at once like the tier/version choices.
        """
        self._comfy_preset.set(_display_to_preset(self._comfy_preset_ui.get()))
        self._refresh_comfy_summary()
        self._save_comfy_settings()

    def _refresh_stack_rows(self, snapshot: Optional[dict] = None) -> None:
        """Paint the \"Active stacks\" panel (one row per workload stack).

        Each row shows that stack's pod status, tunnel, readiness and hourly
        rate, and offers its own Start / Stop / Terminate buttons — so a
        text-agent session, ComfyUI and LoRA training can run and be driven
        side by side.
        """
        rows = getattr(self, "_stack_rows", None)
        if not rows:
            return
        snapshot = snapshot if snapshot is not None else (self._last_snapshot or {})
        data = snapshot.get("active_stacks") or {}
        self._stacks_snapshot = data
        by_stack = {row.get("stack"): row for row in data.get("stacks", [])}
        for stack, widgets in rows.items():
            row = by_stack.get(stack)
            if row is None:
                widgets["status"].configure(text="—")
                widgets["cost"].configure(text="—")
                self._set_fg(widgets["status"], "muted")
                self._set_fg(widgets["cost"], "muted")
                widgets["start"].configure(state="normal")
                widgets["stop"].configure(state="disabled")
                widgets["term"].configure(state="disabled")
                continue
            widgets["status"].configure(
                text=(
                    f"{row.get('runpod', 'UNKNOWN')} · "
                    f"tunnel {row.get('tunnel', 'UNKNOWN')} · "
                    f"{row.get('ready', 'UNKNOWN')}"
                )
            )
            self._set_fg(widgets["status"], "ok" if row.get("running") else "muted")
            widgets["cost"].configure(text=row.get("cost_per_hour") or "—")
            self._set_fg(
                widgets["cost"], "ok" if row.get("cost_per_hour") else "muted"
            )
            running = bool(row.get("running"))
            connected = str(row.get("tunnel", "")).upper() == "CONNECTED"
            widgets["start"].configure(state="disabled" if running else "normal")
            widgets["stop"].configure(
                state="normal" if (running or connected) else "disabled"
            )
            widgets["term"].configure(
                state="normal" if row.get("pod", "NONE") != "NONE" else "disabled"
            )
        total = data.get("cost_per_hour_total")
        if self._widget_exists(self._stacks_total):
            self._stacks_total.configure(
                text=f"Cumulative cost/h: {total}" if total else ""
            )

    def _refresh_pod_card(self) -> None:
        """Fill the pod/GPU card (identity + spend + runtime) next to the table.

        The queue and the VRAM come from the ComfyUI probe: they are unknown
        until the pod answers, so those two rows stay hidden rather than
        showing a permanent "—".
        """
        labels = getattr(self, "_pod_card_values", None)
        if not labels:
            return
        record = None
        try:
            record = PodRegistry().load(self._stack.get())
        except Exception:  # noqa: BLE001 - a missing record is normal
            record = None
        billing = self._last_billing or {}
        comfy = (self._last_snapshot or {}).get("comfy") or {}
        gpu = "—"
        if record is not None:
            gpu = record.gpu_id or "—"
            if record.gpu_count and record.gpu_count > 1:
                gpu = f"{gpu} × {record.gpu_count}"
        values = {
            "pod": record.pod_id if record is not None else "—",
            "name": (record.name or "—") if record is not None else "—",
            "gpu": gpu,
            "zone": (record.data_center_id or "—") if record is not None else "—",
            # Spend/elapsed come from the billing probe: they are known even
            # when the local record is missing (pod created elsewhere).
            "elapsed": (billing.get("elapsed") or "—")
            if billing.get("running") else "—",
            "cost": billing.get("cost") or "—",
            "queue": str(comfy.get("queue") or ""),
            "vram": str(comfy.get("vram") or ""),
        }
        optional = getattr(self, "_pod_card_optional", set())
        for key, text in values.items():
            label = labels.get(key)
            if label is None or not self._widget_exists(label):
                continue
            if key in optional:
                key_label = self._pod_card_key_labels.get(key)
                if not text:
                    label.grid_remove()
                    if key_label is not None and self._widget_exists(key_label):
                        key_label.grid_remove()
                    continue
                if key_label is not None and self._widget_exists(key_label):
                    key_label.grid()
                label.grid()
            label.configure(text=text or "—")

    def _refresh_config_recap(self) -> None:
        """Refresh the Qwen recap card from the live state."""
        labels = getattr(self, "_config_recap_values", None)
        if not labels:
            return
        snapshot = self._last_snapshot or {}
        timer = "—"
        if self._widget_exists(getattr(self, "_timer_label", None)):
            timer = str(self._timer_label.cget("text")) or "—"
        values = {
            "stack": STACK_LABELS.get(self._stack.get(), self._stack.get()),
            "model": snapshot.get("model") or snapshot.get("image") or "—",
            "url": snapshot.get("url") or "—",
            "env": self._env_status_text(),
            "timer": timer,
        }
        for key, text in values.items():
            label = labels.get(key)
            if label is not None and self._widget_exists(label):
                label.configure(text=text)


    def _refresh_comfy_summary(self) -> None:
        """Recompute the one-line summary of the selected ComfyUI config."""
        self._refresh_comfy_preset_warning()
        label = getattr(self, "_comfy_summary_label", None)
        if label is None or not self._widget_exists(label):
            return
        label.configure(
            text=comfy_summary_text(
                self._comfy_preset.get(), self._comfy_tier.get(),
                self._comfy_workflows.get(), self._comfy_models,
                self._comfy_presets.get(self._comfy_preset.get().strip()),
            )
        )

    def _refresh_comfy_preset_warning(self) -> None:
        """Warn that the active preset is sent as-is to the pod.

        The catalog checkboxes only reach the pod through « Sauver »: without
        the warning, unticking a model looked like it applied immediately.
        """
        label = getattr(self, "_comfy_preset_warning", None)
        if label is None or not self._widget_exists(label):
            return
        preset = self._comfy_preset.get().strip()
        if not preset or self._comfy_tier.get() == "auto":
            label.pack_forget()
            return
        label.configure(
            text=f"⚠ Preset « {preset} » is sent as is: tick the boxes then "
            "press Save for your model changes to apply."
        )
        label.pack(fill="x", side="top", pady=(PAD_XS, 0))

    def _start_action(self) -> None:
        """Footer \"Start\": start the stack the mode radios select."""
        self._stack_start(self._stack.get())

    def _stack_start(self, stack: str) -> None:
        """Start one named stack (footer Start, or an \"Active stacks\" row).

        Starting one stack never touches another: each has its own pod,
        tunnel and lifecycle, so a text-agent session keeps running while
        ComfyUI or the training desktop comes up.
        """
        if stack == "comfy":
            # A fresh start lifts the post-termination suppression: the
            # operator asked for a new pod, the reception options apply again.
            self._comfy_watcher_suppressed = False
            if self._comfy_access.get() == "direct":
                try:
                    from tkinter import messagebox

                    if not messagebox.askyesno(
                        "Direct access (public URL)",
                        "Direct mode opens the pod's PUBLIC URL: "
                        "ComfyUI has no authentication, so anyone "
                        "with that URL can generate (GPU cost) and "
                        "reach the files.\n\n"
                        "The local tunnel (default) is recommended.\n\n"
                        "Continue with the public URL?",
                        parent=self.root,
                    ):
                        return
                except tk.TclError:
                    pass
            self._save_comfy_settings()
            self._run(
                "Start",
                lambda: action_start(
                    stack="comfy",
                    comfy=self._comfy_overrides(),
                    gpu=self._selected_gpu(),
                    on_no_capacity=self._ask_recreate,
                ),
            )
            return
        if stack == "train":
            self._save_comfy_settings()
            self._run(
                "Start",
                lambda: action_start(
                    stack="train",
                    train=self._train_overrides(),
                    gpu=self._selected_gpu(),
                    on_no_capacity=self._ask_recreate,
                ),
            )
            return

    def _stop_action(self, terminate: bool) -> None:
        """Footer \"Stop\": stop the stack the mode radios select."""
        self._stack_action(self._stack.get(), terminate)

    def _stack_action(self, stack: str, terminate: bool) -> None:
        """Stop (or terminate) one named stack, leaving the others running."""
        if terminate:
            try:
                if not self._confirm_pod_terminate():
                    return
            except tk.TclError:
                return
        self._run("Stop + terminate the pod" if terminate else "Stop",
                  lambda: action_stop(terminate=terminate, stack=stack))

    def _doctor_action(self) -> None:
        config = self._last_config
        self._run("Doctor", lambda: action_doctor(config))


    def _ask_recreate(self, pod_id: str) -> bool:
        """Ask (on the UI thread) whether to recreate a pod with no free GPU.

        Called from the start worker thread via ``on_no_capacity``. It blocks
        on a ``threading.Event`` while the UI thread shows a yes/no dialog and
        stores the answer. Returns False if the window is closing.
        """
        event = threading.Event()
        holder: dict = {}
        self._queue.put(("prompt_recreate", pod_id, holder, event))
        while not event.wait(0.5):
            if not self._widget_exists(self.root):
                return False
        return bool(holder.get("approved", False))


    def _seed_credentials_state(self) -> None:
        """Fill the \"API keys\" section from the store, right now.

        Called when the dialog is built so the labels are correct before the
        first refresh tick; the same computation is reused by the periodic
        refresh (``_apply_status``) and by the secrets dialog, so all three
        can never disagree."""
        try:
            details = _credentials_details(CredentialStore())
        except Exception:  # noqa: BLE001 - never block opening the dialog
            details = {}
        state = details.get("state") or "UNKNOWN"
        if self._widget_exists(self._creds_label):
            self._creds_label.configure(
                text=f"State: {CREDENTIAL_STATE.get(state, state)}"
            )
            self._set_fg(self._creds_label, "ok" if state == "CONFIGURED" else "muted")
        labels = credential_field_labels(details, self._cred_row_labels)
        for key, (text, color) in labels.items():
            value_label = self._cred_row_labels.get(key)
            if self._widget_exists(value_label):
                value_label.configure(text=text)
                self._set_fg(value_label, color)

    def _update_repair_button(self, snapshot: Optional[dict]) -> None:
        """Offer the one-click repair next to the cause, when it maps.

        The button only appears when repairs are enabled
        (LAUNCHER_INFRA_RECOVERY=confirm), no action is running, and the
        failing component maps onto a recovery action — otherwise the
        remediation text stays the only guidance."""
        action = None
        if snapshot is not None and not self._busy:
            # Only next to an actual cause: with no cause/remediation shown
            # the button would be an unexplained shortcut.
            if snapshot.get("recovery_enabled") and (
                snapshot.get("cause") or snapshot.get("remediation")
            ):
                action = suggest_recovery_action(
                    snapshot.get("components") or {},
                    snapshot.get("stack") or "agent",
                )
        self._repair_action = action
        if not self._widget_exists(self._btn_repair):
            return
        if action is None:
            self._btn_repair.pack_forget()
            return
        label = RECOVERY_LABELS.get(action, action)
        self._btn_repair.configure(text=f"▶ Repair: {label}")
        self._tooltip(
            self._btn_repair,
            f"Runs « {label} » — a confirmation is requested.",
        )
        self._btn_repair.pack(side="right")

    def _repair_action_clicked(self) -> None:
        action = self._repair_action
        if action is None:
            return
        self._recover_action(action)

    def _maybe_first_run_wizard(self) -> None:
        """Offer the credentials dialog on a first run (no key anywhere).

        Opened once per session, and only when nothing is configured: the
        wizard is the "click and go" path's missing half — without it a fresh
        install shows a wall of UNKNOWN with nothing saying what to do. Never
        modal-blocking: the user can simply close it.
        """
        if self._first_run_wizard_shown:
            return
        self._first_run_wizard_shown = True
        if not first_run_needs_credentials(self._last_config):
            return
        self._append_log(
            "info",
            "No RunPod API key configured yet. Opening Settings — paste your "
            "key and press Save, then Start.",
        )
        try:
            self._credentials_set()
        except Exception as exc:  # noqa: BLE001 - never fail the launch
            logger.warning("First-run wizard unavailable: %s", exc)

    def _recover_action(self, action_name: str, stack: Optional[str] = None) -> None:
        """Run one gated recovery action.

        ``stack`` selects the stack the action applies to when it differs from
        the currently selected mode (the ComfyUI toggle must be able to restore
        the comfy stack from the agent tab): the recovery engine refuses an
        action that does not match its config's stack.
        """
        config = self._last_config
        if stack is not None and (config is None or config.stack != stack):
            try:
                config = load_config(stack=stack, store=CredentialStore())
            except ConfigError:
                config = None
        if config is None:
            self._append_log("error", "Repair unavailable: invalid configuration.")
            return
        label = RECOVERY_LABELS[action_name]
        try:
            from tkinter import messagebox

            if not messagebox.askyesno(
                "Repair",
                f"{label} — this action changes the infrastructure.\n\nConfirm?",
                parent=self.root,
            ):
                return
        except tk.TclError:
            pass
        self._run(f"recover:{label}", lambda: action_recover(config, action_name))


    def _toggle_comfy_action(self) -> None:
        """ComfyUI toggle (comfy stack, tunnel access): close the local SSH
        tunnel when alive; otherwise restore it via the ``restart_comfy``
        recovery action (which keeps the pod).

        Works from any mode: the toggle follows the comfy stack's own state,
        not the tab the user happens to be on.
        """
        snapshot = self._last_snapshot or {}
        comfy_info = snapshot.get("comfy") or {}
        if self._mode.get() == "comfy" and comfy_info.get("access") == "direct":
            return
        comfy_row = next(
            (
                row
                for row in (self._stacks_snapshot or {}).get("stacks", [])
                if row.get("stack") == "comfy"
            ),
            None,
        )
        if comfy_row is None and self._mode.get() != "comfy":
            return
        components = snapshot.get("components") or {}
        tunnel_up = (
            str(comfy_row.get("tunnel", "")).upper() == "CONNECTED"
            if comfy_row is not None
            else str(components.get("ssh", "")).upper() == "CONNECTED"
        )
        if tunnel_up:
            config = self._last_config
            if config is None:
                self._append_log(
                    "error",
                    "Cannot stop the ComfyUI tunnel: invalid configuration.",
                )
                return
            try:
                from tkinter import messagebox

                if not messagebox.askyesno(
                    "Stop the ComfyUI tunnel",
                    "The local SSH tunnel to ComfyUI will be closed.\n"
                    "The pod and ComfyUI on the pod stay up.\n\n"
                    "Confirm?",
                    parent=self.root,
                ):
                    return
            except tk.TclError:
                return
            self._run(
                "Stop the ComfyUI tunnel",
                lambda: action_comfy_tunnel_stop(config),
            )
        else:
            self._recover_action("restart_comfy", stack="comfy")

    def _gate_button(self, button, enabled: bool, tooltip_text: str) -> None:
        """Enable *button*, or disable it with *tooltip_text* explaining why."""
        if enabled:
            button.configure(state="normal")
            button.unbind("<Enter>")
            button.unbind("<Leave>")
            button.unbind("<Button-1>")
            self._tooltip_texts.pop(button, None)
        else:
            button.configure(state="disabled")
            self._tooltip(button, tooltip_text)

    def _color_toggle(
        self, button, active: bool, pal: Mapping[str, str],
        *, neutral_when_off: bool = False,
    ) -> None:
        """Paint a status toggle green (active) or red (down); neutral grey
        while it is currently gated/disabled.

        ``neutral_when_off`` paints the inactive state grey instead of red:
        "not open" is a normal state, not an error.
        """
        off_style = "outline-secondary" if neutral_when_off else "danger"
        if self._tb_style is not None:
            if str(button.cget("state")) == "disabled":
                button.configure(bootstyle="outline-secondary")
                return
            button.configure(bootstyle="success" if active else off_style)
            return
        if str(button.cget("state")) == "disabled":
            button.configure(
                bg=pal["button_bg"], fg=pal["button_fg"],
                activebackground=pal["button_bg"], activeforeground=pal["button_fg"],
            )
            return
        bg = pal["ok"] if active else (pal["bg_alt"] if neutral_when_off else pal["error"])
        fg = pal["bg"] if self.settings.theme == "dark" else pal["accent_fg"]
        if neutral_when_off and not active:
            fg = pal["button_fg"]
        button.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)

    def _update_toggle_buttons(self) -> None:
        """Gate + recolor the ComfyUI tunnel toggle.

        Green when the comfy stack's local SSH tunnel is alive, red otherwise;
        enabled as soon as the comfy stack is active (not only when the comfy
        tab is selected). The restore path needs infrastructure recovery
        enabled, and direct access has no local process at all.
        """
        if not self._widget_exists(self._toggle_comfy):
            return
        pal = self._pal
        snapshot = self._last_snapshot or {}
        components = snapshot.get("components") or {}
        recovery_enabled = bool(snapshot.get("recovery_enabled"))
        stack = self._stack.get()

        comfy_info = snapshot.get("comfy") or {}
        direct = comfy_info.get("access") == "direct"
        # The comfy stack's OWN tunnel/state (the health table describes the
        # selected mode's stack, which may be another one entirely).
        comfy_row = next(
            (
                row
                for row in (self._stacks_snapshot or {}).get("stacks", [])
                if row.get("stack") == "comfy"
            ),
            None,
        )
        comfy_active = stack == "comfy" or comfy_row is not None
        if comfy_row is not None:
            tunnel_up = str(comfy_row.get("tunnel", "")).upper() == "CONNECTED"
        else:
            tunnel_up = (
                stack == "comfy"
                and str(components.get("ssh", "")).upper() == "CONNECTED"
            )
        if comfy_active and not direct:
            if tunnel_up or recovery_enabled:
                self._gate_button(self._toggle_comfy, True, "")
            else:
                self._gate_button(
                    self._toggle_comfy, False,
                    TECHNICAL_TOOLTIPS["tunnel_restart_recovery"],
                )
        else:
            tooltip = TECHNICAL_TOOLTIPS["comfy_direct_no_tunnel"]
            self._gate_button(self._toggle_comfy, False, tooltip)
        self._color_toggle(self._toggle_comfy, tunnel_up, pal)


    def _open_url(self, url: str) -> None:
        """Open *url* in the browser from the main thread, journaling the result."""
        error = open_url_in_browser(url)
        if error is None:
            self._append_log("ok", f"Opened {url}")
        else:
            self._append_log("error", f"Could not open the browser: {error}")

    def _open_comfy_surface(self, url: str) -> None:
        """Open ComfyUI, re-establishing its tunnel first when it is down."""
        self._run("Open ComfyUI", lambda: self._open_comfy_checked(url))

    def _open_comfy_button(self) -> None:
        """Footer "Open ComfyUI": open the UI of the active comfy stack."""
        snapshot = self._last_snapshot or {}
        url = (snapshot.get("comfy") or {}).get("url") or "http://127.0.0.1:8188"
        self._open_comfy_surface(url)

    def _comfy_config(self) -> Optional[Config]:
        """Config describing the comfy stack (from the current mode when possible)."""
        config = self._last_config
        if config is not None and config.stack == "comfy":
            return config
        try:
            return load_config(stack="comfy", store=CredentialStore())
        except ConfigError:
            return None

    def _pod_state(self, config: Config) -> str:
        """RunPod status of *config*'s pod, "" when it cannot be read.

        A pod the registry still lists but RunPod no longer has is reported as
        ``MISSING`` and its stale record is dropped: that is exactly the state
        left behind by deleting a pod from the RunPod console.
        """
        from .pod_registry import PodRegistry
        from .runpod import PodNotFoundError, RunPodClient

        registry = PodRegistry()
        pod_id = _resolve_pod_id(config, registry)
        if pod_id is None:
            return "MISSING"
        if not config.secrets.runpod_api_key:
            return ""
        try:
            pod = RunPodClient(
                config.secrets.runpod_api_key, timeout=10.0
            ).get_pod(pod_id)
        except PodNotFoundError:
            try:
                registry.clear(config.stack)
            except Exception:  # noqa: BLE001 - a stale record is not fatal here
                pass
            return "MISSING"
        except Exception:  # noqa: BLE001 - unreachable API => "unknown"
            return ""
        return pod.status

    def _open_comfy_checked(self, url: str) -> str:
        """Worker: check the ComfyUI tunnel, repair it, then open *url*.

        Runs on the action thread (``_run``): journal lines go through the
        queue (never Tk directly), the browser is opened from here, and the
        returned string is the final journal/status line. A refused or failed
        check raises, so ``_finish_action`` reports it once as an error.
        """
        from .tunnel import is_port_free

        config = self._comfy_config()
        if config is None:
            raise RuntimeError("Configuration ComfyUI introuvable.")
        label = "ComfyUI"
        port = config.comfy.local_port
        if is_port_open(port):
            outcome, state = OPEN_TUNNEL_OK, ""
        else:
            state = self._pod_state(config)
            outcome = open_tunnel_outcome(
                tunnel_ok=False, pod_state=state, port_free=is_port_free(port)
            )
        if outcome == OPEN_TUNNEL_REPAIR:
            self._queue.put(("log", "warn", f"Tunnel {label} down — reconnecting…"))
            result = action_reconnect_tunnel(config)
            if not result.ok:
                raise RuntimeError(f"Tunnel {label} not restored: {result.message}")
            self._queue.put(
                ("log", "ok", f"Tunnel {label} restored — {result.message}")
            )
        elif outcome != OPEN_TUNNEL_OK:
            raise RuntimeError(
                open_tunnel_message(outcome, state=state, label=label, port=port)
            )
        else:
            self._queue.put(("log", "ok", f"Tunnel {label} OK (127.0.0.1:{port})"))
        error = open_url_in_browser(url)
        if error is not None:
            raise RuntimeError(f"Could not open the browser: {error}")
        return f"Ouverture de {url}"

    # -- train stack --------------------------------------------------------

    def _train_overrides(self) -> dict:
        """The train values the GUI owns, fed to ``load_config(train=...)``.

        ``lora_dir`` is here rather than in the environment because it is a
        user choice made in this tab; ``fetch_models`` comes from the persisted
        settings for the same reason.
        """
        snapshot = GuiSettings()
        snapshot.train_fetch_models = self.settings.train_fetch_models
        snapshot.train_lora_dir = self._train_lora_dir.get().strip()
        return train_overrides_from_settings(snapshot)

    def _train_lora_dir_path(self) -> Optional[Path]:
        """The chosen local collection folder, or None when nothing is chosen."""
        raw = self._train_lora_dir.get().strip()
        return Path(raw) if raw else None

    def _on_train_fetch_change(self) -> None:
        """Persist the pre-download choice and refresh its cost hint.

        ``FETCH_MODELS`` is pod environment: the launcher re-syncs the pod
        (stop -> update env -> start) when it changes, so the hint says so
        before the user starts anything.
        """
        selected = [
            key for key, var in self._train_fetch_vars.items() if var.get()
        ]
        self.settings.train_fetch_models = ",".join(selected)
        self._save_comfy_settings()
        self._refresh_train_fetch_hint()
        self._append_log(
            "info",
            "Pre-downloaded models: "
            + (", ".join(selected) if selected else "none")
            + " — applied at the pod's next start (re-synchronisation).",
        )

    def _refresh_train_fetch_hint(self) -> None:
        label = getattr(self, "_train_fetch_hint", None)
        if label is None or not self._widget_exists(label):
            return
        selected = [
            key for key, var in getattr(self, "_train_fetch_vars", {}).items()
            if var.get()
        ]
        if not selected:
            text = (
                "No pre-download: the pod starts fast and the "
                "Fizgig Preferences button fetches a family on demand. "
                "MiniMax H3 weighs ~45 GB — leaving the boxes clear is the "
                "cheapest choice."
            )
        else:
            text = (
                "Pre-downloaded at every pod start: "
                + ", ".join(selected)
                + " (~45 GB for MiniMax H3). Pod setting: changing it "
                "restarts the pod (the volume is kept)."
            )
        try:
            label.configure(text=text)
        except tk.TclError:
            pass

    def _train_urls(self) -> tuple[str, str]:
        """(desktop, files) URLs for the *configured* train ports.

        Falls back to the defaults when no config is loaded yet, so the
        buttons always open the same port the tab's own rows display.
        """
        config = self._last_config
        if config is not None and getattr(config, "stack", None) == "train":
            return config.train_base_url(), config.train_files_url()
        return (
            f"http://127.0.0.1:{DEFAULT_TRAIN_LOCAL_PORT}",
            f"http://127.0.0.1:{DEFAULT_TRAIN_FILES_LOCAL_PORT}",
        )

    def _open_train_desktop(self) -> None:
        """Open the Fizgig desktop in the browser (through the SSH tunnel)."""
        url, _files = self._train_urls()
        try:
            webbrowser.open(url)
            self._append_log("ok", f"Ouverture de Fizgig : {url}")
            self._append_log(
                "info",
                "SSH tunnel access only. If the page does not answer, "
                "start the LoRA training mode.",
            )
        except Exception as exc:  # noqa: BLE001
            self._append_log("error", f"Could not open the browser: {exc}")

    def _open_train_files(self) -> None:
        """Open the pod's file manager (datasets in, LoRAs out) in the browser."""
        _desktop, url = self._train_urls()
        try:
            webbrowser.open(url)
            self._append_log("ok", f"Opening the file manager: {url}")
        except Exception as exc:  # noqa: BLE001
            self._append_log("error", f"Could not open the browser: {exc}")

    def _choose_train_lora_dir(self) -> None:
        """Pick the local folder trained LoRAs are collected into."""
        from tkinter import filedialog

        current = self._train_lora_dir.get().strip()
        try:
            chosen = filedialog.askdirectory(
                parent=self.root,
                title="Local LoRA collection folder",
                initialdir=current or str(Path.home()),
                mustexist=False,
            )
        except tk.TclError as exc:
            self._append_log("error", f"Folder picker unavailable: {exc}")
            return
        if not chosen:
            return
        self._train_lora_dir.set(chosen)
        self.settings.train_lora_dir = chosen
        save_settings(self.settings)
        self._append_log("ok", f"LoRA collection folder: {chosen}")

    def _train_ops(self):
        """Build a TrainOps bound to the registered training pod."""
        from .config import load_config
        from .train_ops import build_train_ops

        config = load_config(
            stack="train", train=self._train_overrides(), store=CredentialStore()
        )
        return build_train_ops(config)

    def _list_train_loras(self) -> None:
        """List the LoRA files on the pod, without downloading anything."""

        def _worker() -> None:
            try:
                names = self._train_ops().list_loras()
            except Exception as exc:  # noqa: BLE001
                self._queue.put(
                    ("train_result", "list", ("error", f"Listage impossible : {exc}"))
                )
                return
            if not names:
                self._queue.put(
                    ("train_result", "list",
                     ("info", "No LoRA on the pod yet."))
                )
                return
            preview = ", ".join(names[:6]) + ("…" if len(names) > 6 else "")
            self._queue.put(
                ("train_result", "list",
                 ("ok", f"{len(names)} LoRA file(s) on the pod: {preview}"))
            )

        threading.Thread(target=_worker, daemon=True, name="gui-train-list").start()

    def _collect_train_loras(self) -> None:
        """Download every LoRA from the pod into the chosen local folder."""
        dest = self._train_lora_dir_path()
        if dest is None:
            self._append_log(
                "warn",
                "Pick a local collection folder first "
                "(the « Browse… » button).",
            )
            return
        self._append_log("info", f"Collecting LoRAs to {dest}…")

        def _worker() -> None:
            try:
                downloaded = self._train_ops().collect_loras(dest)
            except Exception as exc:  # noqa: BLE001
                self._queue.put(
                    ("train_result", "collect",
                     ("error", f"Collection failed: {exc}"))
                )
                return
            if not downloaded:
                self._queue.put(
                    ("train_result", "collect",
                     ("info", f"Nothing new to collect in {dest}."))
                )
                return
            self._queue.put(
                ("train_result", "collect",
                 ("ok", f"{len(downloaded)} LoRA collected to {dest}."))
            )

        threading.Thread(target=_worker, daemon=True, name="gui-train-collect").start()

    def _verify_train_template(self) -> None:
        """Check the stored RunPod template against what the stack requires.

        Runs the same checks as ``scripts/train/verify_template.py`` — one
        implementation, two entry points — so the button and the terminal can
        never disagree.
        """

        def _worker() -> None:
            try:
                from .config import load_config
                from .runpod import RunPodClient
                from .train_template import failures, summarize, verify_template

                config = load_config(
                    stack="train", train=self._train_overrides(),
                    store=CredentialStore(),
                )
                template_id = config.secrets.train_template_id
                if not template_id:
                    self._queue.put(
                        ("train_result", "verify",
                         ("error",
                          "No training template ID configured. "
                          "Set it in Credentials → \"LoRA training "
                          "template (private)\"."))
                    )
                    return
                client = RunPodClient(config.secrets.runpod_api_key)
                checks = verify_template(template_id, client)
            except Exception as exc:  # noqa: BLE001
                self._queue.put(
                    ("train_result", "verify",
                     ("error", f"Cannot verify: {exc}"))
                )
                return
            bad = failures(checks)
            if bad:
                detail = " · ".join(f"{c.name} — {c.detail}" for c in bad)
                self._queue.put(
                    ("train_result", "verify",
                     ("error", f"{summarize(checks)} {detail}"))
                )
            else:
                self._queue.put(
                    ("train_result", "verify", ("ok", summarize(checks)))
                )

        threading.Thread(target=_worker, daemon=True, name="gui-train-verify").start()

    def _check_fizgig_version(self) -> None:
        """Compare the Fizgig the pod runs against upstream's newest release.

        Two independent reads, and the tab says which one failed rather than
        guessing:

        * the pod's revision, over SSH — so it needs a live pod *and* a live
          tunnel, and it is whatever ``FIZGIG_REF`` pointed at when the pod last
          started, not a value the launcher chose;
        * upstream's releases, from the GitHub API — public, no credentials,
          and rate-limited per IP, which is why this runs on a button rather
          than on the 5 s poll tick.

        With ``FIZGIG_REF=master`` the update *is* a pod restart: upstream's
        entrypoint pulls the ref at every boot. So a stale pod needs no image
        rebuild and no re-audit — which is exactly why the drift is worth
        surfacing instead of leaving to be discovered.
        """

        def _worker() -> None:
            try:
                from .config import load_config
                from .fizgig_version import Status, check, fetch_releases
                from .train_ops import build_train_ops

                config = load_config(
                    stack="train", train=self._train_overrides(),
                    store=CredentialStore(),
                )
            except Exception as exc:  # noqa: BLE001
                self._queue.put(
                    ("train_result", "fizgig_version",
                     ("error", f"Lecture de la configuration impossible : {exc}", False))
                )
                return

            releases = fetch_releases()

            pod_raw = ""
            pod_error = ""
            try:
                pod_raw = build_train_ops(config).pod_fizgig_version()
            except Exception as exc:  # noqa: BLE001 - a dead pod is a normal answer
                pod_error = str(exc)

            if pod_error:
                latest = releases[0].tag if releases else "illisible"
                self._queue.put(
                    ("train_result", "fizgig_version",
                     ("warn",
                      f"Pod injoignable — version non lue ({pod_error}). "
                      f"Latest upstream release: {latest}.", False, ""))
                )
                return

            result = check(pod_raw, releases)
            message = result.detail
            if result.status is Status.BEHIND:
                message += " — restart the pod to pick it up."
            self._queue.put(
                ("train_result", "fizgig_version",
                 ("warn" if result.is_stale else "ok", message, result.is_stale, pod_raw))
            )

        threading.Thread(
            target=_worker, daemon=True, name="gui-train-fizgig-version"
        ).start()

    def _apply_train_result(self, action: str, result: tuple) -> None:
        """Land a background train-stack result in the journal and the tab."""
        level, message = result[0], result[1]
        self._append_log(level, message)
        if action == "collect":
            self._train_collect_status.configure(text=message)
        elif action == "list":
            self._train_collect_status.configure(text=message)
        elif action == "verify":
            self._train_hint.configure(text=message)
        elif action == "fizgig_version":
            stale = bool(result[2]) if len(result) > 2 else False
            running = str(result[3]) if len(result) > 3 else ""
            self._train_version_hint.configure(text=message)
            # A stale pod is worth a colour: it is the one outcome with an
            # action attached, and it is easy to miss in a muted line.
            self._themed(self._train_version_hint, "warn" if stale else "muted")
            if running:
                # Remembered so the 5 s tab refresh stops overwriting it with the
                # *configured* ref — the row is meant to show what the pod runs.
                self._train_fizgig_running = running
                label = self._train_access_rows.get("fizgig")
                if label is not None:
                    label.configure(text=running)

    def _refresh_train_tab(self) -> None:
        """Show the configured endpoints, without touching the network.

        Deliberately offline: the tab must render even when no pod exists, and
        a status refresh already runs on every poll tick. The train config is
        reused from the last refresh cycle when the active stack already is
        ``train`` — re-deriving it would decrypt the DPAPI credential store on
        the UI thread every 5 s for information that has not changed.
        """
        if not self._widget_exists(self._train_access_frame):
            return
        config = self._last_config
        if config is None or getattr(config, "stack", None) != "train":
            try:
                from .config import load_config

                config = load_config(
                    stack="train", train=self._train_overrides(),
                    store=CredentialStore(),
                )
            except Exception:  # noqa: BLE001 - the tab still shows its defaults
                config = None
        image = config.train.image_name if config else "—"
        desktop = config.train_base_url() if config else f"http://127.0.0.1:{DEFAULT_TRAIN_LOCAL_PORT}"
        files = config.train_files_url() if config else f"http://127.0.0.1:{DEFAULT_TRAIN_FILES_LOCAL_PORT}"
        record = None
        try:
            record = PodRegistry().load("train")
        except Exception:  # noqa: BLE001
            record = None
        pod_text = record.pod_id if record is not None else "no pod registered"
        # The Fizgig row shows what the pod actually runs once the version check
        # has read it; before that it shows the ref the launcher asked for, so
        # the row is never blank and the two are never confused.
        fizgig_text = getattr(self, "_train_fizgig_running", "")
        if not fizgig_text:
            ref = config.train.fizgig_ref if config else "master"
            fizgig_text = f"{ref} (configured reference)"
        for key, value in (
            ("image", image),
            ("fizgig", fizgig_text),
            ("desktop", desktop),
            ("files", files),
            ("pod", pod_text),
        ):
            label = self._train_access_rows.get(key)
            if label is not None:
                label.configure(text=value)

    # -- stack / comfy ------------------------------------------------------

    def _save_comfy_settings(self) -> None:
        """Persist the current stack + ComfyUI choices to gui.json."""
        self.settings.auto_refresh = bool(self._auto_refresh.get())
        self.settings.minimize_on_close = bool(self._minimize_on_close.get())
        self.settings.stack = self._stack.get()
        self.settings.comfy_preset = self._comfy_preset.get().strip()
        self.settings.comfy_tier = self._comfy_tier.get()
        self.settings.comfy_workflows = self._comfy_workflows.get()
        self.settings.comfy_sage_attention = self._comfy_sage.get()
        self.settings.comfy_access = self._comfy_access.get()
        self.settings.comfy_ntfy_topic = self._comfy_ntfy.get().strip()
        self.settings.comfy_personal_repo = self._comfy_repo.get().strip()
        self.settings.comfyui_version = self._comfy_version.get().strip()
        self.settings.comfy_auto_collect = bool(self._comfy_auto_collect.get())
        self.settings.comfy_notify_windows = bool(self._comfy_notify_windows.get())
        self.settings.comfy_notify_ntfy = bool(self._comfy_notify_ntfy.get())
        self.settings.comfy_terminate_after = bool(self._comfy_terminate_after.get())
        self.settings.comfy_outputs_dir = self._comfy_outputs_dir.get().strip()
        # Per-category URLs derive from the catalog (enabled entries joined by
        # comma). Kept in the legacy single-string fields for compatibility.
        enabled = _enabled_urls_from_catalog(self._comfy_models)
        self.settings.diffusion_url = ",".join(enabled.get("diffusion", []))
        self.settings.video_vae_url = ",".join(enabled.get("video_vae", []))
        self.settings.audio_vae_url = ",".join(enabled.get("audio_vae", []))
        self.settings.text_encoder_url = ",".join(enabled.get("text_encoder", []))
        self.settings.tae_url = ",".join(enabled.get("tae", []))
        self.settings.upscaler_url = ",".join(enabled.get("upscaler", []))
        self.settings.frame_interp_url = ",".join(enabled.get("frame_interp", []))
        self.settings.comfy_models = self._comfy_models
        self.settings.comfy_presets = self._comfy_presets
        self.settings.comfy_annuaire = self._annuaire
        # Train stack: the collection folder is typed in the tab, and the
        # pre-download list is driven by its checkboxes — both live here so a
        # single "persist everything" call covers them.
        self.settings.train_lora_dir = self._train_lora_dir.get().strip()
        self.settings_saved_ok = save_settings(self.settings)
        # Keep the watcher's thread-safe snapshot in step with the widgets:
        # the reception folder is typed here, and with auto-refresh off
        # nothing else would refresh it.
        self._refresh_comfy_watch_state()

    def _on_stack_change(self) -> None:
        stack = self._stack.get()
        comfy_active = self._mode.get() == "comfy"
        # Point 4: the inactive mode's options are never hidden — only
        # disabled, with a tooltip explaining why each control is greyed.
        self._set_comfy_frames_enabled(comfy_active)
        # The tier gate applies on top of the mode gate (order matters:
        # the mode gate runs first so the tier gate never re-enables
        # controls of the inactive mode).
        self._update_perso_gating()
        self._refresh_config_recap()
        self._update_toggle_buttons()
        if self._last_snapshot is not None:
            self._redraw_health_table(self._last_snapshot)

    # -- Pod ComfyUI operations (via ComfyOps) ------------------------------

    def _pod_list(self) -> None:
        """List the LoRAs installed on the pod (standard + personal folders)."""

        def work():
            config = load_config(stack="comfy", store=CredentialStore())
            ops = comfy_ops.build_comfy_ops(config)
            main = ops.list_loras(personal=False)
            perso = ops.list_loras(personal=True)
            return main + "\n" + perso

        self._run("List the pod", work)

    def _pod_remove(self) -> None:
        """Remove a LoRA file from the pod's personal folder."""
        name = self._pod_remove_name.get().strip()
        if not name:
            self._append_log("error", "Enter the name of the file to remove from the pod.")
            return

        def work():
            config = load_config(stack="comfy", store=CredentialStore())
            ops = comfy_ops.build_comfy_ops(config)
            return ops.remove_lora(name, personal=True)

        self._run("Remove from the pod", work)

    def _vault_sync(self) -> None:
        def work():
            config = load_config(stack="comfy", store=CredentialStore())
            ops = comfy_ops.build_comfy_ops(config)
            return ops.sync_vault()

        self._run("Sync vault", work)

    def _outputs_download(self) -> None:
        local_dir = Path.home() / "Downloads" / "comfy-outputs"

        def work():
            config = load_config(stack="comfy", store=CredentialStore())
            ops = comfy_ops.build_comfy_ops(config)
            names = ops.list_outputs(subfolders=True)
            if not names:
                return "No output on the pod."
            for name in names:
                parts = name.split("/")
                ops.download_output(
                    parts[-1], local_dir, subfolder="/".join(parts[:-1])
                )
            return f"{len(names)} output(s) downloaded to {local_dir}."

        self._run("Download outputs", work)

    # -- Automatic collection of generations --------------------------------

    def _on_comfy_receive_change(self) -> None:
        """Persist the reception options and (re)start/stop the watcher.

        A toggle also clears the post-termination suppression: the operator
        explicitly asked for the feature again.
        """
        self._comfy_watcher_suppressed = False
        self._save_comfy_settings()
        self._update_receive_gating()
        self._sync_comfy_watcher()

    def _comfy_watch_requested(self) -> bool:
        """True when at least one reception option is checked."""
        return bool(
            self._comfy_auto_collect.get()
            or self._comfy_notify_windows.get()
            or self._comfy_notify_ntfy.get()
            or self._comfy_terminate_after.get()
        )

    def _refresh_comfy_watch_state(self) -> dict:
        """Snapshot the Tk variables into the plain dict the thread reads."""
        state = {
            "enabled": self._comfy_watch_requested(),
            "auto_collect": bool(self._comfy_auto_collect.get()),
            "output_dir": self._comfy_outputs_dir.get(),
            "notify_windows": bool(self._comfy_notify_windows.get()),
            "notify_ntfy": bool(self._comfy_notify_ntfy.get()),
            "ntfy_topic": self._comfy_ntfy.get(),
            # Self-hosted ntfy (the same ``NTFY_SERVER`` the .env gives the
            # pod): without it the launcher would publish on the public
            # ntfy.sh while the pod publishes on the operator's server.
            "ntfy_server": os.environ.get("NTFY_SERVER", "").strip() or None,
            "terminate_after": bool(self._comfy_terminate_after.get()),
            # How long the ComfyUI queue must stay empty before the pod is
            # stopped (env-configurable; see ComfyConfig.terminate_settle_seconds).
            "terminate_settle_seconds": _comfy_terminate_settle_seconds(),
        }
        self._comfy_watch_state = state
        return state

    def _comfy_watch_settings(self) -> dict:
        """Thread-safe settings for the watcher (never touches Tk)."""
        return dict(self._comfy_watch_state)

    def _comfy_watch_ops(self):
        """Build a ComfyOps bound to the comfy pod (worker thread)."""
        config = load_config(stack="comfy", store=CredentialStore())
        return comfy_ops.build_comfy_ops(config)

    def _comfy_watch_terminate(self) -> tuple[bool, str]:
        """Terminate the comfy pod after the watched generation."""
        try:
            return action_stop_outcome(terminate=True, stack="comfy")
        except Exception as exc:  # noqa: BLE001 - reported to the journal
            return False, str(exc)

    def _sync_comfy_watcher(self) -> None:
        """Start or stop the watcher to match the current options.

        Called from the periodic status refresh (and from the option
        checkboxes): the watcher itself decides whether ComfyUI actually
        answers on the tunnel, so a disabled stack is simply an idle thread.
        """
        state = self._refresh_comfy_watch_state()
        if self._comfy_watcher_suppressed or not state["enabled"]:
            self._stop_comfy_watcher()
            return
        watcher = self._comfy_watcher
        if watcher is not None and watcher.alive:
            return
        watcher = comfy_watch.ComfyOutputWatcher(
            self._comfy_watch_ops,
            settings=self._comfy_watch_settings,
            on_notify=lambda title, message: self._queue.put(
                ("comfy_done", title, message)
            ),
            terminate_fn=self._comfy_watch_terminate,
            on_terminated=lambda ok, message: self._queue.put(
                ("comfy_terminated", ok, message)
            ),
        )
        self._comfy_watcher = watcher
        watcher.start()
        logger.info("Auto-collect: generation watching started.")

    def _stop_comfy_watcher(self) -> None:
        watcher = self._comfy_watcher
        if watcher is None:
            return
        self._comfy_watcher = None
        watcher.stop()

    def _notify_windows(self, title: str, message: str) -> None:
        """Windows notification: tray balloon first, system balloon else."""
        tray = getattr(self, "_tray", None)
        if tray is not None:
            try:
                tray.notification(title, message)
                return
            except Exception as exc:  # noqa: BLE001 - fall back, never raise
                logger.debug("tray notification unavailable: %s", exc)
        alerts.notify_windows(title, message)

    def _on_comfy_watch_terminated(self, ok: bool, message: str) -> None:
        """The watcher terminated the pod: uncheck the one-shot option."""
        self._comfy_watcher_suppressed = True
        if self._comfy_terminate_after.get():
            self._comfy_terminate_after.set(False)
            self._save_comfy_settings()
        self._stop_comfy_watcher()
        self._append_log(
            "ok" if ok else "warn", f"Terminate the pod: {message}"
        )

    # -- Annuaire (LoRA / Workflow / Node) ---------------------------------

    def _annuaire_new(self) -> None:
        """Clear and show the form for a brand-new entry."""
        self._annuaire_editing_index = None
        self._annuaire_form.configure(text="New entry")
        self._form_type.set("lora")
        self._form_mode.set("—")
        self._form_model.configure(values=self._annuaire_model_values())
        self._form_model.set("")
        for var in self._form_vars.values():
            var.set("")
        self._form_dl_url.set("")
        self._form_local_path.set("")
        self._form_source.set(ANNUAIRE_SOURCE_URL)
        self._form_note.delete("1.0", "end")
        self._on_form_source_change()
        self._annuaire_form.pack(
            fill="x", padx=8, pady=(4, 0), before=self._pod_actions_frame
        )

    def _annuaire_edit(self, index: int) -> None:
        """Load an entry into the form for editing and show it."""
        if not (0 <= index < len(self._annuaire)):
            return
        entry = self._annuaire[index]
        self._annuaire_editing_index = index
        self._annuaire_form.configure(text="Editing the entry")
        self._form_type.set(entry.get("type", "lora"))
        self._form_mode.set(ANNUAIRE_MODE_LABELS.get(entry.get("mode", ""), "—"))
        self._form_model.configure(values=self._annuaire_model_values())
        self._form_model.set(entry.get("model", ""))
        for key in ("name", "page_url", "trigger_words"):
            self._form_vars[key].set(entry.get(key, ""))
        self._form_dl_url.set(str(entry.get("dl_url", "")))
        self._form_local_path.set(str(entry.get("local_path", "")))
        self._form_source.set(_annuaire_entry_source(entry))
        self._form_note.delete("1.0", "end")
        self._form_note.insert("1.0", entry.get("note", ""))
        self._on_form_source_change()
        self._annuaire_form.pack(
            fill="x", padx=8, pady=(4, 0), before=self._pod_actions_frame
        )

    def _annuaire_cancel_form(self) -> None:
        """Hide the form without saving."""
        self._annuaire_editing_index = None
        self._annuaire_form.pack_forget()

    def _annuaire_export_md(self) -> None:
        """Export the library to a Markdown file in the Downloads folder."""
        if not self._annuaire:
            self._append_log("warn", "The library is empty — nothing to export.")
            return
        md = _annuaire_to_markdown(self._annuaire)
        out_dir = Path.home() / "Downloads"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            out_dir = Path.home()
        path = out_dir / "bibliotheque-loras.md"
        try:
            path.write_text(md, encoding="utf-8")
        except OSError as exc:
            self._append_log("error", f"Export to .md failed: {exc}")
            return
        self._append_log("ok", f"Library exported: {path}")

    def _annuaire_export_json(self) -> None:
        """Export the full library (every field, download links included) to a
        JSON backup file the user can keep and re-import later."""
        if not self._annuaire:
            self._append_log("warn", "The library is empty — nothing to export.")
            return
        from tkinter import filedialog

        payload = annuaire_backup_payload(self._annuaire)
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export the library (JSON)",
            defaultextension=".json",
            initialfile="bibliotheque-loras.json",
            filetypes=[("JSON file", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            self._append_log("error", f"Export to JSON failed: {exc}")
            return
        self._append_log(
            "ok",
            f"Library exported ({len(self._annuaire)} entries): {path}",
        )

    def _annuaire_import_json(self) -> None:
        """Import a JSON backup into the library: normalize, merge (dedup by
        type + name), persist and rebuild the grid."""
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            parent=self.root,
            title="Import a library (JSON)",
            filetypes=[("JSON file", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._append_log("error", f"Unreadable JSON file: {exc}")
            return
        incoming = annuaire_from_backup(data)
        if not incoming:
            self._append_log("warn", "No valid entry in the file.")
            return
        self._annuaire_merge_entries(incoming, "Import done")

    def _annuaire_paste_json(self) -> None:
        """Add the entries the browser userscript put on the clipboard.

        The clipboard carries the same JSON as a backup file (one entry is
        enough), so a single « Copier » in the browser is one « 📋 Coller »
        away from the library. Merging is the same path as the file import:
        **it never removes an existing entry**, duplicates are skipped.
        """
        try:
            raw = self.root.clipboard_get()
        except tk.TclError:
            self._append_log("error", "Presse-papiers vide ou illisible.")
            return
        text = str(raw or "").strip()
        if not text:
            self._append_log("error", "Presse-papiers vide.")
            return
        try:
            data = json.loads(text)
        except ValueError:
            self._append_log(
                "error",
                "Le presse-papiers ne contient pas de JSON valide — cliquez "
                "on « Copy » in the browser script.",
            )
            return
        incoming = annuaire_from_backup(data)
        if not incoming:
            self._append_log(
                "warn",
                "No usable entry in the clipboard (JSON "
                "in library format expected).",
            )
            return
        self._annuaire_merge_entries(incoming, "Paste done")

    def _annuaire_merge_entries(self, incoming: list, label: str) -> None:
        """Merge *incoming* entries into the library, persist, re-render."""
        merged, added, skipped = annuaire_merge(self._annuaire, incoming)
        self._annuaire = merged
        self._save_comfy_settings()
        self._rebuild_annuaire_grid()
        self._append_log(
            "ok",
            f"{label}: {added} added, {skipped} already present. "
            f"Library: {len(self._annuaire)} entries.",
        )

    def _annuaire_save_form(self) -> None:
        """Read the form and create/update the entry, then rebuild the grid."""
        # The "put this on the pod at startup" switch lives on the card, not in
        # the form: editing an entry must never silently re-enable it.
        enabled = True
        index = self._annuaire_editing_index
        if index is not None and 0 <= index < len(self._annuaire):
            enabled = _annuaire_entry_enabled(self._annuaire[index])
        source = self._form_source.get()
        dl_url = self._form_dl_url.get().strip()
        local_path = self._form_local_path.get().strip()
        entry = {
            "type": self._form_type.get().strip(),
            "name": self._form_vars["name"].get().strip(),
            "page_url": self._form_vars["page_url"].get().strip(),
            # Both are kept: switching the source back and forth must not lose
            # what was typed on the other side.
            "dl_url": dl_url,
            "local_path": local_path,
            "trigger_words": self._form_vars["trigger_words"].get().strip(),
            "note": self._form_note.get("1.0", "end").strip(),
            "mode": ANNUAIRE_MODE_RAW_BY_LABEL.get(self._form_mode.get(), ""),
            "model": self._form_model.get().strip(),
            "enabled": enabled,
        }
        if source == ANNUAIRE_SOURCE_LOCAL:
            if not local_path:
                self._append_log(
                    "error",
                    "Set a local path (Browse… button) to "
                    "save the entry.",
                )
                return
            if not Path(local_path).exists():
                # Warned, not refused: the file may be produced later (a
                # training run, a download in progress).
                self._append_log(
                    "warn",
                    f"Local path not found yet: {local_path} "
                    "(the upload will fail until it exists).",
                )
        elif not dl_url:
            self._append_log(
                "error", "Set the download link to save the entry."
            )
            return
        label = entry["name"] or local_path or dl_url
        if self._annuaire_editing_index is not None:
            self._annuaire[self._annuaire_editing_index] = entry
            self._append_log("ok", f"Entry « {label} » updated.")
        else:
            self._annuaire.append(entry)
            self._append_log("ok", f"Entry « {label} » added.")
        self._save_comfy_settings()
        self._annuaire_editing_index = None
        self._annuaire_form.pack_forget()
        self._rebuild_annuaire_grid()

    def _on_form_source_change(self) -> None:
        """Enable the field the chosen source uses, disable the other one.

        Both stay visible: the user must be able to see what is stored on the
        other side before switching.
        """
        local = self._form_source.get() == ANNUAIRE_SOURCE_LOCAL
        for widget, enabled in (
            (self._form_dl_entry, not local),
            (self._form_local_entry, local),
            (self._btn_form_browse, local),
        ):
            try:
                widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                continue
        self._refresh_form_local_hint()

    def _refresh_form_local_hint(self) -> None:
        """Explain what a local source expects for the selected type."""
        label = getattr(self, "_form_local_hint", None)
        if label is None or not self._widget_exists(label):
            return
        entry_type = self._form_type.get()
        if entry_type == "node":
            text = (
                "Node: pick a FOLDER (uploaded recursively into "
                "custom_nodes/, then requirements.txt installed if present)."
            )
        elif entry_type == "workflow":
            text = "Workflow: pick a .json file."
        else:
            text = "LoRA: pick a .safetensors file."
        if self._form_source.get() != ANNUAIRE_SOURCE_LOCAL:
            text = "URL source: the pod downloads it itself at start."
        try:
            label.configure(text=text)
        except tk.TclError:
            pass

    def _annuaire_pick_local(self) -> None:
        """« Browse… » — a file for lora/workflow, a folder for a node."""
        from tkinter import filedialog

        entry_type = self._form_type.get()
        if entry_type == "node":
            chosen = filedialog.askdirectory(
                parent=self.root,
                title="Pick the node folder",
                mustexist=True,
            )
        else:
            if entry_type == "workflow":
                filetypes = [("Workflow JSON", "*.json"), ("All files", "*.*")]
            else:
                filetypes = [
                    ("LoRA safetensors", "*.safetensors"),
                    ("All files", "*.*"),
                ]
            chosen = filedialog.askopenfilename(
                parent=self.root,
                title="Pick the file",
                filetypes=filetypes,
            )
        if not chosen:
            return
        self._form_local_path.set(chosen)
        self._form_source.set(ANNUAIRE_SOURCE_LOCAL)
        self._on_form_source_change()

    def _annuaire_delete(self, index: int) -> None:
        """Remove one annuaire entry and rebuild the grid."""
        if 0 <= index < len(self._annuaire):
            self._annuaire.pop(index)
        if self._annuaire_editing_index == index:
            self._annuaire_cancel_form()
        self._rebuild_annuaire_grid()
        self._save_comfy_settings()

    def _annuaire_open_page(self, index: int) -> None:
        """Open the entry's page link in the default browser."""
        if not (0 <= index < len(self._annuaire)):
            return
        url = str(self._annuaire[index].get("page_url", "")).strip()
        if not url:
            self._append_log("warn", "No page link set for this entry.")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url
        webbrowser.open(url)

    def _annuaire_install(self, index: int) -> None:
        """Put one entry on the pod right now (all three types, both sources).

        A URL entry is downloaded by the pod-side script; a local entry is
        uploaded by the launcher over the same SSH endpoint. The two paths land
        in the same folders, so the pod cannot tell them apart afterwards.
        """
        if not (0 <= index < len(self._annuaire)):
            return
        entry = self._annuaire[index]
        entry_type = entry.get("type", "lora")
        source = _annuaire_entry_source(entry)
        payload = _annuaire_entry_payload(entry)
        if not payload:
            self._append_log(
                "error",
                "Empty source for this entry (neither a download link nor a local path).",
            )
            return
        filename = _annuaire_workflow_filename(str(entry.get("name", "")))
        label = str(entry.get("name", "")).strip() or Path(payload).name

        if source == ANNUAIRE_SOURCE_LOCAL:
            def upload():
                config = load_config(stack="comfy", store=CredentialStore())
                ops = comfy_ops.build_comfy_ops(config)
                return ops.upload_asset(
                    entry_type, payload, name=label, force=True
                )

            self._run("Send to the pod", upload)
            return

        def work():
            config = load_config(stack="comfy", store=CredentialStore())
            ops = comfy_ops.build_comfy_ops(config)
            if entry_type == "lora":
                return ops.install_lora(payload, personal=True)
            if entry_type == "node":
                return ops.install_node(payload)
            if entry_type == "workflow":
                return ops.install_workflow(payload, filename or None)
            return "Unknown entry type."

        self._run("Install on the pod", work)

    def _comfy_local_assets(self) -> list[tuple[str, str, str]]:
        """Enabled **local** entries, as ``(kind, path, name)`` triples.

        These are the assets the launcher uploads itself once ComfyUI is up —
        the pod-side installer only understands URLs, and the pod cannot read
        this machine.
        """
        return _annuaire_local_assets(self._annuaire)

    def _annuaire_toggle_enabled(self, index: int, var) -> None:
        """Flip one entry's "download at pod startup" switch and persist it."""
        if not (0 <= index < len(self._annuaire)):
            return
        enabled = bool(var.get())
        self._annuaire[index]["enabled"] = enabled
        self._save_comfy_settings()
        self._update_annuaire_counter()
        name = self._annuaire[index].get("name") or self._annuaire[index].get(
            "dl_url", ""
        )
        self._append_log(
            "info",
            f"« {name} »: automatic download "
            f"{'enabled' if enabled else 'disabled'}.",
        )

    def _annuaire_set_all(self, enabled: bool) -> None:
        """Check/uncheck every entry at once, then re-render the grid."""
        if not self._annuaire:
            self._append_log("warn", "The library is empty.")
            return
        for entry in self._annuaire:
            entry["enabled"] = enabled
        self._save_comfy_settings()
        self._rebuild_annuaire_grid()
        self._append_log(
            "ok",
            f"Library: {len(self._annuaire)} entry(ies) "
            f"{'enabled' if enabled else 'disabled'}.",
        )

    def _update_annuaire_counter(self) -> None:
        """Show how many entries will be pulled at pod startup."""
        label = getattr(self, "_annuaire_counter", None)
        if label is None or not self._widget_exists(label):
            return
        total = len(self._annuaire)
        active = sum(1 for e in self._annuaire if _annuaire_entry_enabled(e))
        try:
            label.configure(
                text=f"{active} active / {total} — downloaded when the pod starts"
            )
        except tk.TclError:
            pass

    def _annuaire_model_values(self) -> list[str]:
        """Dropdown models: the defaults plus any model already used by an entry."""
        values = list(DEFAULT_ANNUAIRE_MODELS)
        for entry in self._annuaire:
            model = str(entry.get("model", "")).strip()
            if model and model not in values:
                values.append(model)
        return values

    def _annuaire_filtered_entries(self) -> list:
        """Entries matching the model filter, as ``(index, entry)`` pairs."""
        query = self._annuaire_filter.get().strip().lower()
        result = []
        for index, entry in enumerate(self._annuaire):
            if query and query not in str(entry.get("model", "")).lower():
                continue
            result.append((index, entry))
        return result

    @staticmethod
    def _annuaire_grid_columns_for_width(width: int) -> int:
        try:
            width = int(width)
        except (TypeError, ValueError):
            return 1
        if width < 100:
            return 1
        width = min(width, 4096)
        card_min = 190
        gap = 12
        return max(1, (width + gap) // (card_min + gap))

    def _on_biblio_grid_configure(self, event) -> None:
        width = int(getattr(event, "width", 0) or 0)
        if width < 50:
            return
        cols = self._annuaire_grid_columns_for_width(width)
        # A column change re-grids the cards (cheap: no widget is recreated);
        # a mere width change only re-truncates the titles. Neither path
        # rebuilds the cards, so resizing the window no longer costs a full
        # rebuild per step. Both are coalesced, so a burst of <Configure>
        # events (mapping the tab, dragging the window edge) syncs once.
        if cols != self._annuaire_grid_columns:
            self._schedule_annuaire_rebuild(0)
        elif abs(width - self._annuaire_grid_width) >= 24:
            self._schedule_annuaire_rebuild(120)

    def _schedule_annuaire_rebuild(self, delay_ms: int) -> None:
        """Coalesce grid syncs: a burst of events triggers a single sync."""
        pending = self._annuaire_rebuild_after
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        self._annuaire_rebuild_after = self.root.after(
            delay_ms, self._rebuild_annuaire_grid
        )

    def _cancel_annuaire_rebuild(self) -> None:
        """Drop a pending grid sync (called while shutting down)."""
        pending = self._annuaire_rebuild_after
        self._annuaire_rebuild_after = None
        if pending is None:
            return
        try:
            self.root.after_cancel(pending)
        except tk.TclError:
            pass

    def _warm_annuaire_grid(self) -> None:
        """Build the cards during idle time (see the Library tab build).

        The grid frame has no real width yet, so the cards are laid out for
        the fallback budget; opening the tab only re-grids and re-truncates
        the cards it already has (a handful of Tcl calls, no widget churn).
        """
        if not self._widget_exists(self._annuaire_grid):
            return
        if self._annuaire_cards:
            return
        self._rebuild_annuaire_grid()

    def _annuaire_index_of(self, entry) -> int:
        """Current position of *entry* in the library, or -1.

        Cards hold a reference to their entry, never to a position: a card
        survives a rebuild, and an index captured at build time would point
        at a neighbour as soon as an entry above it is deleted.
        """
        for index, candidate in enumerate(self._annuaire):
            if candidate is entry:
                return index
        return -1

    def _annuaire_card_signature(self, entry: dict) -> tuple:
        """Everything a card displays — a change here rebuilds that card."""
        return (
            str(entry.get("type", "lora")),
            str(entry.get("name", "")),
            str(entry.get("model", "")),
            str(entry.get("trigger_words", "")),
            _canonical_annuaire_mode(entry.get("mode", "")),
            _annuaire_entry_source(entry),
            _annuaire_entry_enabled(entry),
        )

    def _truncate(self, text: str, max_width: int, size: int, weight: str) -> str:
        """``truncate_to_width`` with a memo.

        The measurement is a Tcl round trip (~0.2 ms here) and a rebuild
        truncates every title: without the cache a resize paid ~120 ms of
        font measuring alone.
        """
        key = (text, max_width, size, weight)
        cached = self._annuaire_trunc_cache.get(key)
        if cached is None:
            cached = truncate_to_width(
                text, max_width, self._ui_font(size, weight)
            )
            self._annuaire_trunc_cache[key] = cached
        return cached

    def _rebuild_annuaire_grid(self) -> None:
        """Sync the compact card grid with ``self._annuaire`` (filtered).

        Cards are **reused**: one whose content is unchanged keeps its widgets
        and is only re-gridded / re-truncated. A filter keystroke or a window
        resize therefore costs a handful of Tcl calls instead of destroying
        and recreating hundreds of widgets (measured ~400 ms for 36 entries).
        """
        # A direct call (the library changed) supersedes any queued sync.
        self._cancel_annuaire_rebuild()
        frame = self._annuaire_grid
        if not self._widget_exists(frame):
            return
        pal = self._pal
        width = frame.winfo_width()
        if width < 100:
            width = 900
        cols = self._annuaire_grid_columns_for_width(width)
        self._annuaire_grid_columns = cols
        self._annuaire_grid_width = width
        # Pixel budget of one card (padding included) — the card titles are
        # truncated to it with a real ellipsis rather than a character count.
        card_width = max(120, width // max(1, cols) - 14)
        self._annuaire_card_width = card_width
        for col in range(cols):
            frame.grid_columnconfigure(
                col, weight=1, uniform="annuaire_card", minsize=170
            )
        entries = self._annuaire_filtered_entries()
        self._update_annuaire_counter()

        cache = self._annuaire_cards
        keep: dict = {}
        for position, (_index, entry) in enumerate(entries):
            key = id(entry)
            cached = cache.get(key)
            signature = self._annuaire_card_signature(entry)
            if cached is not None and cached[0] is entry and cached[1] == signature:
                card = cached[2]
                name_label, tw_label = cached[3], cached[4]
                if cached[5] != card_width:
                    self._refresh_annuaire_card_width(
                        entry, name_label, tw_label, card_width
                    )
            else:
                if cached is not None:
                    cached[2].destroy()
                card, name_label, tw_label = self._build_annuaire_card(
                    frame, entry, card_width
                )
            card.grid(
                row=position // cols, column=position % cols,
                sticky="nsew", padx=4, pady=4,
            )
            keep[key] = (entry, signature, card, name_label, tw_label, card_width)
        for key, cached in cache.items():
            if key not in keep:
                cached[2].destroy()
        self._annuaire_cards = keep

        # The "nothing to show" hint lives in the grid frame, next to the
        # cards: it is not a card and is rebuilt on each sync.
        placeholder = getattr(self, "_annuaire_placeholder", None)
        if placeholder is not None and self._widget_exists(placeholder):
            placeholder.destroy()
        self._annuaire_placeholder = None
        if not entries:
            if not self._annuaire:
                msg = "No entry — press ➕ New to start."
            else:
                msg = "No entry matches the filter."
            placeholder = self._label(
                frame, text=msg, bg=pal["bg"], fg=pal["muted"],
                font=("Segoe UI", 9), anchor="w",
            )
            placeholder.grid(row=0, column=0, sticky="w", padx=4, pady=6)
            self._themed(placeholder, "muted")
            self._annuaire_placeholder = placeholder

        self._tooltip_texts = {
            w: t for w, t in self._tooltip_texts.items() if self._widget_exists(w)
        }
        self._prune_dead_refs()

    def _refresh_annuaire_card_width(
        self, entry: dict, name_label, tw_label, card_width: int
    ) -> None:
        """Re-truncate a reused card's titles for a new pixel budget."""
        name = str(entry.get("name", "")).strip() or "(sans nom)"
        if name_label is not None and self._widget_exists(name_label):
            name_label.configure(
                text=self._truncate(name, card_width - 14, 10, "bold")
            )
        tw = str(entry.get("trigger_words", "")).strip()
        if tw and tw_label is not None and self._widget_exists(tw_label):
            tw_label.configure(text=self._truncate(tw, card_width - 14, 8, "normal"))

    def _build_annuaire_card(self, parent, entry: dict, card_width: int):
        """Build one compact card (Steam-library style) for *entry*.

        Returns ``(card, name label, trigger label)``: the labels are the
        truncated ones, so a reused card can be re-truncated in place when the
        pixel budget changes. The actions resolve their entry's position at
        click time (see :meth:`_annuaire_index_of`), never at build time.
        """
        pal = self._pal
        entry_type = entry.get("type", "lora")
        type_label = ANNUAIRE_TYPE_LABELS.get(entry_type, entry_type)
        # The banner colour encodes the compatibility mode (Turbo / T2VA /
        # I2VA / FL2VA / L2VA / REF2VA / FL2VA+REF2VA / unspecified) while the
        # type keeps its own text label, so both distinctions stay readable at
        # a glance. One distinct colour per mode: the library can grow without
        # the badges blurring together.
        mode_key = _canonical_annuaire_mode(entry.get("mode", ""))
        header_bg = ANNUAIRE_MODE_COLORS.get(mode_key, ANNUAIRE_MODE_COLORS[""])
        header_fg = _contrast_fg(header_bg)

        card = tk.Frame(
            parent, bg=pal["surface"], highlightthickness=1,
            highlightbackground=pal["panel_border"], bd=0,
        )
        self._themed(card, "panelbg")

        # The banner is a solid colour strip: raw tk labels, so the fill is
        # the mode colour rather than the themed label surface.
        header = tk.Frame(card, bg=header_bg)
        header.pack(fill="x")
        _TKMOD.Label(
            header, text=type_label, bg=header_bg, fg=header_fg,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=6, pady=2)
        mode_label = ANNUAIRE_MODE_LABELS.get(mode_key, "")
        if mode_label and mode_label != "—":
            _TKMOD.Label(
                header, text=mode_label, bg=header_bg, fg=header_fg,
                font=("Segoe UI", 8),
            ).pack(side="right", padx=6, pady=2)

        # Source badge: a URL entry is downloaded by the pod itself, a local
        # one is uploaded by the launcher. Worth seeing at a glance — the two
        # behave differently when the pod starts.
        source = _annuaire_entry_source(entry)
        source_label = "URL" if source == ANNUAIRE_SOURCE_URL else "Local"
        source_chip = _TKMOD.Label(
            header, text=source_label, bg=header_bg, fg=header_fg,
            font=("Segoe UI", 8, "bold"),
        )
        source_chip.pack(side="right", padx=(0, 4), pady=2)
        if source == ANNUAIRE_SOURCE_LOCAL:
            self._tooltip(
                source_chip,
                "Local file: uploaded by the launcher to the pod (tunnel "
                "SSH) at start, or with the ⬇ button.",
            )
        else:
            self._tooltip(
                source_chip,
                "URL: downloaded directly by the pod at start.",
            )

        name = entry.get("name", "").strip() or "(sans nom)"
        name_label = self._label(
            card,
            text=self._truncate(name, card_width - 14, 10, "bold"),
            bg=pal["surface"], fg=pal["fg"],
            font=("Segoe UI", 10, "bold"), anchor="w", panel=True,
        )
        name_label.pack(fill="x", padx=6, pady=(6, 0))
        self._themed(name_label, "panelbg")
        self._tooltip(name_label, name)

        model = entry.get("model", "").strip()
        if model:
            model_label = self._label(
                card, text=model, bg=pal["surface"], fg=pal["muted"],
                font=("Segoe UI", 8), anchor="w", panel=True,
            )
            model_label.pack(fill="x", padx=6, pady=(2, 0))
            self._themed(model_label, "muted")

        tw = entry.get("trigger_words", "").strip()
        tw_label = None
        if tw:
            tw_label = self._label(
                card,
                text=self._truncate(tw, card_width - 14, 8, "normal"),
                bg=pal["surface"], fg=pal["muted"],
                font=("Segoe UI", 8), anchor="w", panel=True,
            )
            tw_label.pack(fill="x", padx=6, pady=(2, 0))
            self._themed(tw_label, "muted")
            self._tooltip(tw_label, tw)

        actions = tk.Frame(card, bg=pal["surface"])
        actions.pack(fill="x", padx=4, pady=6)
        self._themed(actions, "panelbg")

        # "Put this on the pod when it starts": checked = sent at pod start
        # (URL entries through the pod env, local ones uploaded by the
        # launcher); unchecked = the entry stays in the library and can still
        # be sent on demand with the ⬇ button.
        enabled_var = tk.BooleanVar(card, value=_annuaire_entry_enabled(entry))
        chk = self._check(
            actions,
            text="",
            variable=enabled_var,
            panel=True,
            command=lambda e=entry, v=enabled_var: self._annuaire_toggle_enabled(
                self._annuaire_index_of(e), v
            ),
        )
        chk.pack(side="left", padx=(0, 4))
        self._tooltip(
            chk,
            "Send automatically when the pod starts"
            + (
                " (envoi par le launcher, tunnel SSH)"
                if source == ANNUAIRE_SOURCE_LOCAL
                else " (downloaded by the pod)"
            ),
        )

        btn_open = self._make_button(
            actions, "🔗",
            lambda e=entry: self._annuaire_open_page(self._annuaire_index_of(e)),
        )
        btn_open.pack(side="left", padx=(0, 2))
        self._tooltip(btn_open, "Open the web page")

        btn_install = self._make_button(
            actions, "⬇",
            lambda e=entry: self._annuaire_install(self._annuaire_index_of(e)),
            variant="primary",
        )
        btn_install.pack(side="left", padx=(0, 2))
        self._tooltip(
            btn_install,
            "Send to the pod now"
            + (" (local file)" if source == ANNUAIRE_SOURCE_LOCAL else ""),
        )

        btn_edit = self._make_button(
            actions, "✏️",
            lambda e=entry: self._annuaire_edit(self._annuaire_index_of(e)),
            variant="quiet",
        )
        btn_edit.pack(side="left", padx=(0, 2))
        self._tooltip(btn_edit, "Edit")

        btn_del = self._make_button(
            actions, "✕",
            lambda e=entry: self._annuaire_delete(self._annuaire_index_of(e)),
            variant="danger",
        )
        btn_del.pack(side="left")
        self._tooltip(btn_del, "Delete")

        return card, name_label, tw_label

    # -- Models & presets (ComfyUI, fully modular) --------------------------

    def _on_comfy_rows_configure(self, event) -> None:
        """Re-fit the model checklist when the panel width changes.

        The threshold keeps the rebuild from feeding itself (the rebuilt
        rows change the frame's height, never its width)."""
        width = int(getattr(event, "width", 0) or 0)
        if width < 100:
            return
        if abs(width - self._comfy_rows_width) >= 24:
            self._comfy_rows_width = width
            self._rebuild_comfy_model_rows()
            self._rebuild_comfy_extra_rows()

    def _active_preset_model_keys(self) -> set:
        """``(url, target)`` of every model the ACTIVE preset carries.

        Used to badge the catalog cards: a model in this set is what the
        preset actually ships to the pod.
        """
        preset = self._comfy_preset.get()
        entry = self._comfy_presets.get(preset) or {}
        return {
            (model.get("url"), model.get("target"))
            for model in entry.get("models", [])
        }

    def _lock_badge(self, parent, *, bg: str):
        """The amber « locked by the active preset » badge, or None.

        None (no PIL / asset missing) simply means the card shows no badge:
        the information stays available in the row tooltip.
        """
        icon = self._photo_icon(LOCK_BADGE_ICON, 18)
        if icon is None:
            return None
        badge = _TKMOD.Label(parent, image=icon, bg=bg, bd=0)
        badge.image = icon  # type: ignore[attr-defined]
        self._themed(badge, "panelbg")
        self._tooltip(badge, LOCK_BADGE_TOOLTIP)
        return badge

    def _section_card(
        self, parent, section_key: str, title: str, pal: Mapping[str, str],
        *, icon_name: str = "",
    ):
        """A card with a colored banner (icon + title): ``(card, banner, body)``.

        Every section of the "Models & presets" tab uses this, so the model
        categories and the custom-nodes / workflows sections read alike.
        """
        accent = CATEGORY_COLORS.get(section_key, CATEGORY_DEFAULT_COLOR)
        accent_fg = _contrast_fg(accent)
        card = tk.Frame(
            parent, bg=pal["surface"], highlightthickness=1,
            highlightbackground=pal["panel_border"], bd=0,
        )
        self._themed(card, "card")

        banner = tk.Frame(card, bg=accent)
        banner.pack(fill="x")
        icon = (
            self._photo_icon(icon_name, 20, by_height=True) if icon_name else None
        )
        if icon is not None:
            # The glyph sits on a neutral chip: painted straight on the
            # accent banner it would be the same colour as its background.
            # Height-driven scaling keeps a wide glyph (the 4:1 workflow
            # icon) readable instead of squashing it into a square.
            chip = tk.Frame(banner, bg=pal["bg"], bd=0)
            chip.pack(side="left", padx=(PAD_S, PAD_S), pady=PAD_XS)
            self._themed(chip, "bg")
            icon_label = _TKMOD.Label(chip, image=icon, bg=pal["bg"], bd=0)
            icon_label.image = icon  # type: ignore[attr-defined]
            icon_label.pack(padx=PAD_XS, pady=PAD_XS)
            self._themed(icon_label, "bg")
        _TKMOD.Label(
            banner, text=title, bg=accent, fg=accent_fg,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", pady=PAD_XS)

        body = tk.Frame(card, bg=pal["surface"])
        body.pack(fill="x", padx=PAD_M, pady=(PAD_XS, PAD_M))
        self._themed(body, "panelbg")
        return card, banner, body

    def _model_row(
        self, parent, *, name: str, url: str, var, on_toggle, on_remove,
        remove_tooltip: str, pal: Mapping[str, str], model_budget: int,
        tooltip: str = "", extra: bool = False, in_preset: bool = False,
        info: str = "",
    ):
        """One entry line: checkbox + name, optional badges, info, ✕.

        Shared by the catalog entries, the preset-only ("extra") models and
        the custom-nodes / workflows sections so they all keep exactly the
        same layout: one entry per line, checkbox on the left, info (size,
        note, repo…) right-aligned, ✕ at the far right. ``info`` overrides
        the download size when given.
        """
        line = tk.Frame(parent, bg=pal["surface"])
        line.pack(fill="x", pady=(0, PAD_XS))
        self._themed(line, "panelbg")

        cb = self._check(
            line,
            text=truncate_to_width(name, model_budget, self._ui_font(9)),
            variable=var, bg=pal["surface"], fg=pal["fg"],
            activebackground=pal["surface"], activeforeground=pal["fg"],
            selectcolor=pal["bg_alt"], anchor="w",
            command=on_toggle,
            panel=True,
        )
        cb.pack(side="left")
        self._themed(cb, "check")

        if extra:
            # Discreet pill: the model is not in the catalog, only in the
            # preset (imported or added by hand).
            pill = self._label(
                line, text="extra", bg=pal["bg_alt"], fg=pal["muted"],
                font=("Segoe UI", 7), padx=PAD_XS, panel=False,
            )
            pill.pack(side="left", padx=(PAD_M, 0))
            self._themed(pill, "chip")

        # Right-packed order is reversed by Tk (the first one packed is the
        # right-most), so the ✕ goes first and the row reads
        # « name   🔒   4.2 Go   ✕ ».
        rm = self._make_button(line, "✕", on_remove, variant="quiet")
        rm.pack(side="right")
        self._tooltip(rm, remove_tooltip)

        size_text = info or format_model_size(self._model_sizes.get(url))
        if size_text:
            size_label = self._label(
                line, text=size_text, bg=pal["surface"], fg=pal["muted"],
                font=("Segoe UI", 8), anchor="e", panel=True,
            )
            size_label.pack(side="right", padx=(PAD_M, 0))
            self._themed(size_label, "muted")

        if in_preset:
            badge = self._lock_badge(line, bg=pal["surface"])
            if badge is not None:
                badge.pack(side="right", padx=(PAD_M, 0))

        # The technical note stays the tooltip verbatim when the name is
        # fully shown; a truncated name is prepended, and the preset
        # membership appended (the badge stands for it when there is one).
        parts = []
        if str(cb.cget("text")) != name:
            parts.append(name)
        if tooltip:
            parts.append(tooltip)
        if in_preset:
            parts.append("Included in the active preset.")
        if extra:
            parts.append(
                "Preset model missing from the catalogue (the \"extra\" row)."
            )
        if parts:
            self._tooltip(cb, "\n\n".join(parts))
        return line

    def _build_model_category_card(
        self, parent, category: str, entries: list, active_keys: set,
        model_budget: int, pal: Mapping[str, str], extras: Optional[list] = None,
    ):
        """One card per model category: colored banner + one line per model.

        The banner (built by ``_section_card``) carries the category icon and
        its accent color; each line shows the checkbox, the download size
        (when probed) and the "in the active preset" lock badge. The models
        the preset carries without a catalog counterpart are appended to the
        SAME list (with an "extra" pill) instead of living in a separate
        section: they belong to the category their target path points at.
        """
        label = COMFY_MODEL_CATEGORY_LABELS[category]
        card, banner, body = self._section_card(
            parent, category, label, pal, icon_name=CATEGORY_ICONS.get(category, "")
        )

        add_btn = self._make_button(
            banner, "＋", lambda c=category: self._comfy_add_model(c),
            variant="quiet",
        )
        add_btn.pack(side="right", padx=(PAD_XS, PAD_S), pady=PAD_XS)
        self._tooltip(add_btn, f"Add a model to « {label} » (direct URL).")

        tooltip_key = {
            "tae": "tae",
            "video_vae": "video_vae",
            "text_encoder": "text_encoder",
        }.get(category)

        for index, entry in enumerate(entries):
            var = tk.BooleanVar(value=bool(entry.get("enabled")))
            self._comfy_model_vars.append(var)
            url = entry.get("url", "")
            model_name = entry.get("name", url)
            key = (url, model_target_for_url(category, url))
            self._model_row(
                body,
                name=model_name,
                url=url,
                var=var,
                on_toggle=(
                    lambda c=category, i=index, v=var: self._comfy_toggle_model(c, i, v)
                ),
                on_remove=(
                    lambda c=category, i=index: self._comfy_remove_model(c, i)
                ),
                remove_tooltip=(
                    f"Remove « {model_name} » from the {label} category."
                ),
                pal=pal,
                model_budget=model_budget,
                tooltip=TECHNICAL_TOOLTIPS[tooltip_key] if tooltip_key else "",
                in_preset=key in active_keys,
            )

        for index, model in extras or ():
            url = model.get("url", "")
            target = model.get("target") or ""
            name = model.get("name") or _display_name_for_url(target or url)
            var = tk.BooleanVar(value=bool(model.get("enabled", True)))
            self._model_row(
                body,
                name=name,
                url=url,
                var=var,
                on_toggle=(
                    lambda i=index, v=var: self._comfy_toggle_model_entry(i, v)
                ),
                on_remove=(
                    lambda i=index: self._comfy_remove_model_entry(i)
                ),
                remove_tooltip=f"Remove « {name} » from the preset.",
                pal=pal,
                model_budget=model_budget,
                tooltip=f"{url}\n\n→ models/{target}".strip(),
                extra=True,
                in_preset=bool(model.get("enabled", True)),
            )

        return card

    def _rebuild_comfy_model_rows(self) -> None:
        """Rebuild the per-category model cards from ``self._comfy_models``."""
        frame = self._comfy_model_rows
        for child in frame.winfo_children():
            child.destroy()
        self._tooltip_texts = {
            w: t for w, t in self._tooltip_texts.items() if self._widget_exists(w)
        }
        # The destroyed rows must not keep a slot in the recolor/panel sets
        # either: this runs on every model checkbox click.
        self._prune_dead_refs()
        pal = self._pal
        self._comfy_model_vars = []
        active_keys = self._active_preset_model_keys()
        # Pixel budget for a model name: the card body width (falling back to
        # the panel while unmapped) minus the trailing size/badge/✕.
        row_width = frame.winfo_width()
        if row_width < 100:
            row_width = self._comfy_models_frame.winfo_width()
        if row_width < 100:
            # Still unmapped (built during _build_widgets): the <Configure>
            # binding re-derives the budget once the panel has a real width.
            row_width = 600
        else:
            self._comfy_rows_width = row_width
        model_budget = max(120, row_width - 160)
        extras = self._extra_models_by_category()
        for category in COMFY_MODEL_CATEGORIES:
            entries = self._comfy_models.get(category, [])
            card = self._build_model_category_card(
                frame, category, entries, active_keys, model_budget, pal,
                extras=extras.get(category, []),
            )
            card.pack(fill="x", padx=PAD_XS, pady=(PAD_S, PAD_XS))
        # Fresh widgets must inherit the current mode's enabled state
        # (rebuild can happen while the ComfyUI mode is inactive).
        if self._mode.get() != "comfy":
            for child in frame.winfo_children():
                self._set_widget_enabled(
                    child, False, TECHNICAL_TOOLTIPS["comfy_mode_only"]
                )
        # ... and the tier gate, when the ComfyUI mode is active.
        self._update_perso_gating()
        self._refresh_comfy_preset_combo()
        self._refresh_comfy_summary()

    def _comfy_toggle_model(self, category: str, index: int, var: tk.BooleanVar) -> None:
        entries = self._comfy_models.setdefault(category, [])
        if 0 <= index < len(entries):
            entries[index]["enabled"] = bool(var.get())
            # Keep the preset's explicit model list in sync: toggling a
            # catalog checkbox adds/removes the matching (url, target) entry,
            # so « Sauver » writes exactly what the cards show checked.
            url = entries[index].get("url", "")
            target = model_target_for_url(category, url)

            def _matches(m: dict) -> bool:
                return m.get("url") == url and m.get("target") == target

            if var.get():
                if url and not any(_matches(m) for m in self._comfy_edit_models):
                    self._comfy_edit_models.append(
                        {"url": url, "target": target, "enabled": True}
                    )
            else:
                self._comfy_edit_models = [
                    m for m in self._comfy_edit_models if not _matches(m)
                ]
            self._rebuild_comfy_extra_rows()
        self._refresh_comfy_summary()

    def _comfy_remove_model(self, category: str, index: int) -> None:
        entries = self._comfy_models.setdefault(category, [])
        if not (0 <= index < len(entries)):
            return
        name = entries[index].get("name", "")
        try:
            from tkinter import messagebox

            if not messagebox.askyesno(
                "Remove a model",
                f"Remove « {name} » from the list?",
                parent=self.root,
            ):
                return
        except tk.TclError:
            pass
        url = entries[index].get("url", "")
        target = model_target_for_url(category, url)
        self._comfy_edit_models = [
            m for m in self._comfy_edit_models
            if not (m.get("url") == url and m.get("target") == target)
        ]
        del entries[index]
        self._rebuild_comfy_model_rows()
        self._rebuild_comfy_extra_rows()

    def _comfy_add_model(self, category: str) -> None:
        """Add a model to a category (name + full HF resolve URL)."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Add a model")
        dialog.configure(bg=self._pal["bg"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        body = tk.Frame(dialog, bg=self._pal["bg"], padx=16, pady=14)
        body.pack(fill="both", expand=True)
        self._label(
            body, text=f"Add to « {COMFY_MODEL_CATEGORY_LABELS[category]} »",
            bg=self._pal["bg"], fg=self._pal["fg"],
            font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self._label(body, text="Name:", bg=self._pal["bg"], fg=self._pal["fg"]).grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=2
        )
        name_var = tk.StringVar(dialog)
        self._entry(
            body, textvariable=name_var, width=34,
            bg=self._pal["input_bg"], fg=self._pal["input_fg"],
            insertbackground=self._pal["fg"], relief="flat",
        ).grid(row=1, column=1, sticky="w", pady=2)
        self._label(body, text="URL:", bg=self._pal["bg"], fg=self._pal["fg"]).grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=2
        )
        url_var = tk.StringVar(dialog)
        self._entry(
            body, textvariable=url_var, width=52,
            bg=self._pal["input_bg"], fg=self._pal["input_fg"],
            insertbackground=self._pal["fg"], relief="flat",
        ).grid(row=2, column=1, sticky="w", pady=2)
        self._label(
            body,
            text="Full Hugging Face \"resolve\" URL "
            "(https://huggingface.co/<repo>/resolve/<rev>/<file>).",
            bg=self._pal["bg"], fg=self._pal["muted"], font=("Segoe UI", 8),
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 10))

        actions = tk.Frame(body, bg=self._pal["bg"])
        actions.grid(row=4, column=0, columnspan=2, sticky="e")

        def add() -> None:
            name = name_var.get().strip()
            url = url_var.get().strip()
            if not name or not url:
                self._append_log("error", "A name and a URL are required.")
                dialog.destroy()
                return
            self._comfy_models.setdefault(category, []).append(
                {"name": name, "url": url, "enabled": True}
            )
            self._comfy_edit_models.append(
                {"url": url, "target": model_target_for_url(category, url), "enabled": True}
            )
            self._rebuild_comfy_model_rows()
            self._rebuild_comfy_extra_rows()
            self._start_model_size_check()
            self._append_log("info", f"Model « {name} » added.")
            dialog.destroy()

        self._make_button(actions, "Cancel", dialog.destroy, variant="quiet").pack(
            side="right"
        )
        self._make_button(actions, "Add", add, variant="primary").pack(
            side="right", padx=(0, 6)
        )
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.bind("<Return>", lambda _e: add())

    def _refresh_comfy_preset_combo(self) -> None:
        # Preserve the current selection across rebuilds: _rebuild_comfy_model_rows
        # (and therefore _comfy_load_preset) re-runs this on every model-row
        # refresh — clearing the selection here made "Delete"/"Duplicate"
        # read an empty name right after a load, silently blocking both.
        current = self._comfy_preset_combo.get().strip()
        values = sorted(self._comfy_presets.keys())
        self._comfy_preset_combo.configure(values=values)
        if current in values:
            self._comfy_preset_combo.set(current)
        elif self._comfy_preset.get().strip() in values:
            # The editor shows the ACTIVE preset at startup (see
            # _build_widgets): the selector must name it, not stay blank.
            self._comfy_preset_combo.set(self._comfy_preset.get().strip())
        else:
            self._comfy_preset_combo.set("")

    def _refresh_comfy_options_preset_combo(self) -> None:
        """Refresh the *options* preset selector with the user presets.

        The active-preset dropdown next to the tier lists the presets created
        in the "Models & presets" tab (plus "None"). A stale selection
        (e.g. an old hardcoded name) falls back to "None".
        """
        values = [COMFY_PRESET_NONE_LABEL] + sorted(self._comfy_presets.keys())
        current = self._comfy_preset_ui.get()
        self._preset_combo.configure(values=values)
        if current not in values:
            self._comfy_preset_ui.set(COMFY_PRESET_NONE_LABEL)
            self._comfy_preset.set("")

    def _comfy_save_preset(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Save the preset")
        dialog.configure(bg=self._pal["bg"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        body = tk.Frame(dialog, bg=self._pal["bg"], padx=16, pady=14)
        body.pack(fill="both", expand=True)
        self._label(
            body, text="Preset name:", bg=self._pal["bg"], fg=self._pal["fg"],
        ).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        # Pre-fill with the selected preset (if any) so "Save" modifies the
        # loaded preset instead of forcing the user to retype its exact name;
        # a different name creates a new preset.
        current = self._comfy_preset_combo.get().strip()
        name_var = tk.StringVar(
            dialog, value=current if current in self._comfy_presets else ""
        )
        self._entry(
            body, textvariable=name_var, width=28,
            bg=self._pal["input_bg"], fg=self._pal["input_fg"],
            insertbackground=self._pal["fg"], relief="flat",
        ).grid(row=0, column=1, sticky="w", pady=2)
        actions = tk.Frame(body, bg=self._pal["bg"])
        actions.grid(row=1, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def save() -> None:
            name = name_var.get().strip()
            if not name:
                self._append_log("error", "Renseignez un nom de preset.")
                dialog.destroy()
                return
            self._comfy_presets[name] = {
                "models": [
                    {"url": m["url"], "target": m.get("target", ""),
                     "name": m.get("name", "")}
                    for m in self._comfy_edit_models
                    if m.get("enabled", True) and m.get("url")
                ],
                "nodes": [dict(n) for n in self._comfy_edit_nodes],
                "workflows": [dict(w) for w in self._comfy_edit_workflows],
            }
            self._refresh_comfy_preset_combo()
            self._refresh_comfy_options_preset_combo()
            self._append_log("ok", f"Preset « {name} » saved.")
            self._notify(f"Preset « {name} » saved.", "ok")
            self._save_comfy_settings()
            dialog.destroy()

        self._make_button(actions, "Cancel", dialog.destroy, variant="quiet").pack(
            side="right"
        )
        self._make_button(actions, "Save", save, variant="primary").pack(
            side="right", padx=(0, 6)
        )
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.bind("<Return>", lambda _e: save())

    def _comfy_load_preset(self) -> None:
        """Load the selected preset into the editor and refresh the views."""
        name = self._comfy_preset_combo.get().strip()
        if not self._apply_preset_to_editor(name):
            return
        self._rebuild_comfy_model_rows()
        self._rebuild_comfy_extra_rows()
        # A loaded preset can carry model URLs that were never probed.
        self._start_model_size_check()
        self._append_log("ok", f"Preset « {name} » loaded.")

    def _apply_preset_to_editor(self, name: str) -> bool:
        """Copy the named preset's models/nodes/workflows into the editor.

        Returns False when there is no such preset. The catalog checkboxes are
        re-derived too, so the cards show exactly what the preset ships.
        """
        selection = self._comfy_presets.get(name)
        if selection is None:
            return False
        # Match catalog entries by (url, target), not url alone: the same
        # file can legitimately appear under two categories (e.g. the NVFP4
        # AWQ text encoder also added under Video VAE). Matching by URL alone
        # re-enabled EVERY entry sharing that URL, so unchecking one of them
        # had no effect — it "came back" checked on reload. The target (the
        # path under models/) encodes the category, so it disambiguates.
        model_targets: dict = {}
        for m in selection.get("models", []):
            url = m.get("url")
            if not url:
                continue
            model_targets.setdefault(url, set()).add(
                str(m.get("target") or "").strip()
            )
        for category in COMFY_MODEL_CATEGORIES:
            for entry in self._comfy_models.get(category, []):
                url = entry.get("url", "")
                targets = model_targets.get(url)
                if targets is None:
                    entry["enabled"] = False
                    continue
                target = model_target_for_url(category, url)
                # "" in targets covers older presets saved without a target.
                entry["enabled"] = target in targets or "" in targets
        self._comfy_edit_models = [
            {"url": m.get("url", ""), "target": m.get("target", ""),
             "name": m.get("name", ""), "enabled": True}
            for m in selection.get("models", [])
            if m.get("url")
        ]
        self._comfy_edit_nodes = [dict(n) for n in selection.get("nodes", [])]
        self._comfy_edit_workflows = [dict(w) for w in selection.get("workflows", [])]
        # A preset entry that merely restates a catalog model must not show up
        # as a second, unticked line: tick the catalog checkbox it corresponds
        # to (the preset list itself is preserved).
        self._tick_catalog_for_preset_models()
        return True

    def _comfy_duplicate_preset(self) -> None:
        name = self._comfy_preset_combo.get().strip()
        if not name or name not in self._comfy_presets:
            self._append_log("error", "Select a preset to duplicate.")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Duplicate the preset")
        dialog.configure(bg=self._pal["bg"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        body = tk.Frame(dialog, bg=self._pal["bg"], padx=16, pady=14)
        body.pack(fill="both", expand=True)
        self._label(
            body, text="New preset name:", bg=self._pal["bg"], fg=self._pal["fg"],
        ).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        name_var = tk.StringVar(dialog, value=f"{name} (copie)")
        self._entry(
            body, textvariable=name_var, width=28,
            bg=self._pal["input_bg"], fg=self._pal["input_fg"],
            insertbackground=self._pal["fg"], relief="flat",
        ).grid(row=0, column=1, sticky="w", pady=2)
        actions = tk.Frame(body, bg=self._pal["bg"])
        actions.grid(row=1, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def duplicate() -> None:
            new_name = name_var.get().strip()
            if not new_name:
                self._append_log("error", "Renseignez un nom.")
                dialog.destroy()
                return
            source = self._comfy_presets[name]
            self._comfy_presets[new_name] = {
                "models": [dict(m) for m in source.get("models", [])],
                "nodes": [dict(n) for n in source.get("nodes", [])],
                "workflows": [dict(w) for w in source.get("workflows", [])],
            }
            self._refresh_comfy_preset_combo()
            self._refresh_comfy_options_preset_combo()
            self._save_comfy_settings()
            self._append_log("ok", f"Preset « {new_name} » created (copy of « {name} »).")
            dialog.destroy()

        self._make_button(actions, "Cancel", dialog.destroy, variant="quiet").pack(
            side="right"
        )
        self._make_button(actions, "Duplicate", duplicate, variant="primary").pack(
            side="right", padx=(0, 6)
        )
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.bind("<Return>", lambda _e: duplicate())

    def _rebuild_comfy_extra_rows(self) -> None:
        """Rebuild the node + workflow sections of the preset editor.

        Both use the same card + colored banner treatment as the model
        categories, and the same ONE-ENTRY-PER-LINE layout (checkbox + name
        on the left, info right-aligned, ✕ at the far right) so the whole tab
        reads top to bottom the same way. The preset models that have no
        catalog counterpart are NOT here: they are extra lines in their own
        category card (see ``_extra_models_by_category``).
        """
        frame = self._comfy_extra_rows
        for child in frame.winfo_children():
            child.destroy()
        self._prune_dead_refs()
        pal = self._pal

        # Pixel budget for an entry name: the card body width (falling back to
        # the panel while unmapped), minus the trailing info/✕.
        row_width = frame.winfo_width()
        if row_width < 100:
            row_width = self._comfy_models_frame.winfo_width()
        if row_width < 100:
            row_width = 900
        entry_budget = max(140, row_width - 220)

        card, banner, node_body = self._section_card(
            frame, "custom_nodes", "Custom nodes", pal,
            icon_name=SECTION_ICONS["custom_nodes"],
        )
        card.pack(fill="x", padx=PAD_XS, pady=(PAD_S, PAD_XS))
        self._comfy_node_vars = []
        for index, node in enumerate(self._comfy_edit_nodes):
            var = tk.BooleanVar(value=bool(node.get("enabled", True)))
            self._comfy_node_vars.append(var)
            url = node.get("url") or ""
            name = node.get("name") or _display_name_for_url(url)
            tooltip = url
            if node.get("note"):
                tooltip = f"{tooltip}\n\n{node['note']}".strip()
            self._model_row(
                node_body,
                name=name,
                url="",
                var=var,
                on_toggle=lambda i=index, v=var: self._comfy_toggle_node(i, v),
                on_remove=lambda i=index: self._comfy_remove_node(i),
                remove_tooltip=f"Remove the node « {name} » from the preset.",
                pal=pal,
                model_budget=entry_budget,
                tooltip=tooltip,
                info=str(node.get("note") or _display_name_for_url(url)),
            )
        add_node = self._make_button(
            banner, "＋", self._comfy_add_node, variant="quiet",
        )
        add_node.pack(side="right", padx=(PAD_XS, PAD_S), pady=PAD_XS)
        self._tooltip(add_node, "Add a custom node repository (GitHub).")

        card, banner, wf_body = self._section_card(
            frame, "workflows", "Workflows", pal,
            icon_name=SECTION_ICONS["workflows"],
        )
        card.pack(fill="x", padx=PAD_XS, pady=(PAD_S, PAD_XS))
        self._comfy_workflow_vars = []
        for index, workflow in enumerate(self._comfy_edit_workflows):
            var = tk.BooleanVar(value=bool(workflow.get("enabled", True)))
            self._comfy_workflow_vars.append(var)
            if workflow.get("name"):
                name = workflow["name"]
            elif workflow.get("github"):
                name = workflow["github"].split("|")[0]
            else:
                name = _display_name_for_url(workflow.get("url") or "")
            tooltip = workflow.get("url") or (
                f"repo: {workflow['github']}" if workflow.get("github") else ""
            )
            if workflow.get("note"):
                tooltip = f"{tooltip}\n\n{workflow['note']}".strip()
            self._model_row(
                wf_body,
                name=name,
                url="",
                var=var,
                on_toggle=lambda i=index, v=var: self._comfy_toggle_workflow(i, v),
                on_remove=lambda i=index: self._comfy_remove_workflow(i),
                remove_tooltip=f"Remove the workflow « {name} » from the preset.",
                pal=pal,
                model_budget=entry_budget,
                tooltip=tooltip,
                info=str(
                    workflow.get("note")
                    or (workflow.get("github") or "").split("|")[0]
                    or _display_name_for_url(workflow.get("url") or "")
                ),
            )
        add_wf = self._make_button(
            banner, "＋", self._comfy_add_workflow, variant="quiet",
        )
        add_wf.pack(side="right", padx=(PAD_XS, PAD_S), pady=PAD_XS)
        self._tooltip(add_wf, "Ajouter un workflow (GitHub ou URL directe).")

        if self._mode.get() != "comfy":
            for child in frame.winfo_children():
                self._set_widget_enabled(
                    child, False, TECHNICAL_TOOLTIPS["comfy_mode_only"]
                )
        else:
            self._update_perso_gating()

    def _extra_models_by_category(self) -> dict:
        """Preset models with no catalog counterpart, grouped by category.

        Each one is filed under the category its ``target`` path points at
        (``diffusion_models/…`` -> Diffusion, ``latent_upscale_models/…`` ->
        Upscaler, …) so it shows up as an extra line in that category's card
        instead of a separate "hors catalogue" list.
        """
        grouped: dict = {}
        for index, model in enumerate(self._comfy_edit_models):
            if self._match_catalog_entry(model) is not None:
                continue
            category = _category_for_target(
                model.get("target") or "", model.get("name") or ""
            )
            grouped.setdefault(category, []).append((index, model))
        return grouped

    def _match_catalog_entry(self, model: dict) -> Optional[tuple]:
        """The catalog ``(category, index)`` a preset model corresponds to.

        Exact ``(url, target)`` first; then by file name / display name, so a
        preset saved with a different (mirror) URL or written by hand does not
        show up as a duplicate "hors catalogue" entry next to the catalog
        checkbox that already offers the same file.
        """
        url = model.get("url", "")
        target = model.get("target") or ""
        for category in COMFY_MODEL_CATEGORIES:
            for index, entry in enumerate(self._comfy_models.get(category, [])):
                if entry.get("url", "") != url:
                    continue
                if not target or model_target_for_url(category, url) == target:
                    return (category, index)
        wanted = {_file_stem(target), _file_stem(url)} - {""}
        name = str(model.get("name") or "").strip().lower()
        if not wanted and not name:
            return None
        for category in COMFY_MODEL_CATEGORIES:
            for index, entry in enumerate(self._comfy_models.get(category, [])):
                entry_url = entry.get("url", "")
                candidates = {
                    _file_stem(entry_url),
                    _file_stem(model_target_for_url(category, entry_url)),
                    str(entry.get("name") or "").strip().lower(),
                } - {""}
                if wanted & candidates:
                    return (category, index)
                if name and name in candidates:
                    return (category, index)
        return None

    def _tick_catalog_for_preset_models(self) -> int:
        """Tick the catalog checkbox of every preset model that matches one.

        A preset can list the same file as a catalog entry (older save, a
        mirror URL, a hand-written preset): showing it twice — unticked in its
        category AND ticked under "hors catalogue" — was confusing. The
        catalog entry is ticked instead, and the "hors catalogue" list keeps
        only the models with no catalog counterpart.

        The preset's own model list is left untouched: it is what "Save"
        writes, so dropping the matched entries here would silently lose them.
        """
        ticked = 0
        for model in self._comfy_edit_models:
            match = self._match_catalog_entry(model)
            if match is None:
                continue
            category, index = match
            entries = self._comfy_models.get(category, [])
            if 0 <= index < len(entries) and not entries[index].get("enabled"):
                entries[index]["enabled"] = True
                ticked += 1
        return ticked

    def _comfy_toggle_model_entry(self, index: int, var: tk.BooleanVar) -> None:
        if 0 <= index < len(self._comfy_edit_models):
            self._comfy_edit_models[index]["enabled"] = bool(var.get())

    def _comfy_remove_model_entry(self, index: int) -> None:
        if 0 <= index < len(self._comfy_edit_models):
            del self._comfy_edit_models[index]
            self._rebuild_comfy_extra_rows()

    def _comfy_toggle_node(self, index: int, var: tk.BooleanVar) -> None:
        if 0 <= index < len(self._comfy_edit_nodes):
            self._comfy_edit_nodes[index]["enabled"] = bool(var.get())

    def _comfy_toggle_workflow(self, index: int, var: tk.BooleanVar) -> None:
        if 0 <= index < len(self._comfy_edit_workflows):
            self._comfy_edit_workflows[index]["enabled"] = bool(var.get())

    def _comfy_add_node(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Add a custom node")
        dialog.configure(bg=self._pal["bg"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        body = tk.Frame(dialog, bg=self._pal["bg"], padx=16, pady=14)
        body.pack(fill="both", expand=True)

        fields = (
            ("name", "Name:"),
            ("url", "Repository link:"),
            ("note", "Note:"),
            ("post_install", "Post-install (optionnel) :"),
        )
        vars_: dict = {}
        for row, (key, label_text) in enumerate(fields):
            self._label(
                body, text=label_text, bg=self._pal["bg"], fg=self._pal["fg"],
            ).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            var = tk.StringVar(dialog)
            self._entry(
                body, textvariable=var, width=52,
                bg=self._pal["input_bg"], fg=self._pal["input_fg"],
                insertbackground=self._pal["fg"], relief="flat",
            ).grid(row=row, column=1, sticky="w", pady=2)
            vars_[key] = var
        self._label(
            body,
            text="The name and the note are local (never sent to the pod). "
            "Post-install runs once, non-blocking.",
            bg=self._pal["bg"], fg=self._pal["muted"], font=("Segoe UI", 8),
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 10))
        actions = tk.Frame(body, bg=self._pal["bg"])
        actions.grid(row=5, column=0, columnspan=2, sticky="e")

        def add() -> None:
            url = vars_["url"].get().strip()
            if not url:
                self._append_log("error", "Repository link is required.")
                dialog.destroy()
                return
            self._comfy_edit_nodes.append(
                {
                    "name": vars_["name"].get().strip(),
                    "url": url,
                    "note": vars_["note"].get().strip(),
                    "post_install": vars_["post_install"].get().strip(),
                    "enabled": True,
                }
            )
            self._rebuild_comfy_extra_rows()
            dialog.destroy()

        self._make_button(actions, "Cancel", dialog.destroy, variant="quiet").pack(
            side="right"
        )
        self._make_button(actions, "Add", add, variant="primary").pack(
            side="right", padx=(0, 6)
        )
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.bind("<Return>", lambda _e: add())

    def _comfy_remove_node(self, index: int) -> None:
        if 0 <= index < len(self._comfy_edit_nodes):
            del self._comfy_edit_nodes[index]
            self._rebuild_comfy_extra_rows()

    def _comfy_add_workflow(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Ajouter un workflow")
        dialog.configure(bg=self._pal["bg"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        body = tk.Frame(dialog, bg=self._pal["bg"], padx=16, pady=14)
        body.pack(fill="both", expand=True)
        self._label(body, text="Name:", bg=self._pal["bg"], fg=self._pal["fg"]).grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=2
        )
        name_var = tk.StringVar(dialog)
        self._entry(
            body, textvariable=name_var, width=52,
            bg=self._pal["input_bg"], fg=self._pal["input_fg"],
            insertbackground=self._pal["fg"], relief="flat",
        ).grid(row=0, column=1, sticky="w", pady=2)
        self._label(body, text="Source:", bg=self._pal["bg"], fg=self._pal["fg"]).grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=2
        )
        kind_var = tk.StringVar(dialog, value="url")
        kind_combo = ttk.Combobox(
            body, textvariable=kind_var, state="readonly", width=14,
            values=["url", "github"], style="Forge.TCombobox",
        )
        kind_combo.grid(row=1, column=1, sticky="w", pady=2)
        self._label(body, text="Link:", bg=self._pal["bg"], fg=self._pal["fg"]).grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=2
        )
        value_var = tk.StringVar(dialog)
        self._entry(
            body, textvariable=value_var, width=52,
            bg=self._pal["input_bg"], fg=self._pal["input_fg"],
            insertbackground=self._pal["fg"], relief="flat",
        ).grid(row=2, column=1, sticky="w", pady=2)
        self._label(body, text="Note:", bg=self._pal["bg"], fg=self._pal["fg"]).grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=2
        )
        note_var = tk.StringVar(dialog)
        self._entry(
            body, textvariable=note_var, width=52,
            bg=self._pal["input_bg"], fg=self._pal["input_fg"],
            insertbackground=self._pal["fg"], relief="flat",
        ).grid(row=3, column=1, sticky="w", pady=2)
        self._label(
            body,
            text="url = workflow JSON file; github = « owner/repo|branch|subfolder ».",
            bg=self._pal["bg"], fg=self._pal["muted"], font=("Segoe UI", 8),
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 10))
        actions = tk.Frame(body, bg=self._pal["bg"])
        actions.grid(row=5, column=0, columnspan=2, sticky="e")

        def add() -> None:
            kind = kind_var.get().strip()
            value = value_var.get().strip()
            if not value:
                self._append_log("error", "Lien requis.")
                dialog.destroy()
                return
            entry = {
                "name": name_var.get().strip(),
                "note": note_var.get().strip(),
                "enabled": True,
            }
            if kind == "github":
                entry["github"] = value
            else:
                entry["url"] = value
            self._comfy_edit_workflows.append(entry)
            self._rebuild_comfy_extra_rows()
            dialog.destroy()

        self._make_button(actions, "Cancel", dialog.destroy, variant="quiet").pack(
            side="right"
        )
        self._make_button(actions, "Add", add, variant="primary").pack(
            side="right", padx=(0, 6)
        )
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.bind("<Return>", lambda _e: add())

    def _comfy_remove_workflow(self, index: int) -> None:
        if 0 <= index < len(self._comfy_edit_workflows):
            del self._comfy_edit_workflows[index]
            self._rebuild_comfy_extra_rows()

    def _comfy_delete_preset(self) -> None:
        name = self._comfy_preset_combo.get().strip()
        if not name or name not in self._comfy_presets:
            self._append_log("error", "Select a preset to delete.")
            return
        try:
            from tkinter import messagebox

            if not messagebox.askyesno(
                "Delete the preset",
                f"Preset « {name} » will be permanently deleted from the "
                "local library. Models already downloaded to the pod "
                "are untouched.\n\nDelete the preset « "
                f"{name} » ?",
                parent=self.root,
            ):
                return
        except tk.TclError:
            pass
        del self._comfy_presets[name]
        self._comfy_edit_models = []
        self._comfy_edit_nodes = []
        self._comfy_edit_workflows = []
        self._rebuild_comfy_extra_rows()
        self._refresh_comfy_preset_combo()
        self._refresh_comfy_options_preset_combo()
        self._save_comfy_settings()
        self._append_log("info", f"Preset « {name} » deleted.")

    # -- timer -------------------------------------------------------------

    def _timer_start(self) -> None:
        if self._timer_after_id is not None:
            self._append_log("warn", "A timer is already running.")
            return
        try:
            minutes = int(self._timer_minutes_entry.get().strip())
        except ValueError:
            minutes = -1
        if minutes <= 0 or minutes > 10080:
            self._append_log("error", "Invalid duration: enter minutes (1 to 10080).")
            return
        self._timer_deadline = time.time() + minutes * 60
        self._timer_remaining = minutes * 60
        self._persist_pending_stop()
        self._timer_label.configure(text=format_countdown(self._timer_remaining))
        self._timer_minutes_entry.configure(state="disabled")
        self._timer_start_btn.configure(state="disabled")
        self._append_log(
            "info",
            f"Timer started: all pods stop in {minutes} minute(s) "
            f"(Windows action: {POWER_ACTION_LABELS[self._power_mode.get()].lower()}). "
            "The deadline is absolute: it survives a Windows sleep and a "
            "launcher restart.",
        )
        self._timer_after_id = self.root.after(1000, self._timer_tick)

    def _timer_cancel(self) -> None:
        if self._timer_after_id is not None:
            try:
                self.root.after_cancel(self._timer_after_id)
            except tk.TclError:
                pass
            self._timer_after_id = None
        self._timer_remaining = 0
        self._timer_deadline = 0.0
        self._clear_pending_stop()
        self._timer_label.configure(text="—")
        self._timer_minutes_entry.configure(state="normal")
        self._timer_start_btn.configure(state="normal")
        self._append_log("info", "Timer cancelled.")

    def _timer_tick(self) -> None:
        self._timer_after_id = None
        self._timer_remaining = max(0, int(self._timer_deadline - time.time()))
        self._timer_label.configure(text=format_countdown(self._timer_remaining))
        if self._timer_remaining > 0:
            self._timer_after_id = self.root.after(1000, self._timer_tick)
        else:
            self._timer_fire()

    def _timer_fire(self) -> None:
        self._append_log("warn", "Timer elapsed — stopping the pods…")
        self._timer_label.configure(text=format_countdown(0))
        self._busy = True
        self._set_buttons_state("disabled")
        self._status_line.configure(text="Scheduled stop in progress…")
        mode = self._power_mode.get()

        def worker() -> None:
            ok, payload = False, "Scheduled stop not performed."
            for attempt in (1, 2, 3):
                try:
                    # No stack: every registered pod is stopped. The timer is
                    # an "I'm leaving" control — a single-stack stop could
                    # silently leave a billing pod running.
                    ok, payload = action_stop_outcome(terminate=False)
                except Exception as exc:  # noqa: BLE001 - surfaced in the journal
                    ok, payload = False, str(exc) or exc.__class__.__name__
                if ok:
                    break
                if attempt < 3:
                    time.sleep(10 * attempt)
            self._queue.put(("timer_stop_done", ok, payload, mode))

        self._timer_thread = threading.Thread(
            target=worker, daemon=True, name="gui-timer-stop"
        )
        self._timer_thread.start()

    def _timer_stop_finished(self, ok: bool, payload, mode: str) -> None:
        self._busy = False
        self._set_buttons_state("normal")
        if not ok:
            self._append_log(
                "error",
                f"Scheduled stop failed: {payload} — the pod(s) may still be "
                "billing. The launcher will retry at the next start.",
            )
            self._show_error(
                "Timer — pod not stopped",
                f"{payload}\n\nThe pod(s) may still be billing. "
                "Check the RunPod API key and press Stop again.",
            )
            self._status_line.configure(
                text="⚠ Scheduled stop failed — a pod may still be running."
            )
            return
        self._clear_pending_stop()
        self._append_log("ok", str(payload))
        self._status_line.configure(text=str(payload))
        if mode != "none":
            self._append_log("info", "Pods stopped — running the Windows action…")
            self._status_line.configure(text="Action Windows en cours…")

            def power_worker() -> None:
                result = run_power_action(mode)
                self._queue.put(("power_done", result))

            self._power_thread = threading.Thread(
                target=power_worker, daemon=True, name="gui-power-action"
            )
            self._power_thread.start()

    def _persist_pending_stop(self) -> None:
        try:
            path = runtime_state.pending_stop_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "deadline": self._timer_deadline,
                        "power_mode": self._power_mode.get(),
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _clear_pending_stop(self) -> None:
        try:
            runtime_state.pending_stop_path().unlink()
        except OSError:
            pass

    def _load_pending_stop(self) -> None:
        """Resume a pod stop scheduled by a previous launcher session.

        A past deadline fires immediately (the stop was missed while the
        launcher was closed — the pod has been billing the whole time); a
        future one restarts the countdown.
        """
        try:
            data = json.loads(
                runtime_state.pending_stop_path().read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return
        deadline = float(data.get("deadline") or 0)
        if deadline <= 0:
            self._clear_pending_stop()
            return
        mode = str(data.get("power_mode") or "none")
        if mode in POWER_ACTIONS:
            self._power_mode.set(mode)
        self._timer_deadline = deadline
        self._timer_minutes_entry.configure(state="disabled")
        self._timer_start_btn.configure(state="disabled")
        remaining = max(0, int(deadline - time.time()))
        if remaining > 0:
            self._timer_remaining = remaining
            self._timer_label.configure(text=format_countdown(remaining))
            self._append_log(
                "info",
                f"Timer resumed: pods stop in {format_countdown(remaining)} "
                f"(deadline {time.strftime('%H:%M', time.localtime(deadline))}).",
            )
            self._timer_after_id = self.root.after(1000, self._timer_tick)
        else:
            self._append_log(
                "warn",
                "The timer expired while the launcher was closed — "
                "stopping the pods…",
            )
            self._timer_fire()

    def _power_done(self, result: str) -> None:
        self._append_log("info", result)
        self._status_line.configure(text=result)

    # -- credentials -------------------------------------------------------

    def _credentials_set(self, parent=None) -> None:
        parent = parent or self.root

        def on_ok(
            values: tuple[str, str, str, str, str, str, str]
        ) -> None:
            try:
                try:
                    existing_extra = dict(CredentialStore().get().extra)
                except CredentialStoreError:
                    existing_extra = {}
                extra = dict(existing_extra)
                for key, raw in (("HF_TOKEN", values[2]), ("CIVITAI_API_KEY", values[3])):
                    value = raw.strip()
                    if value:
                        extra[key] = value
                CredentialStore().set(
                    values[0], values[1], extra=extra,
                    comfy_template_id=values[4],
                    train_template_id=values[5],
                    vnc_password=values[6],
                )
                self._append_log("ok", "RunPod credentials stored.")
            except CredentialStoreError as exc:
                self._append_log("error", f"Save failed: {exc}")
                self._show_error("Credentials", str(exc))
            self._refresh(force=True)

        try:
            _secrets_dialog(parent, self.settings.theme, on_ok)
        except tk.TclError as exc:
            self._append_log("error", f"Dialog unavailable: {exc}")

    def _credentials_clear(self, parent=None) -> None:
        parent = parent or self.root
        try:
            from tkinter import messagebox

            if not messagebox.askyesno(
                "Clear the credentials",
                "This permanently deletes the credentials stored "
                "locally (RunPod API key, template, Hugging Face, Civitai). "
                "You will have to retype everything to start the stack again.\n\n"
                "Clear the credentials?",
                parent=parent,
            ):
                return
        except tk.TclError:
            return
        try:
            removed = CredentialStore().clear()
            self._append_log(
                "ok" if removed else "info",
                "RunPod credentials deleted."
                if removed
                else "No stored credential to delete.",
            )
        except CredentialStoreError as exc:
            self._append_log("error", f"Delete failed: {exc}")
        self._refresh(force=True)

    # -- .env ----------------------------------------------------------------

    def _env_status_text(self) -> str:
        if self.env_file is not None and self.env_file.is_file():
            return str(self.env_file)
        return "none (optional — create one with \"Create .env\")"

    def _refresh_env_label(self) -> None:
        if self._widget_exists(self._env_label):
            self._env_label.configure(text=self._env_status_text())

    def _create_env(self) -> None:
        target = (self.env_file if self.env_file else env_target_dir() / ".env")
        if target.is_file():
            self._edit_env()
            return
        example = target.with_name(".env.example")
        try:
            if example.is_file():
                target.write_text(
                    example.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8",
                )
                source = f" (copied from {example.name})"
            else:
                target.write_text(ENV_TEMPLATE, encoding="utf-8")
                source = ""
        except OSError as exc:
            self._append_log("error", f"Cannot create {target} : {exc}")
            self._show_error(".env", f"Cannot create {target} : {exc}")
            return
        self.env_file = target
        self._refresh_env_label()
        try:
            loaded = load_env_file(target)
        except OSError as exc:
            loaded = {}
            self._append_log("warn", f"File created but unreadable: {exc}")
        self._append_log(
            "ok",
            f"File {target} created{source}. "
            f"{len(loaded)} variable(s) loaded.",
        )
        self._append_log(
            "info",
            "Enter your RUNPOD_API_KEY there, then press Edit .env.",
        )
        self._refresh(force=True)

    def _edit_env(self) -> None:
        target = self.env_file
        if target is None or not target.is_file():
            self._create_env()
            return
        try:
            if os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:  # pragma: no cover - Windows-first project
                winproc.Popen(["xdg-open", str(target)])
            self._append_log("info", f"Editing {target}")
        except OSError as exc:
            self._append_log("error", f"Cannot open {target}: {exc}")

    def _apply_window_icon(self) -> None:
        """Show the brand icon in the title bar AND taskbar (best effort).

        On Windows a native multi-size .ico is preferred: Tk's ``iconphoto``
        updates the title bar but the taskbar keeps the process class icon
        (e.g. the default pythonw icon), while ``iconbitmap`` with a
        multi-size .ico lets Windows pick the right size for both slots.
        """
        if os.name == "nt":
            ico = _brand_ico_path()
            if ico is not None:
                try:
                    self.root.iconbitmap(str(ico))
                    return
                except Exception as exc:  # noqa: BLE001 - fall through
                    logger.debug("iconbitmap unavailable: %s", exc)
        brand = _load_brand_icon(256)
        if brand is None:
            return
        try:
            import base64
            import io

            buffer = io.BytesIO()
            brand.save(buffer, format="PNG")
            self._icon_image = tk.PhotoImage(
                master=self.root,
                data=base64.b64encode(buffer.getvalue()).decode("ascii"),
            )
            self.root.iconphoto(True, self._icon_image)
        except Exception as exc:  # noqa: BLE001 - icon is best effort
            logger.debug("Window icon unavailable: %s", exc)

    # -- tray ---------------------------------------------------------------

    def _tray_available(self) -> bool:
        return _HAS_PYSTRAY and _HAS_PIL and sys.platform == "win32"

    def _start_tray(self) -> None:
        if not self._tray_available():
            if self._minimize_on_close.get():
                self._minimize_on_close.set(False)
                self._append_log(
                    "info",
                    "Tray icon unavailable (installez l'extra [tray] "
                    "for the taskbar); closing quits the application.",
                )
            return
        try:
            import pystray
            from PIL import Image, ImageDraw

            icon = pystray.Icon(
                "MiniMaxH3Launcher",
                _make_tray_image(),
                "MiniMax H3 Launcher",
                pystray.Menu(
                    pystray.MenuItem(
                        "Afficher",
                        lambda *_args: self._queue.put(("tray_show",)),
                        default=True,
                    ),
                    pystray.MenuItem(
                        "Start",
                        lambda *_args: self._tray_action("start"),
                    ),
                    pystray.MenuItem(
                        "Stop",
                        lambda *_args: self._tray_action("stop"),
                    ),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem(
                        "Quit", lambda *_args: self._queue.put(("tray_quit",))
                    ),
                ),
            )
            self._tray = icon
            self._tray_thread = threading.Thread(
                target=icon.run, daemon=True, name="gui-tray"
            )
            self._tray_thread.start()
        except Exception as exc:  # noqa: BLE001 - tray is best effort
            logger.warning("Tray icon unavailable: %s", exc)
            self._tray = None
            if self._minimize_on_close.get():
                self._minimize_on_close.set(False)
                self._append_log(
                    "info",
                    f"Tray icon unavailable ({exc}) ; la fermeture "
                    "quitte l'application.",
                )

    def _tray_action(self, what: str) -> None:
        self._queue.put(("tray_show",))
        self._queue.put(("tray_action", what))

    def _tray_stop(self) -> None:
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
            self._tray_thread = None

    # -- queue loop / lifecycle ----------------------------------------------

    def _sync_scrollables(self) -> None:
        # Content can grow while a tab's inner frame is clamped to the
        # viewport height (no <Configure> fires then), so the 200 ms poll
        # re-derives each scroller's min-height from the content's
        # requested size.
        if not self._widget_exists(self.root):
            return
        for _canvas, _inner, _state, sync in self._scrollables:
            sync()

    def _poll(self) -> None:
        try:
            try:
                while True:
                    item = self._queue.get_nowait()
                    try:
                        self._handle(item)
                    except Exception:  # noqa: BLE001 - one bad item only
                        logger.exception("Error while handling a UI message")
            except queue.Empty:
                pass
            self._sync_scrollables()
            if self._auto_refresh.get():
                self._refresh()
        except Exception:  # noqa: BLE001 - the pump must never die
            # The re-arm used to be the last statement of an unguarded body:
            # one exception from _sync_scrollables/_refresh
            # stopped the queue pump for the rest of the session — no journal,
            # no status, no auto-refresh, _finish_action never called, and
            # every action button left disabled. The window stayed alive and
            # looked merely frozen.
            logger.exception("Error in the UI loop (continuing)")
        finally:
            if self._widget_exists(self.root):
                self.root.after(POLL_INTERVAL_MS, self._poll)

    def _handle(self, item: tuple) -> None:
        if not self._widget_exists(self.root):
            return
        kind = item[0]
        if kind == "log":
            level = item[1].lower()
            if level == "warning":
                level = "warn"
            self._append_log(level, item[2])
            if "Pod not available" in item[2]:
                # The retry loop can run for a long time: keep the single-line
                # status bar in sync with the latest journal retry.
                self._status_line.configure(
                    text="Pod unavailable — automatic retry…"
                )
        elif kind == "status":
            self._apply_status(item[1])
        elif kind == "action":
            self._finish_action(item[1], item[2], item[3])
        elif kind == "refresh_error":
            self._refresh_in_progress = False
            self._append_log("error", f"Cannot refresh the state: {item[1]}")
        elif kind == "comfy_releases":
            self._apply_comfy_releases(item[1])
        elif kind == "model_sizes":
            self._apply_model_sizes(item[1])
        elif kind == "train_result":
            self._apply_train_result(item[1], item[2])
        elif kind == "tray_show":
            self.root.deiconify()
            self.root.lift()
        elif kind == "comfy_done":
            self._notify_windows(item[1], item[2])
        elif kind == "comfy_terminated":
            self._on_comfy_watch_terminated(item[1], item[2])
        elif kind == "tray_action":
            if item[1] == "start":
                self._start_action()
            else:
                self._stop_action(False)
        elif kind == "tray_quit":
            self._quit()
        elif kind == "prompt_recreate":
            self._prompt_recreate(item[1], item[2], item[3])
        elif kind == "timer_stop_done":
            self._timer_stop_finished(item[1], item[2], item[3])
        elif kind == "power_done":
            self._power_done(item[1])

    def _on_close(self) -> None:
        # Persist the ComfyUI choices on every close: with the default
        # "minimize on close" the app keeps running from the tray, so the
        # tray path is the common one — saving here covers both paths.
        self._settings_from_vars()
        if self._minimize_on_close.get() and self._tray is not None:
            self.root.withdraw()
            if self._tray is not None:
                try:
                    self._tray.notification(
                        "MiniMax H3 Launcher",
                        "Minimised to the taskbar.",
                    )
                except Exception:
                    pass
        else:
            # A status probe may still be winding down. Hide immediately so
            # closing the window feels instant while _quit safely joins it.
            try:
                self.root.withdraw()
            except tk.TclError:
                pass
            self._quit()

    def _quit(self) -> None:
        if self._timer_after_id is not None:
            try:
                self.root.after_cancel(self._timer_after_id)
            except tk.TclError:
                pass
            self._timer_after_id = None
        self._settings_from_vars()
        self._tray_stop()
        self._stop_comfy_watcher()
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        # A pending size-driven card rebuild would fire after the widgets are
        # gone; drop it before tearing anything down. Same for a queued
        # library-grid sync (a filter keystroke or a resize).
        self._cancel_size_rebuild()
        self._cancel_annuaire_rebuild()
        # Wait for background workers to finish before the Tk interpreter
        # goes away: a worker outliving the interpreter and touching it
        # from its own thread is a hard crash on Windows.
        for attr in (
            "_refresh_thread",
            "_release_thread",
            "_action_thread",
            "_timer_thread",
            "_size_thread",
        ):
            thread = getattr(self, attr, None)
            if thread is not None and thread.is_alive():
                thread.join(timeout=3.0)
        if self._widget_exists(self.root):
            self.root.destroy()

    def _settings_from_vars(self) -> None:
        self.settings.auto_refresh = bool(self._auto_refresh.get())
        self.settings.minimize_on_close = bool(self._minimize_on_close.get())
        self._save_comfy_settings()


def _brand_ico_path() -> Optional[Path]:
    """Multi-size brand .ico (16..256), for native Windows icon slots."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        candidates.append(Path(meipass) / "launcher" / "icon.ico")
    candidates.append(Path(__file__).resolve().parent / "icon.ico")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _brand_icon_path() -> Optional[Path]:
    """Locate the bundled brand icon (PNG), or None when absent."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        candidates.append(Path(meipass) / "launcher" / "icon.png")
    candidates.append(Path(__file__).resolve().parent / "icon.png")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _load_brand_icon(size: int) -> Optional[object]:
    """Load the brand icon as a PIL image at the given size, or None."""
    if not _HAS_PIL:
        return None
    path = _brand_icon_path()
    if path is None:
        return None
    try:
        from PIL import Image

        img = Image.open(path)
        img.load()
        return img.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    except Exception:  # noqa: BLE001 - a bad icon must not break the app
        return None


def _asset_icon_path(name: str) -> Optional[Path]:
    """Locate a bundled icon from ``launcher/assets/icons``, or None.

    Resolution mirrors :func:`_brand_icon_path`: the frozen build unpacks its
    ``--add-data`` payload under ``sys._MEIPASS``, the source tree keeps the
    files next to the module.
    """
    if name not in ICON_ASSETS:
        return None
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        candidates.append(Path(meipass) / "launcher" / "assets" / "icons" / name)
    candidates.append(
        Path(__file__).resolve().parent / "assets" / "icons" / name
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _load_asset_icon(
    name: str, size: int, *, by_height: bool = False
) -> Optional[object]:
    """Load a bundled icon scaled for display, or None.

    The icons are stored at ~2x their display size and downscaled once here
    (LANCZOS). A missing icon, a missing PIL or a corrupt file all return
    None: the caller then falls back to its text/glyph rendering instead of
    failing to start.

    ``size`` is the target height. By default the result is a ``size`` x
    ``size`` square with the art centred (aspect preserved); with
    ``by_height=True`` the width follows the art's ratio, so a wide glyph
    (the 4:1 workflow icon) is not squashed into a square.
    """
    if not _HAS_PIL:
        return None
    path = _asset_icon_path(name)
    if path is None:
        return None
    try:
        from PIL import Image

        img = Image.open(path)
        img.load()
        img = img.convert("RGBA")
        if by_height:
            if img.height != size:
                width = max(1, round(img.width * size / img.height))
                img = img.resize((width, size), Image.Resampling.LANCZOS)
            return img
        if img.size != (size, size):
            # Preserve the aspect ratio: two shipped icons are not square
            # (40x27), and stretching them to a square box would distort the
            # glyph. Non-square art is centred on a transparent square.
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            if img.size != (size, size):
                square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                square.paste(
                    img,
                    ((size - img.width) // 2, (size - img.height) // 2),
                )
                img = square
        return img
    except Exception:  # noqa: BLE001 - a bad icon must not break the app
        return None


def _make_tray_image():
    """Brand icon for the tray, falling back to a drawn fox mark."""
    brand = _load_brand_icon(64)
    if brand is not None:
        return brand
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([2, 2, 61, 61], radius=12, fill=(30, 31, 34, 255))
    draw.ellipse([14, 18, 50, 54], fill=(232, 145, 58, 255))
    draw.polygon([(24, 12), (32, 22), (20, 24)], fill=(232, 145, 58, 255))
    draw.polygon([(40, 12), (32, 22), (44, 24)], fill=(232, 145, 58, 255))
    return img


#: Dialog field key -> credential-detail key (the shared « set / not set »
#: computation, see :func:`credential_field_labels`).
_SECRETS_FIELD_TO_DETAIL = {
    "api_key": "runpod",
    "template_id": "template",
    "comfy_template_id": "comfy_template",
    "train_template_id": "train_template",
    "vnc_password": "vnc_password",
    "hf_token": "hf",
    "civitai_key": "civitai",
}


def _secrets_label(parent, text="", *, pal, muted: bool = False, **kwargs):
    """A label for the standalone secrets dialog (no app reference).

    The fallback branch must paint itself: a bare ``tk.Label`` would default
    to ``SystemButtonFace`` — light patches with black text on the dark
    dialog."""
    if _TB_STYLE_ACTIVE:
        for option in ("bg", "fg", "padx", "pady", "width"):
            kwargs.pop(option, None)
        widget = ttk.Label(parent, text=text, **kwargs)
        if muted:
            widget.configure(style="ForgeMuted.TLabel")
        return widget
    kwargs.setdefault("bg", pal["bg"])
    kwargs.setdefault("fg", pal["muted"] if muted else pal["fg"])
    return _TKMOD.Label(parent, text=text, **kwargs)


def _secrets_check(parent, text, *, variable, pal, **kwargs):
    if _TB_STYLE_ACTIVE:
        for option in ("bg", "fg", "activebackground", "activeforeground",
                       "selectcolor", "anchor"):
            kwargs.pop(option, None)
        return ttk.Checkbutton(parent, text=text, variable=variable, **kwargs)
    return _TKMOD.Checkbutton(parent, text=text, variable=variable, **kwargs)


def _secrets_entry(parent, *, pal, **kwargs):
    if _TB_STYLE_ACTIVE:
        for option in ("bg", "fg", "insertbackground", "relief"):
            kwargs.pop(option, None)
        return ttk.Entry(parent, **kwargs)
    return _TKMOD.Entry(parent, **kwargs)


def _secrets_dialog(
    parent,
    theme: str,
    on_ok: Callable[[tuple[str, str, str, str, str, str, str, str]], None],
) -> None:
    """Modal dialog: masked entry for each credential field.

    Shows, per field, whether a value is currently stored — the labels come
    from the shared :func:`credential_field_labels` computation so this
    dialog and "Quick settings" can never disagree. A left-empty
    field keeps the currently stored value (the RunPod key and template
    are both required, so the stored pair is resolved here). The extra API
    keys (Hugging Face, Civitai), the ComfyUI / llama.cpp / train template
    IDs and the training desktop password are optional. A "Reveal"
    checkbox toggles the masking of every field.
    """
    from tkinter import messagebox

    store = None
    stored = None
    try:
        store = CredentialStore()
        stored = store.get()
    except CredentialStoreError:
        pass
    stored_extra = dict(stored.extra) if stored else {}
    details = _credentials_details(store or CredentialStore())
    field_states = credential_field_labels(
        details, _SECRETS_FIELD_TO_DETAIL.values()
    )

    dialog = tk.Toplevel(parent)
    dialog.title("Credentials / API keys")
    dialog.resizable(False, False)
    dialog.transient(parent)
    pal = palette_from_theme(
        _ttkbootstrap.Style.get_instance() if _TB_STYLE_ACTIVE else None,
        theme,
    )
    dialog.configure(bg=pal["bg"])
    _secrets_label(
        dialog,
        text="These values are encrypted locally (DPAPI).\n"
        "Leave a field empty to keep the existing value.\n"
        "The optional keys feed the pod at start.",
        pal=pal,
        muted=True,
        justify="left",
    ).pack(anchor="w", padx=16, pady=(12, 8))

    reveal = tk.BooleanVar(dialog, value=False)
    fields: dict[str, tk.Entry] = {}

    def _apply_mask() -> None:
        show = "" if reveal.get() else "•"
        for entry in fields.values():
            entry.configure(show=show)

    _secrets_check(
        dialog,
        text="Reveal the values",
        variable=reveal,
        command=lambda: _apply_mask(),
        pal=pal,
        bg=pal["bg"],
        fg=pal["fg"],
        activebackground=pal["bg"],
        activeforeground=pal["fg"],
        selectcolor=pal["bg_alt"],
        anchor="w",
    ).pack(anchor="w", padx=16, pady=(0, 4))

    field_specs = (
        ("api_key", "RunPod API key:"),
        ("template_id", "Private RunPod template ID:"),
        ("comfy_template_id", "Private ComfyUI template ID (optional):"),
        ("train_template_id", "Private LoRA training template ID (optional):"),
        ("vnc_password", "Training desktop password, 12+ chars (optional):"),
        ("hf_token", "Hugging Face token (optional):"),
        ("civitai_key", "Civitai API key (optional):"),
    )
    # A fixed first-column width keeps the entries and their
    # « set / not set » statuses aligned whatever the label length
    # (ttk labels have no width option, so the alignment comes from a grid
    # column measured in pixels).
    try:
        measure_font = tkfont.Font(dialog, family="Segoe UI", size=9)
        label_column = max(
            measure_font.measure(label) for _key, label in field_specs
        ) + 10
    except tk.TclError:  # pragma: no cover - defensive
        label_column = 280
    for key, label in field_specs:
        row = tk.Frame(dialog, bg=pal["bg"])
        row.pack(fill="x", padx=16, pady=(0, 6))
        row.grid_columnconfigure(0, minsize=label_column)
        _secrets_label(row, text=label, pal=pal, anchor="w").grid(
            row=0, column=0, sticky="w"
        )
        entry = _secrets_entry(
            row,
            pal=pal,
            show="•",
            width=24,
            bg=pal["input_bg"],
            fg=pal["input_fg"],
            insertbackground=pal["fg"],
            relief="flat",
        )
        entry.grid(row=0, column=1, sticky="w", padx=(0, 8))
        field_label, field_color = field_states[_SECRETS_FIELD_TO_DETAIL[key]]
        _secrets_label(
            row, text=field_label, pal=pal, muted=field_color != "ok",
        ).grid(row=0, column=2, sticky="w")
        fields[key] = entry

    def on_ok_pressed() -> None:
        raw_api = fields["api_key"].get().strip()
        raw_template = fields["template_id"].get().strip()
        api_key = raw_api or (stored.runpod_api_key if stored else "")
        template_id = raw_template or (stored.runpod_template_id if stored else "")
        if not api_key or not template_id:
            messagebox.showwarning(
                "Credentials",
                "The API key and the template ID are both required "
                "(no existing value to keep).",
                parent=dialog,
            )
            return
        raw_comfy = fields["comfy_template_id"].get().strip()
        comfy_template_id = (
            raw_comfy
            if raw_comfy
            else (stored.comfy_template_id if stored is not None else None)
        )
        raw_train = fields["train_template_id"].get().strip()
        train_template_id = (
            raw_train
            if raw_train
            else (stored.train_template_id if stored is not None else None)
        )
        raw_vnc = fields["vnc_password"].get().strip()
        if raw_vnc and len(raw_vnc) < 12:
            messagebox.showwarning(
                "Credentials",
                "The training desktop password must be at least "
                "12 characters (filebrowser refuses shorter ones).",
                parent=dialog,
            )
            return
        vnc_password = (
            raw_vnc
            if raw_vnc
            else (stored.vnc_password if stored is not None else None)
        )
        hf_token = fields["hf_token"].get().strip() or stored_extra.get("HF_TOKEN", "")
        civitai_key = (
            fields["civitai_key"].get().strip()
            or stored_extra.get("CIVITAI_API_KEY", "")
        )
        dialog.destroy()
        on_ok(
            (api_key, template_id, hf_token, civitai_key, comfy_template_id,
             train_template_id, vnc_password)
        )

    buttons = tk.Frame(dialog, bg=pal["bg"])
    buttons.pack(pady=(4, 12))
    if _TB_STYLE_ACTIVE:
        ttk.Button(buttons, text="Save", command=on_ok_pressed).pack(
            side="left", padx=6
        )
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="left")
    else:
        tk.Button(
            buttons, text="Save", command=on_ok_pressed,
            bg=pal["button_bg"], fg=pal["button_fg"], relief="flat", padx=12, pady=4,
        ).pack(side="left", padx=6)
        tk.Button(
            buttons, text="Cancel", command=dialog.destroy,
            bg=pal["button_bg"], fg=pal["button_fg"], relief="flat", padx=12, pady=4,
        ).pack(side="left")
    dialog.grab_set()
    parent.wait_window(dialog)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _show_fatal(title: str, message: str) -> None:
    """Show *message* even where stderr is invisible (windowed exe, pythonw)."""
    try:
        from tkinter import messagebox

        messagebox.showerror(title, message)
        return
    except Exception:  # noqa: BLE001 - tkinter itself may be the problem
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
    except Exception:  # noqa: BLE001 - last resort: nowhere left to show it
        pass


def run_gui() -> int:
    """Start the GUI (called by ``minimax-launcher gui`` and the packaged exe)."""
    if not _HAS_TK:
        message = f"tkinter is unavailable on this Python: {_TK_IMPORT_ERROR}"
        print(message, file=sys.stderr)
        _show_fatal("MiniMax H3 Launcher", message)
        return 1
    if sys.stderr is None:

        class _NullStream:
            def write(self, *_args) -> None:
                return None

            def flush(self) -> None:
                return None

            def isatty(self) -> bool:
                return False

        sys.stderr = _NullStream()  # type: ignore[assignment]
    setup_logging()
    app: Optional[ForgeApp] = None
    root = None
    try:
        env_file = find_env_file(Path(os.getcwd()))
        if env_file is not None:
            try:
                loaded = load_env_file(env_file)
                if loaded:
                    logger.info(
                        "Loaded %d variable(s) from %s", len(loaded), env_file
                    )
            except OSError as exc:
                logger.warning("Could not read %s: %s", env_file, exc)
                env_file = None
        root = tk.Tk()
        app = ForgeApp(root, env_file=env_file)

        def _report_tk_error(exc: BaseException, _func, _tb) -> None:
            logger.exception("Unhandled GUI callback error: %s", exc)
            try:
                app._append_log("error", f"Error in the UI: {exc}")
            except Exception:  # noqa: BLE001 - window may be destroyed
                pass
            _show_fatal(
                "MiniMax H3 Launcher",
                f"An error occurred in the UI:\n\n{exc}",
            )

        root.report_callback_exception = _report_tk_error
        root.mainloop()
        return 0
    except Exception as exc:  # noqa: BLE001 - startup failures must be visible
        logger.exception("GUI startup failed: %s", exc)
        _show_fatal(
            "MiniMax H3 Launcher", f"Cannot start the interface:\n\n{exc}"
        )
        if root is not None:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001 - best effort
                pass
        return 1


def main() -> int:
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())

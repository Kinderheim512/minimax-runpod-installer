"""User-defined ComfyUI presets: data model, normalization, and JSON wire format.

A *preset* is a named bundle of three entry kinds, each defined by a link:

* ``models``    — a direct download URL (Hugging Face ``resolve``, CivitAI, or
  any direct link) plus the target path under ComfyUI's ``models/`` directory;
* ``nodes``     — a custom-node repository URL (git clone / GitHub zip), with
  an optional ``post_install`` escape-hatch command (run once on the pod, with
  a marker + log, non-blocking on failure);
* ``workflows`` — either a single workflow JSON file (``url``) or a repository
  of workflows (``github``, ``owner/repo|branch|subfolder``).

The launcher serializes *all* presets into one JSON document sent to the pod
as ``H3_USER_PRESETS_JSON``; the pod-side installer (``comfy/lib/user_presets.sh``)
reads it and drives the existing generic engine (node clone, model download,
workflow sync) — no new install logic. The pod-side installer also persists
the document (see ``lib/user_presets.sh`` for the location) so user presets
survive a pod resync/recreation.

The wire format is intentionally simple and stable (``version``-tagged):

.. code-block:: json

    {
      "version": 1,
      "presets": {
        "my-preset": {
          "models":   [{"url": "https://...", "target": "diffusion_models/x.safetensors"}],
          "nodes":    [{"url": "https://github.com/o/r.git", "post_install": "apt-get install -y ffmpeg"}],
          "workflows": [{"github": "o/r|main|wf"}, {"url": "https://.../x.json"}]
        }
      }
    }

This module is pure data manipulation: no GUI, no network, no filesystem —
so it is unit-tested directly.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import json
from typing import Any, Mapping, Optional

PRESET_JSON_VERSION = 1

#: The three entry kinds a preset may declare.
ENTRY_KINDS = ("models", "nodes", "workflows")

#: Canonical target directory (relative to ComfyUI's ``models/``) for each
#: model category of the launcher's built-in catalog. Used to derive the
#: ``target`` of a model entry captured from the catalog checkboxes, and to
#: migrate legacy ``{category: [url, ...]}`` presets to the new schema.
MODEL_CATEGORY_TARGET_DIRS = {
    "diffusion": "diffusion_models",
    "video_vae": "vae",
    "audio_vae": "vae",
    "text_encoder": "text_encoders",
    "tae": "vae_approx",
    "upscaler": "upscale_models",
    "frame_interp": "frame_interpolation",
}


def model_target_for_url(category: str, url: str) -> str:
    """Derive a ``models/``-relative target path for a catalog model URL.

    ``category`` maps to a canonical subdirectory; the filename is the URL
    basename (query/fragment stripped). Returns ``""`` (models root) when
    nothing usable can be derived.
    """
    directory = MODEL_CATEGORY_TARGET_DIRS.get(category, "")
    raw = (url or "").split("?")[0].split("#")[0].rstrip("/")
    basename = raw.rsplit("/", 1)[-1] if "/" in raw else ""
    if not basename:
        return directory
    return f"{directory}/{basename}" if directory else basename


def normalize_model_entry(entry: Any) -> Optional[dict]:
    """Normalize one ``models`` entry to ``{"url": str, "target": str}``.

    Returns ``None`` when the entry carries no usable URL.
    """
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url", "")).strip()
    if not url:
        return None
    target = str(entry.get("target", "")).strip()
    # ``name`` is launcher-local display metadata (never sent to the pod).
    name = str(entry.get("name", "")).strip()
    return {"url": url, "target": target, "name": name}


def normalize_node_entry(entry: Any) -> Optional[dict]:
    """Normalize one ``nodes`` entry.

    The full (GUI-side) shape carries local metadata plus the download info:

    ``{"url": str, "post_install": str, "name": str, "note": str, "enabled": bool}``

    ``name``/``note`` are launcher-local metadata (never sent to the pod);
    ``enabled`` (default ``True``) is the "checked in the preset" flag that
    :func:`build_user_presets_json` filters on. ``post_install`` defaults to
    ``""``. Returns ``None`` when the entry carries no usable URL.
    """
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url", "")).strip()
    if not url:
        return None
    post_install = str(entry.get("post_install", "")).strip()
    name = str(entry.get("name", "")).strip()
    note = str(entry.get("note", "")).strip()
    enabled = bool(entry.get("enabled", True))
    return {
        "url": url,
        "post_install": post_install,
        "name": name,
        "note": note,
        "enabled": enabled,
    }


def normalize_workflow_entry(entry: Any) -> Optional[dict]:
    """Normalize one ``workflows`` entry.

    Returns ``{"github": str}`` or ``{"url": str}`` plus the launcher-local
    ``name``/``note`` metadata and the ``enabled`` flag (same convention as
    :func:`normalize_node_entry`). A ``github`` source wins over ``url`` when
    both are present. Returns ``None`` when neither carries a usable value.
    """
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name", "")).strip()
    note = str(entry.get("note", "")).strip()
    enabled = bool(entry.get("enabled", True))
    github = str(entry.get("github", "")).strip()
    if github:
        return {"github": github, "name": name, "note": note, "enabled": enabled}
    url = str(entry.get("url", "")).strip()
    if url:
        return {"url": url, "name": name, "note": note, "enabled": enabled}
    return None


def _normalize_preset_body(body: Any) -> Optional[dict]:
    if not isinstance(body, dict):
        return None
    result: dict = {}
    models = [normalize_model_entry(e) for e in (body.get("models") or [])]
    nodes = [normalize_node_entry(e) for e in (body.get("nodes") or [])]
    workflows = [normalize_workflow_entry(e) for e in (body.get("workflows") or [])]
    result["models"] = [e for e in models if e is not None]
    result["nodes"] = [e for e in nodes if e is not None]
    result["workflows"] = [e for e in workflows if e is not None]
    if not any(result.values()):
        return None
    return result


def normalize_user_presets(data: Any) -> dict:
    """Normalize a persisted presets mapping into ``{name: {kind: [entry]}}``.

    Unknown/malformed names and entries are dropped; a preset whose three
    lists are all empty is dropped. Accepts the legacy shape (see
    :func:`migrate_legacy_presets`) and the new shape indifferently.
    """
    data = migrate_legacy_presets(data)
    out: dict = {}
    if not isinstance(data, dict):
        return out
    for name, body in data.items():
        if not isinstance(name, str) or not name.strip():
            continue
        normalized = _normalize_preset_body(body)
        if normalized is not None:
            out[name.strip()] = normalized
    return out


def migrate_legacy_presets(data: Any) -> dict:
    """Migrate the legacy preset shape ``{name: {category: [url, ...]}}``.

    Legacy presets stored only *enabled* model URLs per category; each is
    converted to a ``models`` entry with a target derived from the category.
    Presets already in the new shape pass through unchanged. Best-effort:
    never raises, drops anything unrecognized.
    """
    if not isinstance(data, dict):
        return {}
    migrated: dict = {}
    for name, body in data.items():
        if not isinstance(name, str) or not isinstance(body, dict):
            continue
        # New shape: at least one known entry-kind key present (or empty).
        if any(kind in body for kind in ENTRY_KINDS):
            migrated[name] = body
            continue
        # Legacy shape: category -> [url, ...]
        models: list = []
        for category, urls in body.items():
            if category not in MODEL_CATEGORY_TARGET_DIRS:
                continue
            url_list = urls if isinstance(urls, list) else (
                [urls] if isinstance(urls, str) and urls.strip() else []
            )
            for url in url_list:
                url = str(url).strip()
                if not url:
                    continue
                target = model_target_for_url(category, url)
                models.append({"url": url, "target": target})
        if models:
            migrated[name] = {"models": models, "nodes": [], "workflows": []}
    return migrated


def _node_wire_entry(entry: dict) -> Optional[dict]:
    """Reduce a normalized node entry to its pod wire form (enabled only)."""
    if not entry.get("enabled", True):
        return None
    return {"url": entry["url"], "post_install": entry.get("post_install", "")}


def _workflow_wire_entry(entry: dict) -> Optional[dict]:
    """Reduce a normalized workflow entry to its pod wire form (enabled only)."""
    if not entry.get("enabled", True):
        return None
    if entry.get("github"):
        return {"github": entry["github"]}
    return {"url": entry.get("url", "")}


def build_user_presets_json(presets: Mapping[str, Any]) -> str:
    """Serialize presets to the version-tagged JSON document sent to the pod.

    Only *enabled* nodes/workflows are emitted, and launcher-local metadata
    (``name``/``note``/``enabled``) is stripped — the pod only needs the
    download links. Empty/absent presets yield ``""`` (callers then omit
    ``H3_USER_PRESETS_JSON``).
    """
    normalized = normalize_user_presets(presets)
    if not normalized:
        return ""
    wire: dict = {}
    for name, body in normalized.items():
        # Models keep only url + target on the wire (``name`` is local).
        models = [
            {"url": m["url"], "target": m.get("target", "")}
            for m in body.get("models", [])
        ]
        nodes = [e for e in (_node_wire_entry(n) for n in body.get("nodes", [])) if e]
        workflows = [
            e for e in (_workflow_wire_entry(w) for w in body.get("workflows", [])) if e
        ]
        if not (models or nodes or workflows):
            continue
        wire[name] = {"models": models, "nodes": nodes, "workflows": workflows}
    if not wire:
        return ""
    document = {"version": PRESET_JSON_VERSION, "presets": wire}
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def parse_user_presets_json(raw: str) -> dict:
    """Parse a wire-format document back into ``{name: {kind: [entry]}}``.

    Used by tests and (optionally) import paths. Returns ``{}`` on any
    malformed/unversioned input — never raises.
    """
    if not raw or not raw.strip():
        return {}
    try:
        document = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(document, dict):
        return {}
    presets = document.get("presets")
    if not isinstance(presets, dict):
        return {}
    return normalize_user_presets(presets)

"""Tests for the user-defined ComfyUI preset model and wire format."""

import json

from launcher.comfy_presets import (
    build_user_presets_json,
    migrate_legacy_presets,
    model_target_for_url,
    normalize_user_presets,
    parse_user_presets_json,
)
from launcher.config import load_config


def test_model_target_for_url_category_dir() -> None:
    url = "https://huggingface.co/Repo/resolve/main/diffusion_models/foo.safetensors"
    assert model_target_for_url("diffusion", url) == "diffusion_models/foo.safetensors"
    assert model_target_for_url("video_vae", url) == "vae/foo.safetensors"


def test_model_target_for_url_no_category() -> None:
    url = "https://huggingface.co/Repo/resolve/main/loras/x.safetensors"
    assert model_target_for_url("unknown", url) == "x.safetensors"


def test_normalize_user_presets_new_shape() -> None:
    presets = normalize_user_presets(
        {
            "My Preset": {
                "models": [{"url": "https://h/x.safetensors", "target": "loras/x.safetensors"}],
                "nodes": [{"url": "https://github.com/o/r.git", "post_install": "apt-get install -y ffmpeg"}],
                "workflows": [{"github": "o/r|main|wf"}, {"url": "https://x/w.json"}],
            }
        }
    )
    assert set(presets) == {"My Preset"}
    body = presets["My Preset"]
    assert body["models"] == [{"url": "https://h/x.safetensors", "target": "loras/x.safetensors", "name": ""}]
    assert body["nodes"] == [
        {"url": "https://github.com/o/r.git", "post_install": "apt-get install -y ffmpeg",
         "name": "", "note": "", "enabled": True}
    ]
    assert body["workflows"] == [
        {"github": "o/r|main|wf", "name": "", "note": "", "enabled": True},
        {"url": "https://x/w.json", "name": "", "note": "", "enabled": True},
    ]


def test_normalize_user_presets_drops_bad_entries() -> None:
    presets = normalize_user_presets(
        {
            "P": {
                "models": [{"url": ""}, "not-a-dict", {"url": "https://h/x", "target": "x"}],
                "nodes": [],
                "workflows": [{"github": ""}, {"url": ""}],
            },
            "Empty": {"models": [], "nodes": [], "workflows": []},
        }
    )
    assert set(presets) == {"P"}
    assert presets["P"]["models"] == [{"url": "https://h/x", "target": "x", "name": ""}]
    assert presets["P"]["workflows"] == []


def test_migrate_legacy_presets() -> None:
    migrated = migrate_legacy_presets(
        {"Favori": {"audio_vae": ["https://h/a.safetensors"], "diffusion": ["https://h/d.safetensors"]}}
    )
    assert set(migrated) == {"Favori"}
    urls = {m["url"] for m in migrated["Favori"]["models"]}
    assert urls == {"https://h/a.safetensors", "https://h/d.safetensors"}
    targets = {m["target"] for m in migrated["Favori"]["models"]}
    assert targets == {"vae/a.safetensors", "diffusion_models/d.safetensors"}


def test_migrate_legacy_presets_passes_new_shape_through() -> None:
    data = {"P": {"models": [{"url": "https://h/x", "target": "x"}]}}
    assert migrate_legacy_presets(data) == data


def test_json_roundtrip() -> None:
    presets = {
        "P": {
            "models": [{"url": "https://h/x.safetensors", "target": "loras/x.safetensors"}],
            "nodes": [{"url": "https://github.com/o/r.git", "post_install": "apt-get install -y ffmpeg"}],
            "workflows": [{"github": "o/r|main|wf"}],
        }
    }
    raw = build_user_presets_json(presets)
    document = json.loads(raw)
    assert document["version"] == 1
    assert parse_user_presets_json(raw) == normalize_user_presets(presets)


def test_build_filters_disabled_nodes_and_workflows() -> None:
    presets = {
        "P": {
            "models": [{"url": "https://h/x", "target": "loras/x.safetensors"}],
            "nodes": [
                {"url": "https://github.com/o/a.git", "enabled": True, "name": "A", "note": "keep"},
                {"url": "https://github.com/o/b.git", "enabled": False, "name": "B", "note": "drop"},
            ],
            "workflows": [
                {"github": "o/r|main|wf", "enabled": False},
                {"url": "https://x/w.json", "enabled": True},
            ],
        }
    }
    raw = build_user_presets_json(presets)
    document = json.loads(raw)
    # name/note/enabled are launcher-local: never in the wire, disabled dropped.
    assert document["presets"]["P"]["nodes"] == [
        {"url": "https://github.com/o/a.git", "post_install": ""}
    ]
    assert document["presets"]["P"]["workflows"] == [{"url": "https://x/w.json"}]
    # Round-trip through parse: only the enabled entries survive.
    parsed = parse_user_presets_json(raw)
    assert parsed["P"]["nodes"] == [
        {"url": "https://github.com/o/a.git", "post_install": "", "name": "", "note": "", "enabled": True}
    ]
    assert parsed["P"]["workflows"] == [
        {"url": "https://x/w.json", "name": "", "note": "", "enabled": True}
    ]


def test_build_empty_returns_empty_string() -> None:
    assert build_user_presets_json({}) == ""
    assert build_user_presets_json(None) == ""


def test_parse_invalid_returns_empty() -> None:
    assert parse_user_presets_json("not json") == {}
    assert parse_user_presets_json("") == {}
    assert parse_user_presets_json('{"version": 1}') == {}


def test_pod_env_omits_user_presets_json_when_empty() -> None:
    config = load_config({}, stack="comfy", comfy={"preset": "dasiwa_mmh3v12"})
    assert "H3_USER_PRESETS_JSON" not in config.resolved_comfy_pod_env()

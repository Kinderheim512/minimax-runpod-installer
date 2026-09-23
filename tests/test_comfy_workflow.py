"""Tests for the ComfyUI UI-to-API workflow converter.

Offline: the workflow files are the real ones from the repository
(``comfy/workflows`` and the ``dasiwa_mmh3v12`` preset), and the
``/object_info`` ground truth comes from :mod:`tests.comfy_fixtures` —
the standard classes use the pod-captured real specs, custom node-pack
classes are auto-derived from the files (named UI sockets plus
positional widget placeholders), so the widget classification and zip
validation are exercised with the true stored value counts.
"""

import json
from pathlib import Path

import pytest

from .comfy_fixtures import derive_object_info

from launcher.comfy_workflow import (
    WorkflowConversionError,
    convert_workflow,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
T2V = REPO_ROOT / "workflows" / "video_minimax_h3_t2v.json"
I2V = REPO_ROOT / "workflows" / "video_minimax_h3_i2v.json"
R2V = REPO_ROOT / "workflows" / "video_minimax_h3_r2v.json"
DASIWA = REPO_ROOT / "comfy" / "presets" / "dasiwa_mmh3v12" / "DasiwaMinimaxH3WorkflowsT2VA_cMMH3V13.json"

SUBGRAPH_ID = "4c314f31-ecda-4b08-ae98-faaba1bf613f"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _t2v_internal_ids(workflow: dict) -> set[str]:
    sub = workflow["definitions"]["subgraphs"][0]
    return {
        f"105-{n['id']}"
        for n in sub.get("nodes") or []
        if isinstance(n, dict) and int(n.get("mode", 0)) == 0
    }


def test_t2v_converts_exactly_the_active_nodes() -> None:
    workflow = _load(T2V)
    prompt = convert_workflow(workflow, derive_object_info(workflow))
    # the subgraph instance is expanded: no virtual subgraph node remains,
    # its internal nodes appear as "<instance>-<local>" ids
    assert SUBGRAPH_ID not in {e["class_type"] for e in prompt.values()}
    assert set(prompt) == {"92", "115"} | _t2v_internal_ids(workflow)
    assert prompt["92"]["class_type"] == "SaveVideo"
    # the instance's VIDEO output is re-pointed at the internal CreateVideo
    assert prompt["92"]["inputs"]["video"] == ["105-91", 0]


def test_t2v_subgraph_widget_values_are_positional() -> None:
    workflow = _load(T2V)
    prompt = convert_workflow(workflow, derive_object_info(workflow))
    wv = next(n for n in workflow["nodes"] if n["id"] == 105)["widgets_values"]
    # instance widgets feed the internal consumers:
    #  prompt/value_1(noise_seed...) -> 104, duration -> PrimitiveFloat 111,
    #  noise_seed -> RandomNoise 15, loader combos -> their loaders
    h3 = prompt["105-104"]["inputs"]
    assert h3["prompt"] == wv[0]
    assert isinstance(wv[0], str) and len(wv[0]) > 100
    assert prompt["105-111"]["inputs"]["value"] == wv[3] == 5
    assert prompt["105-15"]["inputs"]["noise_seed"] == wv[4] == 556589502035082
    assert prompt["105-6"]["inputs"]["unet_name"] == wv[5]
    assert prompt["105-13"]["inputs"]["clip_name"] == wv[6]
    assert prompt["105-11"]["inputs"]["vae_name"] == wv[7]
    assert prompt["105-24"]["inputs"]["vae_name"] == wv[8]


def test_t2v_resolution_selector_links_override_subgraph_width_height() -> None:
    workflow = _load(T2V)
    prompt = convert_workflow(workflow, derive_object_info(workflow))
    h3 = prompt["105-104"]["inputs"]
    assert h3["width"] == ["115", 0]
    assert h3["height"] == ["115", 1]
    assert "first_frame" not in h3
    assert "last_frame" not in h3


def test_t2v_explicit_width_height_override_beats_link() -> None:
    workflow = _load(T2V)
    prompt = convert_workflow(
        workflow,
        derive_object_info(workflow),
        overrides={"105": {"width": 608, "height": 352}},
    )
    h3 = prompt["105-104"]["inputs"]
    assert h3["width"] == 608
    assert h3["height"] == 352


def test_i2v_first_frame_is_wired_from_load_image() -> None:
    workflow = _load(I2V)
    prompt = convert_workflow(workflow, derive_object_info(workflow))
    assert prompt["114"]["class_type"] == "LoadImage"
    assert prompt["114"]["inputs"]["image"] == "transparent_rgb_gaming_mouse.png"
    h3 = prompt["105-104"]["inputs"]
    assert h3["first_frame"] == ["114", 0]
    assert h3["width"] == ["115", 0]
    assert h3["height"] == ["115", 1]


def test_i2v_synthetic_last_frame_node() -> None:
    workflow = _load(I2V)
    prompt = convert_workflow(
        workflow,
        derive_object_info(workflow),
        overrides={"105": {"last_frame": ["9002", 0]}},
        extra_nodes={
            "9002": {"class_type": "LoadImage", "inputs": {"image": "last.png"}},
        },
    )
    assert prompt["9002"]["inputs"]["image"] == "last.png"
    assert prompt["105-104"]["inputs"]["last_frame"] == ["9002", 0]


def test_r2v_explicit_graph_conversion() -> None:
    workflow = _load(R2V)
    prompt = convert_workflow(workflow, derive_object_info(workflow))
    # all non-note nodes
    expected = {
        str(n["id"]) for n in workflow["nodes"]
        if n["type"] not in ("MarkdownNote", "Note")
    }
    assert set(prompt) == expected
    inputs = prompt["136"]["inputs"]
    assert inputs["clip"] == ["128", 0]
    assert inputs["prompt"] == ["138", 0]
    assert inputs["width"] == ["115", 0]
    assert inputs["height"] == ["115", 1]
    assert inputs["length"] == ["131", 1]
    assert inputs["ref_images.ref_image_0"] == ["137", 0]
    assert inputs["ref_images.ref_image_1"] == ["139", 0]
    assert "ref_images.ref_image_2" not in inputs
    assert prompt["131"]["inputs"]["values.a"] == ["132", 0]
    assert prompt["132"]["inputs"]["value"] == 5
    assert prompt["129"]["inputs"]["noise_seed"] == 157368968253448


def test_r2v_ref_image_override() -> None:
    workflow = _load(R2V)
    prompt = convert_workflow(
        workflow,
        derive_object_info(workflow),
        overrides={"137": {"image": "new_ref.png"}},
    )
    assert prompt["137"]["inputs"]["image"] == "new_ref.png"


def test_widget_count_mismatch_is_an_explicit_error() -> None:
    workflow = _load(T2V)
    node = next(n for n in workflow["nodes"] if n["id"] == 105)
    node["widgets_values"] = node["widgets_values"][:-1]
    with pytest.raises(WorkflowConversionError, match="widget values"):
        convert_workflow(workflow, derive_object_info({**workflow}))


def test_dict_shaped_widgets_values_are_accepted() -> None:
    """ComfyUI stores ``widgets_values`` as a *mapping* for every VHS_* node.

    The converter only accepted a list, so a dict-shaped node was read as
    "0 stored widget values" and the whole conversion was refused with a
    message blaming the node's class — including for a workflow the launcher
    itself ships (``comfy/presets/muse_director_seedhunt``).
    """
    workflow = {
        "nodes": [
            {
                "id": 1,
                "type": "KSampler",
                "mode": 0,
                "inputs": [],
                "outputs": [],
                "widgets_values": {"seed": 42, "steps": 8},
            }
        ],
        "links": [],
    }
    object_info = {
        "KSampler": {
            "input": {
                "required": {
                    "seed": ["INT", {"default": 0}],
                    "steps": ["INT", {"default": 20}],
                }
            }
        }
    }
    prompt = convert_workflow(workflow, object_info)
    assert prompt["1"]["inputs"]["seed"] == 42
    assert prompt["1"]["inputs"]["steps"] == 8


def test_dict_shaped_widgets_values_still_report_a_shortfall() -> None:
    """A mapping missing a widget name is still refused, not guessed."""
    workflow = {
        "nodes": [
            {
                "id": 1,
                "type": "KSampler",
                "mode": 0,
                "inputs": [],
                "outputs": [],
                "widgets_values": {"seed": 42},
            }
        ],
        "links": [],
    }
    object_info = {
        "KSampler": {
            "input": {
                "required": {
                    "seed": ["INT", {"default": 0}],
                    "steps": ["INT", {"default": 20}],
                }
            }
        }
    }
    with pytest.raises(WorkflowConversionError, match="widget values"):
        convert_workflow(workflow, object_info)


def test_link_without_a_slot_is_an_explicit_error() -> None:
    """An object-form link may omit ``origin_slot``/``target_slot``.

    ``int(link[2])`` then raised a bare ``TypeError``, which escaped every
    caller catching ``WorkflowConversionError``.
    """
    from launcher.comfy_workflow import _link_slot

    with pytest.raises(WorkflowConversionError, match="slot"):
        _link_slot([1, 2, None, 3, 0, "IMAGE"], 2, 1)
    with pytest.raises(WorkflowConversionError, match="slot"):
        _link_slot([1, 2], 2, 1)
    assert _link_slot([1, 2, 3, 4, 5, "IMAGE"], 2, 1) == 3


def test_unknown_class_is_an_explicit_error() -> None:
    workflow = _load(T2V)
    workflow["nodes"].append(
        {
            "id": 999, "type": "TotallyMissingNode", "mode": 0,
            "inputs": [], "outputs": [], "widgets_values": [],
        }
    )
    object_info = derive_object_info(workflow)
    del object_info["TotallyMissingNode"]
    with pytest.raises(WorkflowConversionError, match="TotallyMissingNode"):
        convert_workflow(workflow, object_info)


def test_override_with_unknown_input_name_is_rejected() -> None:
    workflow = _load(T2V)
    object_info = derive_object_info(workflow)
    with pytest.raises(WorkflowConversionError, match="unknown input"):
        convert_workflow(
            workflow, object_info, overrides={"105": {"nope": 1}}
        )


def test_override_targeting_unknown_node_is_rejected() -> None:
    workflow = _load(T2V)
    object_info = derive_object_info(workflow)
    with pytest.raises(WorkflowConversionError, match="unknown node"):
        convert_workflow(workflow, object_info, overrides={"424242": {"w0": 1}})


def test_missing_class_in_object_info_is_an_explicit_error() -> None:
    workflow = _load(T2V)
    object_info = derive_object_info(workflow)
    del object_info["SaveVideo"]
    with pytest.raises(WorkflowConversionError, match="SaveVideo"):
        convert_workflow(workflow, object_info)


def test_bypassed_nodes_are_skipped() -> None:
    workflow = _load(T2V)
    node = next(n for n in workflow["nodes"] if n["id"] == 115)
    node["mode"] = 4
    object_info = derive_object_info(workflow)
    prompt = convert_workflow(workflow, object_info)
    assert "115" not in prompt
    # links from the bypassed node are deadened: no input dangles at it
    for entry in prompt.values():
        for value in entry["inputs"].values():
            assert not (
                isinstance(value, list) and len(value) == 2 and str(value[0]) == "115"
            )
    # the subgraph width/height are linked widgets: with the link source
    # bypassed they fall back to their stored widget values (frontend rule)
    wv = next(n for n in workflow["nodes"] if n["id"] == 105)["widgets_values"]
    h3 = prompt["105-104"]["inputs"]
    assert h3["width"] == wv[1]
    assert h3["height"] == wv[2]


def test_markdown_notes_are_never_converted() -> None:
    workflow = _load(T2V)
    object_info = derive_object_info(workflow)
    prompt = convert_workflow(workflow, object_info)
    for entry in prompt.values():
        assert entry["class_type"] not in ("MarkdownNote", "Note")

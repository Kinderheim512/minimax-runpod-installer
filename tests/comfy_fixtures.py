"""Shared /object_info fixtures for the ComfyUI workflow tests.

``derive_object_info`` returns the server's real class input specs for the
standard ComfyUI classes the repository workflows use (captured from a
live pod's ``/object_info``, long combo option lists truncated in
``comfy_fixtures_real_object_info.json``), and falls back to
auto-deriving a class from the workflow file itself (named UI sockets plus
positional ``w0..wN`` widget placeholders) for custom node-pack classes
that do not exist on the stock server.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REAL_OBJECT_INFO: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parent / "comfy_fixtures_real_object_info.json").read_text(
        encoding="utf-8"
    )
)


def derive_object_info(workflow: dict) -> dict:
    classes: dict[str, dict] = {}
    nodes = list(workflow.get("nodes") or [])
    for sub in (workflow.get("definitions") or {}).get("subgraphs") or []:
        nodes.extend(sub.get("nodes") or [])
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") is None:
            continue
        ctype = node["type"]
        entry = classes.setdefault(ctype, {"sockets": set(), "widgets": 0})
        for ui_in in node.get("inputs") or []:
            if not isinstance(ui_in, dict):
                continue
            if "widget" not in ui_in and ui_in.get("name"):
                entry["sockets"].add(ui_in["name"])
        entry["widgets"] = max(entry["widgets"], len(node.get("widgets_values") or []))

    object_info: dict[str, dict] = {}
    for ctype in classes:
        if ctype in _REAL_OBJECT_INFO:
            object_info[ctype] = _REAL_OBJECT_INFO[ctype]
            continue
        entry = classes[ctype]
        required: dict[str, Any] = {}
        for i, socket in enumerate(sorted(entry["sockets"])):
            required[socket] = [f"TYPE_{i}"]
        for i in range(entry["widgets"]):
            required[f"w{i}"] = ["STRING", {"multiline": True}]
        object_info[ctype] = {"input": {"required": required, "optional": {}}}
    return object_info

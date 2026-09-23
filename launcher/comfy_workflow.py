"""Conversion of ComfyUI UI workflow JSON into an API prompt.

ComfyUI's ``/prompt`` endpoint does not accept the UI (``workflow.json``)
format: it takes a flat ``{node_id: {"class_type": ..., "inputs": {...}}}``
mapping in which links are encoded as ``[origin_node_id, origin_slot]``
pairs. This module performs that conversion offline, using the workflow's
own serialization plus the server's ``/object_info`` as the ground truth
for each class's input names and order.

Rules:

* every enabled (``mode == 0``) node is converted; frontend-only node types
  (``MarkdownNote``, ``Note``) are dropped — the server has no such classes;
* a regular node's socket inputs take their link values from the graph's
  link table; its widget inputs take values from the positional
  ``widgets_values`` array. A node storing more values than the class has
  widget inputs (extra frontend-only widgets, e.g. LoadImage's output
  selector) has the surplus ignored; storing fewer is an explicit error,
  never a silent guess. Which class inputs are widgets: the node's own UI
  list is authoritative where it lists the input; otherwise the
  ``/object_info`` spec decides — ``/object_info`` does not mark widgets
  explicitly, so a bare type (with or without an options dict that carries
  no widget keys) is a socket, while a combo (nested choice list or
  several parallel strings) or widget options (``default``/``min``/
  ``multiline``/...) mark a widget;
* dynamic input groups (``COMFY_AUTOGROW_V3``: ``ref_images``, ``values``
  ...) are not single widget slots: each member is an independent socket
  named ``"<group>.<member>"`` (``ref_images.ref_image_0``, ``values.a``)
  — exactly the name the server's ``/prompt`` validation expects;
* links whose origin node is not converted (bypassed, frontend-only, or
  absent from the graph) are deadened: consumers leave the input
  unconnected instead of dangling at an id that is not in the prompt;
* a subgraph *instance* (a node whose type is a subgraph definition id) is
  EXPANDED into its internal nodes — the server has no class for a
  subgraph id (subgraphs are a frontend-only concept; the frontend itself
  flattens them when building the API prompt). Each internal node gets the
  global id ``"<instance_id>-<local_id>"``; the instance's exposed inputs
  (widgets and/or main-graph links) feed the internal consumer nodes via
  the definition's virtual ``inputNode``, and main-graph links wired to an
  instance output are re-pointed at the internal producer behind the
  virtual ``outputNode``;
* ``overrides`` (``{node_id: {input_name: value}}``) are applied last, with
  every input name validated against the node's known inputs — overrides
  beat links (that is how an explicit width/height wins over the
  ResolutionSelector wire). An override keyed by a subgraph instance id is
  re-targeted at the internal node that consumes the named exposed input.

Only the standard library is used; the module is fully unit-testable with
no ComfyUI, network, or GPU.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

# Node types that exist only in the ComfyUI frontend: the server has no
# class for them, and sending one would make ``/prompt`` reject the whole
# graph.
FRONTEND_ONLY_TYPES = frozenset({"MarkdownNote", "Note"})


class WorkflowConversionError(ValueError):
    """The workflow cannot be converted (bounded, actionable detail)."""


def _class_input_names(class_def: Mapping[str, Any]) -> list[str]:
    """Ordered input names (required then optional) from an /object_info entry."""
    names: list[str] = []
    inputs = class_def.get("input") or {}
    for section in ("required", "optional"):
        block = inputs.get(section) or {}
        if isinstance(block, Mapping):
            names.extend(str(k) for k in block.keys())
    return names


def _link_table(links: Any) -> dict[Any, list[Any]]:
    """Canonical link table: ``{link_id: [id, origin_id, origin_slot,
    target_id, target_slot, type]}``.

    Main-file links are 6-element arrays; subgraph-definition links are
    objects with the same fields — both are normalized to mutable lists.
    """
    table: dict[Any, list[Any]] = {}
    if not isinstance(links, list):
        return table
    for link in links:
        if isinstance(link, Mapping):
            if link.get("id") is None:
                continue
            table[link["id"]] = [
                link.get("id"),
                link.get("origin_id"),
                link.get("origin_slot"),
                link.get("target_id"),
                link.get("target_slot"),
                link.get("type"),
            ]
        elif isinstance(link, list) and len(link) >= 6:
            table[link[0]] = list(link)
    return table


def _widget_values(raw_values: Any, widget_names: Sequence[str]) -> list:
    """Stored widget values for a node, as a list aligned with *widget_names*.

    ComfyUI stores ``widgets_values`` either as a plain list (most nodes) or as
    a **mapping** keyed by input name (every ``VHS_*`` node, whose
    ``videopreview`` widget makes the UI serialize a dict). Treating a mapping
    as "no values at all" made the converter refuse a workflow the launcher
    itself ships, with a message blaming the node's class.
    """
    if isinstance(raw_values, Mapping):
        return [raw_values[name] for name in widget_names if name in raw_values]
    if isinstance(raw_values, (list, tuple)):
        return list(raw_values)
    return []


def _link_slot(link: Any, index: int, link_id: Any) -> int:
    """Return the integer slot at *index* of a link row, or refuse.

    ``_link_table`` fills the slot columns from ``origin_slot``/``target_slot``,
    which an object-form link may omit: the bare ``int(link[2])`` then raised a
    ``TypeError`` that escaped every caller catching
    ``WorkflowConversionError``.
    """
    try:
        value = link[index]
    except (IndexError, TypeError):
        value = None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkflowConversionError(
            f"link {link_id!r} has no usable slot at position {index}"
        )
    return int(value)


def _known_input_names(
    class_type: str,
    subgraphs: Mapping[str, Mapping[str, Any]],
    object_info: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """All input names a node of *class_type* can legally receive
    (declared inputs plus every dynamic-group member name)."""
    if class_type in subgraphs:
        return [
            str(i.get("name"))
            for i in (subgraphs[class_type].get("inputs") or [])
            if isinstance(i, Mapping) and i.get("name") is not None
        ]
    if class_type in object_info:
        class_def = object_info[class_type]
        names = _class_input_names(class_def)
        block = class_def.get("input") or {}
        for section in ("required", "optional"):
            for name, spec in (block.get(section) or {}).items():
                names.extend(_dynamic_member_names(str(name), spec))
        return names
    return []


# Option keys that only ever appear on widget inputs. /object_info does
# not mark widgets explicitly: a socket can be a bare type string or a
# type with an options dict that carries only display hints (``tooltip``,
# ``advanced``); widget inputs carry these keys (or a combo shape).
_WIDGET_OPTION_KEYS = frozenset(
    {
        "default",
        "min",
        "max",
        "step",
        "multiline",
        "multiselect",
        "options",
        "image_upload",
        "control_after_generate",
        "dynamicPrompts",
        "combo",
        "display",
        "display_name",
    }
)


def _is_widget_spec(spec: Any) -> bool:
    """Classify an /object_info type spec of an input the node's UI does
    not list.

    A socket is a single bare type string, with or without an options
    dict that carries no widget keys (``["MODEL", {}]``,
    ``["VIDEO", {"tooltip": ...}]``); a widget is a combo (a nested
    choice list or several parallel strings) or carries widget options
    (``["INT", {"min": 0}]`` / ``["STRING", {"multiline": true}]``).
    """
    if not isinstance(spec, list):
        return True
    if any(isinstance(item, list) for item in spec):
        return True
    if len([item for item in spec if isinstance(item, str)]) > 1:
        return True
    for item in spec:
        if isinstance(item, Mapping) and item and (_WIDGET_OPTION_KEYS & item.keys()):
            return True
    return False


def _is_dynamic_group_spec(spec: Any) -> bool:
    """True for an autogrouped input (``COMFY_AUTOGROW_V3``): one declared
    input that expands into several member inputs on the server."""
    if not isinstance(spec, list):
        return False
    return any(
        isinstance(item, Mapping) and isinstance(item.get("template"), Mapping)
        for item in spec
    )


def _dynamic_member_names(input_id: str, spec: Any) -> list[str]:
    """The ``/prompt`` names a dynamic group's members are submitted under.

    The server finalizes each member as ``"<group>.<member>"`` — the same
    composite names the frontend shows on the node — where the member is
    ``<prefix><index>`` for a prefix template or a named entry for a
    names template.
    """
    if not isinstance(spec, list):
        return []
    for item in spec:
        if not (isinstance(item, Mapping) and isinstance(item.get("template"), Mapping)):
            continue
        template = item["template"]
        if isinstance(template.get("names"), list):
            return [f"{input_id}.{str(name)}" for name in template["names"]]
        prefix = template.get("prefix")
        maximum = template.get("max")
        if isinstance(prefix, str) and isinstance(maximum, int):
            return [f"{input_id}.{prefix}{i}" for i in range(maximum)]
        return []
    return []


def _convert_node_inputs(
    node: Mapping[str, Any],
    class_def: Mapping[str, Any],
    links: Mapping[Any, list[Any]],
    resolve: Any,
) -> dict[str, Any]:
    """Build the input map of one node.

    Socket inputs take the value *resolve*(link) returns for their link
    (``None`` = leave the input unconnected); widget inputs take values
    from ``widgets_values`` positionally. The node's own UI list is
    authoritative for the inputs it lists (widget marker or not); inputs
    the UI omits are classified by their ``/object_info`` type spec
    (:func:`_is_widget_spec`); dynamic groups are not widget slots at all
    (their members come in as socket-like UI entries). A stored value
    surplus (frontend-only extra widgets) is ignored; a shortfall is an
    explicit error. A resolved link always beats the stored widget value.
    """
    inputs: dict[str, Any] = {}
    listed: dict[str, bool] = {}
    for ui_in in node.get("inputs") or []:
        if not isinstance(ui_in, Mapping):
            continue
        name = str(ui_in.get("name"))
        if name:
            listed[name] = "widget" in ui_in
        link_id = ui_in.get("link")
        if link_id is not None:
            link = links.get(link_id)
            if link is None:
                raise WorkflowConversionError(
                    f"node {node.get('id')} input {ui_in.get('name')!r} "
                    f"references missing link {link_id}"
                )
            value = resolve(link, node)
            if value is not None:
                inputs[str(ui_in.get("name"))] = value

    class_inputs_block = class_def.get("input") or {}
    required = class_inputs_block.get("required") or {}
    optional = class_inputs_block.get("optional") or {}
    widget_names: list[str] = []
    for name in _class_input_names(class_def):
        spec = required.get(name)
        if spec is None:
            spec = optional.get(name)
        if _is_dynamic_group_spec(spec):
            continue
        if name in listed:
            is_widget = listed[name]
        else:
            is_widget = _is_widget_spec(spec)
        if is_widget:
            widget_names.append(name)
    raw_values = node.get("widgets_values")
    values = _widget_values(raw_values, widget_names)
    if len(values) < len(widget_names):
        raise WorkflowConversionError(
            f"class {node.get('type')!r} (node {node.get('id')}): "
            f"{len(values)} stored widget values vs {len(widget_names)} "
            f"widget inputs — refusing to guess the mapping"
        )
    for name, value in zip(widget_names, values):
        if name not in inputs:
            inputs[name] = value
    return inputs


def _virtual_node_id(raw: Any) -> Optional[Any]:
    if isinstance(raw, Mapping):
        return raw.get("id")
    return raw


def _expand_subgraph_instance(
    node: Mapping[str, Any],
    sub: Mapping[str, Any],
    main_links: Mapping[Any, list[Any]],
    object_info: Mapping[str, Mapping[str, Any]],
    dead_link_ids: set[Any],
) -> tuple[
    dict[str, dict[str, Any]], dict[str, list[tuple[str, str]]]
]:
    """Flatten one subgraph instance into its internal nodes.

    Returns ``(entries, input_map)`` where ``entries`` maps each active
    internal node's global id (``"<instance_id>-<local_id>"``) to its
    converted entry, and ``input_map`` maps each exposed input NAME to the
    list of ``(global_id, internal_input_name)`` pairs of the internal
    nodes that consume it (used to re-target overrides). Main-graph links
    wired to the instance's outputs are re-pointed at the internal
    producers (mutating *main_links* in place); a producer that is not
    converted deadens the link (recorded in *dead_link_ids*).
    """
    instance_id = node.get("id")
    sub_nodes = [
        n for n in sub.get("nodes") or []
        if isinstance(n, Mapping) and n.get("id") is not None
    ]
    sub_links = _link_table(sub.get("links"))
    input_node_id = _virtual_node_id(sub.get("inputNode"))
    output_node_id = _virtual_node_id(sub.get("outputNode"))
    sub_inputs = [i for i in (sub.get("inputs") or []) if isinstance(i, Mapping)]
    sub_outputs = [o for o in (sub.get("outputs") or []) if isinstance(o, Mapping)]
    sub_input_index = {
        str(i.get("name")): idx for idx, i in enumerate(sub_inputs)
        if i.get("name") is not None
    }
    sub_output_index = {
        str(o.get("name")): idx for idx, o in enumerate(sub_outputs)
        if o.get("name") is not None
    }
    instance_inputs = [
        i for i in (node.get("inputs") or []) if isinstance(i, Mapping)
    ]
    instance_outputs = [
        o for o in (node.get("outputs") or []) if isinstance(o, Mapping)
    ]
    instance_input_names = [
        str(i.get("name")) for i in instance_inputs if i.get("name") is not None
    ]
    instance_output_names = [
        str(o.get("name")) for o in instance_outputs if o.get("name") is not None
    ]

    # 1. Values of the exposed inputs: widget values first (the instance's
    #    own ``widgets_values`` lines up with the exposed inputs that are
    #    not pure sockets — an unexposed subgraph input is a widget, an
    #    exposed one without a widget marker is a socket), then main-graph
    #    links (which override a stored widget value for a linked socket).
    exposed = {
        str(i.get("name")): i
        for i in instance_inputs
        if i.get("name") is not None
    }
    widget_idxs: list[int] = []
    for idx, sub_input in enumerate(sub_inputs):
        entry = exposed.get(str(sub_input.get("name")))
        is_widget = ("widget" in entry) if entry is not None else True
        if is_widget:
            widget_idxs.append(idx)
    raw_values = node.get("widgets_values")
    values = _widget_values(
        raw_values, [str(i.get("name")) for i in sub_inputs]
    )
    if len(values) != len(widget_idxs):
        raise WorkflowConversionError(
            f"subgraph {str(sub.get('name'))!r} (node {instance_id}): "
            f"{len(values)} stored widget values vs {len(widget_idxs)} "
            f"widget inputs — refusing to guess the mapping"
        )
    input_value: dict[int, Any] = {}
    for pos, idx in enumerate(widget_idxs):
        input_value[idx] = values[pos]
    for link in main_links.values():
        if link[3] != instance_id or link[0] in dead_link_ids:
            continue
        slot = _link_slot(link, 4, link[0])
        if 0 <= slot < len(instance_inputs):
            name = str(instance_inputs[slot].get("name"))
            idx = sub_input_index.get(name)
            if idx is not None:
                input_value[idx] = [str(link[1]), _link_slot(link, 2, link[0])]

    # 2. Internal producers of the exposed outputs (links into the
    #    virtual ``outputNode``).
    output_producer: dict[int, tuple[Any, int]] = {}
    for idx, out in enumerate(sub_outputs):
        for link_id in out.get("linkIds") or []:
            link = sub_links.get(link_id)
            if link is None or link[3] != output_node_id:
                continue
            output_producer[idx] = (link[1], _link_slot(link, 2, link[0]))
            break

    # 3. Internal consumers of the exposed inputs (links out of the
    #    virtual ``inputNode``) — an exposed input may feed several.
    input_consumer: dict[int, list[tuple[Any, int]]] = {}
    for idx, inp in enumerate(sub_inputs):
        for link_id in inp.get("linkIds") or []:
            link = sub_links.get(link_id)
            if link is None or link[1] != input_node_id:
                continue
            input_consumer.setdefault(idx, []).append((link[3], _link_slot(link, 4, link[0])))

    active = {
        n["id"]: n
        for n in sub_nodes
        if int(n.get("mode", 0)) == 0
        and n.get("type") not in FRONTEND_ONLY_TYPES
    }
    gids = {lid: f"{instance_id}-{lid}" for lid in active}

    def resolve(link: list[Any], in_node: Mapping[str, Any]) -> Optional[Any]:
        origin = link[1]
        if origin == input_node_id:
            idx = _link_slot(link, 2, link[0])
            if idx not in input_value:
                return None
            return input_value[idx]
        if origin == output_node_id:
            idx = _link_slot(link, 2, link[0])
            producer = output_producer.get(idx)
            if producer is None or producer[0] not in gids:
                return None
            return [gids[producer[0]], producer[1]]
        if origin in gids:
            return [gids[origin], _link_slot(link, 2, link[0])]
        # producer is bypassed or absent: leave the input unconnected
        return None

    entries: dict[str, dict[str, Any]] = {}
    for lid, in_node in active.items():
        ctype = in_node.get("type")
        if ctype not in object_info:
            raise WorkflowConversionError(
                f"class {ctype!r} (subgraph {str(sub.get('name'))!r}, "
                f"node {lid}) is not available on the ComfyUI server — a "
                "custom node pack may be missing"
            )
        inputs = _convert_node_inputs(in_node, object_info[ctype], sub_links, resolve)
        entries[gids[lid]] = {"class_type": ctype, "inputs": inputs}

    # 4. Re-point main-graph links that originate from this instance at
    #    the internal producers of the corresponding exposed outputs; a
    #    producer that is not converted (bypassed) deadens the link so
    #    consumers leave the input unconnected instead of dangling.
    for link in main_links.values():
        if link[1] != instance_id:
            continue
        slot = _link_slot(link, 2, link[0])
        if 0 <= slot < len(instance_outputs):
            name = str(instance_outputs[slot].get("name"))
            idx = sub_output_index.get(name)
            producer = output_producer.get(idx) if idx is not None else None
            if producer is not None and producer[0] in gids:
                link[1] = gids[producer[0]]
                link[2] = producer[1]
            else:
                dead_link_ids.add(link[0])

    # 5. Override re-targeting map: exposed input name -> every
    #    (global id, internal input name) pair of consuming internal
    #    nodes (an exposed input may feed several).
    input_map: dict[str, list[tuple[str, str]]] = {}
    for idx, inp in enumerate(sub_inputs):
        name = str(inp.get("name"))
        for consumer in input_consumer.get(idx) or []:
            if consumer[0] not in gids:
                continue
            consumer_node = active[consumer[0]]
            in_list = [
                i
                for i in (consumer_node.get("inputs") or [])
                if isinstance(i, Mapping)
            ]
            if 0 <= consumer[1] < len(in_list):
                iname = in_list[consumer[1]].get("name")
                if iname is not None:
                    input_map.setdefault(name, []).append(
                        (gids[consumer[0]], str(iname))
                    )

    return entries, input_map


def convert_workflow(
    workflow: Mapping[str, Any],
    object_info: Mapping[str, Mapping[str, Any]],
    overrides: Optional[Mapping[str, Mapping[str, Any]]] = None,
    extra_nodes: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> dict[str, dict[str, Any]]:
    """Convert a UI workflow into the ``/prompt`` API mapping.

    ``object_info`` is the server's ``/object_info`` response (the ground
    truth for class input names); ``overrides`` applies user parameter
    values (validated against each node's known inputs, links included);
    ``extra_nodes`` injects new nodes (e.g. a synthetic ``LoadImage`` for
    a last-frame input the stored workflow left unconnected).
    """
    if not isinstance(workflow, Mapping):
        raise WorkflowConversionError("workflow is not a mapping")
    definitions = workflow.get("definitions") or {}
    subgraphs: dict[str, Mapping[str, Any]] = {}
    for entry in definitions.get("subgraphs") or []:
        if isinstance(entry, Mapping) and entry.get("id"):
            subgraphs[str(entry["id"])] = entry
    main_links = _link_table(workflow.get("links"))

    # Links that must not be followed: they originate from a node that is
    # not converted (bypassed, frontend-only, or absent from the graph).
    # Following one would dangle the consumer's input at an id absent from
    # the prompt. Expansion deads more (an instance output whose internal
    # producer is bypassed).
    active_ids: set[str] = set()
    for node in workflow.get("nodes") or []:
        if not isinstance(node, Mapping) or node.get("id") is None:
            continue
        if int(node.get("mode", 0)) == 0 and node.get("type") not in FRONTEND_ONLY_TYPES:
            active_ids.add(str(node.get("id")))
    dead_link_ids: set[Any] = set()
    for link in main_links.values():
        if str(link[1]) not in active_ids:
            dead_link_ids.add(link[0])

    result: dict[str, dict[str, Any]] = {}
    instance_sub: dict[str, Mapping[str, Any]] = {}
    instance_input_map: dict[str, dict[str, list[tuple[str, str]]]] = {}

    # Pass 1 — expand subgraph instances. An instance is expanded only
    # once every instance that feeds it is already expanded, so the
    # captured input values always reference final (global) ids.
    instances = [
        node
        for node in workflow.get("nodes") or []
        if isinstance(node, Mapping)
        and node.get("id") is not None
        and int(node.get("mode", 0)) == 0
        and node.get("type") in subgraphs
    ]
    remaining = list(instances)
    while remaining:
        progressed = False
        for node in list(remaining):
            pending = any(
                link[3] == node.get("id")
                and link[1] in {i.get("id") for i in remaining if i is not node}
                for link in main_links.values()
            )
            if pending:
                continue
            entries, input_map = _expand_subgraph_instance(
                node,
                subgraphs[node.get("type")],
                main_links,
                object_info,
                dead_link_ids,
            )
            result.update(entries)
            instance_sub[str(node["id"])] = subgraphs[node.get("type")]
            instance_input_map[str(node["id"])] = input_map
            remaining.remove(node)
            progressed = True
        if not progressed:
            raise WorkflowConversionError(
                "circular subgraph instance wiring — cannot expand"
            )

    # Pass 2 — regular main-graph nodes (links are fully resolved now).
    for node in workflow.get("nodes") or []:
        if not isinstance(node, Mapping) or node.get("id") is None:
            continue
        if int(node.get("mode", 0)) != 0:
            continue
        class_type = node.get("type")
        if not class_type or class_type in subgraphs:
            continue
        if class_type in FRONTEND_ONLY_TYPES:
            continue
        if class_type in object_info:
            def resolve_main(link: list[Any], in_node: Mapping[str, Any]) -> Optional[Any]:
                if link[0] in dead_link_ids:
                    return None
                return [str(link[1]), _link_slot(link, 2, link[0])]

            result[str(node["id"])] = {
                "class_type": class_type,
                "inputs": _convert_node_inputs(
                    node, object_info[class_type], main_links, resolve_main
                ),
            }
        else:
            raise WorkflowConversionError(
                f"class {class_type!r} (node {node.get('id')}) is not available "
                "on the ComfyUI server — a custom node pack may be missing"
            )

    for raw_id, extra in (extra_nodes or {}).items():
        node_id = str(raw_id)
        if not isinstance(extra, Mapping) or extra.get("class_type") is None:
            raise WorkflowConversionError(f"extra node {node_id} is malformed")
        if node_id in result:
            raise WorkflowConversionError(
                f"extra node id {node_id} collides with an existing node"
            )
        class_type = str(extra["class_type"])
        if class_type not in object_info:
            raise WorkflowConversionError(
                f"extra node class {class_type!r} is not available on the server"
            )
        result[node_id] = {
            "class_type": class_type,
            "inputs": {str(k): v for k, v in (extra.get("inputs") or {}).items()},
        }

    for raw_id, mapping in (overrides or {}).items():
        node_id = str(raw_id)
        if not isinstance(mapping, Mapping):
            raise WorkflowConversionError(f"override for node {node_id} is malformed")
        entry = result.get(node_id)
        if entry is not None:
            known = _known_input_names(entry["class_type"], subgraphs, object_info)
            for name, value in mapping.items():
                if str(name) not in known:
                    raise WorkflowConversionError(
                        f"unknown input {name!r} for {entry['class_type']} "
                        f"(node {node_id}); known: {', '.join(known)}"
                    )
                entry["inputs"][str(name)] = value
            continue
        input_map = instance_input_map.get(node_id)
        if input_map is None:
            raise WorkflowConversionError(
                f"override targets unknown node {node_id}"
            )
        sub = instance_sub[node_id]
        exposed_names = [
            str(i.get("name"))
            for i in (sub.get("inputs") or [])
            if isinstance(i, Mapping) and i.get("name") is not None
        ]
        for name, value in mapping.items():
            targets = input_map.get(str(name))
            if name in exposed_names and not targets:
                raise WorkflowConversionError(
                    f"input {name!r} (instance {node_id}) has no active consumer "
                    "(all consuming internal nodes are bypassed)"
                )
            if targets is None:
                raise WorkflowConversionError(
                    f"unknown input {name!r} for subgraph instance {node_id}; "
                    f"known: {', '.join(exposed_names)}"
                )
            for target in targets:
                target_entry = result.get(target[0])
                if target_entry is None:
                    raise WorkflowConversionError(
                        f"subgraph input {name!r} (instance {node_id}) has no "
                        "converting consumer node"
                    )
                target_entry["inputs"][target[1]] = value

    return result

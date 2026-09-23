"""Automatic collection of finished ComfyUI generations (launcher side).

ComfyUI keeps one history entry per submitted prompt. An entry appears as
soon as the prompt *starts* executing (``status.completed`` false) and is
finalized when it ends — with its outputs, and with the full API prompt that
was executed. This module turns that history into a local mirror: every
finished generation is downloaded into a folder of the operator's choosing
(``~/Downloads`` by default) next to a plain-text sidecar carrying the prompt,
the parameters and the resources the generation used.

Two layers:

* **Pure helpers** (:func:`extract_generation_fields`,
  :func:`build_sidecar_text`, :func:`is_terminal_history_entry`,
  :func:`new_terminal_entries`, :func:`collect_generation`) — no network, no
  threads, unit-tested directly;
* :class:`ComfyOutputWatcher` — the polling thread the GUI runs while the
  ComfyUI stack is up. It never touches Tk (notifications and the pod
  termination are reported back through callbacks) and never raises: every
  failure is logged and retried on the next tick.

The pod is only ever reached through the local SSH tunnel (ComfyUI's HTTP API
and its ``/view`` endpoint), exactly like the manual download path.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import base64
import json
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import logging as launcher_logging
from .comfy_ops import ComfyOps, ComfyOpsError
from .health import check_comfy
from .pod_registry import default_home

logger = launcher_logging.get_logger("minimax-launcher.comfy_watch")

#: Where collected generations land when the operator has not chosen a folder.
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads"
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_RETRY_SECONDS = 30.0
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
#: How long the ComfyUI queue must stay EMPTY before "stop the pod when the
#: generation finishes" actually stops it. A queue that momentarily reads empty
#: (the prompt just completed, the next one is being submitted) must not cost
#: the user the rest of their batch. Overridable per watcher and through the
#: ``terminate_settle_seconds`` setting.
DEFAULT_QUEUE_SETTLE_SECONDS = 60.0

#: Local journal mapping a ``prompt_id`` to the workflow it was submitted from
#: (ComfyUI's history does not carry the workflow *name*). Written by
#: ``forge_comfy_generate``, read back when the sidecar is written.
JOURNAL_FILENAME = "comfy-prompts.json"
JOURNAL_LIMIT = 500

#: Node classes that carry the MiniMax H3 text/geometry inputs.
_H3_CLASSES = ("MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo")
_H3_LOWER = tuple(name.lower() for name in _H3_CLASSES)

#: Input names that hold a text prompt (the H3 nodes take ``prompt``; the
#: generic encoders take ``text``; a few packs use ``*_prompt``/``*_text``).
_PROMPT_KEYS = (
    "prompt",
    "text",
    "positive",
    "positive_prompt",
    "text_g",
    "text_l",
)
_NEGATIVE_KEYS = ("negative", "negative_prompt", "neg", "neg_text")

#: Loader input -> human label for the "resources" section.
_LOADER_FIELDS: dict[str, str] = {
    "unet_name": "UNet",
    "ckpt_name": "Checkpoint",
    "clip_name": "CLIP",
    "clip_name1": "CLIP",
    "clip_name2": "CLIP",
    "vae_name": "VAE",
    "model_name": "Model",
    "control_net_name": "ControlNet",
}

_LORA_CLASSES = ("LoraLoader", "LoraLoaderModelOnly")

_journal_lock = threading.Lock()


# ---------------------------------------------------------------------------
# History parsing (pure)
# ---------------------------------------------------------------------------


def _nodes(prompt_graph: Any) -> list[tuple[str, Mapping[str, Any]]]:
    """``[(node_id, node)]`` for a ComfyUI API prompt (order preserved)."""
    if not isinstance(prompt_graph, Mapping):
        return []
    out: list[tuple[str, Mapping[str, Any]]] = []
    for node_id, node in prompt_graph.items():
        if isinstance(node, Mapping):
            out.append((str(node_id), node))
    return out


def _inputs(node: Mapping[str, Any]) -> Mapping[str, Any]:
    inputs = node.get("inputs")
    return inputs if isinstance(inputs, Mapping) else {}


def _class_type(node: Mapping[str, Any]) -> str:
    return str(node.get("class_type") or "")


def _is_link(value: Any) -> bool:
    """True for a ComfyUI link: ``[node_id, output_slot]``."""
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and not isinstance(value[0], (list, tuple, dict))
    )


def _first_int(
    nodes: Sequence[tuple[str, Mapping[str, Any]]], names: Sequence[str]
) -> Optional[int]:
    for _node_id, node in nodes:
        inputs = _inputs(node)
        for name in names:
            value = inputs.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
    return None


def _first_float(
    nodes: Sequence[tuple[str, Mapping[str, Any]]], names: Sequence[str]
) -> Optional[float]:
    for _node_id, node in nodes:
        inputs = _inputs(node)
        for name in names:
            value = inputs.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return float(value)
    return None


#: Input names a value-carrying leaf node exposes (Primitive*, encoders).
_VALUE_KEYS = ("value", "text", "string", "prompt")


def _linked_literal(
    by_id: Mapping[str, Mapping[str, Any]],
    value: Any,
    *,
    keys: Sequence[str],
    kinds: tuple,
    depth: int = 3,
) -> Any:
    """Follow *value* through links until it reaches a literal of *kinds*.

    The standard H3 templates hide the interesting values behind small
    helper nodes (the prompt is a ``PrimitiveStringMultiline``, the duration
    a ``PrimitiveFloat`` fed through a ``ComfyMathExpression``), so a link is
    resolved one node at a time — bounded by *depth*, and only through nodes
    that expose a matching literal or a single outgoing input.
    """
    while depth > 0 and _is_link(value):
        node = by_id.get(str(value[0]))
        if node is None:
            return None
        inputs = _inputs(node)
        for key in keys:
            candidate = inputs.get(key)
            if isinstance(candidate, bool):
                continue
            if isinstance(candidate, kinds) and candidate not in ("", None):
                return candidate
        value = next(
            (item for item in inputs.values() if _is_link(item)), None
        )
        if value is None:
            return None
        depth -= 1
    return None


def _literal_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _snapped_frames(seconds: float) -> int:
    """Seconds -> frame count on the H3 ``17k+5`` grid at 24 fps.

    The same snapping the t2v/i2v templates' ``ComfyMathExpression`` applies
    (and that :mod:`launcher.tools.builtin.forge_comfy_generate` applies when
    a workflow takes frames): used to report the frame count of a generation
    whose graph computes it at runtime.
    """
    n = max(5, int(round(seconds * 24)))
    return 17 * math.ceil((n - 5) / 17) + 5


def _resolution_hint(nodes: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
    """``"16:9 (Widescreen), 0.4 MP"`` from a ``ResolutionSelector`` node.

    The H3 templates wire the H3 node's ``width``/``height`` to that node, so
    the pixel size never appears in the graph — but its own settings do.
    """
    for _node_id, node in nodes:
        if _class_type(node) != "ResolutionSelector":
            continue
        inputs = _inputs(node)
        parts: list[str] = []
        ratio = inputs.get("aspect_ratio")
        if isinstance(ratio, str) and ratio.strip():
            parts.append(ratio.strip())
        megapixels = inputs.get("megapixels")
        if isinstance(megapixels, (int, float)) and not isinstance(megapixels, bool):
            parts.append(f"{megapixels:g} MP")
        multiple = inputs.get("multiple")
        if isinstance(multiple, int) and not isinstance(multiple, bool):
            parts.append(f"multiple {multiple}")
        if parts:
            return ", ".join(parts)
    return ""


def extract_generation_fields(prompt_graph: Any) -> dict[str, Any]:
    """Best-effort view of one API prompt: prompt text, params, resources.

    Reads only well-known input names and follows links to the literal they
    carry, so an unknown/updated workflow still yields whatever it exposes
    instead of failing. Missing values stay ``None``/empty and are rendered
    as "—" by :func:`build_sidecar_text`.
    """
    nodes = _nodes(prompt_graph)
    by_id = {node_id: node for node_id, node in nodes}
    h3_nodes = [n for n in nodes if _class_type(n[1]) in _H3_CLASSES]
    ordered = h3_nodes + [n for n in nodes if n not in h3_nodes]

    positive = ""
    negative = ""
    for _node_id, node in ordered:
        ctype = _class_type(node).lower()
        for name, value in _inputs(node).items():
            key = str(name).lower()
            promptish = (
                key in _PROMPT_KEYS
                or key.endswith("_prompt")
                or key.endswith("_text")
                or ctype in _H3_LOWER
            )
            if not promptish:
                continue
            text = ""
            if isinstance(value, str) and value.strip():
                text = value.strip()
            elif _is_link(value):
                found = _linked_literal(
                    by_id, value, keys=_VALUE_KEYS, kinds=(str,)
                )
                text = found.strip() if isinstance(found, str) else ""
            if not text:
                continue
            if "negative" in ctype or key in _NEGATIVE_KEYS:
                if len(text) > len(negative):
                    negative = text
            elif len(text) > len(positive):
                positive = text

    models: list[tuple[str, str]] = []
    loras: list[tuple[str, float]] = []
    for _node_id, node in nodes:
        ctype = _class_type(node)
        inputs = _inputs(node)
        for name, label in _LOADER_FIELDS.items():
            value = inputs.get(name)
            if isinstance(value, str) and value.strip():
                entry = (label, value.strip())
                if entry not in models:
                    models.append(entry)
        if ctype in _LORA_CLASSES:
            value = inputs.get("lora_name")
            if isinstance(value, str) and value.strip():
                strength = inputs.get("strength_model", inputs.get("strength"))
                weight = float(strength) if isinstance(strength, (int, float)) else None
                loras.append((value.strip(), weight if weight is not None else 1.0))

    # Duration: the H3 nodes take a frame count (``length``), which the
    # templates compute from a requested duration in seconds. Read the frame
    # count when it is a literal, otherwise resolve the helper chain and
    # interpret a small value as seconds, a large one as frames.
    seconds: Optional[float] = None
    frames: Optional[int] = None
    for _node_id, node in h3_nodes:
        length = _inputs(node).get("length")
        if length is None:
            continue
        literal = _literal_int(length)
        if literal is not None:
            frames = literal
            break
        if _is_link(length):
            resolved = _linked_literal(
                by_id, length, keys=("value",), kinds=(int, float)
            )
            if isinstance(resolved, (int, float)) and not isinstance(resolved, bool):
                if resolved <= 60:
                    seconds = float(resolved)
                else:
                    frames = int(resolved)
        break
    if frames is None:
        frames = _first_int(nodes, ("num_frames", "frames"))
    if frames is None and seconds is not None:
        frames = _snapped_frames(seconds)
    fps = _first_float(nodes, ("fps", "frame_rate"))
    duration: Optional[float] = None
    if seconds is not None:
        duration = round(seconds, 3)
    elif frames and fps and fps > 0:
        duration = round(frames / fps, 3)

    return {
        "prompt": positive,
        "negative": negative,
        "seed": _first_int(nodes, ("noise_seed", "seed")),
        "width": _first_int(nodes, ("width",)),
        "height": _first_int(nodes, ("height",)),
        "resolution_hint": _resolution_hint(nodes),
        "frames": frames,
        "fps": fps,
        "duration": duration,
        "models": models,
        "loras": loras,
    }


def is_terminal_history_entry(entry: Any) -> bool:
    """True once a history entry will not change any more.

    ComfyUI registers the entry *before* running the graph
    (``status.completed`` false), so the key's presence is not enough.
    """
    if not isinstance(entry, Mapping):
        return False
    status = entry.get("status")
    if isinstance(status, Mapping) and status:
        # ``completed`` is the authoritative flag: ComfyUI registers the
        # entry before running the graph, and its ``status_str`` already
        # reads "success" at that point.
        completed = status.get("completed")
        if isinstance(completed, bool):
            return completed
        if str(status.get("status_str") or "").lower() in ("success", "error"):
            return True
        return False
    # Servers without a status block: a recorded output means "done".
    return bool(entry.get("outputs"))


def is_success_history_entry(entry: Any) -> bool:
    """True unless the entry's status explicitly reports an error."""
    if not isinstance(entry, Mapping):
        return False
    status = entry.get("status")
    if isinstance(status, Mapping) and status:
        return str(status.get("status_str") or "").lower() != "error"
    return True


def outputs_of(entry: Any) -> list[tuple[str, str, str]]:
    """``(subfolder, filename, type)`` for every file the entry produced."""
    out: list[tuple[str, str, str]] = []
    if not isinstance(entry, Mapping):
        return out
    outputs = entry.get("outputs")
    if not isinstance(outputs, Mapping):
        return out
    for node_output in outputs.values():
        if not isinstance(node_output, Mapping):
            continue
        for items in node_output.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                filename = item.get("filename")
                if not isinstance(filename, str) or not filename:
                    continue
                subfolder = item.get("subfolder")
                # ComfyUI stores some outputs under ``temp`` (Preview* nodes);
                # hardcoding ``type=output`` in the /view URL made those
                # uncollectable — the failure only ever surfaced as a warning.
                kind = item.get("type")
                if not isinstance(kind, str) or not kind:
                    kind = "output"
                out.append((str(subfolder or ""), filename, kind))
    return out


def baseline_seen(history: Any) -> set[str]:
    """The prompt ids already finished when the watcher starts (no backlog).

    Entries still *running* are deliberately left out: enabling the option
    while a generation is in flight must collect that generation once it
    finishes (the operator enables it, then goes to bed).
    """
    if not isinstance(history, Mapping):
        return set()
    return {
        str(prompt_id)
        for prompt_id, entry in history.items()
        if is_terminal_history_entry(entry)
    }


def new_terminal_entries(
    history: Any, seen: set[str]
) -> list[tuple[str, Mapping[str, Any]]]:
    """Finished entries not in *seen* yet; *seen* is updated in place."""
    found: list[tuple[str, Mapping[str, Any]]] = []
    if not isinstance(history, Mapping):
        return found
    for prompt_id, entry in history.items():
        key = str(prompt_id)
        if key in seen or not is_terminal_history_entry(entry):
            continue
        seen.add(key)
        found.append((key, entry))
    return found


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def _value(value: Any, *, empty: str = "—") -> str:
    if value is None:
        return empty
    if isinstance(value, str):
        return value.strip() or empty
    return str(value)


def build_sidecar_text(
    fields: Mapping[str, Any],
    *,
    workflow: Optional[str] = None,
    prompt_id: str = "",
    filename: str = "",
    finished_at: Optional[datetime] = None,
    success: bool = True,
) -> str:
    """The ``.txt`` written next to a collected generation."""
    when = (finished_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    width = fields.get("width")
    height = fields.get("height")
    if width and height:
        size = f"{width} x {height}"
    else:
        # The H3 templates compute the pixel size in the graph (the H3 node's
        # width/height are links to a ResolutionSelector): report that node's
        # settings rather than a guessed pixel size.
        size = _value(fields.get("resolution_hint"))
    frames = fields.get("frames")
    fps = fields.get("fps")
    duration = fields.get("duration")
    parts: list[str] = []
    if frames and fps:
        parts.append(f"{frames} images @ {fps:g} fps")
    elif frames:
        parts.append(f"{frames} images")
    elif fps:
        parts.append(f"{fps:g} fps")
    timing = " (" + ", ".join(parts) + ")" if parts else ""
    duration_text = f"{duration:g} s{timing}" if duration else _value(None)

    lines = [
        "MiniMax H3 Launcher — ComfyUI generation sheet",
        "=" * 42,
        "",
        f"File        : {_value(filename)}",
        f"Workflow    : {_value(workflow)}",
        f"Prompt ID   : {_value(prompt_id)}",
        f"Finished at : {when}",
        f"Status      : {'success' if success else 'failed'}",
        "",
        "--- Prompt ---",
        _value(fields.get("prompt"), empty="(empty)"),
        "",
        "--- Negative prompt ---",
        _value(fields.get("negative"), empty="(none)"),
        "",
        "--- Settings ---",
        f"Seed        : {_value(fields.get('seed'))}",
        f"Dimensions  : {size}",
        f"Duration    : {duration_text}",
        "",
        "--- Resources ---",
    ]
    models = fields.get("models") or []
    if models:
        lines.append("Models:")
        for label, name in models:
            lines.append(f"  - {label}: {name}")
    else:
        lines.append("Models: (none detected)")
    loras = fields.get("loras") or []
    if loras:
        lines.append("LoRAs:")
        for name, weight in loras:
            lines.append(f"  - {name} (weight {weight:g})")
    else:
        lines.append("LoRAs: (none)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# prompt_id -> workflow journal
# ---------------------------------------------------------------------------


def _journal_path(path: Optional[Path] = None) -> Path:
    return Path(path) if path is not None else default_home() / JOURNAL_FILENAME


def _read_journal(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(prompts, Mapping):
        return {}
    return {
        str(key): str(value)
        for key, value in prompts.items()
        if isinstance(value, str)
    }


def record_prompt_workflow(
    prompt_id: str, workflow: str, path: Optional[Path] = None
) -> None:
    """Remember which workflow *prompt_id* was submitted from (best effort)."""
    prompt_id = (prompt_id or "").strip()
    workflow = (workflow or "").strip()
    if not prompt_id or not workflow:
        return
    target = _journal_path(path)
    with _journal_lock:
        entries = _read_journal(target)
        entries.pop(prompt_id, None)
        entries[prompt_id] = workflow
        while len(entries) > JOURNAL_LIMIT:
            entries.pop(next(iter(entries)))
        payload = {"version": 1, "prompts": entries}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp_name = tempfile.mkstemp(
                dir=str(target.parent), prefix=".comfy-prompts-", suffix=".tmp"
            )
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(tmp_name, target)
        except OSError as exc:
            logger.debug("could not write the prompt journal: %s", exc)


def lookup_prompt_workflow(
    prompt_id: str, path: Optional[Path] = None
) -> Optional[str]:
    """The workflow recorded for *prompt_id*, or None when unknown."""
    prompt_id = (prompt_id or "").strip()
    if not prompt_id:
        return None
    with _journal_lock:
        return _read_journal(_journal_path(path)).get(prompt_id)


# ---------------------------------------------------------------------------
# ntfy
# ---------------------------------------------------------------------------


def _ascii_header(text: str) -> str:
    """An HTTP-safe header value: RFC 2047 encoded when it is not ASCII.

    ntfy accepts non-ASCII titles only through RFC 2047 (the pod-side
    ``notify.sh`` sends the raw bytes, which is why its accents survive on
    some servers and not others).
    """
    text = text or ""
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{encoded}?="
    return text


def send_ntfy(
    topic: Optional[str],
    title: str,
    message: str,
    *,
    server: Optional[str] = None,
    timeout: float = 10.0,
) -> bool:
    """Publish one notification on ntfy (the pod-side channel). Never raises.

    The topic comes from the same ``ntfy topic`` field the pod uses, so a
    single topic receives both the pod-side and the launcher-side notices.
    """
    topic = (topic or "").strip()
    if not topic:
        return False
    base = (server or os.environ.get("NTFY_SERVER") or DEFAULT_NTFY_SERVER).strip()
    base = base.rstrip("/") or DEFAULT_NTFY_SERVER
    url = f"{base}/{urllib.parse.quote(topic)}"
    request = urllib.request.Request(
        url, data=(message or "").encode("utf-8"), method="POST"
    )
    # ntfy headers are ASCII-only; a non-ASCII title travels RFC 2047 encoded.
    request.add_header("Title", _ascii_header(title))
    request.add_header("Tags", "video_camera")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except Exception as exc:  # noqa: BLE001 - a notification is never fatal
        logger.debug("ntfy notification failed: %s", exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def resolve_output_dir(value: Optional[str]) -> Path:
    """The collection folder: the configured one, else ``~/Downloads``."""
    text = (value or "").strip()
    if not text:
        return DEFAULT_OUTPUT_DIR
    try:
        return Path(text).expanduser()
    except (OSError, ValueError):
        return DEFAULT_OUTPUT_DIR


@dataclass
class CollectOutcome:
    """What one finished generation produced locally."""

    prompt_id: str
    success: bool
    files: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.success and not self.errors


def _safe_local_name(filename: str) -> str:
    """The local file name of a pod-reported output (traversal-safe)."""
    name = Path(filename.replace("\\", "/")).name
    if not name or name in (".", "..") or name.startswith("."):
        raise ComfyOpsError(f"invalid output file name {filename!r}")
    for char in name:
        if char in '<>:"/\\|?*\x00' or ord(char) < 32:
            raise ComfyOpsError(f"invalid output file name {filename!r}")
    return name


def _unique_local_name(folder: Path, name: str) -> str:
    """A free file name in *folder* — a collection is never overwritten.

    ComfyUI restarts its output counter on every pod boot, so
    ``MiniMax_H3_00001.mp4`` comes back in later sessions; the collection
    keeps the earlier file (and its ``.txt``) and appends `` (2)``, `` (3)``…
    """
    def taken(candidate: str) -> bool:
        path = folder / candidate
        return path.exists() or path.with_suffix(".txt").exists()

    if not taken(name):
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    for index in range(2, 1000):
        candidate = f"{stem} ({index}){suffix}"
        if not taken(candidate):
            return candidate
    return f"{stem} ({int(time.time())}){suffix}"


def collect_generation(
    ops: ComfyOps,
    prompt_id: str,
    entry: Mapping[str, Any],
    *,
    output_dir: Path,
    workflow: Optional[str] = None,
    finished_at: Optional[datetime] = None,
) -> CollectOutcome:
    """Download every output of one finished generation + write the sidecars.

    Flat layout: each file lands directly in *output_dir* (no per-generation
    subfolder) and its ``.txt`` sidecar carries the same base name.
    """
    success = is_success_history_entry(entry)
    outcome = CollectOutcome(prompt_id=prompt_id, success=success)
    files = outputs_of(entry)
    if not files:
        outcome.errors.append("no file produced")
        return outcome
    prompt_graph = None
    raw_prompt = entry.get("prompt") if isinstance(entry, Mapping) else None
    if isinstance(raw_prompt, (list, tuple)) and len(raw_prompt) >= 3:
        prompt_graph = raw_prompt[2]
    fields = extract_generation_fields(prompt_graph)
    when = finished_at or datetime.now()
    for subfolder, filename, kind in files:
        try:
            pod_name = _safe_local_name(filename)
            target_dir = Path(output_dir)
            target = ops.download_output(
                pod_name,
                target_dir,
                subfolder=subfolder,
                mirror_subfolder=False,
                local_name=_unique_local_name(target_dir, pod_name),
                type=kind,
            )
        except (ComfyOpsError, OSError) as exc:
            outcome.errors.append(f"{filename} : {exc}")
            continue
        outcome.files.append(target)
        try:
            sidecar = target.with_suffix(".txt")
            sidecar.write_text(
                build_sidecar_text(
                    fields,
                    workflow=workflow,
                    prompt_id=prompt_id,
                    filename=target.name,
                    finished_at=when,
                    success=success,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            outcome.errors.append(f"{target.name} (.txt) : {exc}")
    return outcome


# ---------------------------------------------------------------------------
# Watcher thread
# ---------------------------------------------------------------------------


class ComfyOutputWatcher:
    """Polls ComfyUI's history and collects every newly finished generation.

    The thread owns no Tk object: the Windows notification and the pod
    termination are delegated to the callbacks the GUI passes in. It only
    works while ``settings()["enabled"]`` is true *and* ComfyUI answers on the
    tunnel; otherwise it idles (cheaply, without building a pod client).
    """

    def __init__(
        self,
        ops_factory: Callable[[], ComfyOps],
        *,
        settings: Callable[[], Mapping[str, Any]],
        probe: Callable[..., bool] = check_comfy,
        interval: float = DEFAULT_POLL_SECONDS,
        retry_interval: float = DEFAULT_RETRY_SECONDS,
        on_notify: Optional[Callable[[str, str], None]] = None,
        terminate_fn: Optional[Callable[[], tuple[bool, str]]] = None,
        on_terminated: Optional[Callable[[bool, str], None]] = None,
        ntfy_sender: Callable[..., bool] = send_ntfy,
        workflow_lookup: Callable[[str], Optional[str]] = lookup_prompt_workflow,
        name: str = "comfy-watch",
        settle_seconds: float = DEFAULT_QUEUE_SETTLE_SECONDS,
    ) -> None:
        self._ops_factory = ops_factory
        self._settings = settings
        self._probe = probe
        self._interval = interval
        self._retry_interval = retry_interval
        self._on_notify = on_notify
        self._terminate_fn = terminate_fn
        self._on_terminated = on_terminated
        self._ntfy_sender = ntfy_sender
        self._workflow_lookup = workflow_lookup
        self._ops: Optional[ComfyOps] = None
        self._seen: set[str] = set()
        self._baseline_done = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._name = name
        self._ops_failure_logged = False
        self._settle_seconds = settle_seconds
        #: ``monotonic()`` when the ComfyUI queue was first seen empty, or None
        #: while it still holds work. Reset on every new submission.
        self._queue_empty_since: Optional[float] = None
        #: True once at least one generation has been handled in this session.
        #: Until then "stop the pod when the generation finishes" stays
        #: disarmed, so enabling it before submitting anything does not stop a
        #: pod the user is still setting up.
        self._batch_started = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Ask the watcher to stop and wait briefly for the thread to end.

        The handle is dropped ONLY once the thread is really gone. A collection
        in flight can outlive the join (its download timeout is measured in
        minutes), and ``alive`` returning False while the old thread kept
        downloading let the GUI start a second watcher that collected the same
        history entry again — both computing the same "unique" local name and
        writing the same file.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if thread is not None and thread.is_alive():
            logger.info(
                "Auto-collect: the collection in flight has not finished; the watcher "
                "stays tracked until it stops."
            )
            return
        self._thread = None

    # -- internals ---------------------------------------------------------

    def _wait(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _ops_or_none(self) -> Optional[ComfyOps]:
        if self._ops is not None:
            return self._ops
        try:
            self._ops = self._ops_factory()
            self._ops_failure_logged = False
        except Exception as exc:  # noqa: BLE001 - the pod may just be down
            if not self._ops_failure_logged:
                logger.warning(
                    "Auto-collect: ComfyUI pod unavailable (%s). Retrying "
                    "automatically.", exc,
                )
                self._ops_failure_logged = True
            return None
        return self._ops

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._tick():
                    return
            except Exception as exc:  # noqa: BLE001 - the watcher must never die
                # The docstring promises "never raises: every failure is logged
                # and retried on the next tick". An unguarded body meant one bad
                # entry (a payload shape change, a NameError) killed the thread
                # for the rest of the session — and because the prompt id was
                # already marked seen, that generation was never collected.
                logger.warning(
                    "Auto-collect: unexpected error (%s); retrying on the next "
                    "cycle",
                    exc,
                )
                self._wait(self._retry_interval)

    def _tick(self) -> bool:
        """One polling cycle; True when the watcher must stop."""
        settings = dict(self._settings() or {})
        if not settings.get("enabled"):
            self._ops = None
            self._baseline_done = False
            self._seen = set()
            self._batch_started = False
            self._queue_empty_since = None
            self._wait(self._interval)
            return False
        ops = self._ops_or_none()
        if ops is None:
            self._wait(self._retry_interval)
            return False
        if not self._probe(ops.base_url, 3.0):
            self._wait(self._interval)
            return False
        try:
            history = ops.get_history()
        except ComfyOpsError as exc:
            logger.debug("Auto-collect: unreadable history (%s)", exc)
            self._ops = None
            self._wait(self._retry_interval)
            return False
        if not self._baseline_done:
            self._seen |= baseline_seen(history)
            self._baseline_done = True
            logger.info(
                "Auto-collect active — %d generation(s) already finished and "
                "ignored; the next ones will be collected.",
                len(self._seen),
            )
        for prompt_id, entry in new_terminal_entries(history, self._seen):
            if self._handle_entry(ops, prompt_id, entry, settings):
                return True
        # "Stop the pod when the generation finishes" is a QUEUE question, not
        # a per-generation one: it is evaluated once per tick, after the batch
        # has been processed, so the last generation of a batch is what arms
        # it — and so the check still runs on the ticks that follow, when no
        # new history entry appears any more.
        if (
            settings.get("terminate_after")
            and self._batch_started
            and self._terminate_fn is not None
            and self._stop_pod_when_queue_drained(ops, settings)
        ):
            return True
        self._wait(self._interval)
        return False

    def _handle_entry(
        self,
        ops: ComfyOps,
        prompt_id: str,
        entry: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> bool:
        """Collect/notify one generation; True when the watcher must stop."""
        success = is_success_history_entry(entry)
        workflow = self._workflow_lookup(prompt_id)
        title = "ComfyUI generation finished" if success else "ComfyUI generation failed"
        details = f"prompt {prompt_id[:8]}"
        if settings.get("auto_collect"):
            outcome = collect_generation(
                ops,
                prompt_id,
                entry,
                output_dir=resolve_output_dir(settings.get("output_dir")),
                workflow=workflow,
            )
            names = ", ".join(path.name for path in outcome.files)
            if outcome.files:
                logger.ok(
                    "Auto-collect: %d file(s) received — %s",
                    len(outcome.files), names,
                )
                details = names
            for error in outcome.errors:
                logger.warning("Auto-collect: failure (%s)", error)
            if not outcome.files:
                # Nothing landed: forget the id so the next tick retries. The
                # entry was already added to ``seen`` by
                # ``new_terminal_entries``, and without this a transient tunnel
                # failure lost the generation for good.
                self._seen.discard(prompt_id)
        if settings.get("notify_ntfy") and settings.get("ntfy_topic"):
            self._ntfy_sender(
                settings.get("ntfy_topic"),
                title,
                f"{details}",
                server=settings.get("ntfy_server"),
            )
        if settings.get("notify_windows") and self._on_notify is not None:
            self._on_notify(title, details)
        # A generation has been handled: "stop the pod when the generation
        # finishes" may now arm itself (see ``_tick``).
        self._batch_started = True
        return False

    def _queue_state(self, ops: ComfyOps) -> Optional[tuple[int, int]]:
        """``(running, pending)`` from ComfyUI's queue, or None when unknown.

        ``None`` means "could not be read", which is deliberately different
        from ``(0, 0)``: an unreachable pod must never be taken as proof that
        the batch is finished.
        """
        try:
            data = ops.get_queue()
        except Exception as exc:  # noqa: BLE001 - never kill the thread
            logger.debug("Auto-collect: queue unreadable (%s)", exc)
            return None
        if not isinstance(data, Mapping):
            return None
        running = data.get("running")
        pending = data.get("pending")
        if not isinstance(running, int) or not isinstance(pending, int):
            return None
        return running, pending

    def _stop_pod_when_queue_drained(
        self, ops: ComfyOps, settings: Mapping[str, Any]
    ) -> bool:
        """Stop the pod once ComfyUI's queue is empty; True when we did.

        "Stop the pod when the generation finishes" used to stop it as soon as
        ONE generation completed, so a batch of ten left nine unrun with the
        pod already gone. The queue is the source of truth for "is anything
        left to do", and it must STAY empty for the settle window before the
        pod is stopped, so a submission in flight is never cut off.
        """
        state = self._queue_state(ops)
        if state is None:
            logger.warning(
                "Auto-collect: ComfyUI queue unreadable — the pod is NOT "
                "stopped; retrying on the next cycle."
            )
            return False
        running, pending = state
        remaining = running + pending
        if remaining:
            self._queue_empty_since = None
            logger.info(
                "Auto-collect: %d generation(s) still queued (%d running, "
                "%d pending) — the pod stays up until the queue is drained.",
                remaining,
                running,
                pending,
            )
            return False

        now = time.monotonic()
        if self._queue_empty_since is None:
            self._queue_empty_since = now
        waited = now - self._queue_empty_since
        settle = self._effective_settle_seconds(settings)
        if waited < settle:
            logger.info(
                "Auto-collect: queue empty — stopping the pod in %.0fs if "
                "nothing else is submitted.",
                settle - waited,
            )
            return False

        logger.info("Auto-collect: queue empty — stopping the pod.")
        try:
            ok, message = self._terminate_fn()
        except Exception as exc:  # noqa: BLE001 - never kill the thread
            ok, message = False, str(exc)
        if self._on_terminated is not None:
            self._on_terminated(ok, message)
        self._stop.set()
        return True

    def _effective_settle_seconds(self, settings: Mapping[str, Any]) -> float:
        """The settle window: the setting wins, then the constructor value."""
        raw = settings.get("terminate_settle_seconds")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
            return float(raw)
        return float(self._settle_seconds)

"""Tests for launcher.comfy_watch (automatic collection of generations).

Everything here is offline: the ComfyUI HTTP API is replaced by a fake
``ComfyOps`` and the tunnel probe by a lambda. The pure helpers (history
parsing, sidecar building, journal) carry most of the coverage.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from launcher import comfy_watch
from launcher.comfy_ops import ComfyOpsError


def h3_api_prompt() -> dict:
    """A trimmed-down MiniMax H3 t2v API prompt (what /history stores)."""
    return {
        "6": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": "minimax_h3_fl2va_pruned_fp8_scaled.safetensors",
                "weight_dtype": "default",
            },
        },
        "13": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                "type": "minimax",
            },
        },
        "11": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"},
        },
        "24": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"},
        },
        "104": {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {
                "prompt": "A cat surfing a wave at sunset.",
                "negative": "blurry, text",
                "width": 1344,
                "height": 768,
                "length": 121,
                "model": ["6", 0],
                "clip": ["13", 0],
            },
        },
        "30": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"lora_name": "openfox_style.safetensors", "strength_model": 0.8},
        },
        "15": {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": 556589502035082},
        },
        "91": {
            "class_type": "CreateVideo",
            "inputs": {"fps": 24.0, "length": 121},
        },
    }


def history_entry(*, completed: bool = True, status_str: str = "success",
                  outputs: dict | None = None) -> dict:
    return {
        "prompt": [1, "pid", h3_api_prompt(), {}, []],
        "outputs": outputs
        if outputs is not None
        else {
            "92": {
                "images": [],
                "videos": [
                    {
                        "filename": "MiniMax_H3_00042.mp4",
                        "subfolder": "video/MiniMax_H3",
                        "type": "output",
                    }
                ],
            }
        },
        "status": {"status_str": status_str, "completed": completed, "messages": []},
    }


class FakeOps:
    """Stand-in for ComfyOps: no tunnel, no HTTP."""

    base_url = "http://127.0.0.1:8188"

    def __init__(self, history: dict | None = None, failing: tuple[str, ...] = ()):
        self.history = history if history is not None else {}
        self.failing = set(failing)
        self.downloads: list[tuple[str, str, bool]] = []
        self.local_names: list[str] = []
        self.types: list[str] = []
        #: ``{"running": n, "pending": n}``; ``{}`` models an unreadable queue
        #: (``ComfyOps.get_queue`` returns ``{}`` on error).
        self.queue: dict = {"running": 0, "pending": 0}

    def get_queue(self) -> dict:
        return self.queue

    def get_history(self, max_items: int = 200, timeout: float = 15.0) -> dict:
        return self.history

    def download_output(self, filename, local_dir, timeout=None, subfolder="",
                        mirror_subfolder=True, local_name=None, type="output"):
        self.downloads.append((filename, subfolder, mirror_subfolder))
        self.local_names.append(local_name or filename)
        self.types.append(type)
        if filename in self.failing:
            raise ComfyOpsError(f"boom {filename}")
        target = Path(local_dir) / (local_name or filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video-bytes")
        return target


class SequencedOps(FakeOps):
    """Returns one scripted history per poll, then the last one forever.

    Makes the "baseline then collect" sequence deterministic: the first
    poll sees an in-flight generation (nothing to baseline), the next sees
    it finished.
    """

    def __init__(self, *responses: dict):
        super().__init__({})
        self._responses = list(responses)
        self.polls = 0

    def get_history(self, max_items: int = 200, timeout: float = 15.0) -> dict:
        index = min(self.polls, len(self._responses) - 1)
        self.polls += 1
        return self._responses[index]


# ---------------------------------------------------------------------------
# History parsing
# ---------------------------------------------------------------------------


def test_extract_generation_fields_reads_the_h3_graph():
    fields = comfy_watch.extract_generation_fields(h3_api_prompt())
    assert fields["prompt"] == "A cat surfing a wave at sunset."
    assert fields["negative"] == "blurry, text"
    assert fields["seed"] == 556589502035082
    assert (fields["width"], fields["height"]) == (1344, 768)
    assert fields["frames"] == 121
    assert fields["fps"] == 24.0
    assert fields["duration"] == pytest.approx(5.042, abs=0.01)
    labels = dict(fields["models"])
    assert labels["UNet"].endswith("_fp8_scaled.safetensors")
    assert labels["CLIP"].startswith("qwen3vl_32b")
    assert "VAE" in labels
    assert fields["loras"] == [("openfox_style.safetensors", 0.8)]


def test_extract_generation_fields_survives_garbage():
    for payload in (None, {}, [], {"1": "not a node"}, {"2": {"inputs": None}}):
        fields = comfy_watch.extract_generation_fields(payload)
        assert fields["prompt"] == ""
        assert fields["models"] == []
        assert fields["seed"] is None
        assert fields["resolution_hint"] == ""


def test_is_terminal_history_entry_waits_for_completion():
    assert not comfy_watch.is_terminal_history_entry(
        history_entry(completed=False, status_str="success", outputs={})
    )
    assert comfy_watch.is_terminal_history_entry(history_entry(completed=True))
    assert comfy_watch.is_terminal_history_entry(
        history_entry(completed=True, status_str="error", outputs={})
    )
    # No status block at all: a recorded output is the completion marker.
    assert comfy_watch.is_terminal_history_entry({"outputs": {"9": {}}})
    assert not comfy_watch.is_terminal_history_entry({})
    assert not comfy_watch.is_terminal_history_entry("nope")


def test_is_success_history_entry_flags_errors():
    assert comfy_watch.is_success_history_entry(history_entry())
    assert not comfy_watch.is_success_history_entry(
        history_entry(status_str="error")
    )
    assert comfy_watch.is_success_history_entry({"outputs": {}})


def test_outputs_of_collects_every_node_output():
    entry = history_entry()
    assert comfy_watch.outputs_of(entry) == [
        ("video/MiniMax_H3", "MiniMax_H3_00042.mp4", "output")
    ]
    assert comfy_watch.outputs_of({}) == []


def test_outputs_of_keeps_the_comfyui_type():
    """A ``temp`` output must be fetched from the ``temp`` tree, not ``output``.

    ``download_output`` hardcoded ``type=output``, so every Preview*-style
    output was requested from the wrong tree and could never be collected.
    """
    entry = history_entry()
    entry["outputs"]["92"]["videos"][0]["type"] = "temp"
    assert comfy_watch.outputs_of(entry) == [
        ("video/MiniMax_H3", "MiniMax_H3_00042.mp4", "temp")
    ]


def test_collect_generation_passes_the_type_through(tmp_path):
    ops = FakeOps()
    entry = history_entry()
    entry["outputs"]["92"]["videos"][0]["type"] = "temp"
    comfy_watch.collect_generation(ops, "pid-1", entry, output_dir=tmp_path)
    assert ops.types == ["temp"]


# ---------------------------------------------------------------------------
# Baseline / new entries
# ---------------------------------------------------------------------------


def test_baseline_ignores_finished_generations_but_keeps_running_ones():
    history = {
        "done": history_entry(completed=True),
        "running": history_entry(completed=False, outputs={}),
    }
    seen = comfy_watch.baseline_seen(history)
    assert seen == {"done"}
    # The in-flight generation is picked up once it finishes.
    history["running"] = history_entry(completed=True)
    found = comfy_watch.new_terminal_entries(history, seen)
    assert [pid for pid, _entry in found] == ["running"]
    # ... and only once.
    assert comfy_watch.new_terminal_entries(history, seen) == []


def test_new_terminal_entries_skips_unfinished_ones():
    history = {
        "a": history_entry(completed=True),
        "b": history_entry(completed=False, outputs={}),
    }
    seen: set[str] = set()
    assert [pid for pid, _ in comfy_watch.new_terminal_entries(history, seen)] == ["a"]
    assert seen == {"a"}


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def test_build_sidecar_text_carries_prompt_params_and_resources():
    fields = comfy_watch.extract_generation_fields(h3_api_prompt())
    text = comfy_watch.build_sidecar_text(
        fields,
        workflow="video_minimax_h3_t2v.json",
        prompt_id="abcd-1234",
        filename="MiniMax_H3_00042.mp4",
        finished_at=datetime(2026, 9, 20, 3, 12, 44),
    )
    assert "A cat surfing a wave at sunset." in text
    assert "blurry, text" in text
    assert "video_minimax_h3_t2v.json" in text
    assert "MiniMax_H3_00042.mp4" in text
    assert "2026-09-20 03:12:44" in text
    assert "1344 x 768" in text
    assert "556589502035082" in text
    assert "minimax_h3_fl2va_pruned_fp8_scaled.safetensors" in text
    assert "openfox_style.safetensors (weight 0.8)" in text
    assert "success" in text
    # The sidecar is a summary, not a node dump.
    assert "class_type" not in text


def test_build_sidecar_text_marks_missing_values():
    text = comfy_watch.build_sidecar_text(
        {}, prompt_id="", filename="x.mp4", success=False
    )
    assert "(empty)" in text
    assert "(none)" in text
    assert "failed" in text
    assert "—" in text


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def test_collect_generation_downloads_flat_and_writes_the_sidecar(tmp_path):
    ops = FakeOps()
    outcome = comfy_watch.collect_generation(
        ops,
        "pid-1",
        history_entry(),
        output_dir=tmp_path,
        workflow="video_minimax_h3_t2v.json",
    )
    assert outcome.ok
    video = tmp_path / "MiniMax_H3_00042.mp4"
    sidecar = tmp_path / "MiniMax_H3_00042.txt"
    assert video.read_bytes() == b"video-bytes"
    assert "A cat surfing a wave at sunset." in sidecar.read_text(encoding="utf-8")
    # Flat layout: the pod's subfolder is only used for the /view URL.
    assert ops.downloads == [
        ("MiniMax_H3_00042.mp4", "video/MiniMax_H3", False)
    ]


def test_collect_generation_never_overwrites_an_earlier_collection(tmp_path):
    """ComfyUI restarts its counter per pod boot: the names come back."""
    (tmp_path / "MiniMax_H3_00042.mp4").write_bytes(b"previous video")
    (tmp_path / "MiniMax_H3_00042.txt").write_text("previous", encoding="utf-8")
    ops = FakeOps()
    outcome = comfy_watch.collect_generation(
        ops,
        "pid-1",
        history_entry(),
        output_dir=tmp_path,
        workflow="video_minimax_h3_t2v.json",
    )
    assert outcome.ok
    assert outcome.files == [tmp_path / "MiniMax_H3_00042 (2).mp4"]
    # The earlier collection (and its sidecar) is untouched.
    assert (tmp_path / "MiniMax_H3_00042.mp4").read_bytes() == b"previous video"
    assert (tmp_path / "MiniMax_H3_00042.txt").read_text(encoding="utf-8") == "previous"
    assert "A cat surfing a wave at sunset." in (
        tmp_path / "MiniMax_H3_00042 (2).txt"
    ).read_text(encoding="utf-8")
    # The pod-side name still drives the /view URL.
    assert ops.downloads == [("MiniMax_H3_00042.mp4", "video/MiniMax_H3", False)]


def test_unique_local_name_avoids_the_media_and_its_sidecar(tmp_path):
    assert comfy_watch._unique_local_name(tmp_path, "a.mp4") == "a.mp4"
    (tmp_path / "a.mp4").write_bytes(b"x")
    assert comfy_watch._unique_local_name(tmp_path, "a.mp4") == "a (2).mp4"
    (tmp_path / "a (2).txt").write_text("x", encoding="utf-8")
    assert comfy_watch._unique_local_name(tmp_path, "a.mp4") == "a (3).mp4"


def test_collect_generation_reports_a_failed_download(tmp_path):
    ops = FakeOps(failing=("MiniMax_H3_00042.mp4",))
    outcome = comfy_watch.collect_generation(
        ops, "pid-1", history_entry(), output_dir=tmp_path
    )
    assert not outcome.ok
    assert outcome.files == []
    assert outcome.errors and "MiniMax_H3_00042.mp4" in outcome.errors[0]


def test_collect_generation_handles_an_entry_without_outputs(tmp_path):
    outcome = comfy_watch.collect_generation(
        FakeOps(),
        "pid-1",
        history_entry(completed=True, status_str="error", outputs={}),
        output_dir=tmp_path,
    )
    assert outcome.success is False
    assert outcome.errors == ["no file produced"]


def test_collect_generation_neutralises_a_traversal_file_name(tmp_path):
    entry = history_entry(
        outputs={"92": {"videos": [{"filename": "..\\..\\evil.mp4", "subfolder": ""}]}}
    )
    outcome = comfy_watch.collect_generation(
        FakeOps(), "pid-1", entry, output_dir=tmp_path
    )
    # The path components are dropped: the file can only land in output_dir.
    assert outcome.files == [tmp_path / "evil.mp4"]
    assert not (tmp_path.parent / "evil.mp4").exists()


def test_collect_generation_rejects_an_unsafe_file_name(tmp_path):
    entry = history_entry(
        outputs={"92": {"videos": [{"filename": "..", "subfolder": ""}]}}
    )
    outcome = comfy_watch.collect_generation(
        FakeOps(), "pid-1", entry, output_dir=tmp_path
    )
    assert not outcome.ok
    assert outcome.files == []


def test_resolve_output_dir_defaults_to_downloads(tmp_path):
    assert comfy_watch.resolve_output_dir("") == Path.home() / "Downloads"
    assert comfy_watch.resolve_output_dir(None) == Path.home() / "Downloads"
    assert comfy_watch.resolve_output_dir(str(tmp_path)) == tmp_path


# ---------------------------------------------------------------------------
# Journal (prompt_id -> workflow)
# ---------------------------------------------------------------------------


def test_prompt_journal_roundtrip(tmp_path):
    path = tmp_path / "comfy-prompts.json"
    assert comfy_watch.lookup_prompt_workflow("pid-1", path) is None
    comfy_watch.record_prompt_workflow("pid-1", "video_minimax_h3_t2v.json", path)
    assert (
        comfy_watch.lookup_prompt_workflow("pid-1", path)
        == "video_minimax_h3_t2v.json"
    )
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    # An unreadable journal degrades to "unknown" instead of raising.
    path.write_text("{not json", encoding="utf-8")
    assert comfy_watch.lookup_prompt_workflow("pid-1", path) is None


def test_prompt_journal_is_bounded(tmp_path):
    path = tmp_path / "comfy-prompts.json"
    for index in range(comfy_watch.JOURNAL_LIMIT + 20):
        comfy_watch.record_prompt_workflow(f"pid-{index}", "wf.json", path)
    stored = json.loads(path.read_text(encoding="utf-8"))["prompts"]
    assert len(stored) == comfy_watch.JOURNAL_LIMIT
    assert "pid-0" not in stored


# ---------------------------------------------------------------------------
# ntfy
# ---------------------------------------------------------------------------


def test_send_ntfy_without_topic_is_a_no_op():
    assert comfy_watch.send_ntfy("", "t", "m") is False
    assert comfy_watch.send_ntfy(None, "t", "m") is False


def test_ntfy_title_header_encoding():
    assert comfy_watch._ascii_header("Plain ASCII") == "Plain ASCII"
    encoded = comfy_watch._ascii_header("Génération terminée")
    assert encoded.startswith("=?UTF-8?B?") and encoded.endswith("?=")
    import base64 as _base64

    payload = encoded[len("=?UTF-8?B?") : -len("?=")]
    assert _base64.b64decode(payload).decode("utf-8") == "Génération terminée"


# ---------------------------------------------------------------------------
# Watcher thread
# ---------------------------------------------------------------------------


def test_watcher_collects_a_generation_and_terminates_the_pod(tmp_path):
    ops = SequencedOps(
        {"pid-1": history_entry(completed=False, outputs={})},
        {"pid-1": history_entry()},
    )
    settings = {
        "enabled": True,
        "auto_collect": True,
        "output_dir": str(tmp_path),
        "notify_windows": True,
        "notify_ntfy": False,
        "ntfy_topic": "",
        "terminate_after": True,
    }
    notified: list[tuple[str, str]] = []
    terminated = threading.Event()
    outcome: list[tuple[bool, str]] = []

    watcher = comfy_watch.ComfyOutputWatcher(
        lambda: ops,
        settings=lambda: settings,
        probe=lambda *_a, **_k: True,
        interval=0.01,
        retry_interval=0.01,
        on_notify=lambda title, message: notified.append((title, message)),
        terminate_fn=lambda: (True, "pod terminé"),
        on_terminated=lambda ok, message: (
            outcome.append((ok, message)),
            terminated.set(),
        ),
        workflow_lookup=lambda _pid: "video_minimax_h3_t2v.json",
        # No settle window here: the queue is already empty and this test is
        # about the collection + notification, not about the batching guard.
        settle_seconds=0.0,
    )
    watcher.start()
    try:
        assert terminated.wait(10.0), "the watcher never terminated the pod"
    finally:
        watcher.stop()
    assert outcome == [(True, "pod terminé")]
    assert (tmp_path / "MiniMax_H3_00042.mp4").exists()
    sidecar = (tmp_path / "MiniMax_H3_00042.txt").read_text(encoding="utf-8")
    assert "video_minimax_h3_t2v.json" in sidecar
    assert notified and "MiniMax_H3_00042.mp4" in notified[0][1]
    assert not watcher.alive


def _watcher_for(ops, settings_map=None, **kwargs):
    """A watcher whose thread is never started: the tests drive it directly."""
    return comfy_watch.ComfyOutputWatcher(
        lambda: ops,
        settings=lambda: (settings_map or {}),
        probe=lambda *_a, **_k: True,
        interval=0.01,
        retry_interval=0.01,
        **kwargs,
    )


def test_terminate_after_waits_for_the_whole_queue(tmp_path):
    """Ten generations queued: the pod stops after the TENTH, not the first.

    The check used to fire from ``_handle_entry`` as soon as ONE generation
    finished, so a batch of ten left nine unrun with the pod already gone.
    """
    calls: list[str] = []
    ops = FakeOps()
    watcher = _watcher_for(
        ops,
        terminate_fn=lambda: calls.append("terminate") or (True, "ok"),
        settle_seconds=0.0,
    )
    settings = {"auto_collect": False, "terminate_after": True}

    for index in range(1, 10):
        # The nine others are still running/pending.
        ops.queue = {"running": 1, "pending": 9 - index}
        assert (
            watcher._stop_pod_when_queue_drained(ops, settings) is False
        ), f"generation {index} must not stop the pod"
    assert calls == []

    # The tenth finishes: nothing is left in the queue.
    ops.queue = {"running": 0, "pending": 0}
    assert watcher._stop_pod_when_queue_drained(ops, settings) is True
    assert calls == ["terminate"]


def test_terminate_after_never_stops_when_the_queue_is_unreadable(tmp_path):
    """An unreadable queue is not proof the batch is done."""
    calls: list[str] = []
    ops = FakeOps()
    ops.queue = {}  # ComfyOps.get_queue() returns {} on error
    watcher = _watcher_for(
        ops,
        terminate_fn=lambda: calls.append("terminate") or (True, "ok"),
        settle_seconds=0.0,
    )
    settings = {"auto_collect": False, "terminate_after": True}
    assert watcher._stop_pod_when_queue_drained(ops, settings) is False
    assert calls == []


def test_terminate_after_waits_out_the_settle_window(tmp_path):
    """An empty queue must STAY empty before the pod is stopped.

    Without the window, a prompt submitted right after the previous one
    finished (the queue momentarily reads empty) would be lost.
    """
    calls: list[str] = []
    ops = FakeOps()
    ops.queue = {"running": 0, "pending": 0}
    watcher = _watcher_for(
        ops,
        terminate_fn=lambda: calls.append("terminate") or (True, "ok"),
        settle_seconds=600.0,
    )
    settings = {"auto_collect": False, "terminate_after": True}
    assert watcher._stop_pod_when_queue_drained(ops, settings) is False
    assert watcher._queue_empty_since is not None
    assert calls == []


def test_terminate_after_settle_window_resets_on_a_new_submission(tmp_path):
    calls: list[str] = []
    ops = FakeOps()
    ops.queue = {"running": 0, "pending": 0}
    watcher = _watcher_for(
        ops,
        terminate_fn=lambda: calls.append("terminate") or (True, "ok"),
        settle_seconds=600.0,
    )
    settings = {"auto_collect": False, "terminate_after": True}
    watcher._stop_pod_when_queue_drained(ops, settings)
    assert watcher._queue_empty_since is not None

    ops.queue = {"running": 1, "pending": 0}
    assert watcher._stop_pod_when_queue_drained(ops, settings) is False
    assert watcher._queue_empty_since is None, "a new submission must restart the timer"
    assert calls == []


def test_terminate_after_reports_the_termination_once(tmp_path):
    ops = FakeOps()
    ops.queue = {"running": 0, "pending": 0}
    seen: list[tuple[bool, str]] = []
    watcher = _watcher_for(
        ops,
        terminate_fn=lambda: (True, "pod stopped"),
        on_terminated=lambda ok, message: seen.append((ok, message)),
        settle_seconds=0.0,
    )
    settings = {"auto_collect": False, "terminate_after": True}
    assert watcher._stop_pod_when_queue_drained(ops, settings) is True
    assert seen == [(True, "pod stopped")]


def test_terminate_after_is_disarmed_until_a_generation_is_handled(tmp_path):
    """Enabling the option before submitting anything must not stop the pod.

    The check is a queue question evaluated every tick; without this arming
    rule, a user who ticks the box while setting up would see the pod stopped
    under them after the settle window.
    """
    calls: list[str] = []
    ops = FakeOps()
    ops.queue = {"running": 0, "pending": 0}
    settings = {
        "enabled": True,
        "auto_collect": False,
        "terminate_after": True,
    }
    watcher = _watcher_for(
        ops,
        settings,
        terminate_fn=lambda: calls.append("terminate") or (True, "ok"),
        settle_seconds=0.0,
    )
    for _ in range(5):
        assert watcher._tick() is False
    assert calls == [], "no generation has run yet: the pod must stay up"

    # One generation lands -> the option arms itself.
    ops.history = {"pid-1": history_entry()}
    assert watcher._tick() is True
    assert calls == ["terminate"]


def test_watcher_drains_a_batch_of_ten_before_stopping_the_pod(tmp_path):
    """End to end through the thread: ten generations, one stop, at the end."""
    ops = FakeOps(history={})
    settings = {
        "enabled": True,
        "auto_collect": True,
        "output_dir": str(tmp_path),
        "terminate_after": True,
    }
    calls: list[str] = []
    terminated = threading.Event()
    watcher = comfy_watch.ComfyOutputWatcher(
        lambda: ops,
        settings=lambda: settings,
        probe=lambda *_a, **_k: True,
        interval=0.01,
        retry_interval=0.01,
        terminate_fn=lambda: calls.append("terminate") or (True, "ok"),
        on_terminated=lambda ok, message: terminated.set(),
        settle_seconds=0.05,
    )
    watcher.start()
    try:
        # Baseline first (nothing finished yet), then the batch lands.
        time.sleep(0.05)
        ops.queue = {"running": 0, "pending": 4}
        ops.history = {f"pid-{i}": history_entry() for i in range(1, 11)}
        for _ in range(40):
            time.sleep(0.01)
        assert calls == [], "the pod must not stop while the queue is not empty"

        ops.queue = {"running": 0, "pending": 0}
        assert terminated.wait(10.0), "the watcher never terminated the pod"
    finally:
        watcher.stop()
    assert calls == ["terminate"]
    assert not watcher.alive


def test_watcher_ignores_generations_finished_before_start(tmp_path):
    ops = FakeOps(history={"old": history_entry()})
    settings = {
        "enabled": True,
        "auto_collect": True,
        "output_dir": str(tmp_path),
        "terminate_after": False,
    }
    watcher = comfy_watch.ComfyOutputWatcher(
        lambda: ops,
        settings=lambda: settings,
        probe=lambda *_a, **_k: True,
        interval=0.01,
        retry_interval=0.01,
    )
    watcher.start()
    try:
        # Let a few polls run: the already-finished generation is baselined.
        for _ in range(50):
            time.sleep(0.01)
        assert not (tmp_path / "MiniMax_H3_00042.mp4").exists()
        assert ops.downloads == []
        # A generation that finishes afterwards *is* collected.
        ops.history["new"] = history_entry()
        for _ in range(500):
            if (tmp_path / "MiniMax_H3_00042.mp4").exists():
                break
            time.sleep(0.01)
        assert (tmp_path / "MiniMax_H3_00042.mp4").exists()
    finally:
        watcher.stop()


def test_watcher_idles_when_disabled(tmp_path):
    ops = FakeOps(history={"pid-1": history_entry()})
    settings = {"enabled": False, "auto_collect": True, "output_dir": str(tmp_path)}
    watcher = comfy_watch.ComfyOutputWatcher(
        lambda: ops,
        settings=lambda: settings,
        probe=lambda *_a, **_k: True,
        interval=0.01,
        retry_interval=0.01,
    )
    watcher.start()
    try:
        time.sleep(0.2)
    finally:
        watcher.stop()
    assert ops.downloads == []
    assert ops.history == {"pid-1": history_entry()}
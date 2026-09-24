"""Tests for the GUI (headless non-UI logic + tkinter smoke when a display exists)."""

import gc
import json
import logging
import os
import queue
import re
import sys
import tempfile
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

#: macOS CI runners have no interactive session: Tk opens no window there, and
#: a Tk call then HANGS instead of raising, which is how a 4-hour CI stall
#: happened. The workflow sets this flag on macOS so the module is skipped;
#: Windows and Linux/xvfb keep running it. It is deliberately an explicit
#: opt-out rather than an auto-detect: "no display" is indistinguishable from
#: "slow to start" at import time.
if os.environ.get("MINIMAX_SKIP_GUI_TESTS"):
    pytest.skip(
        "MINIMAX_SKIP_GUI_TESTS is set (no interactive session on this runner)",
        allow_module_level=True,
    )

try:
    import tkinter as tk
except ImportError:  # pragma: no cover - tkinter is always present in the
    tk = None  # type: ignore[assignment]  # CI / display-bearing test runs

from launcher import gui
from launcher import health
from launcher.config import Config, load_config
from launcher.credentials import CredentialStore
from launcher.doctor import Diagnostic
from launcher.infra_recover import RecoveryResult
from launcher.runpod import PodNotFoundError as _PodNotFound


# ---------------------------------------------------------------------------
# .env parsing / discovery
# ---------------------------------------------------------------------------


def test_parse_env_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "\n"
        "RUNPOD_API_KEY=rp_abc\n"
        'QUOTED="hello world"\n'
        "SINGLE='v2'\n"
        "export EXPORTED=yes\n"
        "INLINE=value  # comment\n"
        "EMPTY=\n"
        "   INDENTED   =   spaced value   \n",
        encoding="utf-8",
    )
    values = gui.parse_env_file(env_file)
    assert values["RUNPOD_API_KEY"] == "rp_abc"
    assert values["QUOTED"] == "hello world"
    assert values["SINGLE"] == "v2"
    assert values["EXPORTED"] == "yes"
    assert values["INLINE"] == "value"
    assert "EMPTY" not in values
    assert values["INDENTED"] == "spaced value"


def test_load_env_file_does_not_override_existing_variables(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=from_file\nB=file_only\n", encoding="utf-8")
    env = {"A": "from_env"}
    loaded = gui.load_env_file(env_file, env)
    assert env["A"] == "from_env"
    assert env["B"] == "file_only"
    assert loaded == {"B": "file_only"}


def test_find_env_file_override_wins(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setenv(gui.ENV_FILE_VARIABLE, str(env_file))
    assert gui.find_env_file() == env_file

    monkeypatch.setenv(gui.ENV_FILE_VARIABLE, str(tmp_path / "missing.env"))
    assert gui.find_env_file() is None


def test_find_env_file_uses_candidates_in_order(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / ".env").write_text("X=1\n", encoding="utf-8")
    assert gui.find_env_file(cwd=tmp_path / "b", candidates=[tmp_path / "b"]) is None
    assert gui.find_env_file(candidates=[tmp_path / "a"]) == tmp_path / "a" / ".env"
    (tmp_path / "b" / ".env").write_text("Y=2\n", encoding="utf-8")
    assert gui.find_env_file(candidates=[tmp_path / "b", tmp_path / "a"]) == (
        tmp_path / "b" / ".env"
    )


# ---------------------------------------------------------------------------
# Persisted settings
# ---------------------------------------------------------------------------


def test_settings_roundtrip(tmp_path) -> None:
    settings = gui.GuiSettings(theme="light", minimize_on_close=False)
    gui.save_settings(settings, home=tmp_path)
    loaded = gui.load_settings(home=tmp_path)
    assert loaded.theme == "light"
    assert loaded.minimize_on_close is False


def test_settings_invalid_values_fall_back_to_defaults(tmp_path) -> None:
    (tmp_path / gui.SETTINGS_FILENAME).write_text(
        '{"theme": "neon", "minimize_on_close": "yes"}', encoding="utf-8"
    )
    settings = gui.load_settings(home=tmp_path)
    assert settings.theme == "dark"
    assert settings.minimize_on_close is True


def test_settings_missing_file_returns_defaults(tmp_path) -> None:
    settings = gui.load_settings(home=tmp_path)
    assert settings.theme == "dark"
    assert settings.minimize_on_close is True


def test_save_settings_is_atomic_and_reports_success(tmp_path) -> None:
    """``gui.json`` holds the catalog, presets and library.

    A direct ``write_text`` truncated by a kill left a file that no longer
    parsed, and the next save overwrote it with the defaults.
    """
    settings = gui.GuiSettings(theme="light")
    assert gui.save_settings(settings, home=tmp_path) is True
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert gui.load_settings(home=tmp_path).theme == "light"


def test_save_settings_reports_failure(tmp_path, monkeypatch) -> None:
    """The caller must be able to tell the user the truth."""
    import os

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    assert gui.save_settings(gui.GuiSettings(), home=tmp_path) is False


def test_save_settings_preserves_an_unreadable_file(tmp_path) -> None:
    """A corrupt gui.json is moved aside, never silently overwritten."""
    path = tmp_path / gui.SETTINGS_FILENAME
    path.write_text("{not json", encoding="utf-8")
    assert gui.save_settings(gui.GuiSettings(theme="light"), home=tmp_path) is True
    assert (tmp_path / (gui.SETTINGS_FILENAME + ".bak")).read_text(encoding="utf-8") == "{not json"
    assert gui.load_settings(home=tmp_path).theme == "light"


def test_save_settings_does_not_back_up_a_healthy_file(tmp_path) -> None:
    gui.save_settings(gui.GuiSettings(), home=tmp_path)
    gui.save_settings(gui.GuiSettings(theme="light"), home=tmp_path)
    assert not (tmp_path / (gui.SETTINGS_FILENAME + ".bak")).exists()


# ---------------------------------------------------------------------------
# Log capture (redaction)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------


def _fake_report(
    overall: str = "STOPPED",
    url: str = "http://127.0.0.1:10369",
    cause=None,
    remediation=None,
) -> SimpleNamespace:
    components = {
        "runpod": SimpleNamespace(status="UNKNOWN", severity="UNKNOWN"),
        "ssh": SimpleNamespace(status="STOPPED", severity="STOPPED"),
        "vllm": SimpleNamespace(status="UNKNOWN", severity="UNKNOWN"),
        "model": SimpleNamespace(status="Qwen/Qwen3.8-27B-FP8", severity="UNKNOWN"),
        "openfox": SimpleNamespace(status="STOPPED", severity="STOPPED"),
        "searxng": SimpleNamespace(status="UNKNOWN", severity="UNKNOWN"),
        "config": SimpleNamespace(status="VALID", severity="HEALTHY"),
    }
    return SimpleNamespace(
        components=components, overall=overall, url=url, cause=cause, remediation=remediation
    )


def test_build_status_snapshot_keeps_healthy_model_label() -> None:
    report = _fake_report()
    report.components["model"] = SimpleNamespace(
        status="Qwen/Qwen3.8-27B-FP8", severity="HEALTHY"
    )
    snapshot = gui.build_status_snapshot(
        None, diagnose_fn=lambda cfg, **kwargs: report
    )
    assert snapshot["components"]["model"] == "Qwen/Qwen3.8-27B-FP8"


def _fake_cipher() -> tuple:
    """Platform-neutral reversible cipher (mirrors tests/test_credentials.py)."""

    def protect(data: bytes) -> bytes:
        return b"CT:" + data[::-1]

    def unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"CT:"):
            raise ValueError("bad ciphertext")
        return blob[3:][::-1]

    return protect, unprotect


def test_credentials_state_labels(tmp_path) -> None:
    protect, unprotect = _fake_cipher()
    store = CredentialStore(
        path=tmp_path / "credentials.dpapi",
        protect=protect,
        unprotect=unprotect,
        apply_user_only_acl=False,
    )
    assert gui._credentials_state(store) == "NOT CONFIGURED"
    store.set("rp_key_value", "tpl_value")
    assert gui._credentials_state(store) == "CONFIGURED"
    store.clear()
    assert gui._credentials_state(store) == "NOT CONFIGURED"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def test_action_doctor_formats_report(monkeypatch) -> None:
    monkeypatch.setattr(
        gui,
        "run_diagnostics",
        lambda cfg, **kwargs: [
            Diagnostic("python", "OK", "3.12 fine"),
            Diagnostic("runpod", "SKIP", "no client"),
        ],
    )
    report = gui.action_doctor(Config())
    assert f"[{ 'OK':<5}] {'python':<20} 3.12 fine" in report
    assert f"[{'SKIP':<5}] {'runpod':<20} no client" in report


def test_action_start_passes_on_no_capacity(monkeypatch) -> None:
    for var in ("MODEL_ID", "MAX_CONTEXT", "LLM_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    started = {}

    def fake_start(config, **kwargs):
        started["on_no_capacity"] = kwargs.get("on_no_capacity")

    monkeypatch.setattr(gui.orchestrator, "start", fake_start)
    marker = lambda pod_id: True
    gui.action_start("fp8", on_no_capacity=marker)
    assert started["on_no_capacity"] is marker


def test_action_stop_delegates_to_orchestrator(monkeypatch) -> None:
    calls = {}

    def fake_stop(**kwargs):
        calls.update(kwargs)
        return {"agent": "stopped"}

    monkeypatch.setattr(gui.orchestrator, "stop", fake_stop)
    summary = gui.action_stop(terminate=False)
    assert calls["terminate"] is False
    assert "Pod agent: RunPod pod stopped (disk kept)." in summary


def test_action_stop_stack_scoped(monkeypatch) -> None:
    calls = {}

    def fake_stop(**kwargs):
        calls.update(kwargs)
        return {"comfy": "stopped"}

    monkeypatch.setattr(gui.orchestrator, "stop", fake_stop)
    gui.action_stop(terminate=False, stack="comfy")
    assert calls["stack"] == "comfy"


def test_action_stop_terminate_reports_outcome(monkeypatch) -> None:
    monkeypatch.setattr(
        gui.orchestrator, "stop", lambda **kwargs: {"agent": "terminated"}
    )
    assert "Pod agent: RunPod pod terminated (deleted)." in gui.action_stop(terminate=True)


def test_format_recovery_result() -> None:
    ok = RecoveryResult(
        action="reconnect_ssh",
        ok=True,
        before="STOPPED",
        after="CONNECTED",
        error_class=None,
        message="SSH tunnel re-established",
    )
    text = gui.format_recovery_result(ok)
    assert "succeeded" in text
    assert "before: STOPPED" in text
    assert "after: CONNECTED" in text

    failed = RecoveryResult(
        action="start_runpod",
        ok=False,
        before="STOPPED",
        after="STOPPED",
        error_class="action_failed",
        message="pod start failed",
    )
    assert "failed" in gui.format_recovery_result(failed)


def test_component_color_key() -> None:
    assert gui._component_color_key("RUNNING") == "ok"
    assert gui._component_color_key("CONNECTED") == "ok"
    assert gui._component_color_key("READY") == "ok"
    assert gui._component_color_key("STARTING") == "warn"
    assert gui._component_color_key("MISSING: RUNPOD_API_KEY") == "error"
    assert gui._component_color_key("NOT READY") == "error"
    assert gui._component_color_key("Qwen/Qwen3.8-27B-FP8") == "muted"


def test_preset_display_roundtrip() -> None:
    assert gui._display_to_preset(gui.COMFY_PRESET_NONE_LABEL) == ""
    assert gui._display_to_preset("dasiwa_mmh3v12") == "dasiwa_mmh3v12"
    assert gui._display_to_preset("  muse_director_seedhunt ") == "muse_director_seedhunt"
    assert gui._preset_to_display("") == gui.COMFY_PRESET_NONE_LABEL
    assert gui._preset_to_display("muse_director_seedhunt") == "muse_director_seedhunt"


def test_format_countdown() -> None:
    assert gui.format_countdown(0) == "00:00:00"
    assert gui.format_countdown(61) == "00:01:01"
    assert gui.format_countdown(3661) == "01:01:01"
    assert gui.format_countdown(-5) == "00:00:00"


def test_first_line_helper() -> None:
    assert gui._first_line("") == ""
    assert gui._first_line("a") == "a"
    assert gui._first_line("a\nb\nc") == "a"


# ---------------------------------------------------------------------------
# Window icon (taskbar)
# ---------------------------------------------------------------------------


def test_apply_window_icon_prefers_native_multi_size_ico(monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("iconbitmap path is Windows-first")
    ico = gui._brand_ico_path()
    assert ico is not None, "launcher/icon.ico must exist in the repo"

    class _Root:
        def __init__(self):
            self.bitmap = None

        def iconbitmap(self, path):
            self.bitmap = path

    root = _Root()
    gui.ForgeApp._apply_window_icon(SimpleNamespace(root=root, _icon_image=None))
    assert root.bitmap == str(ico)


def test_apply_window_icon_falls_back_without_ico(monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("iconbitmap path is Windows-first")
    monkeypatch.setattr(gui, "_brand_ico_path", lambda: None)
    monkeypatch.setattr(gui, "_load_brand_icon", lambda size: None)

    class _Root:
        def __init__(self):
            self.bitmap = None
            self.photo = None

        def iconbitmap(self, path):
            self.bitmap = path

        def iconphoto(self, default, *images):
            self.photo = images

    root = _Root()
    gui.ForgeApp._apply_window_icon(SimpleNamespace(root=root, _icon_image=None))
    assert root.bitmap is None
    assert root.photo is None


def test_power_action_command() -> None:
    assert gui.power_action_command("none") is None
    assert gui.power_action_command("shutdown") == ["shutdown", "/s", "/t", "0"]
    assert gui.power_action_command("sleep") == [
        "rundll32.exe",
        "powrprof.dll,SetSuspendState",
        "0,1,0",
    ]


def test_run_power_action_shutdown(monkeypatch) -> None:
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gui.subprocess, "run", fake_run)
    assert "shutting down" in gui.run_power_action("shutdown")
    assert calls == [["shutdown", "/s", "/t", "0"]]


def test_run_power_action_sleep(monkeypatch) -> None:
    monkeypatch.setattr(
        gui.subprocess, "run", lambda cmd, **kwargs: SimpleNamespace(returncode=0)
    )
    assert "sleep" in gui.run_power_action("sleep")


def test_env_target_dir_is_repo_root_in_dev() -> None:
    assert gui.env_target_dir() == Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def test_main_dispatches_gui(monkeypatch) -> None:
    from launcher import main as main_module

    calls = []
    monkeypatch.setattr(
        "launcher.gui.run_gui", lambda: calls.append(1) or 0
    )
    assert main_module.main(["gui"]) == 0
    assert calls == [1]


# ---------------------------------------------------------------------------
# tkinter smoke (skipped when no display is available)
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(monkeypatch):
    tk = pytest.importorskip("tkinter")
    monkeypatch.setattr(gui, "_HAS_PYSTRAY", False)
    monkeypatch.setattr(gui, "_HAS_PIL", False)
    # Never write the real user's gui.json during tests (the close tests flip
    # minimize_on_close, and _quit() persists it via save_settings).
    monkeypatch.setattr(gui, "save_settings", lambda settings, home=None: None)
    # Hermetic settings: never read the real user's gui.json either, so the
    # initial stack (agent/comfy), theme and preset are deterministic.
    monkeypatch.setattr(
        gui, "load_settings", lambda home=None: gui.GuiSettings()
    )
    # Keep the refresh worker threads hermetic: ForgeApp.__init__ spawns a
    # background refresh that calls build_status_snapshot -> diagnose -> the
    # real health/searxng probes. Those would open real sockets from a daemon
    # thread that may outlive the test and crash the process on teardown.
    monkeypatch.setattr(gui.health, "check_comfy", lambda *a, **k: False)
    monkeypatch.setattr(gui.health, "check_train", lambda *a, **k: False)
    # The startup refresh is scheduled with after_idle() and runs on a worker
    # thread that reads Tk variables (``_load_config_safe`` ->
    # ``self._mode.get()``). No test enters mainloop(), so that Tcl round-trip
    # from a foreign thread costs ~1 s and ends in "main thread is not in main
    # loop" — the worker posted a refresh_error anyway, and the teardown's
    # thread join paid for it. Nothing here asserts on the startup snapshot
    # (the tests that need dashboard state hand a snapshot to
    # ``_apply_status``), so the startup refresh is a no-op.
    monkeypatch.setattr(gui.ForgeApp, "_refresh", lambda self, force=False: None)
    # The first-run wizard must not open a modal dialog during tests.
    monkeypatch.setattr(gui, "first_run_needs_credentials", lambda config: False)
    # The startup ComfyUI release check must never hit the GitHub API.
    monkeypatch.setattr(
        gui, "fetch_comfyui_releases", lambda *a, **k: ["v9.9.9", "v9.9.8"]
    )
    # ... and the model-size probe (one HEAD per model URL) must never touch
    # the network either.
    monkeypatch.setattr(gui, "probe_model_size", lambda *a, **k: None)
    for var in (
        "RUNPOD_API_KEY",
        "RUNPOD_POD_ID",
        "RUNPOD_TEMPLATE_ID",
        "LLM_PROFILE",
        "MODEL_ID",
        "LAUNCHER_INFRA_RECOVERY",
        "MINIMAX_LAUNCHER_ENV_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    # Isolate the launcher state dir (runtime.json / pending_stop.json) from
    # the real user's so a real pending pod-stop can never fire in tests.
    monkeypatch.setenv(
        "MINIMAX_LAUNCHER_STATE_DIR",
        tempfile.mkdtemp(prefix="forge-test-state-"),
    )
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    application = gui.ForgeApp(root, refresh_interval_ms=60_000)
    yield application
    try:
        for _ in range(10):
            root.update()
        if root.winfo_exists():
            application._quit()
            for _ in range(5):
                root.update()
    except tk.TclError:
        pass
    finally:
        # Drop the tkinter Variable references and finalize them here on the
        # main thread (still the Tk interpreter thread). Otherwise a daemon
        # worker thread (refresh/tray) can become the last reference holder
        # and GC them later, and Variable.__del__ -> globalunsetvar raises
        # "main thread is not in main loop" from a non-interpreter thread.
        for name in dir(application):
            var = getattr(application, name, None)
            if isinstance(var, tk.Variable) and var._tk is not None:
                var._tk = None
                var._name = None
        # Sever the app's references to the (now destroyed) widgets so no
        # leftover daemon thread can drag them through a GC on its thread.
        for name in dir(application):
            obj = getattr(application, name, None)
            if name in ("_recolor", "_icon_refs", "_scrollables", "_annuaire_cards"):
                setattr(application, name, [])
            elif name == "_icon_image":
                setattr(application, name, None)
            elif name == "_tb_panel_widgets":
                setattr(application, name, set())
            elif isinstance(obj, (list, tuple)) and all(
                isinstance(item, tk.Widget) for item in obj
            ):
                setattr(application, name, [])
            elif isinstance(obj, dict) and obj and all(
                isinstance(item, tk.Widget) for item in obj.values()
            ):
                setattr(application, name, {})
            elif isinstance(obj, tk.Widget):
                setattr(application, name, None)
        application._last_snapshot = None
        application._last_config = None
        gc.collect()


# -- theme-mode-aware color helpers -----------------------------------------
# Under the ttkbootstrap theme the ttk widgets carry their colors in ttk
# styles (no per-widget bg/fg option); the fallback mode uses plain options.
# These read the effective value either way.


def _style_value(app, widget, option: str) -> str:
    if app._tb_style is not None and isinstance(widget, tk.ttk.Widget):
        # A widget with no explicit style uses the theme's class default
        # ("TLabel", "TButton"…) — looking up an empty style name returns an
        # unrelated fallback, so resolve the default name first.
        style = str(widget.cget("style")) or f"T{type(widget).__name__}"
        try:
            return str(
                app.root.tk.call("ttk::style", "lookup", style, "-" + option)
            )
        except tk.TclError:
            return None
    return str(widget.cget(option))


def _fg_of(app, widget) -> str:
    return _style_value(app, widget, "foreground")


def _bg_of(app, widget) -> str:
    return _style_value(app, widget, "background")


def _is_danger_button(app, button) -> bool:
    if app._tb_style is not None:
        return "danger" in str(button.cget("style"))
    return str(button.cget("bg")) == app._pal["error"]


def _toggle_fill(app, button) -> str:
    """The state a toggle button is painted in: ok / error / neutral."""
    if app._tb_style is not None:
        style = str(button.cget("style"))
        if "success" in style:
            return "ok"
        if "danger" in style:
            return "error"
        return "neutral"
    pal = app._pal
    bg = str(button.cget("bg"))
    if bg == pal["ok"]:
        return "ok"
    if bg == pal["error"]:
        return "error"
    return "neutral"


def test_app_construction_and_theme_switching(app) -> None:
    # The hot theme switch re-derives the palette: framed panels sit on the
    # theme's raised surface in both modes.
    app.root.update_idletasks()
    assert app._overall_label.cget("text").startswith("State")
    assert _bg_of(app, app._dash_frame) == app._pal["surface"]

    app._theme_choice.set("Clair")
    app._on_theme_change()
    app.root.update_idletasks()
    assert app.settings.theme == "light"
    assert _bg_of(app, app._dash_frame) == app._pal["surface"]
    assert app._pal["bg"] != app._pal["surface"]

    app._theme_choice.set("Dark")
    app._on_theme_change()
    assert app.settings.theme == "dark"
    assert _bg_of(app, app._dash_frame) == app._pal["surface"]


def test_app_log_panel_appends_and_trims(app) -> None:
    for index in range(gui.MAX_LOG_LINES + 5):
        app._append_log("info", f"line {index}")
    content = app._log_text.get("1.0", "end-1c")
    lines = [line for line in content.splitlines() if line]
    assert len(lines) <= gui.MAX_LOG_LINES
    assert "[INFO] line 3\n" not in content
    assert "[INFO] line 5\n" in content


def test_app_finish_action_updates_log_and_busy(app) -> None:
    app._finish_action("Démarrage", True, "Démarrage terminé (test).")
    app.root.update_idletasks()
    content = app._log_text.get("1.0", "end-1c")
    assert "Démarrage terminé (test)." in content
    assert app._busy is False
    assert str(app._bar_start_btn.cget("state")) == "normal"


def test_app_finish_action_generic_payload_stays_single_line(app) -> None:
    app._finish_action("Démarrage", True, "ligne 1\nligne 2\nligne 3")
    app.root.update_idletasks()
    assert app._status_line.cget("text") == "ligne 1"
    content = app._log_text.get("1.0", "end-1c")
    assert "ligne 2" in content


def test_finish_action_recover_with_a_string_payload_reports_the_error(
    app, monkeypatch
) -> None:
    """``_run`` turns a raised exception into ``str(exc)``.

    The recover branch annotated the payload as a ``RecoveryResult`` without
    checking, so any failure inside action_recover produced
    "AttributeError: 'str' object has no attribute 'ok'" — the fatal popup
    instead of the real error, and no journal line at all.
    """
    shown: list = []
    monkeypatch.setattr(app, "_show_error", lambda *a, **k: shown.append(a))
    app._finish_action("recover:restart_comfy", False, "ConfigError: boom")
    app.root.update_idletasks()
    content = app._log_text.get("1.0", "end-1c")
    assert "ConfigError: boom" in content
    assert shown, "the real error must reach the user"
    assert "failed" in app._status_line.cget("text")


def test_poll_survives_an_exception_in_a_handler(app, monkeypatch) -> None:
    """One bad queue item must not stop the pump for the rest of the session.

    The re-arm used to be the last statement of an unguarded body: an
    exception left the window alive but frozen — no journal, no status, every
    action button disabled.
    """
    calls: list = []
    handled: list = []

    def flaky(item):
        handled.append(item)
        if item == "boom":
            raise RuntimeError("bad item")

    monkeypatch.setattr(app, "_handle", flaky)
    monkeypatch.setattr(app, "_sync_scrollables", lambda: calls.append("synced"))
    # Drain whatever the fixture queued first.
    while True:
        try:
            app._queue.get_nowait()
        except queue.Empty:
            break
    app._queue.put("boom")
    app._queue.put("fine")
    app._poll()
    # The remaining items are still drained, the tick completes, and the next
    # tick is armed. (The error is journaled through the same queue, hence the
    # extra trailing entries.)
    assert handled[:2] == ["boom", "fine"]
    assert calls == ["synced"]
    assert app.root.tk.call("after", "info") != ""


def test_app_finish_action_failure_shows_error_in_log(app, monkeypatch) -> None:
    monkeypatch.setattr(app, "_show_error", lambda title, message: None)
    app._finish_action("Arrêt", False, "boom error")
    app.root.update_idletasks()
    content = app._log_text.get("1.0", "end-1c")
    # The failure is journalled with its detail (the wording is French, the
    # technical detail is preserved verbatim).
    assert "failed" in content
    assert "boom error" in content


def test_app_close_without_tray_quits(app, monkeypatch) -> None:
    tk = pytest.importorskip("tkinter")
    app._minimize_on_close.set(False)
    app._on_close()
    try:
        assert not app.root.winfo_exists()
    except tk.TclError:
        pass  # the Tk application itself was destroyed


def test_app_close_with_tray_minimizes(app, monkeypatch) -> None:
    tk = pytest.importorskip("tkinter")

    class _FakeTray:
        def __init__(self):
            self.notifications = []

        def notification(self, title, message):
            self.notifications.append((title, message))

        def stop(self):
            pass

    app._tray = _FakeTray()
    app._minimize_on_close.set(True)
    app._on_close()
    assert app.root.winfo_exists()
    assert not app.root.winfo_viewable()
    assert len(app._tray.notifications) == 1
    app._quit()
    try:
        assert not app.root.winfo_exists()
    except tk.TclError:
        pass


# ---------------------------------------------------------------------------
# Stack selection (agent / comfy) + ComfyUI options
# ---------------------------------------------------------------------------


def test_settings_roundtrip_comfy_fields(tmp_path) -> None:
    s = gui.GuiSettings(
        theme="dark",
        auto_refresh=False,
        minimize_on_close=False,
        stack="comfy",
        comfy_preset="muse_director_seedhunt",
        comfy_tier="auto_perso",
        comfy_workflows="t2v,i2v",
        comfy_sage_attention="true",
        comfy_access="direct",
        comfy_ntfy_topic="topic1",
        comfy_personal_repo="user/repo",
        comfyui_version="v0.34.4",
        diffusion_url="https://huggingface.co/x/y/resolve/main/d.safetensors",
        video_vae_url="https://huggingface.co/x/y/resolve/main/v.safetensors",
        audio_vae_url="https://huggingface.co/x/y/resolve/main/a.safetensors",
        text_encoder_url="https://huggingface.co/x/y/resolve/main/t.safetensors",
        tae_url="https://huggingface.co/x/y/resolve/main/tae.safetensors",
        upscaler_url="https://huggingface.co/x/y/resolve/main/u.safetensors",
        frame_interp_url="https://huggingface.co/x/y/resolve/main/f.safetensors",
    )
    gui.save_settings(s, home=tmp_path)
    assert gui.load_settings(home=tmp_path) == s


# ---------------------------------------------------------------------------
# ComfyUI release fetch (GitHub releases, startup refresh)
# ---------------------------------------------------------------------------


class _FakeReleaseResponse:
    def __init__(self, payload: list) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeReleaseResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False


def test_fetch_comfyui_releases_paginates_newest_first(monkeypatch) -> None:
    page1 = [{"tag_name": f"v0.{35 - i}.0"} for i in range(10)]
    page2 = [{"tag_name": "v0.25.0"}, {"tag_name": "v0.24.0"}]
    requested: list = []

    def fake_urlopen(req, timeout=None):
        requested.append(req.full_url)
        page = int(re.search(r"[?&]page=(\d+)", req.full_url).group(1))
        return _FakeReleaseResponse(page1 if page == 1 else page2)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tags = gui.fetch_comfyui_releases(max_pages=2)
    expected = [t["tag_name"] for t in page1] + [t["tag_name"] for t in page2]
    assert tags == expected
    assert requested[0].startswith(
        "https://api.github.com/repos/Comfy-Org/ComfyUI/releases"
    )
    assert all("per_page=10" in url for url in requested)


def test_fetch_comfyui_releases_stops_on_short_page(monkeypatch) -> None:
    page = [{"tag_name": "v0.34.5"}, {"tag_name": "v0.34.4"}]
    requested: list = []

    def fake_urlopen(req, timeout=None):
        requested.append(req.full_url)
        return _FakeReleaseResponse(page)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tags = gui.fetch_comfyui_releases(max_pages=3)
    assert tags == ["v0.34.5", "v0.34.4"]
    assert len(requested) == 1


def test_fetch_comfyui_releases_skips_prereleases(monkeypatch) -> None:
    page = [
        {"tag_name": "v0.34.5", "prerelease": False},
        {"tag_name": "v0.35.0-beta.1", "prerelease": True},
        {"prerelease": False},
        {"tag_name": "v0.34.4"},
    ]

    def fake_urlopen(req, timeout=None):
        return _FakeReleaseResponse(page)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert gui.fetch_comfyui_releases() == ["v0.34.5", "v0.34.4"]


def test_fetch_comfyui_releases_raises_on_error(monkeypatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "rate limited", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        gui.fetch_comfyui_releases()


def test_fetch_comfyui_releases_raises_on_empty(monkeypatch) -> None:
    def fake_urlopen(req, timeout=None):
        return _FakeReleaseResponse([])

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        gui.fetch_comfyui_releases()


def test_version_change_persists_immediately(app, monkeypatch) -> None:
    # C1: picking a version writes it to gui.json at once — not only on
    # start/quit — so the choice survives however the app is closed.
    saved: list = []
    monkeypatch.setattr(
        gui, "save_settings", lambda settings, home=None: saved.append(settings.comfyui_version)
    )
    app._comfy_version_combo.set("v0.34.4")
    app._on_version_change(None)
    assert app._comfy_version.get() == "v0.34.4"
    assert saved and saved[-1] == "v0.34.4"


def test_version_typed_ref_persists_raw(app, monkeypatch) -> None:
    saved: list = []
    monkeypatch.setattr(
        gui, "save_settings", lambda settings, home=None: saved.append(settings.comfyui_version)
    )
    app._comfy_version_combo.set("abc123def")
    app._on_version_change(None)
    assert app._comfy_version.get() == "abc123def"
    assert saved and saved[-1] == "abc123def"


def _pump_until(app, predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.root.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_startup_release_check_lists_releases_and_keeps_auto(app, monkeypatch) -> None:
    # The fixture mocks the fetch to return a canned list; the startup worker
    # must land it on the main thread. With nothing pinned the choice stays
    # "auto" — the pod then follows the latest release at every boot instead of
    # being silently frozen on whatever was newest the day the GUI first ran.
    saved: list = []
    monkeypatch.setattr(
        gui, "save_settings", lambda settings, home=None: saved.append(settings.comfyui_version)
    )
    # The combo starts on « auto », so waiting on it would return before the
    # worker's result lands: wait for the fetched list itself.
    expected = [gui.COMFYUI_VERSION_AUTO_LABEL, "v9.9.9", "v9.9.8"]
    assert _pump_until(
        app,
        lambda: list(app._comfy_version_combo.cget("values"))[:3] == expected,
    ), "startup release check did not apply the fetched list"
    assert app._comfy_version_combo.get() == gui.COMFYUI_VERSION_AUTO_LABEL
    assert app._comfy_version.get() == ""
    assert saved and saved[-1] == ""
    assert "latest release" in app._comfy_version_hint.cget("text")


def test_user_override_of_default_release_persists(app, monkeypatch) -> None:
    # The startup check pre-selects the newest release; the user can still
    # pick another one before starting the pod and that choice is kept.
    saved: list = []
    monkeypatch.setattr(
        gui, "save_settings", lambda settings, home=None: saved.append(settings.comfyui_version)
    )
    # Nothing pinned -> "auto": the installer follows the latest release at
    # every pod boot instead of freezing on whatever was newest that day.
    assert _pump_until(
        app, lambda: app._comfy_version_combo.get() == gui.COMFYUI_VERSION_AUTO_LABEL
    )
    assert app._comfy_version.get() == ""
    app._comfy_version_combo.set("v9.9.8")
    app._on_version_change(None)
    assert app._comfy_version.get() == "v9.9.8"
    assert saved and saved[-1] == "v9.9.8"


def test_startup_release_check_preserves_user_pin(app, monkeypatch) -> None:
    # A deliberate pin must survive the startup release check: it used to be
    # overwritten with the newest release on EVERY launch, so a rollback to an
    # older version never stuck.
    saved: list = []
    monkeypatch.setattr(
        gui, "save_settings", lambda settings, home=None: saved.append(settings.comfyui_version)
    )
    assert _pump_until(
        app, lambda: app._comfy_version_combo.get() == gui.COMFYUI_VERSION_AUTO_LABEL
    )
    # Pin an older release, then simulate a later startup fetch.
    app._comfy_version_combo.set("v0.34.5")
    app._on_version_change(None)
    app._apply_comfy_releases(["v9.9.9", "v9.9.8"])
    assert app._comfy_version_combo.get() == "v0.34.5"
    assert app._comfy_version.get() == "v0.34.5"
    # The off-list pin stays selectable in the refreshed dropdown.
    assert "v0.34.5" in list(app._comfy_version_combo.cget("values"))


def test_auto_version_selection_clears_the_pin(app) -> None:
    """Picking the auto entry must store "" — i.e. leave COMFYUI_COMMIT unset."""
    app._comfy_version_combo.set("v9.9.8")
    app._on_version_change(None)
    assert app._comfy_version.get() == "v9.9.8"

    app._comfy_version_combo.set(gui.COMFYUI_VERSION_AUTO_LABEL)
    app._on_version_change(None)
    assert app._comfy_version.get() == ""
    assert gui.version_from_display(gui.COMFYUI_VERSION_AUTO_LABEL) == ""
    assert gui.version_display("") == gui.COMFYUI_VERSION_AUTO_LABEL
    assert gui.version_display("v0.36.0") == "v0.36.0"


def test_window_close_persists_comfy_settings(app, monkeypatch) -> None:
    # C1: closing the window minimizes to the tray by default (the app keeps
    # running) — the ComfyUI choices must still be persisted at that moment,
    # or a later fresh launch would read the stale gui.json.
    saved: list = []
    monkeypatch.setattr(
        gui, "save_settings", lambda settings, home=None: saved.append(settings.comfyui_version)
    )

    class FakeTray:
        def notification(self, *args) -> None:
            pass

        def stop(self) -> None:
            pass

    app._minimize_on_close.set(True)
    app._tray = FakeTray()
    app._comfy_version.set("v0.33.4")
    app._on_close()
    assert app.root.winfo_exists()
    assert saved and saved[-1] == "v0.33.4"


def test_tier_change_persists_immediately(app, monkeypatch) -> None:
    saved: list = []
    monkeypatch.setattr(
        gui, "save_settings", lambda settings, home=None: saved.append(settings.comfy_tier)
    )
    app._comfy_tier_ui.set("Perso")
    app._on_tier_change(None)
    assert app._comfy_tier.get() == "perso"
    assert saved and saved[-1] == "perso"


def test_tier_change_roundtrip_label_to_raw(app) -> None:
    app._comfy_tier.set("perso")
    app._comfy_tier_ui.set("Auto")
    app._on_tier_change(None)
    assert app._comfy_tier.get() == "auto"
    app._comfy_tier_ui.set("Auto + Perso")
    app._on_tier_change(None)
    assert app._comfy_tier.get() == "auto_perso"
    # An unlisted display value (shouldn't happen on a readonly combobox)
    # falls back to the default mode rather than crashing.
    app._comfy_tier_ui.set("quelconque")
    app._on_tier_change(None)
    assert app._comfy_tier.get() == "auto"


def test_comfy_catalog_defaults_and_enabled_urls() -> None:
    catalog = gui._default_comfy_catalog()
    assert "diffusion" in catalog
    assert "audio_vae" in catalog
    # The dasiwa-validated set is enabled by default; alternates ship disabled.
    enabled = gui._enabled_urls_from_catalog(catalog)
    assert len(enabled["audio_vae"]) == 1
    assert enabled["audio_vae"][0].endswith("minimax_h3_audio_vae_fp32.safetensors")
    # Two video VAEs exist in the catalog; only the int8 one is enabled.
    assert len(catalog["video_vae"]) == 2
    assert len(enabled["video_vae"]) == 1
    assert enabled["video_vae"][0].endswith("minimax_h3_video_vae_int8_convrot.safetensors")


def test_comfy_catalog_normalize_drops_bad_entries() -> None:
    catalog = gui._normalize_catalog(
        {"audio_vae": [{"name": "ok", "url": "https://x/y.safetensors", "enabled": True},
                       {"name": "", "url": "https://x/bad.safetensors"},
                       "not-a-dict"]}
    )
    assert len(catalog["audio_vae"]) == 1
    assert catalog["audio_vae"][0]["name"] == "ok"
    # Categories not in the input keep their built-in entries.
    assert len(catalog["diffusion"]) >= 1


def test_comfy_presets_roundtrip() -> None:
    # Legacy {category: [url]} presets are migrated to the new 3-type shape.
    presets = gui._normalize_presets(
        {"Favori Daisiwa": {"audio_vae": ["https://x/a.safetensors"],
                             "diffusion": ["https://x/d.safetensors"]}}
    )
    assert "Favori Daisiwa" in presets
    urls = {m["url"] for m in presets["Favori Daisiwa"]["models"]}
    assert urls == {"https://x/a.safetensors", "https://x/d.safetensors"}
    assert presets["Favori Daisiwa"]["nodes"] == []
    assert presets["Favori Daisiwa"]["workflows"] == []


def test_comfy_presets_new_shape_roundtrip() -> None:
    presets = gui._normalize_presets(
        {"P": {"models": [{"url": "https://x/a.safetensors", "target": "vae/a.safetensors"}],
               "nodes": [{"url": "https://github.com/o/r.git"}],
               "workflows": [{"github": "o/r|main|wf"}]}}
    )
    assert presets["P"]["models"] == [{"url": "https://x/a.safetensors", "target": "vae/a.safetensors", "name": ""}]
    assert presets["P"]["nodes"] == [
        {"url": "https://github.com/o/r.git", "post_install": "", "name": "", "note": "", "enabled": True}
    ]
    assert presets["P"]["workflows"] == [
        {"github": "o/r|main|wf", "name": "", "note": "", "enabled": True}
    ]


def test_named_preset_selection_survives_refresh(app) -> None:
    app._comfy_presets["Favori"] = {"models": [], "nodes": [], "workflows": []}
    app._comfy_presets["Autre"] = {"models": [], "nodes": [], "workflows": []}
    app._refresh_comfy_preset_combo()
    app._comfy_preset_combo.set("Favori")
    app._refresh_comfy_preset_combo()
    assert app._comfy_preset_combo.get() == "Favori"
    # A name no longer present clears the selection (don't leave a stale one).
    del app._comfy_presets["Favori"]
    app._refresh_comfy_preset_combo()
    assert app._comfy_preset_combo.get() == ""


def test_named_preset_load_preserves_selection_and_content(app) -> None:
    app._mode.set("comfy")
    app._on_stack_change()
    app._comfy_tier.set("perso")
    app._comfy_presets["Favori"] = {
        "models": [{"url": "https://x/a.safetensors", "target": "vae/a.safetensors"}],
        "nodes": [{"url": "https://github.com/o/r.git", "post_install": ""}],
        "workflows": [{"github": "o/r|main|wf"}],
    }
    app._refresh_comfy_preset_combo()
    app._comfy_preset_combo.set("Favori")
    app._comfy_load_preset()
    # The rebuild inside _comfy_load_preset must not wipe the selection.
    assert app._comfy_preset_combo.get() == "Favori"
    assert app._comfy_edit_nodes == [{"url": "https://github.com/o/r.git", "post_install": ""}]
    assert app._comfy_edit_workflows == [{"github": "o/r|main|wf"}]


def test_named_preset_delete_uses_selected_name(app, monkeypatch) -> None:
    import tkinter.messagebox as mb

    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    app._comfy_presets["Favori"] = {"models": [], "nodes": [], "workflows": []}
    app._refresh_comfy_preset_combo()
    app._comfy_preset_combo.set("Favori")
    app._comfy_load_preset()
    app._comfy_delete_preset()
    assert "Favori" not in app._comfy_presets
    assert app._comfy_preset_combo.get() == ""


def test_settings_legacy_tier_values_migrate_to_auto_perso(tmp_path) -> None:
    # Pre-C2 gui.json values picked a standard-tier flavor; the closest new
    # mode is "Auto + Perso" (standard stack kept, personal additions kept).
    for legacy in ("light", "pruned", "pruned_scaled", "balanced", "max", "hybrid"):
        (tmp_path / "gui.json").write_text(
            json.dumps({"stack": "comfy", "comfy_tier": legacy}), encoding="utf-8"
        )
        assert gui.load_settings(home=tmp_path).comfy_tier == "auto_perso"


def test_settings_new_tier_values_kept_invalid_defaults_to_auto(tmp_path) -> None:
    for value in ("auto", "auto_perso", "perso"):
        (tmp_path / "gui.json").write_text(
            json.dumps({"comfy_tier": value}), encoding="utf-8"
        )
        assert gui.load_settings(home=tmp_path).comfy_tier == value
    (tmp_path / "gui.json").write_text(
        json.dumps({"comfy_tier": "bogus"}), encoding="utf-8"
    )
    assert gui.load_settings(home=tmp_path).comfy_tier == "auto"
    assert gui.load_settings(home=tmp_path / "missing").comfy_tier == "auto"


def test_comfy_frames_never_hidden_only_disabled(app) -> None:
    # Point 4: the options of the inactive mode stay visible (never
    # pack_forget) — they are only disabled.
    for stack in ("agent", "comfy"):
        app._stack.set(stack)
        app._on_stack_change()
        app.root.update_idletasks()
        assert app._comfy_frame.winfo_manager() == "pack"
        assert app._annuaire_frame.winfo_manager() == "pack"
        assert app._comfy_models_frame.winfo_manager() == "pack"


def test_app_comfy_overrides_empty_preset_means_none(app) -> None:
    # "Aucun (tier standard)" must survive as an empty preset, not silently
    # fall back to the default preset.
    app._comfy_preset.set("")
    assert app._comfy_overrides()["preset"] == ""


def _collect_checkbuttons(widget, out):
    for child in widget.winfo_children():
        try:
            if child.winfo_class() in ("Checkbutton", "TCheckbutton"):
                out.append(child)
        except tk.TclError:
            pass
        _collect_checkbuttons(child, out)
    return out


def test_preset_catalog_checkboxes_usable_in_auto_tier(app) -> None:
    # Regression: in "Auto" tier the catalog checkboxes were disabled, so
    # unchecking a model did nothing and the change was lost on reload.
    app._mode.set("comfy")
    app._on_stack_change()
    app._comfy_tier.set("auto")
    app._update_perso_gating()
    cbs = _collect_checkbuttons(app._comfy_model_rows, [])
    assert cbs, "expected catalog checkboxes"
    for cb in cbs:
        assert str(cb.cget("state")) != "disabled"


def test_preset_model_edit_persists_through_save_reload(app) -> None:
    # Full round-trip: uncheck a model, save (same logic as the dialog),
    # reload -> the model stays unchecked.
    app._mode.set("comfy")
    app._on_stack_change()
    cat = "text_encoder"
    idx = next(
        i for i, e in enumerate(app._comfy_models[cat]) if "nvfp4" in e["url"].lower()
    )
    app._comfy_toggle_model(cat, idx, tk.BooleanVar(app.root, value=True))
    app._comfy_presets["P"] = {
        "models": gui._enabled_models_from_catalog(app._comfy_models),
        "nodes": [], "workflows": [],
    }
    app._refresh_comfy_preset_combo()
    app._comfy_preset_combo.set("P")
    app._comfy_load_preset()
    assert app._comfy_models[cat][idx]["enabled"] is True

    app._comfy_toggle_model(cat, idx, tk.BooleanVar(app.root, value=False))
    app._comfy_presets["P"] = {
        "models": gui._enabled_models_from_catalog(app._comfy_models),
        "nodes": [], "workflows": [],
    }
    app._comfy_load_preset()
    assert app._comfy_models[cat][idx]["enabled"] is False
    assert all(
        "nvfp4" not in m["url"].lower() for m in app._comfy_presets["P"]["models"]
    )


def test_preset_model_list_keeps_non_catalog_models(app) -> None:
    # A preset whose models are NOT in the category catalog (e.g. a CivitAI
    # checkpoint) must still be loaded and saved verbatim: the cards only
    # show the catalog, so the list is the source of truth for save/load.
    app._comfy_presets["V2"] = {
        "models": [
            {"url": "https://civitai.red/api/download/models/3314675?fileId=3203130",
             "target": "diffusion_models/MiniMaxH3/v2.safetensors"},
        ],
        "nodes": [], "workflows": [],
    }
    app._refresh_comfy_preset_combo()
    app._comfy_preset_combo.set("V2")
    app._comfy_load_preset()
    assert app._comfy_edit_models == [{
        "url": "https://civitai.red/api/download/models/3314675?fileId=3203130",
        "target": "diffusion_models/MiniMaxH3/v2.safetensors",
        "name": "",
        "enabled": True,
    }]


def test_catalog_toggle_syncs_preset_model_list(app) -> None:
    # Toggling a catalog checkbox adds/removes the matching entry in the
    # preset's model list (the catalog cards are now the only editor), so the
    # list always holds what will be saved with the preset.
    cat = "text_encoder"
    app._comfy_edit_models = []
    url = app._comfy_models[cat][0]["url"]
    app._comfy_toggle_model(cat, 0, tk.BooleanVar(app.root, value=True))
    assert any(m["url"] == url for m in app._comfy_edit_models)
    app._comfy_toggle_model(cat, 0, tk.BooleanVar(app.root, value=False))
    assert not any(m["url"] == url for m in app._comfy_edit_models)


def test_preset_load_disambiguates_shared_url(app) -> None:
    # Two catalog entries can share a URL across categories (the NVFP4 AWQ
    # text encoder also added under Video VAE). A preset keeping only the
    # text_encoder one must NOT re-enable the video_vae one on reload.
    shared = (
        "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/"
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
    )
    app._comfy_models["video_vae"] = [
        {"name": "nvfp4_awq", "url": shared, "enabled": True},
    ]
    app._comfy_models["text_encoder"] = [
        {"name": "Text encoder NVFP4 AWQ", "url": shared, "enabled": True},
    ]
    app._comfy_presets["P"] = {
        "models": [
            {"url": shared,
             "target": "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"},
        ],
        "nodes": [], "workflows": [],
    }
    app._refresh_comfy_preset_combo()
    app._comfy_preset_combo.set("P")
    app._comfy_load_preset()
    assert app._comfy_models["video_vae"][0]["enabled"] is False
    assert app._comfy_models["text_encoder"][0]["enabled"] is True

    # Uncheck the video_vae one, save, reload -> stays unchecked.
    app._comfy_toggle_model("video_vae", 0, tk.BooleanVar(app.root, value=False))
    app._comfy_presets["P"] = {
        "models": gui._enabled_models_from_catalog(app._comfy_models),
        "nodes": [], "workflows": [],
    }
    app._comfy_load_preset()
    assert app._comfy_models["video_vae"][0]["enabled"] is False
    assert app._comfy_models["text_encoder"][0]["enabled"] is True


def test_comfy_overrides_auto_mode_ignores_personal(app) -> None:
    # C2: Auto = standard stack only. The preset is forced to "" and the
    # catalog URLs / categories are cleared from the pod env.
    o = app._comfy_overrides()
    assert o["tier"] == "auto"
    assert o["preset"] == ""
    assert o["custom_model_categories"] is None
    for key in (
        "diffusion_url", "video_vae_url", "audio_vae_url", "text_encoder_url",
        "tae_url", "upscaler_url", "frame_interp_url",
    ):
        assert o[key] == ""


def test_comfy_overrides_carry_the_reception_options(app) -> None:
    """The reception options travel as launcher-side overrides (not pod env)."""
    o = app._comfy_overrides()
    assert o["auto_collect"] is False
    assert o["notify_windows"] is False
    assert o["notify_ntfy"] is False
    assert o["terminate_after_generation"] is False
    assert o["outputs_dir"] is None
    app._comfy_auto_collect.set(True)
    app._comfy_notify_windows.set(True)
    app._comfy_notify_ntfy.set(True)
    app._comfy_terminate_after.set(True)
    app._comfy_outputs_dir.set(r"D:\Vidéos")
    o = app._comfy_overrides()
    assert o["auto_collect"] is True
    assert o["notify_windows"] is True
    assert o["notify_ntfy"] is True
    assert o["terminate_after_generation"] is True
    assert o["outputs_dir"] == r"D:\Vidéos"


def test_reception_options_are_persisted_and_reloaded(tmp_path) -> None:
    settings = gui.GuiSettings()
    settings.comfy_auto_collect = True
    settings.comfy_notify_windows = True
    settings.comfy_notify_ntfy = True
    settings.comfy_terminate_after = True
    settings.comfy_outputs_dir = r"D:\Vidéos"
    gui.save_settings(settings, home=tmp_path)
    reloaded = gui.load_settings(home=tmp_path)
    assert reloaded.comfy_auto_collect is True
    assert reloaded.comfy_notify_windows is True
    assert reloaded.comfy_notify_ntfy is True
    assert reloaded.comfy_terminate_after is True
    assert reloaded.comfy_outputs_dir == r"D:\Vidéos"


def test_reception_watcher_state_follows_the_options(app, monkeypatch) -> None:
    """The watcher thread reads a plain dict, never a Tk variable."""
    state = app._refresh_comfy_watch_state()
    assert state["enabled"] is False
    app._comfy_auto_collect.set(True)
    app._comfy_outputs_dir.set(r"D:\Vidéos")
    monkeypatch.setenv("NTFY_SERVER", "https://ntfy.example.org")
    state = app._refresh_comfy_watch_state()
    assert state["enabled"] is True
    assert state["auto_collect"] is True
    assert state["output_dir"] == r"D:\Vidéos"
    assert state["terminate_after"] is False
    # The launcher publishes on the same server as the pod.
    assert state["ntfy_server"] == "https://ntfy.example.org"
    # The thread-facing accessor never hands out the live dict.
    assert app._comfy_watch_settings() == state
    assert app._comfy_watch_settings() is not state


def test_reception_folder_field_is_gated_on_the_collection(app) -> None:
    """The folder has no effect while the collection is off."""
    app._mode.set("comfy")
    app._comfy_auto_collect.set(False)
    app._update_receive_gating()
    assert str(app._comfy_outputs_dir_entry.cget("state")) == "disabled"
    app._comfy_auto_collect.set(True)
    app._update_receive_gating()
    assert str(app._comfy_outputs_dir_entry.cget("state")) == "normal"
    # The mode gate still owns the field outside the ComfyUI mode.
    app._set_comfy_frames_enabled(False)
    assert str(app._comfy_outputs_dir_entry.cget("state")) == "disabled"


def test_sync_comfy_watcher_starts_and_stops_with_the_options(app, monkeypatch) -> None:
    monkeypatch.setattr(gui, "save_settings", lambda settings, home=None: None)
    started: list = []
    stopped: list = []

    class FakeWatcher:
        alive = True

        def __init__(self, *_args, **_kwargs):
            started.append(self)

        def start(self):
            pass

        def stop(self):
            stopped.append(self)

    original = gui.comfy_watch.ComfyOutputWatcher
    gui.comfy_watch.ComfyOutputWatcher = FakeWatcher
    try:
        app._comfy_auto_collect.set(True)
        app._sync_comfy_watcher()
        assert len(started) == 1
        # Idempotent: a second refresh does not spawn a second thread.
        app._sync_comfy_watcher()
        assert len(started) == 1
        app._comfy_auto_collect.set(False)
        app._sync_comfy_watcher()
        assert stopped and app._comfy_watcher is None
        # A suppressed watcher stays down until the operator toggles again.
        app._comfy_watcher_suppressed = True
        app._comfy_auto_collect.set(True)
        app._sync_comfy_watcher()
        assert len(started) == 1
        app._on_comfy_receive_change()
        assert len(started) == 2
    finally:
        gui.comfy_watch.ComfyOutputWatcher = original
        app._comfy_watcher = None
        app._comfy_watcher_suppressed = False


def test_comfy_overrides_perso_mode_sends_selected_catalog(app) -> None:
    app._comfy_tier.set("perso")
    app._comfy_presets["Mon preset"] = {
        "models": [], "nodes": [{"url": "https://github.com/o/r.git"}], "workflows": [],
    }
    app._comfy_preset.set("Mon preset")
    o = app._comfy_overrides()
    assert o["tier"] == "perso"
    # Hardcoded installer presets are no longer exposed: H3_PRESETS is empty
    # and only the *active* user preset is sent.
    assert o["preset"] == ""
    assert list(o["user_presets"]) == ["Mon preset"]
    assert o["custom_model_categories"] == (
        "diffusion,video_vae,audio_vae,text_encoder,tae,upscaler,frame_interp"
    )
    assert o["diffusion_url"].endswith("DasiwaMinimaxH3_dasiwaREF2VAHybridV1.safetensors")
    assert o["audio_vae_url"].endswith("minimax_h3_audio_vae_fp32.safetensors")


def test_comfy_overrides_auto_perso_mode_keeps_preset_and_catalog(app) -> None:
    app._comfy_tier.set("auto_perso")
    app._comfy_presets["Mon preset"] = {"models": [], "nodes": [], "workflows": []}
    app._comfy_preset.set("Mon preset")
    o = app._comfy_overrides()
    assert o["tier"] == "auto_perso"
    assert o["preset"] == ""
    assert list(o["user_presets"]) == ["Mon preset"]
    assert o["custom_model_categories"] == (
        "diffusion,video_vae,audio_vae,text_encoder,tae,upscaler,frame_interp"
    )


def test_comfy_overrides_perso_without_active_preset_sends_none(app) -> None:
    app._comfy_tier.set("perso")
    app._comfy_preset.set("")
    o = app._comfy_overrides()
    assert o["preset"] == ""
    assert o["user_presets"] == {}


def test_comfy_overrides_unticked_category_absent(app) -> None:
    app._comfy_tier.set("perso")
    var = app._comfy_model_vars[0]  # first diffusion entry (enabled by default)
    assert var.get() is True
    var.set(False)
    app._comfy_toggle_model("diffusion", 0, var)
    o = app._comfy_overrides()
    assert "diffusion" not in o["custom_model_categories"]
    assert o["diffusion_url"] == ""


def test_app_comfy_panel_in_own_tab_not_buried_by_journal(app) -> None:
    # Regression: the ComfyUI options used to be packed after the expanding
    # journal (zero height -> invisible). They now live in their own tab,
    # never in the same container as the journal text (which itself was
    # merged into the dashboard tab).
    app._mode.set("comfy")
    app._on_stack_change()
    app.root.update_idletasks()
    assert app._comfy_frame.winfo_manager() == "pack"
    assert app._annuaire_frame.winfo_manager() == "pack"
    assert app._comfy_frame.master is app._comfy_tab_inner
    assert app._annuaire_frame.master is app._biblio_inner
    assert app._comfy_frame.master is not app._log_text.master
    assert app._log_text.master is app._log_inner
    # The journal now lives inside the dashboard tab.
    assert app._log_frame.master is app._dash_inner


def test_journal_level_filters(app) -> None:
    app._append_log("info", "une info")
    app._append_log("warn", "un avertissement")
    app._append_log("error", "une erreur")
    app._log_filter_warn.set(False)
    app._log_filter_changed()
    content = app._log_text.get("1.0", "end-1c")
    assert "un avertissement" not in content
    assert "une info" in content
    assert "une erreur" in content
    app._log_filter_warn.set(True)
    app._log_filter_changed()
    content = app._log_text.get("1.0", "end-1c")
    assert "un avertissement" in content


def test_journal_clear(app) -> None:
    app._append_log("info", "a supprimer")
    app._log_clear()
    assert app._log_entries == []
    assert app._log_text.get("1.0", "end-1c").strip() == ""


def test_journal_copy_to_clipboard(app) -> None:
    app._log_clear()
    app._append_log("info", "ligne copiable")
    app._log_copy()
    assert "ligne copiable" in app.root.clipboard_get()


def test_journal_autoscroll_off_keeps_view(app) -> None:
    app._log_autoscroll.set(False)
    for i in range(200):
        app._append_log("info", f"ligne {i}")
    _top, bottom = app._log_text.yview()
    assert bottom < 1.0


def test_navigation_menu_lists_only_real_tabs(app) -> None:
    labels = [
        app._navigation_menu.entrycget(i, "label")
        for i in range(app._navigation_menu.index("end") + 1)
    ]
    assert "Clés API" not in labels
    assert labels == list(gui.NAV_TAB_LABELS)
    # Every navigation shortcut must point at a tab that actually exists.
    notebook_labels = [app._notebook.tab(t, "text") for t in app._notebook.tabs()]
    assert set(gui.NAV_TAB_LABELS) <= set(notebook_labels)


def _settings_dialog(app):
    """Open the quick-settings dialog (C3: it hosts the Clés API section)."""
    app._open_settings()
    app.root.update()
    return app._settings_dialog


def _ancestor_of_class(widget, cls_names):
    names = (cls_names,) if isinstance(cls_names, str) else tuple(cls_names)
    node = widget
    while node is not None:
        if type(node).__name__ in names:
            return node
        node = getattr(node, "master", None)
    return None


def _cred_block_class():
    # tk.LabelFrame in the fallback, ttk.Labelframe under the theme.
    return ("LabelFrame", "Labelframe")


def test_prompt_recreate_yes(app, monkeypatch) -> None:
    import threading
    import tkinter.messagebox as mb

    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    holder = {}
    event = threading.Event()
    app._prompt_recreate("pod_x", holder, event)
    assert holder["approved"] is True
    assert event.is_set()


def test_prompt_recreate_no(app, monkeypatch) -> None:
    import threading
    import tkinter.messagebox as mb

    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: False)
    holder = {}
    event = threading.Event()
    app._prompt_recreate("pod_x", holder, event)
    assert holder["approved"] is False
    assert event.is_set()


def test_handle_dispatches_prompt_recreate(app, monkeypatch) -> None:
    import threading
    import tkinter.messagebox as mb

    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    holder = {}
    event = threading.Event()
    app._handle(("prompt_recreate", "pod_x", holder, event))
    assert holder["approved"] is True
    assert event.is_set()


# --- OpenFox port pre-flight + pod-unavailable presentation ------------------


class _EmptyRuntimeState:
    def load(self):
        return {}


def test_finish_action_pod_unavailable_is_quiet(app, monkeypatch) -> None:
    """A pod-unavailability failure stays discreet: warning, no red popup."""
    shown = []
    monkeypatch.setattr(
        app, "_show_error", lambda title, message: shown.append((title, message))
    )
    payload = (
        "RunPod has no free GPU on the pod's host (HTTP 400): There are not "
        "enough free GPUs on the host machine to start this pod."
    )
    app._finish_action("Démarrage", False, payload)

    assert shown == []
    assert any(level == "warn" for level, _ in app._log_entries)
    assert not any(level == "error" for level, _ in app._log_entries)


def test_finish_action_generic_error_still_shows_popup(app, monkeypatch) -> None:
    shown = []
    monkeypatch.setattr(
        app, "_show_error", lambda title, message: shown.append((title, message))
    )
    app._finish_action("Démarrage", False, "SSH tunnel failed authentication")
    assert len(shown) == 1
    assert any(level == "error" for level, _ in app._log_entries)


def test_comfy_dashboard_info_degrades_without_pod(monkeypatch) -> None:
    from launcher.config import load_config as real_load_config
    from launcher.pod_registry import PodRegistry

    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    config = real_load_config(stack="comfy")
    monkeypatch.setattr(gui.health, "check_comfy", lambda *a, **k: False)
    info = gui._comfy_dashboard_info(config, runpod=None, registry=PodRegistry())
    assert info["url"] == config.comfy_base_url()
    assert info["queue"] is None
    assert info["vram"] is None
    assert info["cost_per_hour"] is None
    assert info["preset"] == config.comfy.preset


def test_comfy_dashboard_info_reports_queue_vram_cost(monkeypatch) -> None:
    from types import SimpleNamespace

    from launcher.config import load_config as real_load_config
    from launcher.pod_registry import PodRecord

    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    config = real_load_config(stack="comfy", comfy={"preset": "dasiwa_mmh3v12"})
    monkeypatch.setattr(gui.health, "check_comfy", lambda *a, **k: True)
    monkeypatch.setattr(
        gui.health,
        "get_comfy_stats",
        lambda *a, **k: {
            # The real /system_stats schema: flat vram_total / vram_free.
            "devices": [
                {
                    "name": "RTX A6000",
                    "vram_total": 48 * 1024 ** 3,
                    "vram_free": 8 * 1024 ** 3,
                }
            ]
        },
    )
    monkeypatch.setattr(
        gui.health, "get_comfy_queue", lambda *a, **k: {"running": 1, "pending": 2}
    )
    runpod = SimpleNamespace(
        get_gpu_types=lambda: [
            {"gpu": {"id": "NVIDIA RTX A6000", "price": {"secure": 0.53}}}
        ]
    )
    registry = SimpleNamespace(
        load=lambda stack: PodRecord(
            pod_id="p1", name="n", created_at=0.0,
            gpu_id="NVIDIA RTX A6000", gpu_count=1, stack="comfy",
        )
    )
    info = gui._comfy_dashboard_info(config, runpod=runpod, registry=registry)
    assert info["queue"] == "1 en cours / 2 en attente"
    assert info["vram"] == "40.0 / 48.0 Go"
    assert info["cost_per_hour"] == "0.53 $/h"
    assert info["url"] == config.comfy_base_url()


# ---------------------------------------------------------------------------
# Tab ComfyUI: type selector, zone result "Lister", resume de config
# ---------------------------------------------------------------------------


def test_pod_list_result_goes_to_journal(app) -> None:
    app._finish_action("Lister le pod", True, "lora-a.safetensors\nlora-b.safetensors")
    app.root.update_idletasks()
    joined = "\n".join(msg for _, msg in app._log_entries)
    assert "lora-a.safetensors" in joined
    assert "lora-b.safetensors" in joined


def test_annuaire_install_empty_dl_url_warns(app) -> None:
    app._annuaire = [
        {"type": "lora", "name": "", "page_url": "", "dl_url": "", "note": "",
         "mode": "", "trigger_words": "", "model": ""}
    ]
    app._annuaire_install(0)
    assert any("Empty source" in msg for _, msg in app._log_entries)


def test_comfy_summary_text() -> None:
    catalog = gui._default_comfy_catalog()
    text = gui.comfy_summary_text(
        "dasiwa_mmh3v12", "auto", "t2v,i2v", catalog
    )
    assert "models" in text
    assert "tier Auto" in text
    # In Auto mode the preset is ignored — the summary says so, it never
    # shows the (disabled) preset choice as if it were applied.
    assert "ignored" in text
    assert "dasiwa_mmh3v12" not in text
    assert "workflows t2v,i2v" in text
    assert "tier Auto + Perso" in gui.comfy_summary_text("x", "auto_perso", "t2v", catalog)
    assert "tier Perso" in gui.comfy_summary_text("x", "perso", "t2v", catalog)
    # Non-auto mode with no preset selected -> the summary says "aucun".
    assert "none" in gui.comfy_summary_text("", "auto_perso", "t2v", catalog)
    # Perso with neither preset nor checked model: ComfyUI could not
    # generate — explicit warning.
    empty = {c: [] for c in gui.COMFY_MODEL_CATEGORIES}
    assert "generate" in gui.comfy_summary_text("", "perso", "t2v", empty)
    # ... but a selected preset alone is enough to generate (no warning).
    assert "generate" not in gui.comfy_summary_text("dasiwa_mmh3v12", "perso", "t2v", empty)


def test_comfy_summary_text_shows_active_preset_content() -> None:
    empty = {c: [] for c in gui.COMFY_MODEL_CATEGORIES}
    content = {
        "models": [{"url": "https://x/a"}],
        "nodes": [{"url": "https://github.com/o/r.git"}, {"url": "https://github.com/o/s.git"}],
        "workflows": [{"github": "o/r|main|wf"}],
    }
    text = gui.comfy_summary_text("MonPreset", "auto_perso", "t2v", empty, content)
    assert "MonPreset (1 models, 2 nodes, 1 workflows)" in text


def test_comfy_summary_label_tracks_selection(app) -> None:
    initial = app._comfy_summary_label.cget("text")
    assert "models" in initial

    def _count(text: str) -> int:
        return int(text.split("models")[0].strip().split()[-1])

    before = _count(initial)
    # The first diffusion entry ships enabled: flip it off.
    var = app._comfy_model_vars[0]
    assert var.get() is True
    var.set(False)
    app._comfy_toggle_model("diffusion", 0, var)
    app.root.update_idletasks()
    assert _count(app._comfy_summary_label.cget("text")) == before - 1


# ---------------------------------------------------------------------------
# S15 : une seule barre de controle persistante (duplications consolidees)
# ---------------------------------------------------------------------------


def _footer_button_texts(app) -> list:
    texts = []
    for child in app._footer.winfo_children():
        if isinstance(child, tk.Frame):
            for sub in child.winfo_children():
                if isinstance(sub, (tk.Button, tk.ttk.Button)):
                    texts.append(str(sub.cget("text")))
    return texts


def test_comfy_toggle_green_when_tunnel_up(app) -> None:
    # ComfyUI stack, tunnel access: the toggle reflects the local SSH
    # tunnel (green when alive).
    app._mode.set("comfy")
    app._apply_status(
        _full_snapshot(stack="comfy", ssh="CONNECTED", comfy={"access": "tunnel"})
    )
    assert str(app._toggle_comfy.cget("state")) == "normal"
    assert _toggle_fill(app, app._toggle_comfy) == "ok"


def test_comfy_toggle_red_when_tunnel_down_and_recovery_enabled(app) -> None:
    app._mode.set("comfy")
    app._apply_status(
        _full_snapshot(
            stack="comfy", ssh="STOPPED", recovery_enabled=True,
            comfy={"access": "tunnel"},
        )
    )
    assert str(app._toggle_comfy.cget("state")) == "normal"
    assert _toggle_fill(app, app._toggle_comfy) == "error"


def test_comfy_toggle_red_greyed_without_recovery(app) -> None:
    # The restore path is a recovery action: with LAUNCHER_INFRA_RECOVERY off
    # the down toggle stays visible but disabled, and says exactly what to
    # enable.
    app._mode.set("comfy")
    app._apply_status(
        _full_snapshot(
            stack="comfy", ssh="STOPPED", recovery_enabled=False,
            comfy={"access": "tunnel"},
        )
    )
    assert str(app._toggle_comfy.cget("state")) == "disabled"
    assert "LAUNCHER_INFRA_RECOVERY" in app._tooltip_texts.get(app._toggle_comfy, "")


def test_comfy_toggle_greyed_in_direct_mode(app) -> None:
    # Direct access has no local process at all: the toggle is disabled even
    # with recovery enabled, and explains why.
    app._mode.set("comfy")
    app._apply_status(
        _full_snapshot(
            stack="comfy", ssh="STOPPED", recovery_enabled=True,
            comfy={"access": "direct"},
        )
    )
    assert str(app._toggle_comfy.cget("state")) == "disabled"
    assert (
        app._tooltip_texts.get(app._toggle_comfy)
        == gui.TECHNICAL_TOOLTIPS["comfy_direct_no_tunnel"]
    )


def test_comfy_toggle_click_stops_tunnel_when_up(app, monkeypatch) -> None:
    import tkinter.messagebox as mb

    stopped = []
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(
        gui, "action_comfy_tunnel_stop",
        lambda config, state=None: stopped.append(config),
    )
    monkeypatch.setattr(app, "_run", lambda label, fn: fn())
    app._last_config = Config()
    app._mode.set("comfy")
    app._apply_status(
        _full_snapshot(stack="comfy", ssh="CONNECTED", comfy={"access": "tunnel"})
    )
    app._toggle_comfy_action()
    assert len(stopped) == 1


def test_comfy_toggle_click_restores_down_tunnel_via_recovery(app, monkeypatch) -> None:
    import tkinter.messagebox as mb

    labels = []
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(app, "_run", lambda label, fn: labels.append(label))
    app._last_config = Config()
    app._mode.set("comfy")
    app._apply_status(
        _full_snapshot(
            stack="comfy", ssh="STOPPED", recovery_enabled=True,
            comfy={"access": "tunnel"},
        )
    )
    app._toggle_comfy_action()
    assert labels == ["recover:Restart ComfyUI"]


def test_train_mode_start_dispatches_to_the_train_stack(app, monkeypatch) -> None:
    """« Démarrer » in train mode must start the TRAIN pod, not the agent one."""
    calls: list = []
    monkeypatch.setattr(
        gui, "action_start", lambda *a, **k: calls.append((a, k)) or "ok"
    )
    monkeypatch.setattr(gui.ForgeApp, "_run", lambda self, label, work: work())
    monkeypatch.setattr(
        gui.ForgeApp, "_save_comfy_settings", lambda self: None
    )
    app._mode.set("train")
    app._on_stack_change()
    assert app._stack.get() == "train"
    app._start_action()
    assert calls
    _args, kwargs = calls[-1]
    assert kwargs["stack"] == "train"
    assert kwargs["train"] == app._train_overrides()


def test_train_fetch_models_checkboxes_persist(app, monkeypatch) -> None:
    """The ~45 GB boot download is a cost decision: it must be visible."""
    saved: list = []
    monkeypatch.setattr(
        gui, "save_settings", lambda settings, home=None: saved.append(settings)
    )
    assert set(app._train_fetch_vars) == {"krea2", "klein", "minimax", "tools"}
    assert all(not var.get() for var in app._train_fetch_vars.values())
    assert "No pre-download" in str(app._train_fetch_hint.cget("text"))

    app._train_fetch_vars["klein"].set(True)
    app._on_train_fetch_change()
    assert app.settings.train_fetch_models == "klein"
    assert app._train_overrides()["fetch_models"] == "klein"
    assert "klein" in str(app._train_fetch_hint.cget("text"))
    assert saved


def test_action_comfy_tunnel_stop_not_running(tmp_path, monkeypatch) -> None:
    from launcher import runtime_state
    from launcher.runtime_state import RuntimeState

    monkeypatch.setattr(runtime_state, "pid_is_alive", lambda pid: False)
    state = RuntimeState(tmp_path / "runtime.json")
    result = gui.action_comfy_tunnel_stop(Config(), state=state)
    assert "not running" in result


def test_action_comfy_tunnel_stop_stops_only_comfy_tunnel(tmp_path, monkeypatch) -> None:
    import time

    from launcher import runtime_state
    from launcher.runtime_state import ProcessEntry, RuntimeState

    state = RuntimeState(tmp_path / "runtime.json")
    entries = state.load()
    entries["tunnels:comfy"] = ProcessEntry(
        pid=4321, label="tunnels:comfy", port=8188, marker="comfy",
        created_at=time.time(),
    )
    entries["tunnels:llm"] = ProcessEntry(
        pid=4444, label="tunnels:llm", port=8000, marker="llm",
        created_at=time.time(),
    )
    state.save(entries)
    killed: list = []
    monkeypatch.setattr(runtime_state, "pid_is_alive", lambda pid: True)
    monkeypatch.setattr(
        runtime_state, "terminate_pid",
        lambda pid: (killed.append(pid), True)[1],
    )
    result = gui.action_comfy_tunnel_stop(Config(), state=state)
    assert killed == [4321]
    assert "unchanged" in result
    remaining = state.load()
    assert "tunnels:comfy" not in remaining
    assert "tunnels:llm" in remaining  # the agent tunnel is untouched


# ---------------------------------------------------------------------------
# S14 : layout adaptatif - les lignes denses ne debordent jamais a la largeur min
# ---------------------------------------------------------------------------


def _max_subrow_req(frame):
    """Largest requested width among a row's sub-rows (or the row itself)."""
    import tkinter as tk

    subrows = [c for c in frame.winfo_children() if isinstance(c, tk.Frame)]
    if subrows:
        return max(c.winfo_reqwidth() for c in subrows)
    return frame.winfo_reqwidth()


def _dense_rows(app):
    """Rows flagged as dense in the ergonomics rework.

    All of them must fit at the minimum window width; the ones that actually
    overflow (every row except ``version_row``, which already fits) are split
    across stacked sub-rows.
    """
    comfy = app._comfy_frame.winfo_children()
    models = app._comfy_models_frame.winfo_children()
    return {
        "comfy_row1": comfy[0],
        "comfy_row2": comfy[1],
        # The version row lives in the options frame (3rd child; the Accès
        # hint moved onto the Workflows/Access sub-row) since it is
        # tier-independent; the models frame now starts with the preset row.
        "version_row": comfy[2],
        "preset_row": models[0],
        "footer": app._footer,
    }


def test_dense_rows_never_overflow_min_width(app) -> None:
    # Margin covers the vertical scrollbar + the frame paddings, so the
    # available content width at the minimum window size is min_width - 40.
    avail = app.root.minsize()[0] - 40
    for name, frame in _dense_rows(app).items():
        assert _max_subrow_req(frame) <= avail, (
            f"{name} requests {_max_subrow_req(frame)}px > {avail}px "
            "available at the minimum window width"
        )


def test_dense_rows_wrap_on_multiple_subrows(app) -> None:
    import tkinter as tk

    rows = _dense_rows(app)
    # version_row already fits on a single line and is intentionally not split;
    # every other dense row wraps onto at least two stacked sub-rows.
    for name, frame in rows.items():
        if name == "version_row":
            continue
        subrows = [c for c in frame.winfo_children() if isinstance(c, tk.Frame)]
        assert len(subrows) >= 2, f"{name} must wrap onto >= 2 sub-rows"


# ---------------------------------------------------------------------------
# Actions destructives: differentiation visuelle + confirmations contextualisees
# ---------------------------------------------------------------------------


def test_stop_pod_confirmation_states_concrete_consequences(app, monkeypatch) -> None:
    # The confirmation is a Forge-themed dialog now (the native messagebox
    # ignored the theme), but it must still spell out the consequences.
    captured: dict = {}

    def fake_confirm(title, message, **kwargs):
        captured["title"] = title
        captured["message"] = message
        return False

    monkeypatch.setattr(app, "_ask_confirm", fake_confirm)
    app._stop_action(True)
    assert "permanently" in captured["message"].lower()
    assert "interrupt" in captured["message"]
    assert captured["title"] == "Terminate the pod"


def test_stop_pod_confirm_is_themed_not_native(app) -> None:
    # Regression guard: the destructive confirmation must go through the
    # Forge dialog helper, never through tkinter.messagebox.
    source = Path(gui.__file__).read_text(encoding="utf-8")
    stop = source.split("def _stop_action", 1)[1].split("def ", 1)[0]
    assert "messagebox" not in stop


def test_credentials_clear_confirmation_states_concrete_consequences(app, monkeypatch) -> None:
    import tkinter.messagebox as mb

    captured: dict = {}

    def fake_askyesno(title, message, **kwargs):
        captured["message"] = message
        return False

    monkeypatch.setattr(mb, "askyesno", fake_askyesno)
    app._credentials_clear()
    assert "permanently" in captured["message"].lower()
    assert "retype" in captured["message"]


def test_preset_delete_confirmation_states_concrete_consequences(app, monkeypatch) -> None:
    import tkinter.messagebox as mb

    app._comfy_presets["MonPreset"] = {"tae": ["https://x/y.safetensors"]}
    app._comfy_preset_combo.set("MonPreset")
    captured: dict = {}

    def fake_askyesno(title, message, **kwargs):
        captured["message"] = message
        return False

    monkeypatch.setattr(mb, "askyesno", fake_askyesno)
    app._comfy_delete_preset()
    assert "MonPreset" in captured["message"]
    assert "permanently" in captured["message"].lower()
    assert "pod" in captured["message"]  # explains what is NOT touched


# ---------------------------------------------------------------------------
# Indicateur de progression (operations multi-etapes du demarrage)
# ---------------------------------------------------------------------------


def test_map_progress_event_comfy() -> None:
    assert gui.map_progress_event("comfy", "Creating RunPod comfy pod x") == (
        (0,),
        False,
    )
    assert gui.map_progress_event("comfy", "Pod is RUNNING") == ((0,), True)
    assert gui.map_progress_event(
        "comfy", "Waiting for ComfyUI readiness at http://127.0.0.1:8188"
    ) == ((1,), False)
    assert gui.map_progress_event("comfy", "ComfyUI ready (preset dasiwa)") == (
        (1,),
        True,
    )
    # An agent-only line must not map on the comfy step list.
    assert gui.map_progress_event("comfy", "LLM API ready (x)") is None


def test_apply_progress_event_cascades_done() -> None:
    states = ["pending"] * 5
    gui.apply_progress_event(states, (1,), False)
    assert states == ["pending", "active", "pending", "pending", "pending"]
    gui.apply_progress_event(states, (3,), True)
    assert states == ["done", "done", "done", "done", "pending"]
    # A done step is never demoted back to active.
    gui.apply_progress_event(states, (2,), False)
    assert states == ["done", "done", "done", "done", "pending"]


# ---------------------------------------------------------------------------
# Badge d'etat global: actionnable (popover cause + remediation)
# ---------------------------------------------------------------------------


def test_overall_popover_lines_degraded() -> None:
    lines = gui.overall_popover_lines(_full_snapshot(
        overall="DEGRADED",
        cause="vLLM not ready",
        remediation="Redemarrer la pile",
    ))
    assert lines == [
        "State: DEGRADED",
        "Cause: vLLM not ready",
        "Remediation: Redemarrer la pile",
    ]


def test_overall_popover_lines_config_error() -> None:
    lines = gui.overall_popover_lines(_full_snapshot(config_error="port invalide"))
    assert lines[0] == "State: INVALID CONFIGURATION"
    assert "port invalide" in lines[1]
    assert len(lines) == 2


def test_overall_badge_click_shows_popover_from_anywhere(app) -> None:
    app._apply_status(_full_snapshot(
        overall="DEGRADED", cause="ssh down", remediation="reconnect ssh",
    ))
    assert "<Button-1>" in app._overall_label.bind()
    app._show_overall_popover()
    app.root.update()
    assert app._overall_popover is not None
    text = app._overall_popover_label.cget("text")
    assert "Cause: ssh down" in text
    assert "Remediation: reconnect ssh" in text
    app._hide_overall_popover()
    assert app._overall_popover is None


# ---------------------------------------------------------------------------
# Santé de la pile: tableau Composant/État/Info + badges + état vide
# ---------------------------------------------------------------------------


def _full_snapshot(
    *,
    overall: str = "HEALTHY",
    stack: str = "agent",
    runpod: str = "RUNNING",
    ssh: str = "CONNECTED",
    vllm: str = "READY",
    model: str = "Qwen/Qwen3.8-27B-FP8",
    openfox: str = "RUNNING",
    searxng: str = "READY",
    config: str = "VALID",
    url: str = "http://127.0.0.1:10369",
    comfy=None,
    cause=None,
    remediation=None,
    config_error=None,
    recovery_enabled: bool = False,
) -> dict:
    return {
        "overall": overall,
        "stack": stack,
        "cause": cause,
        "remediation": remediation,
        "url": url,
        "components": {
            "runpod": runpod,
            "ssh": ssh,
            "vllm": vllm,
            "model": model,
            "openfox": openfox,
            "searxng": searxng,
            "config": config,
        },
        "credentials": "CONFIGURED",
        "credentials_detail": {
            "state": "CONFIGURED",
            "runpod": True,
            "template": True,
            "comfy_template": False,
            "hf": True,
            "civitai": False,
        },
        "recovery_enabled": recovery_enabled,
        "profile": "fp8",
        "model_id": model,
        "config_error": config_error,
        "comfy": comfy,
    }


def _row_map(snapshot: dict, active_preset: str = "") -> dict:
    return {
        name: (state, info, color)
        for name, state, info, color in gui.build_health_table_rows(
            snapshot, active_preset=active_preset
        )
    }


def test_comfy_vram_text_reads_the_real_system_stats_schema() -> None:
    # ComfyUI reports vram_total/vram_free FLAT on each device; the nested
    # "vram" mapping the code used to read never existed, which is why the
    # dashboard never showed any VRAM.
    stats = {
        "system": {"os": "linux"},
        "devices": [
            {"name": "RTX A6000", "vram_total": 48 * 1024 ** 3,
             "vram_free": 38 * 1024 ** 3},
        ],
    }
    assert health.comfy_vram_text(stats) == "10.0 / 48.0 Go"


def test_comfy_vram_text_ignores_the_legacy_nested_shape() -> None:
    stats = {"devices": [{"name": "x", "vram": {"total": 1, "used": 1}}]}
    assert health.comfy_vram_text(stats) == ""


def test_comfy_vram_text_degrades_without_devices() -> None:
    assert health.comfy_vram_text({}) == ""
    assert health.comfy_vram_text({"devices": []}) == ""
    assert health.comfy_vram_text({"devices": [{"vram_total": 0, "vram_free": 0}]}) == ""
    assert health.comfy_vram_text({"devices": ["nope"]}) == ""
    # A second device is used when the first has no counters.
    stats = {"devices": [{"name": "a"}, {"vram_total": 8 * 1024 ** 3,
                                        "vram_free": 8 * 1024 ** 3}]}
    assert health.comfy_vram_text(stats) == "0.0 / 8.0 Go"


def test_comfy_vram_text_clamps_a_negative_used() -> None:
    # free > total (transient reporting artefact): never a negative usage.
    stats = {"devices": [{"vram_total": 1024 ** 3, "vram_free": 2 * 1024 ** 3}]}
    assert health.comfy_vram_text(stats) == "0.0 / 1.0 Go"


def test_build_health_table_rows_comfy_uses_the_active_preset() -> None:
    # The pod config carries no preset name for most setups (it is a GUI
    # setting): the effective active preset is shown instead of "—".
    snapshot = _full_snapshot(stack="comfy", comfy={"preset": "", "url": ""})
    rows = _row_map(snapshot, active_preset="Dasiwa V2")
    assert rows["Preset"][0] == "Dasiwa V2"
    assert rows["Preset"][2] == "ok"


def test_build_health_table_rows_comfy_hides_an_unknown_preset() -> None:
    # Neither the config nor the GUI knows a preset: no empty "—" row.
    snapshot = _full_snapshot(stack="comfy", comfy={"preset": "", "url": ""})
    assert "Preset" not in _row_map(snapshot)
    assert "Preset" not in _row_map(snapshot, active_preset="")


def test_build_health_table_rows_comfy_keeps_the_config_preset() -> None:
    snapshot = _full_snapshot(stack="comfy", comfy={"preset": "from_env"})
    assert _row_map(snapshot)["Preset"][0] == "from_env"


def _table_names(app) -> list:
    return [
        app._dash_table.item(iid, "values")[0]
        for iid in app._dash_table.get_children()
    ]


# ---------------------------------------------------------------------------
# Profil LLM: radios dynamiques (un par profil de la config)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Footer: barre de controle persistante (mode + demarrer/arreter)
# ---------------------------------------------------------------------------


def test_footer_minimize_checkbox_removed(app) -> None:
    # The "minimize on close" control lives only in the Parametres dialog now.
    import tkinter as tk

    checkbuttons = [
        child for child in app._footer.winfo_children()
        if isinstance(child, tk.Checkbutton)
    ]
    assert not checkbuttons, (
        "the footer must no longer repeat the minimize checkbox"
    )


# ---------------------------------------------------------------------------
# Notification banner (legers retours d'action)
# ---------------------------------------------------------------------------


def test_notify_shows_the_banner(app) -> None:
    # A 60 s timer: the auto-hide cannot fire inside the assertion window, so
    # this only asserts what it says it does. (The old version used 500 ms and
    # raced its own timer on a loaded machine.)
    app._notify("Preset saved.", "ok", after_ms=60_000)
    app.root.update()
    assert app._notify_label.cget("text") == "Preset saved."
    assert app._notify_label.winfo_manager() == "pack"
    app._hide_notify()
    app.root.update()
    assert app._notify_label.winfo_manager() == ""


def test_notify_autohides_when_the_timer_fires(app) -> None:
    app._notify("Preset saved.", "ok", after_ms=1)
    # Wait for the timer rather than assuming one update() is enough: a busy
    # event loop can still be draining the previous test's callbacks.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        app.root.update()
        if app._notify_label.winfo_manager() == "":
            break
        time.sleep(0.02)
    assert app._notify_label.winfo_manager() == ""
    assert app._notify_after is None


def test_notify_colors_follow_kind(app) -> None:
    app._notify("succes", "ok", after_ms=60_000)
    app.root.update()
    assert _fg_of(app, app._notify_label) == app._pal["ok"]
    app._notify("echec", "error", after_ms=60_000)
    app.root.update()
    assert _fg_of(app, app._notify_label) == app._pal["error"]
    app._hide_notify()


def test_finish_action_notifies(app, monkeypatch) -> None:
    items: list = []
    monkeypatch.setattr(
        app, "_notify", lambda msg, kind="info": items.append((msg, kind))
    )
    app._finish_action("Démarrage", True, "Démarrage terminé (test).")
    assert ("Démarrage terminé (test).", "ok") in items
    items.clear()
    monkeypatch.setattr(app, "_show_error", lambda title, message: None)
    app._finish_action("Arrêt", False, "boom error")
    assert any(kind == "error" for _msg, kind in items)


# ---------------------------------------------------------------------------
# Tooltips (parametres techniques)
# ---------------------------------------------------------------------------


def test_tooltip_helper_registers_text(app) -> None:
    app._tooltip(app._log_text, "aide de test")
    assert app._tooltip_texts[app._log_text] == "aide de test"


def test_tooltip_popover_shows_on_enter_and_hides_on_leave(app) -> None:
    target = app._log_text
    app._tooltip(target, "texte infobulle", delay_ms=1)
    target.event_generate("<Enter>")
    app.root.update()
    assert app._tooltip_popover is not None
    assert app._tooltip_popover.winfo_exists()
    assert "texte infobulle" in app._tooltip_popover_label.cget("text")
    target.event_generate("<Leave>")
    app.root.update()
    assert app._tooltip_popover is None


def test_tooltip_delay_cancellable_before_show(app) -> None:
    target = app._log_text
    app._tooltip(target, "tardive", delay_ms=60_000)
    target.event_generate("<Enter>")
    target.event_generate("<Leave>")
    app.root.update()
    assert app._tooltip_popover is None


# ---------------------------------------------------------------------------
# Shared spacing / indicator gap (point 0: one fix for every screen)
# ---------------------------------------------------------------------------


def _icon_buttons(widget):
    """Every button-like descendant whose label is a bare action glyph."""
    found = []
    for child in widget.winfo_children():
        try:
            text = str(child.cget("text")).strip()
        except Exception:  # noqa: BLE001 - not every widget has a text option
            text = ""
        if text in gui.ACTION_GLYPHS:
            found.append(child)
        found.extend(_icon_buttons(child))
    return found


def test_check_and_radio_indicators_are_spaced_from_their_label(app) -> None:
    # The indicator used to sit flush against its text on every screen: the
    # gap now comes from the shared styles instead of per-widget padding.
    if app._tb_style is None:
        pytest.skip("the shared ttk styles are a ttkbootstrap concern")
    for style_name in (
        "Forge.TCheckbutton",
        "Forge.TRadiobutton",
        "ForgePanel.TCheckbutton",
        "ForgePanel.TRadiobutton",
    ):
        margin = app.root.tk.splitlist(
            app.root.tk.call(
                "ttk::style", "lookup", style_name, "-indicatormargin"
            )
        )
        assert [str(part) for part in margin] == [
            "0", "0", str(gui.PAD_M), "0",
        ], style_name


def test_every_check_and_radio_uses_a_shared_forge_style(app) -> None:
    if app._tb_style is None:
        pytest.skip("the shared ttk styles are a ttkbootstrap concern")
    page_check = app._check(app._dash_inner, "case")
    page_radio = app._radio(app._dash_inner, "option")
    assert str(page_check.cget("style")) == "Forge.TCheckbutton"
    assert str(page_radio.cget("style")) == "Forge.TRadiobutton"
    panel_check = app._check(app._dash_inner, "case", panel=True)
    panel_radio = app._radio(app._dash_inner, "option", panel=True)
    assert str(panel_check.cget("style")) == "ForgePanel.TCheckbutton"
    assert str(panel_radio.cget("style")) == "ForgePanel.TRadiobutton"


def test_page_checkbutton_style_keeps_the_theme_colors(app) -> None:
    # The page-level style must only add the gap: hard-coding a background
    # there would paint the wrong surface wherever the widget sits.
    if app._tb_style is None:
        pytest.skip("the shared ttk styles are a ttkbootstrap concern")
    base_fg = app.root.tk.call("ttk::style", "lookup", "TCheckbutton", "-foreground")
    forge_fg = app.root.tk.call(
        "ttk::style", "lookup", "Forge.TCheckbutton", "-foreground"
    )
    assert str(forge_fg) == str(base_fg)


# ---------------------------------------------------------------------------
# Bundled icons (assets)
# ---------------------------------------------------------------------------


def test_asset_icon_path_rejects_unknown_names() -> None:
    assert gui._asset_icon_path("nope_not_an_icon.png") is None


def test_load_asset_icon_without_pil_returns_none(monkeypatch) -> None:
    # A PIL-less install must degrade to the text/glyph rendering rather
    # than crash: no image, no exception.
    monkeypatch.setattr(gui, "_HAS_PIL", False)
    assert gui._load_asset_icon("logo_openfoxforge_main.png", 40) is None


def test_load_asset_icon_preserves_the_aspect_ratio(monkeypatch) -> None:
    # Two shipped icons are 40x27: stretching them to a square would distort
    # the glyph, so they are centred on a transparent square instead.
    pytest.importorskip("PIL")
    monkeypatch.setattr(gui, "_HAS_PIL", True)
    wide = gui._load_asset_icon("category_audio_vae_amber.png", 20)
    assert wide is not None
    assert wide.size == (20, 20)
    # The art keeps its ratio: the top row is fully transparent (letterbox).
    alpha_top = [wide.getpixel((x, 0))[3] for x in range(wide.width)]
    assert max(alpha_top) == 0
    # ... and the middle row carries the glyph.
    alpha_mid = [wide.getpixel((x, wide.height // 2))[3] for x in range(wide.width)]
    assert max(alpha_mid) > 0


def test_shrink_icons_keeps_the_source_aspect_ratio() -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    path = gui._asset_icon_path("category_audio_vae_amber.png")
    assert path is not None
    with Image.open(path) as image:
        # 1536x1024 source -> 40x27 at the 40px box (thumbnail, not resize).
        assert image.size == (40, 27)


def test_load_asset_icon_missing_file_returns_none(monkeypatch) -> None:
    pytest.importorskip("PIL")
    monkeypatch.setattr(gui, "_HAS_PIL", True)
    assert gui._load_asset_icon("nope_not_an_icon.png", 16) is None


def test_load_asset_icon_unreadable_file_returns_none(monkeypatch, tmp_path) -> None:
    pytest.importorskip("PIL")
    monkeypatch.setattr(gui, "_HAS_PIL", True)
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png")
    monkeypatch.setattr(gui, "_asset_icon_path", lambda name: broken)
    assert gui._load_asset_icon("logo_openfoxforge_main.png", 16) is None


def test_health_status_icons_cover_every_state() -> None:
    # One icon per health state the table can render, so a state can never
    # silently fall back to the plain bullet.
    assert set(gui.HEALTH_STATUS_ICONS) == {"ok", "warn", "error", "idle"}
    for name in gui.HEALTH_STATUS_ICONS.values():
        assert name in gui.ICON_ASSETS


def test_photo_icon_returns_none_without_pil(app) -> None:
    # The fixture pins _HAS_PIL to False: no image, no crash.
    assert app._photo_icon("logo_openfoxforge_main.png", 40) is None


# ---------------------------------------------------------------------------
# Model cards (size in Go, active-preset badge)
# ---------------------------------------------------------------------------


def test_format_model_size() -> None:
    assert gui.format_model_size(None) == ""
    assert gui.format_model_size(0) == ""
    assert gui.format_model_size(-1) == ""
    assert gui.format_model_size(1024 ** 3) == "1.0 Go"
    assert gui.format_model_size(int(3.0 * 1024 ** 3)) == "3.0 Go"
    assert gui.format_model_size(605254808) == "577 Mo"
    # A small model must not read "0.0 Go".
    assert gui.format_model_size(45 * 1024 ** 2) == "45 Mo"


def test_probe_model_size_never_raises(monkeypatch) -> None:
    # Offline / 404 / HEAD refused: all "unknown", never an exception.
    def _boom(*_a, **_k):
        raise OSError("no network")

    monkeypatch.setattr(gui.urllib.request, "urlopen", _boom)
    assert gui.probe_model_size("https://example.invalid/x.safetensors") is None
    assert gui.probe_model_size("") is None


def test_probe_model_size_reads_content_length(monkeypatch) -> None:
    class _Resp:
        headers = {"Content-Length": "3221225472"}

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(gui.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert gui.probe_model_size("https://example.invalid/x.safetensors") == 3221225472


def test_model_sizes_are_shown_on_the_cards(app) -> None:
    app._mode.set("comfy")
    app._on_stack_change()
    url = app._comfy_models["diffusion"][0]["url"]
    app._apply_model_sizes({url: 4 * 1024 ** 3})
    assert app._model_sizes[url] == 4 * 1024 ** 3
    # The rebuild is coalesced (one per burst of probed URLs).
    time.sleep(0.4)
    app.root.update()
    texts = _label_texts(app._comfy_model_rows)
    assert "4.0 Go" in texts


def test_model_size_rebuilds_are_coalesced(app, monkeypatch) -> None:
    app._mode.set("comfy")
    app._on_stack_change()
    calls: list = []
    original = app._rebuild_comfy_model_rows
    monkeypatch.setattr(
        app, "_rebuild_comfy_model_rows", lambda: calls.append(1)
    )
    try:
        url = app._comfy_models["diffusion"][0]["url"]
        for size in (1, 2, 3):
            app._apply_model_sizes({url: size * 1024 ** 3})
        time.sleep(0.4)
        app.root.update()
    finally:
        monkeypatch.setattr(app, "_rebuild_comfy_model_rows", original)
    assert calls == [1], "one rebuild for the whole burst"


def _label_texts(widget, out=None):
    out = [] if out is None else out
    for child in widget.winfo_children():
        try:
            text = str(child.cget("text"))
        except Exception:  # noqa: BLE001 - not every widget has text
            text = ""
        if text:
            out.append(text)
        _label_texts(child, out)
    return out


def test_model_card_banner_shows_the_category(app) -> None:
    app._mode.set("comfy")
    app._on_stack_change()
    texts = _label_texts(app._comfy_model_rows)
    for category in gui.COMFY_MODEL_CATEGORIES:
        assert gui.COMFY_MODEL_CATEGORY_LABELS[category] in texts


def test_active_preset_models_are_badged(app, monkeypatch) -> None:
    pytest.importorskip("PIL")
    monkeypatch.setattr(gui, "_HAS_PIL", True)
    app._mode.set("comfy")
    app._on_stack_change()
    cat = "text_encoder"
    url = app._comfy_models[cat][0]["url"]
    target = gui.model_target_for_url(cat, url)
    # Nothing in the preset yet: no badge.
    app._comfy_presets["P"] = {"models": [], "nodes": [], "workflows": []}
    app._comfy_preset.set("P")
    app._rebuild_comfy_model_rows()
    assert gui.LOCK_BADGE_TOOLTIP not in app._tooltip_texts.values()
    # The model enters the preset: its card carries the lock badge + tooltip.
    app._comfy_presets["P"]["models"] = [{"url": url, "target": target}]
    app._rebuild_comfy_model_rows()
    assert gui.LOCK_BADGE_TOOLTIP in app._tooltip_texts.values()


def test_active_preset_badge_tooltip_on_the_checkbox(app, monkeypatch) -> None:
    # Without the badge image (no PIL) the membership is still announced in
    # the checkbox tooltip, so the information is never lost.
    app._mode.set("comfy")
    app._on_stack_change()
    cat = "text_encoder"
    url = app._comfy_models[cat][0]["url"]
    target = gui.model_target_for_url(cat, url)
    app._comfy_presets["P"] = {
        "models": [{"url": url, "target": target}], "nodes": [], "workflows": [],
    }
    app._comfy_preset.set("P")
    app._rebuild_comfy_model_rows()
    assert any(
        "Included in the active preset." in text
        for text in app._tooltip_texts.values()
    )


def test_explicit_preset_model_section_is_gone(app) -> None:
    # The catalog cards carry the information now (badge + size): the
    # separate "Modèles du preset" list was redundant.
    app._mode.set("comfy")
    app._on_stack_change()
    texts = _label_texts(app._comfy_extra_rows)
    assert "Modèles du preset" not in texts
    # The remaining sections are banners now, not "Titre :" headers.
    assert "Custom nodes" in texts
    assert "Workflows" in texts


def test_extra_sections_use_the_category_card_treatment(app) -> None:
    # Same visual language as Diffusion / Video VAE / …: one card per section,
    # each with its own colored banner.
    app._mode.set("comfy")
    app._on_stack_change()
    app.root.update_idletasks()
    cards = [
        child for child in app._comfy_extra_rows.winfo_children()
        if str(child.cget("highlightthickness")) == "1"
    ]
    assert len(cards) == 2, "expected a card per extra section"
    banners = [
        child for card in cards for child in card.winfo_children()
        if child.cget("bg") in (gui.CATEGORY_COLORS["custom_nodes"],
                                gui.CATEGORY_COLORS["workflows"])
    ]
    assert len(banners) == 2
    # Distinct, section-specific banner colors.
    assert {str(banner.cget("bg")) for banner in banners} == {
        gui.CATEGORY_COLORS["custom_nodes"],
        gui.CATEGORY_COLORS["workflows"],
    }


def _card_for(app, title: str):
    """The category card whose banner title is *title*."""
    for card in app._comfy_model_rows.winfo_children():
        if title in _label_texts(card):
            return card
    return None


def test_extra_model_is_a_row_inside_its_category(app) -> None:
    # A preset model the catalog does not offer is an extra LINE in the card
    # of the category its target path points at — not a separate section.
    app._mode.set("comfy")
    app._on_stack_change()
    app._comfy_edit_models = [
        {"url": "https://huggingface.co/private/resolve/main/dasiwa_hybrid_v2.safetensors",
         "target": "diffusion_models/MiniMaxH3/dasiwa_hybrid_v2.safetensors",
         "name": "DaSiWa Hybrid V2 (int8)", "enabled": True},
    ]
    app._rebuild_comfy_model_rows()
    app.root.update_idletasks()
    assert "Modèles hors catalogue" not in _label_texts(app._comfy_model_rows)
    diffusion = _card_for(app, gui.COMFY_MODEL_CATEGORY_LABELS["diffusion"])
    assert diffusion is not None
    texts = _label_texts(diffusion)
    assert "DaSiWa Hybrid V2 (int8)" in texts
    assert "extra" in texts  # the discreet pill
    # Exactly one checkbox line was added to the Diffusion card.
    assert _collect_checkbuttons(diffusion, [])


def test_extra_model_row_has_the_catalog_line_layout(app) -> None:
    # Same layout as a catalog entry: checkbox left, size right-aligned, ✕ at
    # the far right, one model per line.
    app._mode.set("comfy")
    app._on_stack_change()
    app._comfy_edit_models = [
        {"url": "https://x/y/extra.safetensors",
         "target": "upscale_models/extra.safetensors",
         "name": "Extra", "enabled": True},
    ]
    app._model_sizes["https://x/y/extra.safetensors"] = 2 * 1024 ** 3
    app._rebuild_comfy_model_rows()
    app.root.update_idletasks()
    card = _card_for(app, gui.COMFY_MODEL_CATEGORY_LABELS["upscaler"])
    texts = _label_texts(card)
    assert "Extra" in texts
    assert "extra" in texts
    assert "2.0 Go" in texts
    # The ✕ sits at the far right of the row.
    rows = [
        child for child in card.winfo_children()[1].winfo_children()
        if _label_texts(child) or child.winfo_children()
    ]
    extra_row = next(
        row for row in rows
        if any("extra" in _label_texts(row) for _ in (0,))
    )
    buttons = [
        child for child in extra_row.winfo_children()
        if str(child.cget("text")) == "✕"
    ]
    assert buttons and str(buttons[0].pack_info().get("side")) == "right"


def test_extra_model_is_filed_by_its_target(app) -> None:
    assert gui._category_for_target("diffusion_models/MiniMaxH3/v2.safetensors") == "diffusion"
    assert gui._category_for_target("latent_upscale_models/latent_upscaler_3d_bf16.safetensors") == "upscaler"
    assert gui._category_for_target("upscale_models/2x-AnimeSharp.safetensors") == "upscaler"
    assert gui._category_for_target("vae/minimax_h3_audio_vae_fp32.safetensors") == "audio_vae"
    assert gui._category_for_target("vae/minimax_h3_video_vae_int8.safetensors") == "video_vae"
    assert gui._category_for_target("vae_approx/taeh3.pth") == "tae"
    assert gui._category_for_target("text_encoders/qwen3vl.safetensors") == "text_encoder"
    assert gui._category_for_target("frame_interpolation/rife.pth") == "frame_interp"
    # Unrecognisable: the checkpoint category is the safest home.
    assert gui._category_for_target("something.safetensors") == "diffusion"
    assert gui._category_for_target("", "My Checkpoint") == "diffusion"


def test_no_extra_pill_without_an_extra_model(app) -> None:
    app._mode.set("comfy")
    app._on_stack_change()
    app._comfy_edit_models = []
    app._rebuild_comfy_model_rows()
    assert "extra" not in _label_texts(app._comfy_model_rows)


def test_extra_models_are_not_duplicated_in_the_extra_rows(app) -> None:
    app._mode.set("comfy")
    app._on_stack_change()
    app._comfy_edit_models = [
        {"url": "https://x/y/extra.safetensors", "target": "upscale_models/extra.safetensors",
         "name": "Extra", "enabled": True},
    ]
    app._rebuild_comfy_extra_rows()
    assert "Extra" not in _label_texts(app._comfy_extra_rows)


def test_non_catalog_model_can_be_toggled_and_removed(app) -> None:
    app._mode.set("comfy")
    app._on_stack_change()
    app._comfy_edit_models = [
        {"url": "https://civitai.red/api/download/models/1", "target": "x.safetensors",
         "name": "V2", "enabled": True},
    ]
    app._comfy_toggle_model_entry(0, tk.BooleanVar(app.root, value=False))
    assert app._comfy_edit_models[0]["enabled"] is False
    app._comfy_remove_model_entry(0)
    assert app._comfy_edit_models == []


# --- no duplicate between the catalog cards and the extra lines ------------


def test_catalog_match_by_url_and_target(app) -> None:
    cat = "video_vae"
    url = app._comfy_models[cat][0]["url"]
    target = gui.model_target_for_url(cat, url)
    assert app._match_catalog_entry({"url": url, "target": target}) == (cat, 0)


def test_catalog_match_by_file_name_when_the_url_differs(app) -> None:
    # A mirror URL / a hand-written preset: same file, different URL.
    cat = "video_vae"
    url = app._comfy_models[cat][0]["url"]
    target = gui.model_target_for_url(cat, url)
    stem = target.rsplit("/", 1)[-1]
    assert app._match_catalog_entry(
        {"url": "https://mirror.example/other/path/" + stem, "target": target}
    ) == (cat, 0)


def test_catalog_match_by_display_name(app) -> None:
    cat = "text_encoder"
    entry = app._comfy_models[cat][0]
    assert app._match_catalog_entry(
        {"url": "", "target": "", "name": entry["name"]}
    ) == (cat, 0)


def test_catalog_match_none_for_a_real_extra(app) -> None:
    assert app._match_catalog_entry(
        {"url": "https://civitai.red/api/download/models/1",
         "target": "diffusion_models/MiniMaxH3/v2.safetensors"}
    ) is None


def test_preset_load_adopts_catalog_matches(app) -> None:
    # A preset listing a file the catalog already offers must not appear a
    # second time (unticked) under "hors catalogue": the catalog checkbox is
    # ticked instead, and the preset's own list is preserved so "Save"
    # cannot silently drop the model.
    cat = "video_vae"
    url = app._comfy_models[cat][0]["url"]
    target = gui.model_target_for_url(cat, url)
    app._comfy_models[cat][0]["enabled"] = False
    app._comfy_presets["P"] = {
        "models": [{"url": url, "target": target}], "nodes": [], "workflows": [],
    }
    app._refresh_comfy_preset_combo()
    app._comfy_preset_combo.set("P")
    app._comfy_load_preset()
    app.root.update_idletasks()
    assert app._comfy_models[cat][0]["enabled"] is True
    assert len(app._comfy_edit_models) == 1
    # The matched model is a normal catalog line: no "extra" pill anywhere.
    assert "extra" not in _label_texts(app._comfy_model_rows)


def test_extra_models_without_a_catalog_match_stay(app) -> None:
    app._comfy_presets["P"] = {
        "models": [{"url": "https://civitai.red/api/download/models/1",
                    "target": "diffusion_models/MiniMaxH3/v2.safetensors"}],
        "nodes": [], "workflows": [],
    }
    app._refresh_comfy_preset_combo()
    app._comfy_preset_combo.set("P")
    app._comfy_load_preset()
    app.root.update_idletasks()
    assert len(app._comfy_edit_models) == 1
    # Kept (it is what "Save" writes) and rendered as an extra line in the
    # Diffusion card.
    assert "extra" in _label_texts(
        _card_for(app, gui.COMFY_MODEL_CATEGORY_LABELS["diffusion"])
    )


def test_preset_save_keeps_catalog_matched_models(app) -> None:
    # Regression: matching a preset model to a catalog entry must not remove
    # it from the preset's own list — "Save" writes that list, so dropping
    # the entry would silently lose the model.
    cat = "video_vae"
    url = app._comfy_models[cat][0]["url"]
    target = gui.model_target_for_url(cat, url)
    app._comfy_presets["P"] = {
        "models": [{"url": url, "target": target}], "nodes": [], "workflows": [],
    }
    app._refresh_comfy_preset_combo()
    app._comfy_preset_combo.set("P")
    app._comfy_load_preset()
    assert len(app._comfy_edit_models) == 1
    assert app._comfy_edit_models[0]["url"] == url


def test_nodes_and_workflows_use_the_model_row_layout(app) -> None:
    # Same visual language as the model categories: one entry per LINE
    # (checkbox + name on the left, info right-aligned, ✕ far right) instead
    # of a horizontal wrapping flow.
    app._mode.set("comfy")
    app._on_stack_change()
    app._comfy_edit_nodes = [
        {"url": "https://github.com/u/a", "name": "NodeA", "note": "noteA",
         "enabled": True},
        {"url": "https://github.com/u/b", "name": "NodeB", "enabled": True},
    ]
    app._comfy_edit_workflows = [
        {"url": "https://x/w.json", "name": "FlowA", "note": "noteW",
         "enabled": True},
    ]
    app._rebuild_comfy_extra_rows()
    app.root.update_idletasks()
    for title, names in (
        ("Custom nodes", ("NodeA", "NodeB")),
        ("Workflows", ("FlowA",)),
    ):
        card = next(
            child for child in app._comfy_extra_rows.winfo_children()
            if title in _label_texts(child)
        )
        rows = _entry_rows(card)
        assert len(rows) == len(names), title
        for row, name in zip(rows, names):
            assert name in _label_texts(row)
            remove = [
                child for child in row.winfo_children()
                if str(child.cget("text")) == "✕"
            ]
            assert remove and str(remove[0].pack_info().get("side")) == "right"
            # One entry per line: the rows stack vertically.
            assert str(row.pack_info().get("side")) == "top"
    # The note (when present) is the right-aligned info of its row.
    nodes_card = next(
        child for child in app._comfy_extra_rows.winfo_children()
        if "Custom nodes" in _label_texts(child)
    )
    assert "noteA" in _label_texts(_entry_rows(nodes_card)[0])


def _entry_rows(card) -> list:
    """The per-entry rows of a section card (the body's children)."""
    body = card.winfo_children()[-1]
    return [
        child for child in body.winfo_children()
        if str(child.cget("bg")) == str(card.cget("bg"))
    ]


def test_model_size_probe_skips_known_urls(app, monkeypatch) -> None:
    # A second call must not re-probe URLs already in the cache.
    seen: list = []
    monkeypatch.setattr(gui, "probe_model_size", lambda url, *a, **k: seen.append(url))

    def _probe_and_wait() -> None:
        app._start_model_size_check()
        thread = getattr(app, "_size_thread", None)
        if thread is not None:
            thread.join(timeout=10.0)

    app._model_sizes.clear()
    _probe_and_wait()
    first = len(seen)
    assert first > 0
    for url in list(seen):
        app._model_sizes[url] = 1
    _probe_and_wait()
    assert len(seen) == first


# ---------------------------------------------------------------------------
# Dashboard layout (progress chips, journal expansion, cost meter)
# ---------------------------------------------------------------------------


def test_health_status_icon_column_leaves_a_gap(app) -> None:
    # The status icon is drawn in the tree column: its width is the only
    # space between the icon and the « Composant » text.
    width = int(app._dash_table.column("#0", "width"))
    assert width >= gui.STATUS_ICON_COLUMN_WIDTH
    assert width > 26  # the former width left the icon touching the text


def test_apply_preset_to_editor_populates_the_editor(app) -> None:
    app._comfy_presets["P"] = {
        "models": [{"url": "https://x/y/extra.safetensors",
                    "target": "upscale_models/extra.safetensors",
                    "name": "Extra"}],
        "nodes": [{"url": "https://github.com/u/n", "name": "N"}],
        "workflows": [{"url": "https://x/y.json", "name": "W"}],
    }
    assert app._apply_preset_to_editor("P") is True
    assert [m["name"] for m in app._comfy_edit_models] == ["Extra"]
    assert [n["name"] for n in app._comfy_edit_nodes] == ["N"]
    assert [w["name"] for w in app._comfy_edit_workflows] == ["W"]
    assert app._apply_preset_to_editor("nope") is False


def test_editor_is_synced_with_the_active_preset_at_startup(app) -> None:
    # The tab must show what the pod will receive as soon as it opens, not an
    # empty editor (the "extra" lines were invisible until the preset was
    # re-selected in the dropdown).
    app._comfy_presets["P"] = {
        "models": [{"url": "https://x/y/extra.safetensors",
                    "target": "upscale_models/extra.safetensors",
                    "name": "Extra"}],
        "nodes": [], "workflows": [],
    }
    app._apply_preset_to_editor("P")
    app._rebuild_comfy_model_rows()
    app.root.update_idletasks()
    assert "extra" in _label_texts(app._comfy_model_rows)
    # ... and the build path does load the active preset into the editor.
    source = Path(gui.__file__).read_text(encoding="utf-8")
    build = source.split("def _build_widgets", 1)[1].split("def _lframe", 1)[0]
    assert "_apply_preset_to_editor(self._comfy_preset.get().strip())" in build
    assert str(app._log_frame.pack_info().get("expand")) in ("1", "True", "true")
    assert str(app._dash_frame.pack_info().get("fill")) == "x"


def test_health_panel_does_not_stretch_and_the_pod_card_fills(app) -> None:
    # Point 2: the health table is content-sized; the pod/GPU card takes the
    # width the three narrow columns leave free.
    assert str(app._dash_frame.pack_info().get("side")) == "left"
    assert str(app._dash_frame.pack_info().get("expand")) in ("0", "False", "false")
    assert str(app._pod_card.pack_info().get("side")) == "left"
    assert str(app._pod_card.pack_info().get("expand")) in ("1", "True", "true")


def test_pod_card_shows_gpu_count_and_spend(app, tmp_path, monkeypatch) -> None:
    registry = gui.PodRegistry(tmp_path / "pod.json")
    registry.save(
        gui.PodRecord(pod_id="p", name="n", created_at=1.0, gpu_id="H100",
                      gpu_count=2, data_center_id=None, stack="comfy"),
        stack="comfy",
    )
    monkeypatch.setattr(gui, "PodRegistry", lambda *a, **k: registry)
    app._mode.set("comfy")
    app._last_billing = {
        "has_pod": True, "running": True, "elapsed": "1 h 12 min",
        "cost": "0.64 $", "rate": "$0.53/h",
    }
    app._refresh_pod_card()
    values = {k: v.cget("text") for k, v in app._pod_card_values.items()}
    assert values["gpu"] == "H100 × 2"
    assert values["zone"] == "—"
    assert values["elapsed"] == "1 h 12 min"
    assert values["cost"] == "0.64 $"


def test_pod_card_hides_queue_and_vram_until_known(app, tmp_path, monkeypatch) -> None:
    # A permanent "—" row tells nothing: those two rows only appear once the
    # ComfyUI probe reports something.
    monkeypatch.setattr(
        gui, "PodRegistry", lambda *a, **k: gui.PodRegistry(tmp_path / "none.json")
    )
    app._last_billing = None
    app._last_snapshot = {"comfy": {"queue": None, "vram": None}}
    app._refresh_pod_card()
    app.root.update_idletasks()
    assert app._pod_card_values["queue"].winfo_manager() == ""
    assert app._pod_card_values["vram"].winfo_manager() == ""
    assert app._pod_card_key_labels["queue"].winfo_manager() == ""


def test_pod_card_shows_queue_and_vram_when_known(app, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        gui, "PodRegistry", lambda *a, **k: gui.PodRegistry(tmp_path / "none.json")
    )
    app._last_snapshot = {
        "comfy": {"queue": "1 en cours / 2 en attente", "vram": "40.0 / 48.0 Go"}
    }
    app._refresh_pod_card()
    app.root.update_idletasks()
    values = {k: v.cget("text") for k, v in app._pod_card_values.items()}
    assert values["queue"] == "1 en cours / 2 en attente"
    assert values["vram"] == "40.0 / 48.0 Go"
    assert app._pod_card_values["queue"].winfo_manager() == "grid"
    assert app._pod_card_values["vram"].winfo_manager() == "grid"


def test_pod_card_hides_them_again_when_the_pod_goes_away(app, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        gui, "PodRegistry", lambda *a, **k: gui.PodRegistry(tmp_path / "none.json")
    )
    app._last_snapshot = {"comfy": {"queue": "1 en cours", "vram": "40.0 / 48.0 Go"}}
    app._refresh_pod_card()
    app._last_snapshot = {"comfy": {"queue": None, "vram": None}}
    app._refresh_pod_card()
    app.root.update_idletasks()
    assert app._pod_card_values["queue"].winfo_manager() == ""
    assert app._pod_card_values["vram"].winfo_manager() == ""


def test_pod_card_is_all_dashes_without_a_record(app, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        gui, "PodRegistry", lambda *a, **k: gui.PodRegistry(tmp_path / "none.json")
    )
    app._last_billing = None
    app._refresh_pod_card()
    assert {v.cget("text") for v in app._pod_card_values.values()} == {"—"}


def test_pod_card_keeps_spend_without_a_record(app, tmp_path, monkeypatch) -> None:
    # The billing probe knows the spend even when the local record is gone
    # (pod created by another session): the card must not blank it out.
    monkeypatch.setattr(
        gui, "PodRegistry", lambda *a, **k: gui.PodRegistry(tmp_path / "none.json")
    )
    app._last_billing = {
        "has_pod": True, "running": True, "elapsed": "1 h 12 min",
        "cost": "0.64 $", "rate": "$0.53/h",
    }
    app._refresh_pod_card()
    values = {k: v.cget("text") for k, v in app._pod_card_values.items()}
    assert values["pod"] == "—"
    assert values["elapsed"] == "1 h 12 min"
    assert values["cost"] == "0.64 $"


def test_cost_meter_shows_the_rate_and_hides_when_unknown(app) -> None:
    app._apply_cost_meter({"rate": "$0.53/h"})
    app.root.update_idletasks()
    assert "$0.53/h" in app._cost_label.cget("text")
    assert app._cost_label.winfo_manager() != ""
    app._apply_cost_meter(None)
    assert app._cost_label.winfo_manager() == ""


def test_cost_meter_sits_next_to_the_state_badge(app) -> None:
    app._apply_cost_meter({"rate": "$0.53/h"})
    app.root.update_idletasks()
    order = [str(w) for w in app._cost_label.master.pack_slaves()]
    # Pack order for side="right": first packed is right-most. The chip must
    # land between the buttons and the global state badge.
    assert order.index(str(app._cost_label)) > order.index(str(app._overall_label))
    assert order.index(str(app._cost_label)) < order.index(str(app._btn_settings))


def test_cost_meter_is_updated_from_billing(app) -> None:
    app._apply_billing({"billing": {"has_pod": True, "running": True, "rate": "$0.69/h"}})
    assert "$0.69/h" in app._cost_label.cget("text")


def test_health_table_marks_the_status_with_an_icon(app, monkeypatch) -> None:
    pytest.importorskip("PIL")
    monkeypatch.setattr(gui, "_HAS_PIL", True)
    app._apply_status(_full_snapshot())
    app.root.update_idletasks()
    images = [
        app._dash_table.item(iid, "image") for iid in app._dash_table.get_children()
    ]
    assert any(images), "expected a status icon in the tree column"
    # The bullet fallback is gone once the icon is drawn.
    statuses = [
        app._dash_table.item(iid, "values")[1]
        for iid in app._dash_table.get_children()
    ]
    assert all(not str(status).startswith("●") for status in statuses)


def test_health_table_keeps_the_bullet_without_pil(app) -> None:
    # No PIL (the fixture default): the colored bullet is the fallback, so
    # the state is never lost.
    app._apply_status(_full_snapshot())
    app.root.update_idletasks()
    statuses = [
        app._dash_table.item(iid, "values")[1]
        for iid in app._dash_table.get_children()
    ]
    assert any(str(status).startswith(("●", "○")) for status in statuses)


# ---------------------------------------------------------------------------
# ComfyUI tab layout (access help, preset warning, summary card)
# ---------------------------------------------------------------------------


def test_access_hint_sits_under_its_field(app) -> None:
    # The hint documents « Accès »: it lives on the line below, not as a
    # trailing caption on the same line.
    assert app._comfy_access_hint.winfo_manager() != ""
    assert app._comfy_access_hint.master is app._comfy_access_row
    assert app._comfy_access_row.master is app._comfy_access_hint.master.master


def test_preset_warning_appears_only_with_an_active_preset(app) -> None:
    app._mode.set("comfy")
    app._on_stack_change()
    app._comfy_presets["P"] = {"models": [], "nodes": [], "workflows": []}
    app._refresh_comfy_options_preset_combo()
    # No preset: no warning.
    app._comfy_preset.set("")
    app._comfy_tier.set("perso")
    app._refresh_comfy_summary()
    assert app._comfy_preset_warning.winfo_manager() == ""
    # A preset is active: the warning explains that "Save" is what applies.
    app._comfy_preset.set("P")
    app._refresh_comfy_summary()
    assert app._comfy_preset_warning.winfo_manager() != ""
    assert "P" in app._comfy_preset_warning.cget("text")
    # "Auto" ignores the preset entirely: no warning either.
    app._comfy_tier.set("auto")
    app._refresh_comfy_summary()
    assert app._comfy_preset_warning.winfo_manager() == ""


def test_summary_is_rendered_in_a_card(app) -> None:
    assert app._comfy_summary_label.master is app._comfy_summary_card
    assert app._comfy_summary_card.winfo_manager() != ""


# ---------------------------------------------------------------------------
# Qwen tab (anchor top + recap card) & settings dialog spacing
# ---------------------------------------------------------------------------


def _collect_toggles(widget, out=None):
    out = [] if out is None else out
    for child in widget.winfo_children():
        try:
            if child.winfo_class() in ("Checkbutton", "TCheckbutton",
                                       "Radiobutton", "TRadiobutton"):
                out.append(child)
        except tk.TclError:
            pass
        _collect_toggles(child, out)
    return out


def test_config_tab_blocks_are_anchored_at_the_top(app) -> None:
    # The three blocks used to be spread over the whole tab height; they are
    # now top-anchored, with a plain filler absorbing the spare height so the
    # tab is still painted edge to edge.
    children = app._config_recap.master.winfo_children()
    for region in children:
        if region is app._config_filler:
            continue
        assert str(region.pack_info().get("expand")) in ("0", "False", "false")
    assert str(app._config_filler.pack_info().get("expand")) in ("1", "True", "true")
    # The recap card is content-sized, not stretched to the tab height.
    assert str(app._config_recap.pack_info().get("fill")) == "x"


def test_biblio_grid_expands_vertically(app) -> None:
    # The Library tab's card grid is its expanding surface: the tab never
    # ends on a dead canvas strip.
    assert str(app._annuaire_frame.pack_info().get("expand")) in ("1", "True", "true")
    assert str(app._annuaire_grid.pack_info().get("expand")) in ("1", "True", "true")


# ---------------------------------------------------------------------------
# Pod termination resync (point 1: stale/cleared registry record)
# ---------------------------------------------------------------------------


class _RunPodStub:
    def __init__(self, pod_id, name="openfox-forge-comfy-x", status="RUNNING",
                 created_at=100.0, env=None):
        self.id = pod_id
        self.name = name
        self.status = status
        self.created_at = created_at
        self.env = env or {}
        self.gpu_id = None
        self.data_center_id = None


class _RunPodClientStub:
    """Minimal RunPodClient stand-in (404s on unknown / missing ids)."""

    def __init__(self, pods, *, missing=(), misses=0):
        self._pods = {pod.id: pod for pod in pods}
        self._missing = set(missing)
        #: Number of leading get_pod calls that 404 before the pod shows up
        #: (models RunPod's indexing delay for a freshly created pod).
        self._misses = misses
        self.get_calls = 0
        self.terminated: list = []
        self.stopped: list = []

    def _require(self, pod_id):
        if pod_id in self._missing or pod_id not in self._pods:
            raise _PodNotFound(f"Pod not found (HTTP 404): {pod_id}")
        return self._pods[pod_id]

    def get_pod(self, pod_id):
        self.get_calls += 1
        if self.get_calls <= self._misses:
            raise _PodNotFound(f"Pod not found (HTTP 404): {pod_id}")
        return self._require(pod_id)

    def list_pods(self):
        return [pod for pod in self._pods.values() if pod.id not in self._missing]

    def terminate_pod(self, pod_id):
        self._require(pod_id)
        self.terminated.append(pod_id)
        return self._pods[pod_id]

    def stop_pod(self, pod_id):
        self._require(pod_id)
        self.stopped.append(pod_id)
        return self._pods[pod_id]


def _registry(tmp_path) -> gui.PodRegistry:
    return gui.PodRegistry(tmp_path / "pod.json")


def test_resync_keeps_a_valid_record(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry.save(gui.PodRecord(pod_id="abc", name="n", created_at=1.0,
                                gpu_id=None, gpu_count=1, stack="comfy"), stack="comfy")
    runpod = _RunPodClientStub([_RunPodStub("abc")])
    pod_id, note = gui.resync_registered_pod(None, runpod, registry, "comfy")
    assert pod_id == "abc"
    assert note is None


def test_resync_returns_none_when_no_pod_matches(tmp_path) -> None:
    registry = _registry(tmp_path)
    runpod = _RunPodClientStub([_RunPodStub("a", name="unrelated-pod", env={})])
    pod_id, note = gui.resync_registered_pod(None, runpod, registry, "comfy")
    assert pod_id is None
    assert note and "no active pod" in note


def test_resync_ignores_a_terminated_record(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry.save(gui.PodRecord(pod_id="gone", name="n", created_at=1.0,
                                gpu_id=None, gpu_count=1, stack="comfy"), stack="comfy")
    runpod = _RunPodClientStub([_RunPodStub("gone", status="TERMINATED")])
    pod_id, _note = gui.resync_registered_pod(None, runpod, registry, "comfy")
    assert pod_id is None
    assert registry.load("comfy") is None


def test_resync_keeps_the_record_when_the_api_is_unreachable(tmp_path) -> None:
    # An unverifiable record must not be discarded (the API may just be down).
    from launcher.runpod import RunPodError

    registry = _registry(tmp_path)
    registry.save(gui.PodRecord(pod_id="abc", name="n", created_at=1.0,
                                gpu_id=None, gpu_count=1, stack="comfy"), stack="comfy")

    class _Boom(_RunPodClientStub):
        def get_pod(self, pod_id):
            raise RunPodError("API down")

    pod_id, note = gui.resync_registered_pod(None, _Boom([_RunPodStub("abc")]),
                                             registry, "comfy")
    assert pod_id == "abc"
    assert note is None
    assert registry.load("comfy").pod_id == "abc"


def test_resync_without_a_client_is_a_noop(tmp_path) -> None:
    registry = _registry(tmp_path)
    assert gui.resync_registered_pod(None, None, registry, "comfy") == (None, None)


def test_resync_keeps_a_fresh_record_that_404s_once(tmp_path, monkeypatch) -> None:
    # The bug behind "impossible to terminate a recently created pod": RunPod
    # 404s a pod for a few seconds after creation while it is alive. The record
    # must survive that window (it is the only link to the running pod).
    monkeypatch.setattr(gui.orchestrator, "_POD_PRESENCE_DELAY", 0.0)
    registry = _registry(tmp_path)
    registry.save(
        gui.PodRecord(pod_id="pod_new", name="n", created_at=time.time(),
                      gpu_id=None, gpu_count=1, stack="comfy"),
        stack="comfy",
    )
    runpod = _RunPodClientStub([_RunPodStub("pod_new")], misses=1)
    pod_id, note = gui.resync_registered_pod(None, runpod, registry, "comfy")
    assert pod_id == "pod_new"
    assert note is None
    assert registry.load("comfy").pod_id == "pod_new"
    assert runpod.get_calls >= 2  # the retry is what saved it


def test_resync_terminates_a_fresh_pod_found_by_the_retry(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gui.orchestrator, "_POD_PRESENCE_DELAY", 0.0)
    registry = _registry(tmp_path)
    registry.save(
        gui.PodRecord(pod_id="pod_new", name="n", created_at=time.time(),
                      gpu_id=None, gpu_count=1, stack="comfy"),
        stack="comfy",
    )
    runpod = _RunPodClientStub([_RunPodStub("pod_new")], misses=1)
    monkeypatch.setattr(gui, "PodRegistry", lambda *a, **k: registry)
    monkeypatch.setattr(gui, "RunPodClient", lambda *a, **k: runpod)
    monkeypatch.setattr(
        gui,
        "load_config",
        lambda *a, **k: SimpleNamespace(
            stack="comfy", runpod=SimpleNamespace(pod_id=None),
            secrets=SimpleNamespace(runpod_api_key="rp_key"),
        ),
    )
    ok, _message = gui.action_stop_outcome(terminate=True, stack="comfy")
    assert ok is True
    assert runpod.terminated == ["pod_new"]


def test_action_stop_terminate_without_any_pod_raises_a_clear_error(
    tmp_path, monkeypatch
) -> None:
    registry = _registry(tmp_path)
    runpod = _RunPodClientStub([_RunPodStub("a", name="unrelated", env={})])
    monkeypatch.setattr(gui, "PodRegistry", lambda *a, **k: registry)
    monkeypatch.setattr(gui, "RunPodClient", lambda *a, **k: runpod)
    monkeypatch.setattr(
        gui,
        "load_config",
        lambda *a, **k: SimpleNamespace(
            stack="comfy", runpod=SimpleNamespace(pod_id=None),
            secrets=SimpleNamespace(runpod_api_key="rp_key"),
        ),
    )
    with pytest.raises(gui.orchestrator.OrchestrationError) as excinfo:
        gui.action_stop_outcome(terminate=True, stack="comfy")
    # French, actionable, and no pod was touched.
    assert "No active" in str(excinfo.value)
    assert runpod.terminated == []


# --- error translation ------------------------------------------------------


def test_translate_action_error_keeps_unknown_details() -> None:
    text = gui.translate_action_error("some unexpected failure")
    assert "some unexpected failure" in text
    assert text.startswith("The operation failed")


def test_translate_action_error_is_idempotent() -> None:
    once = gui.translate_action_error("Pod not found (HTTP 404): x")
    assert gui.translate_action_error(once) == once
    # Our own stop-path message is left alone too.
    own = "No active ComfyUI video pod to terminate: nothing was deleted."
    assert gui.translate_action_error(own) == own


def test_translate_action_error_handles_empty() -> None:
    assert gui.translate_action_error("") == "The operation failed (no detail provided)."


def test_error_dialog_is_themed(app) -> None:
    # The error box must be a Forge dialog, not the native messagebox.
    dialog = app._message_dialog("Démarrage", "message", kind="error")
    try:
        assert dialog.winfo_exists()
        assert str(dialog.cget("bg")) == app._pal["bg"]
        texts = _label_texts(dialog)
        assert "Démarrage" in texts
        assert "message" in texts
    finally:
        dialog.destroy()


def test_show_error_translates_and_does_not_use_messagebox(app, monkeypatch) -> None:
    captured: dict = {}

    def fake_dialog(title, message, **kwargs):
        captured["title"] = title
        captured["message"] = message
        return None

    monkeypatch.setattr(app, "_message_dialog", fake_dialog)
    app._show_error("Démarrage", "Pod not found (HTTP 404): {'detail': 'x'}")
    assert captured["title"] == "Démarrage"
    assert "not found" in captured["message"]


# ---------------------------------------------------------------------------
# Annuaire (LoRA / Workflow / Node)
# ---------------------------------------------------------------------------


# --- local sources (uploaded by the launcher, not downloaded by the pod) ----


def test_annuaire_normalize_keeps_a_local_only_entry() -> None:
    entries = gui._normalize_annuaire(
        [
            {"type": "lora", "name": "Local", "local_path": "C:/x/y.safetensors"},
            {"type": "workflow", "name": "Both", "dl_url": "https://d",
             "local_path": "C:/w.json"},
            {"type": "lora", "name": "Empty"},
        ]
    )
    assert [e["name"] for e in entries] == ["Local", "Both"]
    assert entries[0]["dl_url"] == ""
    assert entries[0]["local_path"] == "C:/x/y.safetensors"


def test_annuaire_source_prefers_the_local_path() -> None:
    assert gui._annuaire_entry_source({"dl_url": "https://d"}) == "url"
    assert gui._annuaire_entry_source({"local_path": "C:/x"}) == "local"
    assert gui._annuaire_entry_source(
        {"dl_url": "https://d", "local_path": "C:/x"}
    ) == "local"
    assert gui._annuaire_entry_payload({"local_path": "C:/x"}) == "C:/x"
    assert gui._annuaire_entry_payload({"dl_url": "https://d"}) == "https://d"


def test_annuaire_download_lists_skip_local_entries() -> None:
    """A local path must never reach the pod env (the pod cannot read it)."""
    loras, nodes, workflows = gui._annuaire_download_lists(
        [
            {"type": "lora", "dl_url": "https://c/on"},
            {"type": "lora", "local_path": "C:/local.safetensors"},
            {"type": "node", "local_path": "C:/nodes/n"},
            {"type": "workflow", "name": "W", "local_path": "C:/w.json"},
        ]
    )
    assert loras == ["https://c/on"]
    assert nodes == []
    assert workflows == []


def test_annuaire_local_path_survives_the_settings_roundtrip(tmp_path) -> None:
    settings = gui.GuiSettings(
        comfy_annuaire=[
            {"type": "lora", "name": "L", "local_path": "C:/x/y.safetensors",
             "enabled": True}
        ]
    )
    gui.save_settings(settings, home=tmp_path)
    loaded = gui.load_settings(home=tmp_path)
    assert loaded.comfy_annuaire[0]["local_path"] == "C:/x/y.safetensors"


def test_comfy_local_assets_only_lists_enabled_local_entries(app) -> None:
    app._annuaire = gui._normalize_annuaire(
        [
            {"type": "lora", "name": "On", "local_path": "C:/on.safetensors"},
            {"type": "lora", "name": "Off", "local_path": "C:/off.safetensors",
             "enabled": False},
            {"type": "workflow", "name": "Url", "dl_url": "https://d"},
            {"type": "node", "name": "N", "local_path": "C:/nodes/n"},
        ]
    )
    assert app._comfy_local_assets() == [
        ("lora", "C:/on.safetensors", "On"),
        ("node", "C:/nodes/n", "N"),
    ]
    overrides = app._comfy_overrides()
    assert overrides["local_assets"] == app._comfy_local_assets()
    # ... and the URL lists stay URL-only: the local LoRA/node never appear.
    assert overrides["custom_loras"] == []
    assert overrides["custom_nodes"] == []
    assert overrides["custom_workflows"] == ["https://d Url.json"]


def test_form_source_switch_toggles_the_two_fields(app) -> None:
    _fill_form(app, name="L", local_path="C:/x/y.safetensors",
               source=gui.ANNUAIRE_SOURCE_LOCAL)
    app._on_form_source_change()
    assert str(app._form_local_entry.cget("state")) == "normal"
    assert str(app._form_dl_entry.cget("state")) == "disabled"
    app._form_source.set(gui.ANNUAIRE_SOURCE_URL)
    app._on_form_source_change()
    assert str(app._form_dl_entry.cget("state")) == "normal"
    assert str(app._form_local_entry.cget("state")) == "disabled"


def test_save_form_requires_the_selected_source(app) -> None:
    _fill_form(app, name="L", source=gui.ANNUAIRE_SOURCE_LOCAL)
    app._annuaire_save_form()
    assert app._annuaire == []
    assert any("local path" in msg for _, msg in app._log_entries)

    _fill_form(app, name="U", source=gui.ANNUAIRE_SOURCE_URL)
    app._annuaire_save_form()
    assert app._annuaire == []
    assert any("download link" in msg for _, msg in app._log_entries)


def test_save_form_warns_on_a_missing_local_path(app) -> None:
    _fill_form(app, name="L", source=gui.ANNUAIRE_SOURCE_LOCAL,
               local_path="C:/nope/missing.safetensors")
    app._annuaire_save_form()
    assert len(app._annuaire) == 1
    assert any("not found" in msg for _, msg in app._log_entries)


def test_annuaire_install_uploads_a_local_entry(app, monkeypatch) -> None:
    calls: list = []

    class _Ops:
        def upload_asset(self, kind, path, *, name="", force=False):
            calls.append((kind, path, name, force))
            return "ok"

    monkeypatch.setattr(gui.comfy_ops, "build_comfy_ops", lambda config: _Ops())
    monkeypatch.setattr(
        gui.ForgeApp, "_run", lambda self, label, work: work()
    )
    app._annuaire = gui._normalize_annuaire(
        [{"type": "lora", "name": "L", "local_path": "C:/x/y.safetensors"}]
    )
    app._annuaire_install(0)
    assert calls == [("lora", "C:/x/y.safetensors", "L", True)]


def test_annuaire_install_keeps_the_url_path_for_url_entries(app, monkeypatch) -> None:
    calls: list = []

    class _Ops:
        def install_lora(self, url, personal=False):
            calls.append(("lora", url, personal))
            return "ok"

        def upload_asset(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("a URL entry must not be uploaded")

    monkeypatch.setattr(gui.comfy_ops, "build_comfy_ops", lambda config: _Ops())
    monkeypatch.setattr(gui.ForgeApp, "_run", lambda self, label, work: work())
    app._annuaire = gui._normalize_annuaire(
        [{"type": "lora", "name": "U", "dl_url": "https://c/lora"}]
    )
    app._annuaire_install(0)
    assert calls == [("lora", "https://c/lora", True)]


def test_card_shows_the_source_badge(app) -> None:
    app._annuaire = gui._normalize_annuaire(
        [
            {"type": "lora", "name": "Local", "local_path": "C:/x/y.safetensors"},
            {"type": "lora", "name": "Remote", "dl_url": "https://c/lora"},
        ]
    )
    app._rebuild_annuaire_grid()
    texts = _label_texts(app._annuaire_grid)
    assert "Local" in texts
    assert "URL" in texts


def test_markdown_marks_local_entries_without_the_path() -> None:
    md = gui._annuaire_to_markdown(
        [
            {"type": "lora", "name": "L", "local_path": "C:/secret/path.safetensors",
             "enabled": True},
            {"type": "lora", "name": "U", "dl_url": "https://d", "enabled": True},
        ]
    )
    assert "- **Source**: local file" in md
    assert "C:/secret/path.safetensors" not in md
    assert md.count("**Source**") == 1


def test_annuaire_normalize_drops_bad_entries() -> None:
    entries = gui._normalize_annuaire(
        [
            {"type": "lora", "name": "Galaxy", "page_url": "https://civitai.com/models/1",
             "dl_url": "https://civitai.com/api/download/models/1", "note": "note",
             "mode": "ref2va", "trigger_words": "galaxy, nebula", "model": "Flux"},
            {"type": "bogus", "dl_url": "https://x/y"},
            {"type": "node", "dl_url": ""},
            "not-a-dict",
            None,
        ]
    )
    assert entries == [
        {"type": "lora", "name": "Galaxy", "page_url": "https://civitai.com/models/1",
         "dl_url": "https://civitai.com/api/download/models/1", "local_path": "",
         "note": "note", "mode": "ref2va", "trigger_words": "galaxy, nebula",
         "model": "Flux", "enabled": True}
    ]


def test_annuaire_normalize_invalid_mode_dropped() -> None:
    entries = gui._normalize_annuaire(
        [{"type": "lora", "dl_url": "https://c/l", "mode": "bogus"}]
    )
    assert entries[0]["mode"] == ""


def test_annuaire_normalize_non_list() -> None:
    assert gui._normalize_annuaire(None) == []
    assert gui._normalize_annuaire({"a": 1}) == []


def test_annuaire_download_lists_partition_by_type() -> None:
    loras, nodes, workflows = gui._annuaire_download_lists(
        [
            {"type": "lora", "dl_url": "https://c/lora1"},
            {"type": "node", "dl_url": "https://github.com/u/n.git"},
            {"type": "workflow", "name": "Mon Workflow", "dl_url": "https://c/wf.json"},
            {"type": "workflow", "name": "", "dl_url": "https://c/other.json"},
            {"type": "lora", "dl_url": ""},
        ]
    )
    assert loras == ["https://c/lora1"]
    assert nodes == ["https://github.com/u/n.git"]
    assert workflows == [
        "https://c/wf.json Mon_Workflow.json",
        "https://c/other.json",
    ]


def test_annuaire_download_lists_skips_unchecked_entries() -> None:
    loras, nodes, workflows = gui._annuaire_download_lists(
        [
            {"type": "lora", "dl_url": "https://c/on", "enabled": True},
            {"type": "lora", "dl_url": "https://c/off", "enabled": False},
            {"type": "node", "dl_url": "https://github.com/u/n.git", "enabled": False},
            {"type": "workflow", "name": "W", "dl_url": "https://c/w.json"},
        ]
    )
    assert loras == ["https://c/on"]
    assert nodes == []
    assert workflows == ["https://c/w.json W.json"]


def test_annuaire_normalize_defaults_enabled_to_true() -> None:
    # A library saved before the flag existed keeps downloading everything.
    entries = gui._normalize_annuaire(
        [
            {"type": "lora", "dl_url": "https://c/a"},
            {"type": "lora", "dl_url": "https://c/b", "enabled": False},
            {"type": "lora", "dl_url": "https://c/c", "enabled": "yes"},
        ]
    )
    assert [e["enabled"] for e in entries] == [True, False, True]


def test_annuaire_backup_round_trips_the_enabled_flag() -> None:
    entries = gui._normalize_annuaire(
        [
            {"type": "lora", "name": "A", "dl_url": "https://c/a", "enabled": False},
            {"type": "lora", "name": "B", "dl_url": "https://c/b"},
        ]
    )
    payload = gui.annuaire_backup_payload(entries)
    restored = gui.annuaire_from_backup(payload)
    assert [e["enabled"] for e in restored] == [False, True]


def test_annuaire_markdown_marks_disabled_entries() -> None:
    md = gui._annuaire_to_markdown(
        [
            {"type": "lora", "name": "Off", "dl_url": "https://c/a", "enabled": False},
            {"type": "lora", "name": "On", "dl_url": "https://c/b", "enabled": True},
        ]
    )
    assert "### Off" in md
    assert "**Disabled**" in md
    assert md.count("**Disabled**") == 1


def test_annuaire_card_has_the_autodownload_checkbox(app) -> None:
    from launcher import gui as gui_module

    app._annuaire = gui_module._normalize_annuaire(
        [{"type": "lora", "name": "A", "dl_url": "https://c/a"}]
    )
    app._rebuild_annuaire_grid()
    checkboxes = []

    def walk(widget):
        for child in widget.winfo_children():
            if isinstance(child, (tk.Checkbutton, tk.ttk.Checkbutton)):
                checkboxes.append(child)
            walk(child)

    walk(app._annuaire_grid)
    assert len(checkboxes) == 1
    assert "active" in str(app._annuaire_counter.cget("text"))


def test_annuaire_toggle_and_bulk_actions_persist(app, monkeypatch) -> None:
    from launcher import gui as gui_module

    saved: list = []
    monkeypatch.setattr(
        gui_module, "save_settings", lambda settings, home=None: saved.append(settings)
    )
    app._annuaire = gui_module._normalize_annuaire(
        [
            {"type": "lora", "name": "A", "dl_url": "https://c/a"},
            {"type": "lora", "name": "B", "dl_url": "https://c/b"},
        ]
    )
    app._rebuild_annuaire_grid()

    var = tk.BooleanVar(app.root, value=False)
    app._annuaire_toggle_enabled(0, var)
    assert app._annuaire[0]["enabled"] is False
    assert app.settings.comfy_annuaire[0]["enabled"] is False
    assert saved

    app._annuaire_set_all(False)
    assert [e["enabled"] for e in app._annuaire] == [False, False]
    app._annuaire_set_all(True)
    assert [e["enabled"] for e in app._annuaire] == [True, True]


def test_annuaire_edit_preserves_the_disabled_state(app) -> None:
    from launcher import gui as gui_module

    app._annuaire = gui_module._normalize_annuaire(
        [{"type": "lora", "name": "A", "dl_url": "https://c/a", "enabled": False}]
    )
    app._annuaire_edit(0)
    app._form_vars["name"].set("A2")
    app._annuaire_save_form()
    assert app._annuaire[0]["name"] == "A2"
    assert app._annuaire[0]["enabled"] is False


# --- grid rendering: card reuse, coalescing, clipboard paste ---------------


def _annuaire_entry(name, **extra):
    entry = {"type": "lora", "name": name, "dl_url": f"https://c/{name.lower()}"}
    entry.update(extra)
    return entry


def test_annuaire_grid_reuses_cards_across_rebuilds(app) -> None:
    app._annuaire = gui._normalize_annuaire(
        [_annuaire_entry("A"), _annuaire_entry("B"), _annuaire_entry("C")]
    )
    app._rebuild_annuaire_grid()
    before = {key: value[2] for key, value in app._annuaire_cards.items()}
    assert len(before) == 3

    # A second sync (a resize, a tab switch) must not recreate any widget.
    app._rebuild_annuaire_grid()
    assert {key: value[2] for key, value in app._annuaire_cards.items()} == before


def test_annuaire_grid_filter_keeps_the_surviving_cards(app) -> None:
    app._annuaire = gui._normalize_annuaire(
        [
            _annuaire_entry("A", model="Wan2.2"),
            _annuaire_entry("B", model="Wan2.2"),
            _annuaire_entry("C", model="MinimaxH3"),
        ]
    )
    app._rebuild_annuaire_grid()
    before = {key: value[2] for key, value in app._annuaire_cards.items()}

    app._annuaire_filter.set("wan")
    app._rebuild_annuaire_grid()
    after = {key: value[2] for key, value in app._annuaire_cards.items()}
    assert len(after) == 2
    assert all(before[key] is card for key, card in after.items())


def test_annuaire_card_is_rebuilt_when_its_content_changes(app) -> None:
    app._annuaire = gui._normalize_annuaire([_annuaire_entry("A")])
    app._rebuild_annuaire_grid()
    first = app._annuaire_cards[id(app._annuaire[0])][2]

    app._annuaire[0]["name"] = "A renamed"
    app._rebuild_annuaire_grid()
    assert app._annuaire_cards[id(app._annuaire[0])][2] is not first
    assert "A renamed" in _label_texts(app._annuaire_grid)


def test_annuaire_card_action_follows_its_entry_after_a_deletion(app, monkeypatch) -> None:
    """A reused card must act on its own entry, never on a stale position."""
    app._annuaire = gui._normalize_annuaire(
        [_annuaire_entry("A"), _annuaire_entry("B")]
    )
    app._rebuild_annuaire_grid()
    second = app._annuaire[1]
    opened: list = []
    monkeypatch.setattr(app, "_annuaire_open_page", opened.append)

    app._annuaire_delete(0)
    card = app._annuaire_cards[id(second)][2]
    actions = card.winfo_children()[-1]
    button = next(
        child for child in actions.winfo_children()
        if str(child.cget("text")) == "\U0001f517"
    )
    button.invoke()
    assert opened == [0]
    assert app._annuaire[opened[0]] is second


def test_annuaire_rebuilds_are_coalesced(app) -> None:
    app._annuaire = gui._normalize_annuaire([_annuaire_entry("A")])
    app._schedule_annuaire_rebuild(50)
    first = app._annuaire_rebuild_after
    app._schedule_annuaire_rebuild(50)
    assert app._annuaire_rebuild_after != first  # the first one was cancelled
    assert not app._annuaire_cards  # nothing is built before the deadline

    app._cancel_annuaire_rebuild()
    assert app._annuaire_rebuild_after is None


def test_annuaire_warm_builds_the_cards_without_showing_the_tab(app) -> None:
    app._annuaire = gui._normalize_annuaire([_annuaire_entry("A")])
    app._annuaire_cards.clear()
    app._warm_annuaire_grid()
    card = app._annuaire_cards[id(app._annuaire[0])][2]

    app._warm_annuaire_grid()  # already built: nothing to do
    assert app._annuaire_cards[id(app._annuaire[0])][2] is card


def test_annuaire_paste_adds_entries_without_touching_the_rest(app, monkeypatch) -> None:
    saved: list = []
    monkeypatch.setattr(
        gui, "save_settings", lambda settings, home=None: saved.append(settings)
    )
    app._annuaire = gui._normalize_annuaire([_annuaire_entry("Keep")])
    payload = gui.annuaire_backup_payload(
        [_annuaire_entry("New", model="Krea2", trigger_words="tw", mode="fl2va")]
    )
    app.root.clipboard_clear()
    app.root.clipboard_append(json.dumps(payload))
    app.root.update()

    app._annuaire_paste_json()

    assert [entry["name"] for entry in app._annuaire] == ["Keep", "New"]
    assert app._annuaire[1]["model"] == "Krea2"
    assert app._annuaire[1]["mode"] == "fl2va"
    assert app._annuaire[1]["trigger_words"] == "tw"
    assert saved
    assert "1 added" in app._log_text.get("1.0", "end-1c")


def test_annuaire_paste_is_idempotent(app) -> None:
    app._annuaire = gui._normalize_annuaire([_annuaire_entry("Keep")])
    payload = gui.annuaire_backup_payload([_annuaire_entry("New")])
    app.root.clipboard_clear()
    app.root.clipboard_append(json.dumps(payload))
    app.root.update()

    app._annuaire_paste_json()
    app._annuaire_paste_json()

    assert [entry["name"] for entry in app._annuaire] == ["Keep", "New"]
    assert "1 already present" in app._log_text.get("1.0", "end-1c")


def test_annuaire_paste_rejects_a_clipboard_that_is_not_json(app) -> None:
    app._annuaire = gui._normalize_annuaire([_annuaire_entry("Keep")])
    app.root.clipboard_clear()
    app.root.clipboard_append("pas du tout du json")
    app.root.update()

    app._annuaire_paste_json()

    assert [entry["name"] for entry in app._annuaire] == ["Keep"]
    assert "ne contient pas de JSON" in app._log_text.get("1.0", "end-1c")


def test_annuaire_paste_rejects_json_without_usable_entry(app) -> None:
    app._annuaire = gui._normalize_annuaire([_annuaire_entry("Keep")])
    app.root.clipboard_clear()
    app.root.clipboard_append(json.dumps({"entries": [{"type": "bogus"}]}))
    app.root.update()

    app._annuaire_paste_json()

    assert [entry["name"] for entry in app._annuaire] == ["Keep"]
    assert "No usable entry" in app._log_text.get("1.0", "end-1c")


def test_annuaire_truncation_is_memoized(app) -> None:
    calls: list = []
    original = gui.truncate_to_width
    app._annuaire_trunc_cache.clear()

    def counting(text, max_width, font):
        calls.append(text)
        return original(text, max_width, font)

    gui.truncate_to_width = counting
    try:
        first = app._truncate("un nom assez long pour être tronqué", 60, 10, "bold")
        second = app._truncate("un nom assez long pour être tronqué", 60, 10, "bold")
    finally:
        gui.truncate_to_width = original

    assert first == second
    assert calls == ["un nom assez long pour être tronqué"]


def test_annuaire_workflow_filename_sanitized() -> None:
    assert gui._annuaire_workflow_filename("Mon Workflow") == "Mon_Workflow.json"
    assert gui._annuaire_workflow_filename("déjà.json") == "d_j_.json"
    assert gui._annuaire_workflow_filename("  ") == ""


def test_settings_annuaire_roundtrip(tmp_path) -> None:
    s = gui.GuiSettings(
        comfy_annuaire=[
            {"type": "lora", "name": "A", "page_url": "p", "dl_url": "d",
             "local_path": "", "note": "n", "mode": "fl2va_ref2va",
             "trigger_words": "w1, w2", "model": "Wan2.2", "enabled": False}
        ]
    )
    gui.save_settings(s, home=tmp_path)
    loaded = gui.load_settings(home=tmp_path)
    assert loaded.comfy_annuaire == s.comfy_annuaire


def test_settings_annuaire_defaults_empty(tmp_path) -> None:
    assert gui.load_settings(home=tmp_path).comfy_annuaire == []


def _fill_form(app, **fields) -> None:
    """Open the form (comfy mode) and fill its widgets."""
    app._mode.set("comfy")
    app._on_stack_change()
    app._annuaire_new()
    for key, value in fields.items():
        if key == "note":
            app._form_note.delete("1.0", "end")
            app._form_note.insert("1.0", value)
        elif key == "type":
            app._form_type.set(value)
        elif key == "mode":
            app._form_mode.set(value)
        elif key == "model":
            app._form_model.set(value)
        elif key == "dl_url":
            app._form_dl_url.set(value)
        elif key == "local_path":
            app._form_local_path.set(value)
        elif key == "source":
            app._form_source.set(value)
        else:
            app._form_vars[key].set(value)


def test_annuaire_create_and_delete_via_form(app) -> None:
    _fill_form(app, name="A", dl_url="https://a")
    app._annuaire_save_form()
    _fill_form(app, name="B", dl_url="https://b")
    app._annuaire_save_form()
    assert len(app._annuaire) == 2
    assert app._annuaire[0]["name"] == "A"
    app._annuaire_delete(0)
    assert len(app._annuaire) == 1
    assert app._annuaire[0]["name"] == "B"


def test_annuaire_save_form_requires_dl_url(app) -> None:
    _fill_form(app, name="sans lien")
    app._annuaire_save_form()
    assert len(app._annuaire) == 0
    assert any("download link" in msg for _, msg in app._log_entries)


def test_annuaire_save_form_reads_all_fields(app) -> None:
    _fill_form(
        app,
        type="workflow",
        name="Mon LoRA",
        page_url="https://page",
        dl_url="https://dl",
        trigger_words="a, b",
        note="ma note",
        mode="REF2VA",
        model="Flux",
    )
    app._annuaire_save_form()
    assert app._annuaire[0] == {
        "type": "workflow", "name": "Mon LoRA", "page_url": "https://page",
        "dl_url": "https://dl", "trigger_words": "a, b", "note": "ma note",
        "mode": "ref2va", "model": "Flux", "enabled": True,
        "local_path": "",
    }


def test_annuaire_edit_loads_and_updates(app) -> None:
    app._annuaire = [
        {"type": "lora", "name": "A", "page_url": "p", "dl_url": "d", "note": "n",
         "mode": "", "trigger_words": "", "model": "Flux"}
    ]
    app._annuaire_edit(0)
    assert app._form_vars["name"].get() == "A"
    app._form_vars["name"].set("B")
    app._annuaire_save_form()
    assert len(app._annuaire) == 1
    assert app._annuaire[0]["name"] == "B"


def test_annuaire_filter_by_model(app) -> None:
    app._annuaire = [
        {"type": "lora", "name": "A", "page_url": "", "dl_url": "d1", "note": "",
         "mode": "", "trigger_words": "", "model": "Flux"},
        {"type": "lora", "name": "B", "page_url": "", "dl_url": "d2", "note": "",
         "mode": "", "trigger_words": "", "model": "Wan2.2"},
    ]
    app._annuaire_filter.set("flux")
    assert [i for i, _ in app._annuaire_filtered_entries()] == [0]
    app._annuaire_filter.set("")
    assert len(app._annuaire_filtered_entries()) == 2


def test_annuaire_grid_columns_for_width() -> None:
    fn = gui.ForgeApp._annuaire_grid_columns_for_width
    assert fn(50) == 1
    assert fn(900) == 4
    assert fn(999999) <= 25


def test_annuaire_grid_renders_cards(app) -> None:
    _fill_form(app, name="A", dl_url="https://a")
    app._annuaire_save_form()
    app.root.update_idletasks()
    assert len(app._annuaire_grid.winfo_children()) >= 1


def test_annuaire_to_markdown_groups_and_omits_links() -> None:
    md = gui._annuaire_to_markdown(
        [
            {"type": "lora", "name": "Galaxy", "page_url": "https://page",
             "dl_url": "https://dl", "note": "espace", "mode": "ref2va",
             "trigger_words": "galaxy, nebula", "model": "Flux"},
            {"type": "lora", "name": "Wan", "page_url": "", "dl_url": "https://d2",
             "note": "", "mode": "", "trigger_words": "", "model": "Wan2.2"},
            {"type": "lora", "name": "SansMod", "page_url": "", "dl_url": "https://d3",
             "note": "n", "mode": "", "trigger_words": "w", "model": ""},
        ]
    )
    assert "## Flux" in md
    assert "### Galaxy" in md
    assert "Trigger words" in md
    assert "galaxy, nebula" in md
    assert "espace" in md
    assert "## Wan2.2" in md
    assert "## No model" in md
    assert "https://dl" not in md
    assert "https://d2" not in md


def test_annuaire_export_md_writes_file(app, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gui.Path, "home", lambda: tmp_path)
    app._annuaire = [
        {"type": "lora", "name": "A", "page_url": "", "dl_url": "d",
         "note": "n", "mode": "", "trigger_words": "w1", "model": "Flux"},
    ]
    app._annuaire_export_md()
    out = tmp_path / "Downloads" / "bibliotheque-loras.md"
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "### A" in content
    assert "w1" in content


# ---------------------------------------------------------------------------
# Bibliothèque : export/import JSON (sauvegarde portable)
# ---------------------------------------------------------------------------

_BACKUP_ENTRY_A = {
    "type": "lora", "name": "Alpha", "page_url": "https://pA",
    "dl_url": "https://dA", "note": "note A", "mode": "fl2va",
    "trigger_words": "alpha, beta", "model": "MinimaxH3", "enabled": True,
    "local_path": "",
}
_BACKUP_ENTRY_B = {
    "type": "workflow", "name": "BetaFlow", "page_url": "",
    "dl_url": "https://dB", "note": "", "mode": "",
    "trigger_words": "", "model": "Wan2.2", "enabled": True,
    "local_path": "",
}


def test_annuaire_backup_roundtrip_keeps_all_fields() -> None:
    payload = gui.annuaire_backup_payload(
        [dict(_BACKUP_ENTRY_A), dict(_BACKUP_ENTRY_B)]
    )
    assert payload["format"] == gui.ANNUAIRE_BACKUP_FORMAT
    assert payload["version"] == gui.ANNUAIRE_BACKUP_VERSION
    # Unlike the .md export, the download link is preserved.
    assert payload["entries"][0]["dl_url"] == "https://dA"
    restored = gui.annuaire_from_backup(json.loads(json.dumps(payload)))
    assert restored == [dict(_BACKUP_ENTRY_A), dict(_BACKUP_ENTRY_B)]


def test_annuaire_from_backup_accepts_bare_list_and_garbage() -> None:
    assert gui.annuaire_from_backup([dict(_BACKUP_ENTRY_A)]) == [
        dict(_BACKUP_ENTRY_A)
    ]
    assert gui.annuaire_from_backup({"entries": [dict(_BACKUP_ENTRY_A)]}) == [
        dict(_BACKUP_ENTRY_A)
    ]
    assert gui.annuaire_from_backup({"entries": None}) == []
    assert gui.annuaire_from_backup("nonsense") == []
    assert gui.annuaire_from_backup([{"type": "lora", "name": "NoDL"}]) == []


def test_annuaire_merge_dedups_by_type_and_name() -> None:
    current = [dict(_BACKUP_ENTRY_A)]
    incoming = [
        dict(_BACKUP_ENTRY_A),
        {**_BACKUP_ENTRY_A, "name": "ALPHA"},
        dict(_BACKUP_ENTRY_B),
    ]
    merged, added, skipped = gui.annuaire_merge(current, incoming)
    assert added == 1
    assert skipped == 2
    assert [e["name"] for e in merged] == ["Alpha", "BetaFlow"]


def test_annuaire_export_json_writes_backup_file(app, monkeypatch, tmp_path) -> None:
    out = tmp_path / "backup.json"
    monkeypatch.setattr(
        "tkinter.filedialog.asksaveasfilename", lambda **kwargs: str(out)
    )
    app._annuaire = [dict(_BACKUP_ENTRY_A), dict(_BACKUP_ENTRY_B)]
    app._annuaire_export_json()
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["format"] == gui.ANNUAIRE_BACKUP_FORMAT
    assert [e["name"] for e in data["entries"]] == ["Alpha", "BetaFlow"]
    assert data["entries"][0]["dl_url"] == "https://dA"


def test_annuaire_export_json_empty_library_skips_dialog(app, monkeypatch) -> None:
    called: list = []
    monkeypatch.setattr(
        "tkinter.filedialog.asksaveasfilename",
        lambda **kwargs: called.append(1) or "",
    )
    app._annuaire = []
    app._annuaire_export_json()
    assert called == []


def test_annuaire_import_json_merges_and_persists(app, monkeypatch, tmp_path) -> None:
    saved: list = []
    monkeypatch.setattr(
        gui,
        "save_settings",
        lambda s, home=None: saved.append(list(s.comfy_annuaire)),
    )
    src = tmp_path / "backup.json"
    src.write_text(
        json.dumps(
            gui.annuaire_backup_payload(
                [dict(_BACKUP_ENTRY_A), dict(_BACKUP_ENTRY_B)]
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tkinter.filedialog.askopenfilename", lambda **kwargs: str(src)
    )
    app._annuaire = [dict(_BACKUP_ENTRY_A)]
    app._annuaire_import_json()
    assert [e["name"] for e in app._annuaire] == ["Alpha", "BetaFlow"]
    assert saved
    assert [e["name"] for e in saved[-1]] == ["Alpha", "BetaFlow"]


def test_annuaire_import_json_invalid_file_keeps_library(app, monkeypatch, tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        "tkinter.filedialog.askopenfilename", lambda **kwargs: str(bad)
    )
    app._annuaire = [dict(_BACKUP_ENTRY_A)]
    app._annuaire_import_json()
    assert [e["name"] for e in app._annuaire] == ["Alpha"]


def test_annuaire_import_json_no_valid_entries_keeps_library(
    app, monkeypatch, tmp_path
) -> None:
    src = tmp_path / "empty.json"
    src.write_text(json.dumps({"entries": []}), encoding="utf-8")
    monkeypatch.setattr(
        "tkinter.filedialog.askopenfilename", lambda **kwargs: str(src)
    )
    app._annuaire = [dict(_BACKUP_ENTRY_A)]
    app._annuaire_import_json()
    assert [e["name"] for e in app._annuaire] == ["Alpha"]


def test_annuaire_open_page_normalizes_and_opens(app, monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(gui.webbrowser, "open", lambda url: opened.append(url))
    app._annuaire = [
        {"type": "lora", "name": "", "page_url": "civitai.com/models/123",
         "dl_url": "", "note": ""},
        {"type": "lora", "name": "", "page_url": "",
         "dl_url": "", "note": ""},
    ]
    app._annuaire_open_page(0)
    assert opened == ["https://civitai.com/models/123"]
    # Empty page link -> no browser call, just a warning log.
    app._annuaire_open_page(1)
    assert opened == ["https://civitai.com/models/123"]


def test_annuaire_model_values_defaults_plus_used(app) -> None:
    app._annuaire = [
        {"type": "lora", "name": "", "page_url": "", "dl_url": "d", "note": "",
         "mode": "", "trigger_words": "", "model": "FooModel"},
    ]
    values = app._annuaire_model_values()
    assert "FooModel" in values
    assert values.count("FooModel") == 1
    for default in gui.DEFAULT_ANNUAIRE_MODELS:
        assert default in values


def test_on_mousewheel_scrolls_matching_canvas(app) -> None:
    deltas = []

    class _Canvas:
        pass

    canvas = _Canvas()
    canvas._forge_scroll = lambda delta: deltas.append(delta)
    child = type("W", (), {})()
    child.master = canvas
    grandchild = type("W", (), {})()
    grandchild.master = child

    class _Event:
        widget = grandchild
        delta = -120

    app._on_mousewheel(_Event())
    assert deltas == [-120]


class _FakeCanvas:
    def __init__(self, y0, y1):
        self._y = (y0, y1)
        self.scrolled = []

    def yview(self):
        return self._y

    def yview_scroll(self, units, what="units"):
        self.scrolled.append((units, what))


def test_scroll_canvas_clamped_stops_at_bounds() -> None:
    # Content fits -> no scroll either way.
    c = _FakeCanvas(0.0, 1.0)
    gui._scroll_canvas_clamped(c, -120)
    gui._scroll_canvas_clamped(c, 120)
    assert c.scrolled == []
    # At top of an overflowing view: scroll up is blocked, scroll down works.
    c = _FakeCanvas(0.0, 0.5)
    gui._scroll_canvas_clamped(c, 120)
    assert c.scrolled == []
    gui._scroll_canvas_clamped(c, -120)
    assert c.scrolled == [(1, "units")]
    # At bottom: scroll down is blocked, scroll up works.
    c = _FakeCanvas(0.5, 1.0)
    gui._scroll_canvas_clamped(c, -120)
    assert c.scrolled == []
    gui._scroll_canvas_clamped(c, 120)
    assert c.scrolled == [(-1, "units")]


def test_comfy_overrides_includes_annuaire(app) -> None:
    app._annuaire = [
        {"type": "lora", "name": "", "page_url": "", "dl_url": "https://c/l.safetensors", "note": ""},
        {"type": "node", "name": "", "page_url": "", "dl_url": "https://github.com/u/n.git", "note": ""},
        {"type": "workflow", "name": "Ref", "page_url": "", "dl_url": "https://c/w.json", "note": ""},
    ]
    o = app._comfy_overrides()
    assert o["custom_loras"] == ["https://c/l.safetensors"]
    assert o["custom_nodes"] == ["https://github.com/u/n.git"]
    assert o["custom_workflows"] == ["https://c/w.json Ref.json"]


# ---------------------------------------------------------------------------
# Minuteur : échéance absolue, persistance, arrêt de tous les pods, retries
# ---------------------------------------------------------------------------


def _set_timer_state_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MINIMAX_LAUNCHER_STATE_DIR", str(tmp_path))


def test_timer_tick_uses_absolute_deadline(app, monkeypatch) -> None:
    # A deadline in the past fires immediately, even if a stale tick counter
    # (e.g. frozen while the machine slept) still shows a long countdown.
    fired = []
    monkeypatch.setattr(app, "_timer_fire", lambda: fired.append(1))
    app._timer_deadline = time.time() - 5
    app._timer_remaining = 3600
    app._timer_tick()
    assert fired == [1]


def test_timer_tick_future_deadline_keeps_counting(app) -> None:
    app._timer_deadline = time.time() + 300
    app._timer_remaining = 999
    app._timer_tick()
    assert app._timer_after_id is not None
    assert 295 <= app._timer_remaining <= 300


def test_timer_start_persists_and_cancel_clears(app, monkeypatch, tmp_path) -> None:
    _set_timer_state_dir(monkeypatch, tmp_path)
    entry = app._timer_minutes_entry
    entry.configure(state="normal")
    entry.delete(0, "end")
    entry.insert(0, "10")
    app._timer_start()
    p = gui.runtime_state.pending_stop_path()
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert 590 <= data["deadline"] - time.time() <= 600
    app._timer_cancel()
    assert not p.exists()
    assert app._timer_after_id is None


def test_load_pending_stop_past_deadline_fires(app, monkeypatch, tmp_path) -> None:
    # A stop missed while the launcher was closed must fire immediately on
    # the next start; the schedule file is kept until the stop succeeds.
    _set_timer_state_dir(monkeypatch, tmp_path)
    fired = []
    monkeypatch.setattr(app, "_timer_fire", lambda: fired.append(1))
    p = gui.runtime_state.pending_stop_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"deadline": time.time() - 10, "power_mode": "sleep"}),
        encoding="utf-8",
    )
    app._load_pending_stop()
    assert fired == [1]
    assert app._power_mode.get() == "sleep"
    assert p.exists()


def test_load_pending_stop_future_deadline_resumes(app, monkeypatch, tmp_path) -> None:
    _set_timer_state_dir(monkeypatch, tmp_path)
    p = gui.runtime_state.pending_stop_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"deadline": time.time() + 300, "power_mode": "none"}),
        encoding="utf-8",
    )
    app._load_pending_stop()
    assert app._timer_after_id is not None
    assert 295 <= app._timer_remaining <= 300
    assert str(app._timer_label.cget("text")) != "—"


def test_load_pending_stop_missing_file_is_noop(app) -> None:
    app._load_pending_stop()
    assert app._timer_after_id is None
    assert app._timer_deadline == 0.0


def test_timer_stop_finished_success_clears_schedule(
    app, monkeypatch, tmp_path
) -> None:
    _set_timer_state_dir(monkeypatch, tmp_path)
    p = gui.runtime_state.pending_stop_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"deadline": time.time() - 1, "power_mode": "none"}),
        encoding="utf-8",
    )
    app._timer_stop_finished(True, "Pod comfy : Pod RunPod arrêté (disque conservé).", "none")
    assert not p.exists()


def test_timer_fire_retries_until_success(app, monkeypatch) -> None:
    results = [
        (False, "boom 1"),
        (False, "boom 2"),
        (True, "pods stopped"),
    ]
    it = iter(results)
    monkeypatch.setattr(
        gui, "action_stop_outcome", lambda **kw: next(it, (True, "pods stopped"))
    )
    sleeps = []
    real_sleep = time.sleep

    def guarded_sleep(seconds):
        if seconds in (10, 20):
            sleeps.append(seconds)
            return
        real_sleep(min(seconds, 0.01))

    monkeypatch.setattr(gui.time, "sleep", guarded_sleep)
    app._timer_fire()
    app._timer_thread.join(timeout=10)
    assert sleeps == [10, 20]
    found = None
    limit = time.time() + 5
    while time.time() < limit and found is None:
        try:
            item = app._queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if item[0] == "timer_stop_done":
            found = item
    assert found is not None
    assert found[1] is True
    assert found[2] == "pods stopped"


def test_action_stop_outcome_no_registered_pod_is_ok(app, monkeypatch) -> None:
    class EmptyRegistry:
        def all(self):
            return {}

    monkeypatch.setattr(gui, "PodRegistry", lambda: EmptyRegistry())
    monkeypatch.setattr(gui.orchestrator, "stop", lambda **kw: None)
    monkeypatch.setattr(
        gui, "load_config", lambda **kw: (_ for _ in ()).throw(gui.ConfigError("no env"))
    )
    ok, msg = gui.action_stop_outcome(terminate=False)
    assert ok is True
    assert "Local processes" in msg


def test_action_stop_outcome_missing_api_key_fails(app, monkeypatch) -> None:
    class Reg:
        def all(self):
            return {"comfy": object()}

    monkeypatch.setattr(gui, "PodRegistry", lambda: Reg())
    monkeypatch.setattr(gui.orchestrator, "stop", lambda **kw: None)
    monkeypatch.setattr(
        gui, "load_config", lambda **kw: (_ for _ in ()).throw(gui.ConfigError("no env"))
    )
    ok, msg = gui.action_stop_outcome(terminate=False)
    assert ok is False
    assert "were NOT stopped" in msg


def test_action_stop_outcome_bad_outcome_fails(app, monkeypatch) -> None:
    class Reg:
        def all(self):
            return {"comfy": object()}

    class FakeCfg:
        class secrets:
            runpod_api_key = "rp_test"

    monkeypatch.setattr(gui, "PodRegistry", lambda: Reg())
    monkeypatch.setattr(gui.orchestrator, "stop", lambda **kw: {"comfy": "unavailable"})
    monkeypatch.setattr(gui, "load_config", lambda **kw: FakeCfg())
    monkeypatch.setattr(gui, "RunPodClient", lambda key, timeout=10.0: object())
    ok, msg = gui.action_stop_outcome(terminate=False)
    assert ok is False
    assert "billing" in msg


def test_action_stop_outcome_all_stopped_is_ok(app, monkeypatch) -> None:
    class Reg:
        def all(self):
            return {"comfy": object(), "agent": object()}

    class FakeCfg:
        class secrets:
            runpod_api_key = "rp_test"

    monkeypatch.setattr(gui, "PodRegistry", lambda: Reg())
    monkeypatch.setattr(
        gui.orchestrator,
        "stop",
        lambda **kw: {"comfy": "stopped", "agent": "already_stopped"},
    )
    monkeypatch.setattr(gui, "load_config", lambda **kw: FakeCfg())
    monkeypatch.setattr(gui, "RunPodClient", lambda key, timeout=10.0: object())
    ok, msg = gui.action_stop_outcome(terminate=False)
    assert ok is True
    assert "already stopped" in msg


# ---------------------------------------------------------------------------
# Billing widget (pod spend so far + RunPod wallet balance)
# ---------------------------------------------------------------------------


class _FakeWallet:
    def __init__(self, balance=5.49, spend_per_hour=0.541):
        self.balance = balance
        self.spend_per_hour = spend_per_hour
        self.spend_limit = None
        self.email = None


class _FakePod:
    def __init__(
        self,
        started_at=None,
        created_at=None,
        cost_per_hour=None,
        status="RUNNING",
    ):
        self.started_at = started_at
        self.created_at = created_at
        self.cost_per_hour = cost_per_hour
        self.status = status

    @property
    def is_running(self):
        return self.status == "RUNNING"


class _FakeBillingRunPod:
    def __init__(self, pod=None, wallet=None, pod_error=None, wallet_error=None):
        self._pod = pod
        self._wallet = wallet
        self._pod_error = pod_error
        self._wallet_error = wallet_error

    def get_pod(self, pod_id):
        if self._pod_error is not None:
            raise self._pod_error
        return self._pod

    def get_wallet(self):
        if self._wallet_error is not None:
            raise self._wallet_error
        return self._wallet


class _FakeRecord:
    def __init__(self, created_at=None):
        self.pod_id = "pod-1"
        self.gpu_id = None
        self.created_at = created_at


class _FakeRegistry:
    def __init__(self, record=None):
        self._record = record

    def load(self, stack="agent"):
        return self._record


def test_pod_billing_info_happy_path() -> None:
    now = 1_000_000.0
    rp = _FakeBillingRunPod(
        pod=_FakePod(started_at=now - 7200, cost_per_hour=0.53),
        wallet=_FakeWallet(balance=5.49, spend_per_hour=0.541),
    )
    info = gui._pod_billing_info(None, rp, _FakeRegistry(_FakeRecord()), now=now)
    assert info["running"] is True
    assert info["elapsed"] == "2 h 00 min"
    assert info["cost"] == "$1.06"
    assert info["rate"] == "$0.53/h"
    assert info["balance"] == "$5.49"
    assert info["hours_left"] == pytest.approx(5.49 / 0.53)
    assert info["hours_left_text"] == "~10 h"


def test_pod_billing_info_short_elapsed_uses_minutes() -> None:
    now = 1_000_000.0
    rp = _FakeBillingRunPod(
        pod=_FakePod(started_at=now - 2700, cost_per_hour=0.53),
        wallet=_FakeWallet(),
    )
    info = gui._pod_billing_info(None, rp, _FakeRegistry(_FakeRecord()), now=now)
    assert info["elapsed"] == "45 min"


def test_pod_billing_info_wallet_down_keeps_pod_spend() -> None:
    now = 1_000_000.0
    rp = _FakeBillingRunPod(
        pod=_FakePod(started_at=now - 7200, cost_per_hour=0.53),
        wallet_error=RuntimeError("boom"),
    )
    info = gui._pod_billing_info(None, rp, _FakeRegistry(_FakeRecord()), now=now)
    assert info["has_wallet"] is False
    assert info["balance"] is None
    assert info["elapsed"] == "2 h 00 min"
    assert info["cost"] == "$1.06"


def test_pod_billing_info_pod_down_keeps_balance() -> None:
    now = 1_000_000.0
    rp = _FakeBillingRunPod(
        pod_error=RuntimeError("boom"), wallet=_FakeWallet()
    )
    info = gui._pod_billing_info(None, rp, _FakeRegistry(_FakeRecord()), now=now)
    assert info["has_pod"] is False
    assert info["elapsed"] is None
    assert info["balance"] == "$5.49"


def test_pod_billing_info_both_down_is_none() -> None:
    rp = _FakeBillingRunPod(pod_error=RuntimeError("a"), wallet_error=RuntimeError("b"))
    assert gui._pod_billing_info(None, rp, _FakeRegistry(_FakeRecord())) is None


def test_pod_billing_info_no_runpod_is_none() -> None:
    assert gui._pod_billing_info(None, None, _FakeRegistry(_FakeRecord())) is None


def test_pod_billing_info_registry_start_fallback() -> None:
    now = 1_000_000.0
    rp = _FakeBillingRunPod(
        pod=_FakePod(cost_per_hour=0.53),
        wallet=_FakeWallet(spend_per_hour=0.541),
    )
    info = gui._pod_billing_info(
        None, rp, _FakeRegistry(_FakeRecord(created_at=now - 3600)), now=now
    )
    assert info["elapsed"] == "1 h 00 min"
    assert info["cost"] == "$0.53"


def test_pod_billing_info_stopped_pod_no_cost() -> None:
    now = 1_000_000.0
    rp = _FakeBillingRunPod(
        pod=_FakePod(started_at=now - 7200, status="STOPPED"),
        wallet=_FakeWallet(balance=5.49, spend_per_hour=0.0),
    )
    info = gui._pod_billing_info(None, rp, _FakeRegistry(_FakeRecord()), now=now)
    assert info["running"] is False
    assert info["cost"] is None
    assert info["rate"] is None
    assert info["hours_left"] is None
    assert info["balance"] == "$5.49"


def test_snapshot_includes_billing(monkeypatch) -> None:
    monkeypatch.setattr(
        gui,
        "_pod_billing_info",
        lambda config, runpod, registry, now=None: {"balance": "$5.49"},
    )
    snapshot = gui.build_status_snapshot(
        None, diagnose_fn=lambda cfg, **kwargs: _fake_report()
    )
    assert snapshot["billing"] == {"balance": "$5.49"}


def _billing_dict(**overrides) -> dict:
    d = {
        "running": True,
        "has_pod": True,
        "has_wallet": True,
        "elapsed": "3 h 12 min",
        "cost": "$1.71",
        "rate": "$0.53/h",
        "balance": "$5.49",
        "hours_left": 10.0,
        "hours_left_text": "~10 h",
    }
    d.update(overrides)
    return d


def test_billing_label_renders_values(app) -> None:
    app._apply_billing({"billing": _billing_dict()})
    app.root.update()
    text = app._billing_label.cget("text")
    assert "3 h 12 min" in text
    assert "$1.71" in text
    assert "$0.53/h" in text
    assert "$5.49" in text
    assert "~10 h" in text
    assert app._billing_label.winfo_manager() != ""


def test_billing_label_normal_color_when_plenty_left(app) -> None:
    app._apply_billing({"billing": _billing_dict(hours_left=10.0)})
    assert _fg_of(app, app._billing_label) == app._pal["fg"]


def test_billing_label_warns_under_6h(app) -> None:
    app._apply_billing({"billing": _billing_dict(hours_left=3.0, hours_left_text="~3 h")})
    assert _fg_of(app, app._billing_label) == app._pal["warn"]


def test_billing_label_errors_under_2h(app) -> None:
    app._apply_billing(
        {"billing": _billing_dict(hours_left=0.5, hours_left_text="~30 min")}
    )
    assert _fg_of(app, app._billing_label) == app._pal["error"]


def test_billing_label_keeps_last_value_on_probe_failure(app) -> None:
    app._apply_billing({"billing": _billing_dict()})
    app._apply_billing({"billing": None})
    app.root.update()
    assert app._billing_label.winfo_manager() != ""
    assert "3 h 12 min" in app._billing_label.cget("text")


def test_billing_label_hidden_when_no_billing(app) -> None:
    app._apply_billing({"billing": None})
    app.root.update()
    assert app._billing_label.winfo_manager() == ""


def test_billing_label_wallet_only_when_no_pod(app) -> None:
    app._apply_billing(
        {
            "billing": _billing_dict(
                running=False,
                has_pod=False,
                elapsed=None,
                cost=None,
                rate=None,
                hours_left=None,
                hours_left_text=None,
            )
        }
    )
    text = app._billing_label.cget("text")
    assert "Solde RunPod : $5.49" in text
    assert "Pod" not in text.split("Solde")[0]
# ---------------------------------------------------------------------------
# Open-stack re-adoption (pod opened from another session)
# ---------------------------------------------------------------------------


class _FakeScanRunPod:
    def __init__(self, pods=None, error=None):
        self._pods = pods
        self._error = error
        self.calls = 0

    def list_pods(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._pods


class _FakeScanRegistry:
    def __init__(self, record=None, fail_save=False):
        self._record = record
        self._fail_save = fail_save
        self.saved = []

    def load(self, stack="agent"):
        return self._record

    def save(self, record, stack=None):
        if self._fail_save:
            raise RuntimeError("save failed")
        self.saved.append((record, stack))


class _PodCfg:
    class secrets:
        runpod_api_key = "rp_test"

    stack = "agent"


def _scan_pod(pod_id, name, status="RUNNING", created_at=100.0, env=None):
    return SimpleNamespace(
        id=pod_id, name=name, status=status, created_at=created_at,
        gpu_id="G", data_center_id="US", env=env or {},
    )


def test_detect_pod_stack_unknown_returns_none() -> None:
    class P:
        name = "random-pod"
        env = {"FOO": "bar"}

    assert gui._detect_pod_stack(P()) is None


def test_scan_open_stacks_degrades_without_runpod() -> None:
    assert gui.scan_open_stacks(_PodCfg, None, _FakeScanRegistry(), "agent") == ([], None)


def test_scan_open_stacks_degrades_on_list_error() -> None:
    rp = _FakeScanRunPod(pods=None, error=RuntimeError("api down"))
    found, adopted = gui.scan_open_stacks(_PodCfg, rp, _FakeScanRegistry(), "agent")
    assert found == [] and adopted is None


def _idle_snapshot() -> dict:
    return _full_snapshot(
        overall="STOPPED", runpod="STOPPED", ssh="STOPPED", vllm="UNKNOWN",
        model="UNKNOWN", openfox="STOPPED", searxng="UNKNOWN", url="",
    )


# ---------------------------------------------------------------------------
# Refonte visuelle : panneaux, onglets, troncature, réparation en un clic
# ---------------------------------------------------------------------------


class _FakeFont:
    """Fixed-width stand-in: measures len(text) * per_char pixels."""

    def __init__(self, per_char: int = 10) -> None:
        self.per_char = per_char

    def measure(self, text: str) -> int:
        return len(text) * self.per_char


def test_truncate_to_width_measures_pixels_not_characters() -> None:
    font = _FakeFont(per_char=10)
    assert gui.truncate_to_width("abcdefghij", 200, font) == "abcdefghij"
    short = gui.truncate_to_width("abcdefghijklmnopqrstuvwxyz", 100, font)
    assert short.endswith("…")
    assert font.measure(short) <= 100
    assert len(short) == 10  # 9 characters + the ellipsis at 10 px each
    assert gui.truncate_to_width("abc", 0, font) == "abc"


def test_truncate_to_width_uses_the_real_font(app) -> None:
    font = app._ui_font(10, "bold")
    long_name = "A" * 200
    truncated = gui.truncate_to_width(long_name, 120, font)
    assert truncated.endswith("…")
    assert font.measure(truncated) <= 120
    assert gui.truncate_to_width("court", 500, font) == "court"


def test_credential_field_labels_shared_computation() -> None:
    details = {
        "state": "CONFIGURED",
        "runpod": True,
        "template": False,
        "comfy_template": False,
        "hf": True,
        "civitai": False,
    }
    labels = gui.credential_field_labels(details, ("runpod", "template", "hf"))
    assert labels["runpod"] == ("set", "ok")
    assert labels["template"] == ("not set", "muted")
    assert labels["hf"] == ("set", "ok")
    for state in ("UNKNOWN", "CORRUPT"):
        unknown = gui.credential_field_labels({"state": state}, ("runpod",))
        assert unknown["runpod"] == ("unknown", "muted")
    assert gui.credential_field_labels({}, ("runpod",))["runpod"] == ("unknown", "muted")


def _open_secrets_dialog(app, monkeypatch):
    """Build the standalone credentials dialog without blocking on it."""
    from launcher.credentials import CredentialStoreError

    class _Store:
        def get(self):
            raise CredentialStoreError("test")

        def status(self):
            raise CredentialStoreError("test")

    monkeypatch.setattr(gui, "CredentialStore", lambda *a, **k: _Store())
    app.root.wait_window = lambda dialog: None
    gui._secrets_dialog(app.root, app.settings.theme, lambda values: None)
    dialogs = [
        child for child in app.root.winfo_children()
        if isinstance(child, tk.Toplevel)
    ]
    return dialogs[-1]


def _walk_widgets(node) -> list:
    found = []
    for child in node.winfo_children():
        found.append(child)
        found.extend(_walk_widgets(child))
    return found


def test_secrets_dialog_labels_are_painted_with_the_palette(app, monkeypatch) -> None:
    # Regression: the fallback branch must pass bg/fg, otherwise the dark
    # dialog shows light-grey patches with black text (SystemButtonFace).
    dialog = _open_secrets_dialog(app, monkeypatch)
    try:
        labels = [
            widget for widget in _walk_widgets(dialog)
            if isinstance(widget, (tk.Label, tk.ttk.Label))
        ]
        assert labels
        # Whatever the mode, every label must sit on the dialog's own
        # background — never on the light system default.
        dialog_bg = str(dialog.cget("bg"))
        assert dialog_bg == app._pal["bg"]
        for label in labels:
            assert _bg_of(app, label) == dialog_bg, str(label.cget("text"))
    finally:
        dialog.destroy()


def test_panels_sit_on_a_distinct_surface_with_a_visible_border(app) -> None:
    # Page vs panel: the framed sections use the raised surface (different
    # from the page background) and a border lifted toward the foreground.
    assert app._pal["surface"] != app._pal["bg"]
    assert app._pal["panel_border"] != app._pal["border"]
    for panel in (app._dash_frame, app._log_frame, app._comfy_models_frame):
        assert _bg_of(app, panel) == app._pal["surface"]
    if app._tb_style is None:
        assert (
            str(app._dash_frame.cget("highlightbackground"))
            == app._pal["panel_border"]
        )


def test_inactive_tabs_differ_from_the_page_background(app) -> None:
    style = app.root.tk.call(
        "ttk::style", "lookup", "Forge.TNotebook.Tab", "-background"
    )
    selected = app.root.tk.call(
        "ttk::style", "lookup", "Forge.TNotebook.Tab", "-background", "selected"
    )
    assert str(style) == app._pal["bg_alt"]
    assert str(style) != app._pal["bg"]
    assert str(selected) == app._pal["accent"]


def test_health_table_columns_fit_their_content(app) -> None:
    app._apply_status(_full_snapshot())
    app.root.update_idletasks()
    info_width = int(app._dash_table.column("info", "width"))
    component_width = int(app._dash_table.column("component", "width"))
    # Sized to the content and capped — never stretched across the panel.
    assert 40 <= info_width <= 460
    assert 40 <= component_width <= 240
    assert app._dash_table.column("info", "stretch") in (0, "0", False)
    assert app._dash_table.column("component", "stretch") in (0, "0", False)


def test_preset_delete_sits_next_to_the_preset_combo(app) -> None:
    assert app._btn_preset_delete.pack_info()["side"] == "left"
    assert app._btn_preset_delete.master is app._comfy_preset_combo.master
    # In agent mode the control is greyed with the mode explanation; in the
    # ComfyUI mode (with a personal tier, which is what activates presets)
    # it is live and its own tooltip says what it deletes.
    app._mode.set("comfy")
    app._comfy_tier.set("perso")
    app._on_stack_change()
    assert str(app._btn_preset_delete.cget("state")) == "normal"
    tooltip = app._tooltip_texts.get(app._btn_preset_delete, "")
    assert "preset selected" in tooltip


def test_comfy_model_rows_refit_on_resize(app) -> None:
    from types import SimpleNamespace

    calls: list = []
    original = app._rebuild_comfy_model_rows
    app._rebuild_comfy_model_rows = lambda: calls.append(1)
    app._comfy_rows_width = 0
    try:
        # Unmapped / too narrow: nothing to do.
        app._on_comfy_rows_configure(SimpleNamespace(width=20))
        assert calls == []
        # First real width, then a small nudge (below the 24 px threshold).
        app._on_comfy_rows_configure(SimpleNamespace(width=400))
        app._on_comfy_rows_configure(SimpleNamespace(width=410))
        assert len(calls) == 1
        # A meaningful resize re-derives the pixel budget.
        app._on_comfy_rows_configure(SimpleNamespace(width=520))
        assert len(calls) == 2
    finally:
        app._rebuild_comfy_model_rows = original


def test_annuaire_card_banner_colour_follows_mode(app) -> None:
    # One distinct banner colour per mode, so a growing library stays
    # scannable at a glance.
    for mode, expected in gui.ANNUAIRE_MODE_COLORS.items():
        card, _name_label, _tw_label = app._build_annuaire_card(
            app._annuaire_grid,
            {"name": "x", "type": "lora", "mode": mode, "model": "m", "url": "u"},
            200,
        )
        header = card.winfo_children()[0]
        assert str(header.cget("bg")) == expected, mode
        # The type stays readable as text on the banner.
        labels = [str(child.cget("text")) for child in header.winfo_children()]
        assert gui.ANNUAIRE_TYPE_LABELS["lora"] in labels
        card.destroy()


def test_annuaire_mode_colours_are_all_distinct(app) -> None:
    colours = list(gui.ANNUAIRE_MODE_COLORS.values())
    assert len(set(colours)) == len(colours)


def test_annuaire_card_banner_shows_the_extended_modes(app) -> None:
    for mode, label in gui.ANNUAIRE_MODE_LABELS.items():
        if not mode:
            continue
        card, _name_label, _tw_label = app._build_annuaire_card(
            app._annuaire_grid,
            {"name": "x", "type": "lora", "mode": mode, "model": "m", "url": "u"},
            200,
        )
        header = card.winfo_children()[0]
        labels = [str(child.cget("text")) for child in header.winfo_children()]
        assert label in labels, mode
        card.destroy()


def test_annuaire_legacy_modes_migrate_to_the_new_vocabulary() -> None:
    entries = gui._normalize_annuaire(
        [
            {"type": "lora", "dl_url": "https://x/1", "mode": "fl2v"},
            {"type": "lora", "dl_url": "https://x/2", "mode": "ref2v"},
            {"type": "lora", "dl_url": "https://x/3", "mode": "both"},
            {"type": "lora", "dl_url": "https://x/4", "mode": "turbo"},
            {"type": "lora", "dl_url": "https://x/5", "mode": "l2va"},
            {"type": "lora", "dl_url": "https://x/6", "mode": "bogus"},
        ]
    )
    assert [entry["mode"] for entry in entries] == [
        "fl2va", "ref2va", "fl2va_ref2va", "turbo", "l2va", "",
    ]


def test_canonical_annuaire_mode_helper() -> None:
    assert gui._canonical_annuaire_mode("fl2v") == "fl2va"
    assert gui._canonical_annuaire_mode("both") == "fl2va_ref2va"
    assert gui._canonical_annuaire_mode("t2va") == "t2va"
    assert gui._canonical_annuaire_mode("") == ""
    assert gui._canonical_annuaire_mode(None) == ""
    assert gui._canonical_annuaire_mode("nonsense") == ""


def test_card_titles_are_truncated_by_pixel_width(app) -> None:
    long_name = "un nom de modele vraiment tres long pour deborder de la carte"
    app._annuaire = [
        {"name": long_name, "type": "lora", "mode": "fl2va", "model": "m", "url": "u"}
    ]
    try:
        app._annuaire_grid.configure(width=400)
        app._rebuild_annuaire_grid()
        app.root.update_idletasks()
        card = app._annuaire_grid.winfo_children()[0]
        name_label = card.winfo_children()[1]
        assert str(name_label.cget("text")).endswith("…")
        assert app._tooltip_texts.get(name_label) == long_name
    finally:
        app._annuaire = []


# ---------------------------------------------------------------------------
# Piles actives — concurrent stacks, each independently controllable
# ---------------------------------------------------------------------------


def _stacks_snapshot(*rows, total=None) -> dict:
    return {
        "stacks": [
            {
                "stack": stack,
                "label": stack,
                "pod": pod,
                "runpod": runpod,
                "tunnel": tunnel,
                "ready": "READY",
                "url": "—",
                "cost_per_hour": rate,
                "running": runpod == "RUNNING",
            }
            for stack, pod, runpod, tunnel, rate in rows
        ],
        "cost_per_hour_total": total,
    }


def test_active_stacks_panel_disables_start_on_a_running_pod(app) -> None:
    app._apply_status(
        {
            **_full_snapshot(),
            "active_stacks": _stacks_snapshot(
                ("comfy", "pod_c", "RUNNING", "CONNECTED", None),
            ),
        }
    )
    row = app._stack_rows["comfy"]
    assert str(row["start"].cget("state")) == "disabled"
    assert str(row["stop"].cget("state")) == "normal"
    assert str(row["term"].cget("state")) == "normal"


def test_stack_action_stops_only_the_requested_stack(app, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        gui, "action_stop",
        lambda terminate=False, stack=None: calls.append((stack, terminate)) or "ok",
    )
    monkeypatch.setattr(app, "_run", lambda label, fn: fn())
    app._mode.set("comfy")
    app._stack_action("train", False)
    assert calls == [("train", False)]


# ---------------------------------------------------------------------------
# « Ouvrir … » self-healing: the tunnel is checked (and repaired) first
# ---------------------------------------------------------------------------


def test_open_tunnel_outcome_matrix() -> None:
    ok = gui.open_tunnel_outcome
    # A live tunnel is all that matters: the port is ours by definition.
    assert ok(tunnel_ok=True, pod_state="RUNNING", port_free=False) == "ok"
    assert ok(tunnel_ok=True, pod_state="", port_free=False) == "ok"
    # Dead tunnel + running pod: reconnect, unless a foreign process holds it.
    assert ok(tunnel_ok=False, pod_state="RUNNING", port_free=True) == "repair"
    assert ok(tunnel_ok=False, pod_state="RUNNING", port_free=False) == "port_busy"
    # A pod that is not running cannot be reconnected: never start it here.
    assert ok(tunnel_ok=False, pod_state="STOPPED", port_free=True) == "pod_stopped"
    assert ok(tunnel_ok=False, pod_state="EXITED", port_free=True) == "pod_stopped"
    # Gone pod, and the unknown case (RunPod API unreachable).
    assert ok(tunnel_ok=False, pod_state="TERMINATED", port_free=True) == "pod_gone"
    assert ok(tunnel_ok=False, pod_state="MISSING", port_free=True) == "pod_gone"
    assert ok(tunnel_ok=False, pod_state="", port_free=True) == "pod_unknown"
    assert ok(tunnel_ok=False, pod_state=None, port_free=True) == "pod_unknown"


def test_open_tunnel_messages_are_actionable() -> None:
    msg = gui.open_tunnel_message
    assert "Start" in msg("pod_stopped", state="STOPPED", label="ComfyUI", port=8188)
    assert "STOPPED" in msg("pod_stopped", state="STOPPED", label="ComfyUI", port=8188)
    assert "Start" in msg("pod_gone", state="TERMINATED", label="ComfyUI", port=8188)
    assert "unavailable" in msg("pod_unknown", state="", label="ComfyUI", port=8188)
    busy = msg("port_busy", state="RUNNING", label="ComfyUI", port=8188)
    assert "8188" in busy and 'nothing was killed' in busy
    assert msg("ok", state="RUNNING", label="ComfyUI", port=8188) == ""
    assert msg("repair", state="RUNNING", label="ComfyUI", port=8188) == ""


def test_action_reconnect_tunnel_ignores_the_operator_gate(monkeypatch) -> None:
    """The self-heal must work without LAUNCHER_INFRA_RECOVERY (it mutates nothing)."""
    seen = {}

    class _FakeEngine:
        def recover(self, action, confirm, reason=None):
            seen["action"] = action
            seen["confirm"] = confirm
            return RecoveryResult(
                action=action, ok=True, before="STOPPED", after="CONNECTED",
                error_class=None, message="SSH tunnel re-established",
            )

    def fake_build(config, authorizer=None):
        seen["authorizer"] = authorizer
        return _FakeEngine()

    monkeypatch.setattr(gui, "build_recovery_engine", fake_build)
    config = load_config(
        env={"RUNPOD_API_KEY": "rp_secret", "RUNPOD_POD_ID": "pod_abc"}
    )

    result = gui.action_reconnect_tunnel(config)

    assert seen["action"] == "reconnect_ssh"
    assert seen["confirm"] is True
    assert seen["authorizer"].mode == "confirm"  # not config.recover.mode (disabled)
    assert result.ok is True


def test_action_recover_stays_gated(monkeypatch) -> None:
    """The regular recovery actions keep the operator gate (no authorizer override)."""
    seen = {}

    class _FakeEngine:
        def recover(self, action, confirm, reason=None):
            return RecoveryResult(
                action=action, ok=False, before="", after="",
                error_class="denied", message="infrastructure recovery is disabled",
            )

    def fake_build(config, authorizer=None):
        seen["authorizer"] = authorizer
        return _FakeEngine()

    monkeypatch.setattr(gui, "build_recovery_engine", fake_build)
    config = load_config(
        env={"RUNPOD_API_KEY": "rp_secret", "RUNPOD_POD_ID": "pod_abc"}
    )

    gui.action_recover(config, "start_runpod")

    # No override: build_recovery_engine applies config.recover.mode, which is
    # "disabled" here — so the gated action is denied, as it must be.
    assert config.recover.mode == "disabled"
    assert seen["authorizer"] is None


def test_open_tunnel_refusals_are_not_wrapped_as_technical_detail() -> None:
    """The refusal messages are already French: the error path must not rewrap them."""
    for message in (
        gui.open_tunnel_message("pod_stopped", state="STOPPED", label="ComfyUI", port=8188),
        gui.open_tunnel_message("pod_gone", state="TERMINATED", label="ComfyUI", port=8188),
        gui.open_tunnel_message("port_busy", state="RUNNING", label="ComfyUI", port=8188),
        gui.open_tunnel_message("pod_unknown", state="", label="ComfyUI", port=8188),
    ):
        assert gui.translate_action_error(message) == message


# ---------------------------------------------------------------------------
# First-run wizard: the other half of "click and go"
# ---------------------------------------------------------------------------


def test_first_run_needs_credentials_when_nothing_is_configured(
    monkeypatch, tmp_path
) -> None:
    """No key in the environment and none stored -> the wizard should offer."""
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setenv("MINIMAX_LAUNCHER_CREDENTIALS_DIR", str(tmp_path / "creds"))
    assert gui.first_run_needs_credentials(None) is True


def test_first_run_needs_credentials_is_false_with_an_environment_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "rp_env_key")
    assert gui.first_run_needs_credentials(None) is False


def test_first_run_needs_credentials_is_false_with_a_configured_config(
    monkeypatch, tmp_path
) -> None:
    """A config that already carries a key never triggers the wizard."""
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setenv("MINIMAX_LAUNCHER_CREDENTIALS_DIR", str(tmp_path / "creds"))
    config = load_config(env={"RUNPOD_API_KEY": "rp_configured_key"})
    assert gui.first_run_needs_credentials(config) is False


def test_the_wizard_opens_the_credentials_dialog_once(app, monkeypatch) -> None:
    """A fresh install is offered the dialog — exactly once per session."""
    opened = []
    monkeypatch.setattr(gui, "first_run_needs_credentials", lambda config: True)
    monkeypatch.setattr(app, "_credentials_set", lambda parent=None: opened.append(1))

    app._maybe_first_run_wizard()
    app._maybe_first_run_wizard()

    assert opened == [1], "the dialog must open once, not on every refresh"


def test_the_wizard_stays_quiet_when_a_key_is_configured(app, monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(gui, "first_run_needs_credentials", lambda config: False)
    monkeypatch.setattr(app, "_credentials_set", lambda parent=None: opened.append(1))

    app._maybe_first_run_wizard()

    assert opened == []


def test_the_wizard_survives_a_failing_dialog(app, monkeypatch) -> None:
    """A dialog that cannot open must not break the launch."""
    monkeypatch.setattr(gui, "first_run_needs_credentials", lambda config: True)

    def boom(parent=None):
        raise RuntimeError("no display")

    monkeypatch.setattr(app, "_credentials_set", boom)
    app._maybe_first_run_wizard()  # must not raise

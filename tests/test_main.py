"""Tests for the main entry point."""

import logging
import tomllib
from pathlib import Path

import pytest

from launcher import main as main_module
from launcher.main import main


def test_main_returns_zero_without_contacting_services(monkeypatch, caplog) -> None:
    # A clean environment with no secrets or service configuration.
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("RUNPOD_POD_ID", raising=False)
    monkeypatch.delenv("SSH_KEY_PATH", raising=False)

    with caplog.at_level(logging.INFO):
        exit_code = main()

    assert exit_code == 0
    messages = [record.message for record in caplog.records]
    assert "Loading configuration" in messages
    assert "Configuration validated" in messages


def test_main_does_not_leak_secrets_in_output(monkeypatch, caplog) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "rp_super_secret_value_123456")

    with caplog.at_level(logging.INFO):
        main()

    joined = "\n".join(record.message for record in caplog.records)
    assert "rp_super_secret_value_123456" not in joined


@pytest.mark.parametrize("command", ["status", "doctor"])
def test_main_dispatches_subcommands(monkeypatch, command) -> None:
    called = []
    monkeypatch.setattr(
        main_module, f"_{command}", lambda *a, **k: called.append(command) or 0
    )

    exit_code = main([command])

    assert called == [command]
    assert exit_code == 0


def test_main_dispatches_stop(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        main_module,
        "_stop",
        lambda terminate=False, stack=None: calls.append((terminate, stack)) or 0,
    )

    assert main(["stop"]) == 0
    assert calls == [(False, None)]

    assert main(["stop", "--terminate"]) == 0
    assert calls == [(False, None), (True, None)]

    assert main(["stop", "--stack", "comfy"]) == 0
    assert calls[-1] == (False, "comfy")


def test_main_empty_argv_keeps_status_default(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(
        main_module, "_status", lambda *a, **k: called.append("status") or 0
    )

    exit_code = main([])

    assert called == ["status"]
    assert exit_code == 0


def test_main_status_all_flag_requests_every_stack(monkeypatch) -> None:
    """`status --all` must report concurrent stacks, not just one."""
    seen = []
    monkeypatch.setattr(
        main_module,
        "_status",
        lambda *a, **k: seen.append(k.get("all_stacks")) or 0,
    )

    assert main(["status", "--all"]) == 0
    assert seen == [True]

    assert main(["status"]) == 0
    assert seen[-1] is False


def test_start_gui_settings_never_fails_on_unreadable_settings(monkeypatch) -> None:
    from launcher import gui as gui_module

    def boom(*a, **k):
        raise RuntimeError("gui.json is broken")

    monkeypatch.setattr(gui_module, "load_settings", boom)
    captured = {}
    monkeypatch.setattr(
        main_module,
        "_start",
        lambda **k: captured.update(k) or 0,
    )

    assert main(["start", "--stack", "comfy", "--gui-settings"]) == 0
    assert captured["comfy"] is None


# ---------------------------------------------------------------------------
# Interactive LLM profile resolution (start)
# ---------------------------------------------------------------------------

from launcher.config import ConfigError  # noqa: E402


class _FakeStream:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _FakeSys:
    def __init__(self, stdin_tty: bool, stdout_tty: bool):
        self.stdin = _FakeStream(stdin_tty)
        self.stdout = _FakeStream(stdout_tty)


def test_comfy_ops_delegates_to_shared_binder(monkeypatch) -> None:
    calls = {}

    class _Cfg:
        pass

    def fake_load_config(profile=None, stack=None, comfy=None):
        calls["load"] = (stack,)
        return _Cfg(), None

    def fake_build(config, runpod=None, registry=None):
        calls["config"] = config
        return "OPS"

    monkeypatch.setattr(main_module, "_load_config", fake_load_config)
    monkeypatch.setattr(main_module.comfy_ops, "build_comfy_ops", fake_build)
    config, ops = main_module._comfy_ops()
    assert ops == "OPS"
    assert calls["load"] == ("comfy",)
    assert calls["config"] is config


# ---------------------------------------------------------------------------
# `comfy outputs`: the pod's output tree is nested
# ---------------------------------------------------------------------------


class _FakeComfyOutputs:
    """Lists nested output paths, as the H3 templates produce."""

    def __init__(self):
        self.downloads = []
        self.fail_for = set()

    def list_outputs(self, subfolders=False):
        assert subfolders is True, "a root-only listing returns directories"
        return ["video/MiniMax_H3/a.mp4", "video/MiniMax_H3/b.mp4"]

    def download_output(self, filename, local_dir, **kwargs):
        self.downloads.append((filename, kwargs.get("subfolder")))
        if filename in self.fail_for:
            raise main_module.comfy_ops.ComfyOpsError(f"HTTP 404 for {filename}")
        return Path(local_dir) / filename


def _install_comfy_outputs(monkeypatch, ops):
    monkeypatch.setattr(
        main_module, "_comfy_ops", lambda: (object(), ops)
    )


def test_comfy_outputs_lists_the_nested_paths(monkeypatch, capsys, tmp_path) -> None:
    ops = _FakeComfyOutputs()
    _install_comfy_outputs(monkeypatch, ops)
    assert main(["comfy", "outputs", "--dir", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert "video/MiniMax_H3/a.mp4" in printed


def test_comfy_outputs_all_splits_the_subfolder(monkeypatch, tmp_path) -> None:
    """``/view`` needs the folder and the file as two parameters.

    Passing ``video/MiniMax_H3/a.mp4`` as ``filename`` made ComfyUI answer 404,
    and the resulting HTTPError (an OSError) escaped the ComfyOpsError handler.
    """
    ops = _FakeComfyOutputs()
    _install_comfy_outputs(monkeypatch, ops)
    assert main(["comfy", "outputs", "--all", "--dir", str(tmp_path)]) == 0
    assert ops.downloads == [
        ("a.mp4", "video/MiniMax_H3"),
        ("b.mp4", "video/MiniMax_H3"),
    ]


def test_comfy_outputs_all_reports_partial_failures(monkeypatch, capsys, tmp_path) -> None:
    ops = _FakeComfyOutputs()
    ops.fail_for = {"b.mp4"}
    _install_comfy_outputs(monkeypatch, ops)
    assert main(["comfy", "outputs", "--all", "--dir", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert "1 file(s) downloaded" in printed
    assert "b.mp4" in printed


def test_comfy_outputs_by_name_resolves_a_nested_path(monkeypatch, capsys, tmp_path) -> None:
    ops = _FakeComfyOutputs()
    _install_comfy_outputs(monkeypatch, ops)
    assert main(["comfy", "outputs", "a.mp4", "--dir", str(tmp_path)]) == 0
    assert ops.downloads == [("a.mp4", "video/MiniMax_H3")]


def test_comfy_outputs_by_name_rejects_an_unknown_file(monkeypatch, tmp_path) -> None:
    ops = _FakeComfyOutputs()
    _install_comfy_outputs(monkeypatch, ops)
    assert main(["comfy", "outputs", "nope.mp4", "--dir", str(tmp_path)]) == 1
    assert ops.downloads == []


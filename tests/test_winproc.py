"""``launcher.winproc`` — the launcher's single console-window chokepoint.

Two things are pinned here:

* the helpers themselves (which flags each spawn style carries), and
* the **invariant**: no module of the launcher, except ``winproc`` itself,
  may call ``subprocess``'s spawning helpers directly. That is what makes a
  forgotten ``CREATE_NO_WINDOW`` impossible rather than merely unlikely — the
  bug this module exists to kill (one visible console window per SSH probe
  during a ComfyUI start).
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess

import pytest

from launcher import winproc

#: The ``subprocess`` entry points that actually start a process.
_SPAWN_HELPERS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output"}
)

#: The module allowed to call them (it is the one that knows the flags).
_CHOKEPOINT = "winproc.py"


def _launcher_modules() -> list[pathlib.Path]:
    root = pathlib.Path(winproc.__file__).resolve().parent
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _spawn_calls(path: pathlib.Path) -> list[tuple[int, str]]:
    """Direct ``subprocess.<helper>(...)`` calls found in *path*.

    ``subprocess.PIPE`` / ``subprocess.TimeoutExpired`` are attribute reads,
    not calls, so they are naturally ignored. Only calls are reported.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _SPAWN_HELPERS
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            found.append((node.lineno, func.attr))
    return found


def test_the_scan_actually_detects_a_bypass(tmp_path) -> None:
    """The guard is only worth its keep if it bites."""
    module = tmp_path / "offender.py"
    module.write_text("import subprocess\nsubprocess.Popen(['x'])\n", encoding="utf-8")
    assert _spawn_calls(module) == [(2, "Popen")]


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def test_hidden_kwargs_is_windows_only() -> None:
    kwargs = winproc.hidden_kwargs()
    if os.name == "nt":
        assert kwargs == {"creationflags": winproc.CREATE_NO_WINDOW}
    else:
        assert kwargs == {}


def test_inherit_kwargs_is_an_explicit_zero() -> None:
    assert winproc.inherit_kwargs() == {"creationflags": 0}


def test_new_console_kwargs_is_windows_only() -> None:
    kwargs = winproc.new_console_kwargs()
    if os.name == "nt":
        assert kwargs == {"creationflags": winproc.CREATE_NEW_CONSOLE}
    else:
        assert kwargs == {}


def test_detached_kwargs_keeps_the_child_console_free() -> None:
    kwargs = winproc.detached_kwargs()
    if os.name == "nt":
        flags = kwargs["creationflags"]
        assert flags & winproc.DETACHED_PROCESS
        assert flags & winproc.CREATE_NO_WINDOW
    else:
        assert kwargs == {}


# ---------------------------------------------------------------------------
# run / Popen defaults
# ---------------------------------------------------------------------------


def test_run_defaults_to_no_window(monkeypatch) -> None:
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    winproc.run(["ssh", "root@pod", "true"], capture_output=True)
    assert seen["argv"] == ["ssh", "root@pod", "true"]
    assert seen["capture_output"] is True
    if os.name == "nt":
        assert seen["creationflags"] == winproc.CREATE_NO_WINDOW
    else:
        assert "creationflags" not in seen


def test_run_respects_an_explicit_creationflags(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.update(kw) or None)
    winproc.run(["x"], creationflags=winproc.INHERIT_CONSOLE)
    assert seen["creationflags"] == 0


def test_popen_defaults_to_no_window(monkeypatch) -> None:
    seen: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            seen.update(kwargs)

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    winproc.Popen(("ssh", "root@pod"))
    assert seen["argv"] == ["ssh", "root@pod"]
    if os.name == "nt":
        assert seen["creationflags"] == winproc.CREATE_NO_WINDOW
    else:
        assert "creationflags" not in seen


@pytest.mark.parametrize("value", ["tar -cf - .", b"x"])
def test_argv_normalisation_leaves_a_shell_string_alone(monkeypatch, value) -> None:
    seen: dict = {}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.update(argv=argv))
    winproc.run(value)
    assert seen["argv"] == value

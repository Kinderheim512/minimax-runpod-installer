"""C3 (C13) guard: a Windows branch always has a POSIX counterpart.

The cross-platform work replaced the Windows-only code paths, but nothing
stops the next change from adding a bare ``os.name == "nt"`` branch on a path
a macOS or Linux user walks. This test is that stop: for every function that
tests ``os.name``, the same function must also mention a POSIX-specific
mechanism.

It is deliberately a *coarse* check — it asserts the counterpart exists in the
same unit of code, not that it is reachable — because a static "is this branch
covered on Linux" analysis is not something a test can honestly do. What it
does catch is the real regression: a new Windows-only shortcut with no
alternative at all.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

LAUNCHER = pathlib.Path(__file__).resolve().parents[1] / "launcher"

#: Mechanisms that only exist on macOS/Linux. A function that tests ``os.name``
#: must mention at least one of them (or call another function that does).
POSIX_TOKENS = (
    "posix",
    "darwin",
    "linux",
    "os.kill",
    "os.chmod",
    "osascript",
    "notify-send",
    "paplay",
    "aplay",
    "afplay",
    "secret-tool",
    "security",
    "lsof",
    "ps -o",
    "/proc/",
    "XDG_DATA_HOME",
    "HOME",
    "xdg-open",
    "open -a",
)


def _functions_testing_os_name() -> list[tuple[pathlib.Path, ast.FunctionDef, str]]:
    found: list[tuple[pathlib.Path, ast.FunctionDef, str]] = []
    for path in sorted(LAUNCHER.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.split("\n")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            if "os.name" in body:
                found.append((path, node, body))
    return found


def _tests_os_name(test: ast.expr) -> bool:
    """True when *test* compares ``os.name`` (``==`` or ``!=``)."""
    for node in ast.walk(test):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "name"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ):
            return True
    return False


def _is_a_preference(node: ast.FunctionDef) -> bool:
    """True when the ``os.name`` test is a *preference*, not the only path.

    Two shapes qualify: an explicit ``else``, or code that runs after the
    ``if`` block (the Windows shortcut falls through to the generic path).
    A bare ``if os.name == "nt": ...`` as the last statement is exactly the
    shape this guard exists to catch.
    """
    for statement in node.body:
        if isinstance(statement, ast.If) and _tests_os_name(statement.test):
            if statement.orelse:
                return True
            if statement is not node.body[-1]:
                return True
    return False


def _is_explicitly_platform_specific(name: str) -> bool:
    """``_require_windows`` says what it is: a Windows-only helper.

    Its macOS/Linux counterpart is the backend selection that never calls it,
    so demanding a POSIX token *inside* it would be asking for dead code.
    """
    return "windows" in name.lower()


def test_every_os_name_branch_has_a_posix_counterpart() -> None:
    offenders = []
    for path, node, body in _functions_testing_os_name():
        if _is_explicitly_platform_specific(node.name):
            continue
        if any(token in body for token in POSIX_TOKENS):
            continue
        if _is_a_preference(node):
            continue
        offenders.append(f"{path.name}::{node.name}")
    assert not offenders, (
        "these functions branch on os.name with no macOS/Linux alternative — "
        "the launcher must run on all three platforms:\n  "
        + "\n  ".join(offenders)
    )


def test_the_preference_shape_is_recognised() -> None:
    """The two shapes the guard accepts, and the one it rejects."""

    def _func(source: str) -> ast.FunctionDef:
        return ast.parse(source).body[0]

    with_else = _func(
        "def f():\n"
        "    if os.name == 'nt':\n"
        "        return 1\n"
        "    else:\n"
        "        return 2\n"
    )
    assert _is_a_preference(with_else)

    fallthrough = _func(
        "def f():\n"
        "    if os.name == 'nt':\n"
        "        try:\n"
        "            return 1\n"
        "        except Exception:\n"
        "            pass\n"
        "    return 2\n"
    )
    assert _is_a_preference(fallthrough)

    only_path = _func(
        "def f():\n"
        "    if os.name == 'nt':\n"
        "        return 1\n"
    )
    assert not _is_a_preference(only_path)


def test_the_guard_finds_the_branches_it_is_meant_to_police() -> None:
    """A guard that matches nothing is not a guard."""
    found = _functions_testing_os_name()
    assert len(found) >= 8, [f"{p.name}::{n.name}" for p, n, _ in found]
    names = {n.name for _, n, _ in found}
    for expected in (
        "play_pod_ready",
        "notify_windows",
        "default_backend_name",
        "find_listener_pid",
        "get_process_name",
        "pid_is_alive",
        "terminate_pid",
    ):
        assert expected in names, f"{expected} no longer tests os.name"


@pytest.mark.parametrize(
    "module, expected",
    [
        ("alerts", "notify-send"),
        ("credentials", "secret-tool"),
        ("portctl", "lsof"),
        ("runtime_state", "os.kill"),
    ],
)
def test_each_platform_aware_module_names_its_posix_mechanism(
    module: str, expected: str
) -> None:
    source = (LAUNCHER / f"{module}.py").read_text(encoding="utf-8")
    assert expected in source, f"{module}.py never mentions {expected!r}"

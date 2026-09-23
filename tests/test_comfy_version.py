"""Tests for launcher.comfy_version — verifying (and repairing) the pod's ComfyUI.

Fully offline: the pod is a fake exposing ``run_shell``.

The three findings asserted here are the ones observed live on image 2.0.3:
a work tree made dirty by the installer's own version marker (which blocked
every pinned checkout), a marker that never matched the target, and the
``comfyui_manager`` package missing while the git clone sat at 3.41 — which
left ``--enable-manager`` disabled.
"""

from __future__ import annotations

from launcher import comfy_version


class _FakeOps:
    """Records every command; answers from a canned {substring: output} map."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.commands: list = []

    def run_shell(self, command, timeout=None):
        self.commands.append(command)
        for needle, output in self.answers.items():
            if needle in command:
                return output
        return ""


def _state(**overrides) -> comfy_version.PodComfyState:
    base = dict(
        version="0.35.1",
        marker="release:v0.35.1",
        head="856a922b",
        dirty="",
        manager_package="4.2.2",
        manager_required="comfyui_manager==4.2.2",
    )
    base.update(overrides)
    return comfy_version.PodComfyState(**base)


# --------------------------------------------------------------------------- #
# read_state
# --------------------------------------------------------------------------- #


def test_read_state_parses_every_probe() -> None:
    ops = _FakeOps(
        {
            "__version__": "0.35.1\n",
            ".comfyui_target_installed": "release:v0.35.1",
            "rev-parse": "856a922b",
            "status --porcelain": "?? .comfyui_target_installed",
            "pip show comfyui-manager": "4.2.2",
            "manager_requirements.txt": "comfyui_manager==4.2.2",
        }
    )
    state = comfy_version.read_state(ops)
    assert state.version == "0.35.1"
    assert state.version_tag == "v0.35.1"
    assert state.marker == "release:v0.35.1"
    assert state.head == "856a922b"
    assert state.dirty == "?? .comfyui_target_installed"
    assert state.manager_package == "4.2.2"
    assert state.manager_required_version == "4.2.2"


def test_read_state_degrades_to_empty_on_a_dead_pod() -> None:
    class _Boom:
        def run_shell(self, command, timeout=None):
            raise RuntimeError("no ssh")

    state = comfy_version.read_state(_Boom())
    assert state == comfy_version.PodComfyState()


def test_manager_required_version_handles_padding_and_operators() -> None:
    assert _state(manager_required="comfyui_manager==4.2.2").manager_required_version == "4.2.2"
    assert _state(manager_required="comfyui_manager >= 4.2.1").manager_required_version == ""
    assert _state(manager_required="").manager_required_version == ""


# --------------------------------------------------------------------------- #
# expected_target
# --------------------------------------------------------------------------- #


def test_expected_target_prefers_an_explicit_pin() -> None:
    assert comfy_version.expected_target("v0.36.0", "v0.37.0") == "commit:v0.36.0"


def test_expected_target_falls_back_to_the_latest_release() -> None:
    assert comfy_version.expected_target("", "v0.36.0") == "release:v0.36.0"


def test_expected_target_is_empty_when_nothing_is_known() -> None:
    assert comfy_version.expected_target("", "") == ""


# --------------------------------------------------------------------------- #
# diagnose
# --------------------------------------------------------------------------- #


def test_a_matching_pod_reports_nothing() -> None:
    report = comfy_version.diagnose(
        _state(marker="commit:v0.36.0", version="0.36.0", dirty=""),
        "commit:v0.36.0",
        pinned_version="v0.36.0",
    )
    assert report.ok is True
    assert "No problem" in report.render()


def test_the_marker_dirtying_its_own_tree_is_reported() -> None:
    """The root cause: `git status` sees only the installer's own marker."""
    report = comfy_version.diagnose(
        _state(dirty="?? .comfyui_target_installed"),
        "release:v0.35.1",
    )
    codes = {f.code for f in report.findings}
    assert "dirty_tree" in codes


def test_a_pin_that_was_never_applied_is_reported() -> None:
    report = comfy_version.diagnose(
        _state(version="0.35.1", marker="release:v0.35.1"),
        "commit:v0.36.0",
        pinned_version="v0.36.0",
    )
    codes = {f.code for f in report.findings}
    assert "pin_not_applied" in codes
    assert "marker_stale" in codes


def test_a_missing_manager_package_is_reported() -> None:
    """ComfyUI core disables --enable-manager without the *package*."""
    report = comfy_version.diagnose(
        _state(manager_package="", marker="release:v0.35.1"),
        "release:v0.35.1",
    )
    codes = {f.code for f in report.findings}
    assert "manager_package" in codes
    assert "--enable-manager" in report.render()


def test_an_unreadable_pod_is_reported_once() -> None:
    report = comfy_version.diagnose(comfy_version.PodComfyState(), "release:v0.36.0")
    assert [f.code for f in report.findings] == ["unreadable"]


# --------------------------------------------------------------------------- #
# repair
# --------------------------------------------------------------------------- #


def test_repair_excludes_the_marker_and_pins_the_tag() -> None:
    ops = _FakeOps()
    steps = comfy_version.repair(
        ops, _state(), target="commit:v0.36.0"
    )
    joined = "\n".join(ops.commands)
    assert ".git/info/exclude" in joined
    assert comfy_version.MARKER_NAME in joined
    assert "fetch --tags" in joined
    assert "checkout v0.36.0" in joined
    assert "manager_requirements.txt" in joined
    assert any("v0.36.0" in step for step in steps)


def test_repair_without_a_pin_does_not_checkout_anything() -> None:
    """Auto mode: the pod resolves the latest release itself at boot."""
    ops = _FakeOps()
    steps = comfy_version.repair(ops, _state(), target="release:v0.36.0")
    joined = "\n".join(ops.commands)
    assert "checkout" not in joined
    assert "fetch" not in joined
    # ...but the marker exclusion and the manager still apply.
    assert ".git/info/exclude" in joined
    assert "manager_requirements.txt" in joined
    # ...and the report says so instead of claiming the version was fixed.
    assert any("NOT applied" in step for step in steps)


def test_repair_without_a_target_says_the_version_was_not_applied() -> None:
    ops = _FakeOps()
    steps = comfy_version.repair(ops, _state(), target="")
    assert any("NOT applied" in step for step in steps)


def test_repair_quotes_a_malicious_tag() -> None:
    """``tag`` comes from COMFYUI_COMMIT and _TARGET_RE accepts anything."""
    ops = _FakeOps()
    comfy_version.repair(ops, _state(), target="commit:v0.36.0; rm -rf ~")
    joined = "\n".join(ops.commands)
    assert "checkout 'v0.36.0; rm -rf ~'" in joined
    # The unquoted payload must never appear as its own shell word.
    assert "checkout v0.36.0; rm -rf ~" not in joined


def test_repair_never_restarts_anything() -> None:
    """ComfyUI is PID 1: restarting is the caller's explicit decision."""
    ops = _FakeOps()
    comfy_version.repair(ops, _state(), target="commit:v0.36.0")
    joined = "\n".join(ops.commands).lower()
    for forbidden in ("kill", "pkill", "reboot", "shutdown", "systemctl", "docker"):
        assert forbidden not in joined

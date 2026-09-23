"""What ComfyUI version the pod actually runs — and why it may be the wrong one.

Two defects were observed live on a pod built from image ``2.0.3``
(2026-09-18), both silent:

* the launcher sent ``COMFYUI_COMMIT=v0.36.0`` and the pod still ran
  ``v0.35.1``. ``git status --porcelain`` returned exactly one line —
  ``?? .comfyui_target_installed``, the marker file the installer writes
  *inside* the repository. The installer's "local changes" guard therefore
  refused the pinned checkout on every boot, and because the marker is only
  written on a clean tree it was never updated: the target never stuck.
* ``importlib.util.find_spec("comfyui_manager")`` was ``None`` and ``pip list``
  empty, so ComfyUI core disabled ``--enable-manager`` and the UI showed
  "upgrade ComfyUI-Manager to version 4.2.1 or higher" — while the git clone
  sat at 3.41.

The installer fixes address both. This module is the launcher's half: it reads
what the pod really has, compares it to what was asked for, and names the
problem instead of leaving the user with a version selector that quietly does
nothing.

Read-only by default (:func:`read_state` / :func:`diagnose`); :func:`repair`
is the only function that changes anything, and it never restarts a pod.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Optional, Sequence

#: Where the image installs ComfyUI (``image-contract.json`` -> install_dir).
DEFAULT_INSTALL_DIR = "/opt/ComfyUI"

#: The marker the installer uses to remember the target it last applied.
MARKER_NAME = ".comfyui_target_installed"

_READ_TIMEOUT = 20.0

#: ``release:v0.35.1`` / ``commit:v0.36.0`` / ``branch:master``.
_TARGET_RE = re.compile(r"^(release|commit|branch):(.+)$")


@dataclass(frozen=True)
class PodComfyState:
    """What the pod actually holds (every field degrades to ``""``)."""

    version: str = ""
    """``__version__`` from ``comfyui_version.py`` — the code ComfyUI runs."""

    marker: str = ""
    """Contents of ``.comfyui_target_installed`` — the last target applied."""

    head: str = ""
    """``git rev-parse --short HEAD`` — the actual checkout."""

    dirty: str = ""
    """``git status --porcelain`` output; non-empty blocks every checkout."""

    manager_package: str = ""
    """Installed ``comfyui-manager`` *package* version ("" when absent).

    ComfyUI core looks for the package (``find_spec("comfyui_manager")``), not
    for the ``custom_nodes/comfyui-manager`` clone — the clone alone leaves
    ``--enable-manager`` disabled."""

    manager_required: str = ""
    """``manager_requirements.txt`` as shipped by this ComfyUI core, verbatim."""

    @property
    def version_tag(self) -> str:
        """The version as a git tag would spell it (``v0.35.1``)."""
        return self.version and f"v{self.version}"

    @property
    def manager_required_version(self) -> str:
        """``comfyui_manager==4.2.2`` -> ``4.2.2`` (``""`` when unparseable)."""
        match = re.search(r"==\s*([0-9][^\s,;]*)", self.manager_required or "")
        return match.group(1) if match else ""


@dataclass
class Finding:
    """One diagnosed problem, with the fix that applies to it."""

    code: str
    message: str
    fix: str = ""


@dataclass
class Report:
    state: PodComfyState
    expected: str = ""
    findings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def render(self) -> str:
        lines = [
            f"ComfyUI installed : {self.state.version or '(unknown)'}"
            f"  (HEAD {self.state.head or '?'})",
            f"Expected target   : {self.expected or '(unresolved)'}",
            f"Marker            : {self.state.marker or '(absent)'}",
            f"Manager (paquet)  : {self.state.manager_package or '(absent)'}"
            + (
                f"  (required: {self.state.manager_required_version})"
                if self.state.manager_required_version
                else ""
            ),
        ]
        if not self.findings:
            lines.append("")
            lines.append("No problem detected.")
            return "\n".join(lines)
        lines.append("")
        for finding in self.findings:
            lines.append(f"[!] {finding.message}")
            if finding.fix:
                lines.append(f"    -> {finding.fix}")
        return "\n".join(lines)


def _one(ops, command: str) -> str:
    """Run one read-only command on the pod; ``""`` on any failure."""
    try:
        return (ops.run_shell(command, timeout=_READ_TIMEOUT) or "").strip()
    except Exception:  # noqa: BLE001 - a probe must never raise
        return ""


def read_state(ops, install_dir: str = DEFAULT_INSTALL_DIR) -> PodComfyState:
    """Read the pod's ComfyUI state (five bounded, read-only SSH probes)."""
    return PodComfyState(
        version=_one(
            ops,
            f"awk -F'\"' '/__version__/{{print $2; exit}}' "
            f"{install_dir}/comfyui_version.py 2>/dev/null",
        ),
        marker=_one(ops, f"cat {install_dir}/{MARKER_NAME} 2>/dev/null"),
        head=_one(ops, f"git -C {install_dir} rev-parse --short HEAD 2>/dev/null"),
        dirty=_one(ops, f"git -C {install_dir} status --porcelain 2>/dev/null"),
        manager_package=_one(
            ops,
            f"{install_dir}/venv/bin/pip show comfyui-manager 2>/dev/null "
            "| awk '/^Version:/{print $2}'",
        ),
        manager_required=_one(
            ops,
            f"tr -d ' \\r' < {install_dir}/manager_requirements.txt 2>/dev/null",
        ),
    )


def expected_target(pinned_version: str, latest_release: str = "") -> str:
    """The ``mode:target`` string the installer should have applied.

    Mirrors ``resolve_comfyui_effective_target()`` (``comfy/lib/comfyui.sh``):
    an explicit pin wins, otherwise the latest stable release is resolved on
    the pod at every boot.
    """
    pinned = (pinned_version or "").strip()
    if pinned:
        return f"commit:{pinned}"
    latest = (latest_release or "").strip()
    return f"release:{latest}" if latest else ""


def diagnose(
    state: PodComfyState,
    expected: str,
    pinned_version: str = "",
) -> Report:
    """Name every way the pod's ComfyUI diverges from what was asked for."""
    report = Report(state=state, expected=expected)

    if not state.version:
        report.findings.append(
            Finding(
                "unreadable",
                "ComfyUI version unreadable on the pod (SSH or missing "
                "install tree).",
                "Check that the comfy stack is running: `minimax-launcher status`.",
            )
        )
        return report

    pinned = (pinned_version or "").strip()
    if pinned and state.version_tag != pinned:
        report.findings.append(
            Finding(
                "pin_not_applied",
                f"The pod runs {state.version_tag} while {pinned} is pinned: "
                "the checkout is refused at every boot.",
                "The version marker dirties its own work tree — "
                "`minimax-launcher comfy repair` fixes both.",
            )
        )

    if state.dirty:
        first = state.dirty.splitlines()[0]
        report.findings.append(
            Finding(
                "dirty_tree",
                f"Dirty ComfyUI work tree ({len(state.dirty.splitlines())} "
                f"entry(ies), e.g. \"{first}\") : every checkout is refused.",
                "The installer's marker file must be excluded "
                "(.git/info/exclude) — `minimax-launcher comfy repair`.",
            )
        )

    if expected and state.marker != expected:
        report.findings.append(
            Finding(
                "marker_stale",
                f"Marker \"{state.marker or '(absent)'}\" differs from the target "
                f"\"{expected}\": the target was never applied.",
                "`minimax-launcher comfy repair`.",
            )
        )

    required = state.manager_required_version
    if required and state.manager_package != required:
        report.findings.append(
            Finding(
                "manager_package",
                "Paquet ComfyUI-Manager "
                f"{state.manager_package or 'absent'} differs from {required}, required by "
                "this core: `--enable-manager` is DISABLED.",
                "`minimax-launcher comfy repair` (installs the package from "
                "manager_requirements.txt).",
            )
        )

    return report


def repair(
    ops,
    state: PodComfyState,
    target: str = "",
    install_dir: str = DEFAULT_INSTALL_DIR,
) -> list:
    """Apply the installer fixes to a running pod; returns the steps run.

    Nothing here restarts anything: ComfyUI is PID 1 in the image
    (``docker-entrypoint.sh`` execs ``launch.sh``), so a version change only
    takes effect after a pod restart. The caller reports that.

    * the marker is added to ``.git/info/exclude`` so it stops dirtying its own
      work tree (this is the root cause of the refused checkouts);
    * the requested tag is fetched and checked out, when one is given;
    * the ``comfyui-manager`` package is installed from the version this
      ComfyUI core pins in ``manager_requirements.txt``.
    """
    steps: list = []

    exclude = f"{install_dir}/.git/info/exclude"
    ops.run_shell(
        f"grep -qxF {MARKER_NAME} {exclude} 2>/dev/null || "
        f"{{ mkdir -p $(dirname {exclude}) && echo {MARKER_NAME} >> {exclude}; }}",
        timeout=_READ_TIMEOUT,
    )
    steps.append(f"marqueur {MARKER_NAME} exclu de git ({exclude})")

    tag = ""
    match = _TARGET_RE.match(target or "")
    if match and match.group(1) == "commit":
        tag = match.group(2)
    if tag:
        # ``tag`` comes from COMFYUI_COMMIT (via config) and _TARGET_RE accepts
        # anything after the colon: ``commit:v0.36.0; rm -rf ~`` would be
        # executed by the pod's shell. Everything else in this module quotes
        # what it interpolates; this path did not.
        quoted_tag = shlex.quote(tag)
        ops.run_shell(
            f"git -C {shlex.quote(install_dir)} fetch --tags --force origin",
            timeout=600.0,
        )
        ops.run_shell(
            f"git -C {shlex.quote(install_dir)} checkout {quoted_tag}", timeout=300.0
        )
        steps.append(f"ComfyUI checkout {tag}")
    elif target:
        # The target is a release/branch name, not a commit: ``checkout`` is
        # skipped by design (the installer resolves releases itself), so saying
        # "fixed" would be a lie. Say what was NOT applied instead.
        steps.append(
            f"version NOT applied: target {target!r} with no pinned commit "
            "(set COMFYUI_COMMIT=commit:<sha> to force a checkout)"
        )
    else:
        steps.append(
            "version NOT applied: no target configured "
            "(COMFYUI_COMMIT is empty — the image keeps its own version)"
        )

    # Always: it is idempotent (pip does not reinstall an already satisfied
    # version) and manager_requirements.txt is the single source of truth for
    # the version THIS core requires.
    ops.run_shell(
        f"{install_dir}/venv/bin/python -m pip install "
        f"-r {install_dir}/manager_requirements.txt",
        timeout=900.0,
    )
    steps.append("comfyui-manager package installed (manager_requirements.txt)")

    return steps

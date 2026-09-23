"""RunPod template checks for the `train` stack.

A RunPod template is created once, by hand, in the console, and then referenced
by id for every pod the launcher creates. Nothing re-checks it. A template that
drifts — a port dropped, SSH turned off, the volume mount path edited, the image
moved to a different tag — produces a pod that boots and then cannot be reached,
or that silently loses every downloaded weight when it stops. The failure
surfaces much later and looks like a Fizgig bug.

This module is the single source of those checks. Two callers use it:

* ``scripts/train/verify_template.py`` — the full, spec-driven audit, run from a
  terminal (and by CI);
* the launcher GUI's "Verify the template" button — the same checks, reported
  into the journal.

Neither creates, edits or deletes anything. Read-only, one ``GET``.

The API payload's field names are read defensively: a field this module cannot
find is reported as UNKNOWN, never assumed correct. An UNKNOWN is a FAILURE —
silence is what lets drift through.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

#: Image repository the template must point at. Upstream's, deliberately: the
#: launcher used to publish a forked image (baked + pinned Fizgig), and retired
#: it because a fork can only ever lag upstream's release cadence. A digest-pinned
#: image is still rejected — RunPod's create call refuses one — but a floating
#: ``:latest`` is accepted on purpose: see ``check_image``.
EXPECTED_IMAGE_REPOSITORY = "ghcr.io/shootthesound/fizgig"

#: Ports the launcher depends on. 22 is the tunnel; the other two are what it
#: forwards, and both are reached ONLY over that tunnel.
REQUIRED_PORTS = ("22/tcp", "6080/http", "8080/http")

#: Minimum volume, in GB. MiniMax H3 weights alone are ~45 GB before any
#: dataset, latent cache or checkpoint.
MIN_VOLUME_GB = 100

#: The mount path the image expects. Anything else and models land on the
#: container disk, which RunPod erases when the pod stops.
EXPECTED_VOLUME_MOUNT = "/workspace"


@dataclass(frozen=True)
class Check:
    """One verification result."""

    name: str
    ok: bool
    detail: str = ""

    def render(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        return f"[{mark}] {self.name}: {self.detail}".rstrip()


def _get(payload: Mapping[str, Any], *names: str) -> Any:
    """Read the first present field among *names*, tolerating case differences.

    RunPod's payload casing has changed between API revisions, so a hard-coded
    key would make this module fail for the wrong reason. Missing is returned as
    None and reported as UNKNOWN.
    """
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for name in names:
        if name in payload:
            return payload[name]
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def parse_ports(raw: Any) -> Optional[set[str]]:
    """Normalise a template's ``ports`` field into a set of 'port/protocol'.

    RunPod accepts a comma-separated string in the console and may return either
    that string or a list of objects. Both shapes are handled; anything else is
    reported as unreadable rather than guessed at.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return {p.strip() for p in raw.split(",") if p.strip()}
    if isinstance(raw, (list, tuple)):
        out: set[str] = set()
        for entry in raw:
            if isinstance(entry, str):
                out.add(entry.strip())
                continue
            if isinstance(entry, Mapping):
                private = _get(entry, "privatePort", "port")
                protocol = _get(entry, "protocol") or "tcp"
                if private is not None:
                    out.add(f"{private}/{protocol}")
                continue
            return None
        return out
    return None


def check_image(template: Mapping[str, Any]) -> Check:
    """The image must be our repository, at an explicit version tag."""
    image = _get(template, "imageName", "image")
    if not isinstance(image, str) or not image:
        return Check("image", False, "UNKNOWN — no image field in the template payload")
    if "@sha256:" in image:
        return Check(
            "image",
            False,
            f"pinned by digest ({image}) — RunPod rejects pod creation from a "
            "digest-pinned template image; use a version tag",
        )
    if not image.startswith(EXPECTED_IMAGE_REPOSITORY + ":"):
        return Check(
            "image", False, f"{image} does not start with {EXPECTED_IMAGE_REPOSITORY}:"
        )
    tag = image[len(EXPECTED_IMAGE_REPOSITORY) + 1:]
    if not tag:
        return Check("image", False, f"{image} has no tag")
    if tag == "latest":
        # Accepted, and deliberately. The runtime tracks upstream's newest image
        # the same way FIZGIG_REF tracks its newest source: the two agree at pod
        # creation and nobody has to bump a tag by hand. Pinning only one axis
        # is what rots — the floating side races ahead while the pin waits.
        #
        # The cost is real and worth stating in the report: a pod created today
        # can boot a different runtime than one created yesterday, and a broken
        # upstream image breaks the next pod creation. Rollback is to set
        # TRAIN_IMAGE_TAG to a concrete tag.
        return Check("image", True, f"{image} (flottante — voir TRAIN_IMAGE_TAG)")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", tag):
        return Check("image", False, f"suspicious tag {tag!r}")
    return Check("image", True, image)


def check_ports(template: Mapping[str, Any]) -> Check:
    """All three ports must be present. A missing 22 makes the stack unreachable."""
    ports = parse_ports(_get(template, "ports"))
    if ports is None:
        return Check("ports", False, "UNKNOWN — no readable ports field")
    missing = [p for p in REQUIRED_PORTS if p not in ports]
    if missing:
        hint = ""
        if "22/tcp" in missing:
            hint = (
                " — without 22/tcp the launcher's tunnel cannot be established "
                "and the stack is unreachable"
            )
        return Check("ports", False, f"missing {missing}{hint}; found {sorted(ports)}")
    extra = sorted(ports - set(REQUIRED_PORTS))
    if extra:
        # Not a failure — RunPod adds its own entries — but worth surfacing.
        return Check("ports", True, f"all required present; extra: {extra}")
    return Check("ports", True, ", ".join(REQUIRED_PORTS))


def _persistent_mount(template: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The template's persistent-volume descriptor, in either payload shape.

    The v2 API returns ``{"mounts": {"persistent": {"path", "size"}}}``; the
    documented flat fields (``volumeMountPath`` / ``volumeInGb``) are accepted
    too, so the same check works against a console export or a future revision.
    """
    mounts = _get(template, "mounts")
    if isinstance(mounts, Mapping):
        persistent = _get(mounts, "persistent")
        if isinstance(persistent, Mapping):
            return persistent
    return None


def check_volume(template: Mapping[str, Any]) -> Check:
    """The volume must be mounted where the image expects and be big enough."""
    persistent = _persistent_mount(template)
    if persistent is not None:
        mount = _get(persistent, "path", "mountPath")
        size = _get(persistent, "size", "sizeInGb", "volumeInGb")
    else:
        mount = _get(template, "volumeMountPath", "volumeMount", "mountPath")
        size = _get(template, "volumeInGb", "volumeSizeInGb", "volumeInGB")
    if mount is None or size is None:
        return Check(
            "volume",
            False,
            "UNKNOWN — no persistent volume "
            f"(mount={mount!r} size={size!r}); models and datasets would land "
            "on the container disk and be erased when the pod stops",
        )
    if str(mount) != EXPECTED_VOLUME_MOUNT:
        return Check(
            "volume",
            False,
            f"mounted at {mount!r}, expected {EXPECTED_VOLUME_MOUNT!r} — models and "
            "datasets would land on the container disk and be erased when the pod "
            "stops",
        )
    try:
        size_gb = int(size)
    except (TypeError, ValueError):
        return Check("volume", False, f"unreadable size {size!r}")
    if size_gb < MIN_VOLUME_GB:
        return Check(
            "volume",
            False,
            f"{size_gb} GB is below the {MIN_VOLUME_GB} GB minimum (MiniMax H3 "
            "weights are ~45 GB before any dataset or checkpoint)",
        )
    return Check("volume", True, f"{mount} = {size_gb} GB")


def check_ssh(template: Mapping[str, Any]) -> Check:
    """SSH must be enabled on the template.

    RunPod only injects ``PUBLIC_KEY`` when the template opts into SSH, and the
    training image deliberately leaves sshd down without it. With SSH off the
    launcher's tunnel can never come up and the stack is unreachable — the pod
    boots, bills, and answers nothing.
    """
    start_ssh = _get(template, "startSsh", "sshEnabled", "startSshTerminal")
    if start_ssh is None:
        return Check("ssh", False, "UNKNOWN — no startSsh field in the template payload")
    if start_ssh is True or str(start_ssh).lower() in ("true", "1", "yes"):
        return Check("ssh", True, "enabled (RunPod injects PUBLIC_KEY)")
    return Check(
        "ssh",
        False,
        f"disabled ({start_ssh!r}) — the image only starts sshd when PUBLIC_KEY "
        "is set, so the launcher's tunnel can never be established",
    )


def check_env(env: Any) -> list[Check]:
    """Check the template's env MAPPING (not the whole template payload)."""
    if not isinstance(env, Mapping):
        return [Check("env", False, "UNKNOWN — no readable env mapping")]

    results: list[Check] = []

    if _get(env, "HF_TOKEN"):
        results.append(
            Check(
                "env.HF_TOKEN",
                True,
                "present (scope is NOT machine-checkable — confirm it is read-only)",
            )
        )
    else:
        results.append(
            Check("env.HF_TOKEN", False, "absent — licence-gated model downloads would fail")
        )

    telemetry = _get(env, "HF_HUB_DISABLE_TELEMETRY")
    if str(telemetry) == "1":
        results.append(Check("env.HF_HUB_DISABLE_TELEMETRY", True, "1"))
    else:
        results.append(
            Check(
                "env.HF_HUB_DISABLE_TELEMETRY",
                False,
                f"{telemetry!r} — upstream's image does not bake this, so the "
                "template is the only place the guarantee can live",
            )
        )

    # FIZGIG_REF is the *update mechanism* now: upstream's entrypoint pulls this
    # ref at every boot, so restarting the pod is the update and no image
    # rebuild is involved. It must be present and explicit — unset, the pod
    # would still work (upstream defaults to master) but the launcher could no
    # longer report which ref it was meant to be on.
    ref = _get(env, "FIZGIG_REF")
    if ref is None:
        results.append(
            Check(
                "env.FIZGIG_REF",
                False,
                "absent — the pod falls back to upstream's default, so the "
                "launcher cannot report the intended ref",
            )
        )
    else:
        results.append(Check("env.FIZGIG_REF", True, str(ref)))

    # FIZGIG_REPO would point the pod at a different Fizgig. Nothing here needs
    # that, and it is how a pod ends up running code nobody chose.
    repo = _get(env, "FIZGIG_REPO")
    if repo is not None:
        results.append(
            Check(
                "env.FIZGIG_REPO",
                False,
                f"present ({repo!r}) — the pod would run Fizgig from somewhere "
                "other than upstream's repository",
            )
        )
    else:
        results.append(Check("env.FIZGIG_REPO", True, "absent"))

    return results

def run_checks(template: Mapping[str, Any]) -> list[Check]:
    """Every check, in report order."""
    checks = [
        check_image(template),
        check_ports(template),
        check_ssh(template),
        check_volume(template),
    ]
    checks.extend(check_env(_get(template, "env")))
    return checks


def verify_template(template_id: str, runpod) -> list[Check]:
    """Fetch *template_id* through *runpod* and run every check.

    *runpod* is a ``RunPodClient`` (or anything exposing ``get_template``), so
    the caller owns the credentials and this module stays free of them.
    """
    return run_checks(runpod.get_template(template_id))


def summarize(checks: Sequence[Check]) -> str:
    """One-line summary for the GUI journal (counts, no values)."""
    failed = [c for c in checks if not c.ok]
    if not failed:
        return f"Template compliant: {len(checks)} checks passed."
    names = ", ".join(c.name for c in failed)
    return f"Template NOT compliant: {len(failed)}/{len(checks)} checks failed ({names})."


def failures(checks: Sequence[Check]) -> list[Check]:
    """Only the failed checks."""
    return [c for c in checks if not c.ok]

"""Infrastructure diagnosis for the MiniMax H3 Launcher tool layer.

Aggregates the health of the whole launcher-managed stack — RunPod, SSH tunnel,
the stack's own service (ComfyUI / the training desktop), the preset or image
identity, and configuration consistency — into a compact, deterministic report
with an overall grade plus a one-line cause and remediation when something is
wrong.

This module is **read-only**: it only probes (via injected, bounded callables)
and never starts/stops anything, mutates configuration, writes files, or runs
arbitrary commands. It reuses the existing probe and lifecycle primitives:

* :func:`launcher.orchestrator.operational_status` for RunPod/SSH/service labels
  (single source of truth);
* :func:`launcher.health.check_comfy` / ``check_train`` for service readiness;
* :func:`launcher.config` for configuration consistency.

Every probe is wrapped so a timeout/error degrades that component to
``UNKNOWN`` rather than raising — the report is always produced.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from . import orchestrator
from .config import Config

# Severity / overall grades.
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
FAILED = "FAILED"
STOPPED = "STOPPED"
UNKNOWN = "UNKNOWN"

# Fixed render order (deterministic output).
_ORDER = ("runpod", "ssh", "vllm", "model")


@dataclass(frozen=True)
class ComponentState:
    """Human label + severity for a single infrastructure component."""

    status: str
    severity: str


_LABELS = {
    "comfy": {
        "runpod": "RunPod",
        "ssh": "SSH",
        "vllm": "ComfyUI",
        "model": "Preset",
    },
    "train": {
        "runpod": "RunPod",
        "ssh": "SSH",
        "vllm": "Desktop",
        "model": "Image",
    },
}


@dataclass(frozen=True)
class InfraReport:
    """Aggregated, rendered diagnosis of the infrastructure stack."""

    components: Mapping[str, ComponentState]
    url: str
    overall: str
    cause: Optional[str] = None
    remediation: Optional[str] = None
    stack: str = "comfy"

    def render(self) -> str:
        labels = _LABELS.get(self.stack, _LABELS["comfy"])
        lines = []
        for key in _ORDER:
            comp = self.components[key]
            lines.append(f"{labels[key]}:".ljust(10) + comp.status)
        lines.append("URL:".ljust(10) + self.url)
        # Config is rendered separately after URL.
        cfg = self.components["config"]
        lines.append("Config:".ljust(10) + cfg.status)
        lines.append("Overall:".ljust(10) + self.overall)

        if self.overall != HEALTHY and (self.cause or self.remediation):
            lines.append("")
            if self.cause:
                lines.append(f"Cause: {self.cause}")
            if self.remediation:
                lines.append(f"Remediation: {self.remediation}")
        return "\n".join(lines)


def _severity_from_status(status: str) -> str:
    if status == "RUNNING":
        return HEALTHY
    if status in ("STARTING", "PROVISIONING"):
        return DEGRADED
    if status in ("EXITED", "TERMINATED", "STOPPED"):
        return STOPPED
    if status == "ERROR":
        return FAILED
    return UNKNOWN


def _map_runpod(status: str) -> ComponentState:
    return ComponentState(status=status, severity=_severity_from_status(status))


def _map_tunnel(status: str) -> ComponentState:
    if status == "CONNECTED":
        return ComponentState(status=status, severity=HEALTHY)
    if status == "STOPPED":
        return ComponentState(status=status, severity=STOPPED)
    return ComponentState(status=status, severity=UNKNOWN)


def _config_state(config: Optional[Config], registry=None) -> ComponentState:
    if config is None:
        return ComponentState(status="UNKNOWN", severity=UNKNOWN)
    missing_blocking = []
    if not config.secrets.runpod_api_key:
        missing_blocking.append("RUNPOD_API_KEY")
    pod_id = config.runpod.pod_id
    if not pod_id and registry is not None:
        try:
            record = registry.load(config.stack)
        except Exception:
            record = None
        if record is not None:
            pod_id = record.pod_id
    if not pod_id:
        missing_blocking.append("RUNPOD_POD_ID")
    if missing_blocking:
        return ComponentState(
            status=f"MISSING: {', '.join(missing_blocking)}", severity=FAILED
        )
    if not config.ssh.key_path:
        return ComponentState(status="SSH_KEY_PATH not set", severity=DEGRADED)
    return ComponentState(status="VALID", severity=HEALTHY)


#: The components that actually *serve* each stack. A report is HEALTHY only
#: when these are healthy too: a ComfyUI process that is dead (UNKNOWN, because
#: nothing answered) next to a healthy tunnel/pod/config used to be graded
#: "Overall: HEALTHY", and the remediation was suppressed with it.
_SERVING_COMPONENTS: dict[str, tuple[str, ...]] = {
    "comfy": ("vllm",),
    "train": ("vllm",),
}


def _compute_overall(
    severities: list[str],
    serving_keys: tuple[str, ...] = (),
    components: Optional[Mapping[str, Any]] = None,
) -> str:
    has_failed = FAILED in severities
    has_healthy = HEALTHY in severities
    has_degraded = DEGRADED in severities
    non_unknown = [s for s in severities if s != UNKNOWN]

    if has_failed and has_healthy:
        return DEGRADED
    if has_failed:
        return FAILED
    if has_degraded:
        return DEGRADED
    if components is not None and serving_keys:
        for key in serving_keys:
            component = components.get(key)
            if component is not None and component.severity != HEALTHY:
                # The stack's own service is not healthy (or could not be
                # probed): never claim the whole stack is.
                return UNKNOWN
    if non_unknown and all(s == HEALTHY for s in non_unknown):
        return HEALTHY
    if non_unknown and all(s == STOPPED for s in non_unknown):
        return STOPPED
    return UNKNOWN


# Cause/remediation in fixed priority order (first failing/degraded/stopped
# component wins). Only emitted when the overall grade is not HEALTHY.
# The serving-component entries are stack-scoped: the same component keys are
# probed for both stacks, but the wording differs.
_REMEDIATION = {
    "comfy": [
        ("vllm", FAILED, "ComfyUI is not answering /system_stats through the SSH tunnel.",
         "Re-run `minimax-launcher start --stack comfy`; after a (re)start the pod "
         "may still be downloading the preset's model weights."),
        ("runpod", STOPPED, "RunPod pod is stopped.",
         "Start the pod via `minimax-launcher start --stack comfy`."),
    ],
    "train": [
        ("vllm", FAILED, "The training desktop is not answering through the SSH tunnel.",
         "Re-run `minimax-launcher start --stack train`; after a (re)start the pod "
         "may still be pulling its image or downloading model weights. If it "
         "never comes up, check the template enables SSH access."),
        ("runpod", STOPPED, "RunPod pod is stopped.",
         "Start the pod via `minimax-launcher start --stack train`."),
    ],
    "common": [
        ("runpod", FAILED, "Pod entered ERROR state.",
         "Restart the pod via `minimax-launcher start`."),
        ("runpod", STOPPED, "RunPod pod is stopped.",
         "Start the pod via `minimax-launcher start`."),
        ("ssh", STOPPED, "SSH tunnel is not established.",
         "Run `minimax-launcher start`."),
        ("config", FAILED, "Required configuration is missing.",
         "Set the missing variable and retry."),
        ("config", DEGRADED, "SSH key path is not set.",
         "Set SSH_KEY_PATH."),
    ],
}


def _cause_and_remediation(
    components: Mapping[str, ComponentState], stack: str = "comfy"
):
    ordered = (
        _REMEDIATION.get(stack, _REMEDIATION["comfy"])
        + _REMEDIATION["common"]
    )
    for key, severity, cause, remediation in ordered:
        comp = components.get(key)
        if comp is not None and comp.severity == severity:
            return cause, remediation
    return None, None


def _probe_service(check_fn, base_url: str) -> ComponentState:
    """Probe the stack's own service readiness.

    The exception cause decides the classification (``HealthError`` carries
    the original network exception in ``__cause__``):

    * the server **answered with an HTTP error** (e.g. 503): the process is
      up but unhealthy — that is ``FAILED`` (the most common real failure,
      and the one that needs a remediation, not a shrug);
    * **no response at all** (connection refused/reset/timeout): the server
      may simply still be starting — ``UNKNOWN`` (no false alarm during a
      legitimate startup window).
    """
    if check_fn is None:
        return ComponentState(status="UNKNOWN", severity=UNKNOWN)
    try:
        healthy = check_fn(base_url, 5.0)
    except Exception as exc:
        import urllib.error

        from .health import HealthError

        if isinstance(exc, HealthError) and isinstance(
            exc.__cause__, urllib.error.HTTPError
        ):
            return ComponentState(status="UNHEALTHY", severity=FAILED)
        return ComponentState(status="UNAVAILABLE", severity=UNKNOWN)
    if healthy:
        return ComponentState(status="READY", severity=HEALTHY)
    return ComponentState(status="NOT READY", severity=FAILED)


def diagnose(
    config: Optional[Config],
    runpod=None,
    tunnels=None,
    state=None,
    check_comfy_fn: Optional[Callable[..., bool]] = None,
    check_train_fn: Optional[Callable[..., bool]] = None,
    registry=None,
) -> InfraReport:
    """Probe and aggregate the full infrastructure stack.

    All probes are optional and injectable. A missing probe degrades the
    corresponding component to ``UNKNOWN``. A probe that raises (e.g. timeout)
    also degrades to ``UNKNOWN`` — it is never mistaken for a hard ``FAILED``.
    RunPod/SSH labels come from :func:`orchestrator.operational_status`;
    ComfyUI (comfy stack) and the Fizgig desktop (train stack) are probed here
    with exception-aware wrapping. ``registry`` supplies the pod id when
    ``RUNPOD_POD_ID`` is not set (environment first).
    """
    stack = config.stack if config is not None else "comfy"
    base = orchestrator.operational_status(
        config=config,
        runpod=runpod,
        tunnels=tunnels,
        state=state,
        check_comfy_fn=None,
        check_train_fn=None,
        registry=registry,
    )

    if stack == "train":
        base_url = (
            config.train_base_url()
            if config is not None
            else "http://127.0.0.1:6080"
        )
        image = config.train.image_name if config is not None else "unknown"
        # The desktop sits behind HTTP Basic auth: the probe treats ANY HTTP
        # answer as healthy (see launcher.health.check_train). Using the
        # ComfyUI probe here would report a perfectly healthy KasmVNC as
        # FAILED, because it insists on a JSON /system_stats 200.
        vllm_comp = _probe_service(check_train_fn, base_url)
        if vllm_comp.severity == HEALTHY:
            model_comp = ComponentState(status=image, severity=HEALTHY)
        else:
            model_comp = ComponentState(status=image, severity=UNKNOWN)
    else:
        base_url = (
            config.comfy_base_url()
            if config is not None
            else "http://127.0.0.1:8188"
        )
        preset = config.comfy.preset if config is not None else "dasiwa_mmh3v12"
        vllm_comp = _probe_service(check_comfy_fn, base_url)
        if vllm_comp.severity == HEALTHY:
            model_comp = ComponentState(status=preset, severity=HEALTHY)
        else:
            model_comp = ComponentState(status=preset, severity=UNKNOWN)

    components: dict[str, ComponentState] = {
        "runpod": _map_runpod(base.get("runpod", "UNKNOWN")),
        "ssh": _map_tunnel(base.get("tunnel", "UNKNOWN")),
        "vllm": vllm_comp,
        "model": model_comp,
    }

    components["config"] = _config_state(config, registry)

    url = base.get("url") or base_url

    overall = _compute_overall(
        [c.severity for c in components.values()],
        serving_keys=_SERVING_COMPONENTS.get(stack, ()),
        components=components,
    )
    cause, remediation = (None, None) if overall == HEALTHY else _cause_and_remediation(
        components, stack
    )

    return InfraReport(
        components=components,
        url=url,
        overall=overall,
        cause=cause,
        remediation=remediation,
        stack=stack,
    )

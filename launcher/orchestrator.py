"""Startup orchestration for the MiniMax H3 Launcher launcher.

Wires the existing RunPod client, SSH tunnel manager and health checkers
into a single startup sequence per workload stack:

* ``comfy`` —
    resolve pod -> (start if needed / sync preset) -> wait ready
    -> resolve ssh.direct -> establish tunnel (ComfyUI port)
    -> health-check ComfyUI (/system_stats) -> open the UI in the browser
    (local tunnel by default, public direct URL as an explicit option)
* ``train`` —
    resolve pod -> (start if needed / sync training env) -> wait ready
    -> resolve ssh.direct -> establish one tunnel (desktop + file
    manager) -> health-check the KasmVNC desktop -> open it

The two stacks run on independent pods (managed by the per-stack pod
registry) and can be started and stopped separately.

Pod resolution, in priority order:

1. ``RUNPOD_POD_ID`` (environment) — used as-is; a missing pod is a hard,
   actionable error (no fallthrough to provisioning).
2. The pod registry (``<home>\\pod.json``) — the pod this launcher previously
   created. A missing or TERMINATED pod is cleared and a new one is created.
3. Automatic provisioning — only when a template ID is configured. Creation
   is guarded by the single-machine provisioning lock, and the pod identity
   is recorded in the registry immediately after the create call succeeds
   (before any wait), so a crash can never leave an untracked pod.

The launcher only ever stops a pod it created in this invocation (on failure
rollback) or a pod it created on this machine (via ``stop``/``stop
--terminate``); pods referenced by ``RUNPOD_POD_ID`` are never stopped or
terminated by the launcher.

The orchestrator is dependency-injected (fakes in tests) and performs NO
network/GPU/SSH work itself — it coordinates the existing components.

Strict process ownership: the launcher only reuses or terminates PIDs recorded
in the persistent runtime state. It never adopts or kills externally-launched
processes. ``ssh.proxy`` is never used for ``-L``; a missing direct/tcp endpoint
is a hard, actionable error.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence, Union

from . import alerts
from . import logging as launcher_logging
from . import portctl
from . import runtime_state
from .config import (
    Config,
    DEFAULT_COMFY_PRESET,
    DEFAULT_COMFY_TIER,
    DEFAULT_STACK,
    STACKS,
)
from .health import HealthError, wait_comfy, wait_train
from .portctl import is_port_open
from .pod_registry import PodRecord, PodRegistry
from .provisioning_lock import ProvisioningLock
from .runpod import (
    PlacementError,
    PodNotFoundError,
    RunPodError,
    RunPodClient,
    STATUS_ERROR,
    STATUS_EXITED,
    STATUS_TERMINATED,
)
from .tunnel import (
    SshEndpoint,
    TunnelError,
    TunnelManager,
    TunnelTarget,
    can_connect,
    from_runpod_endpoint,
    host_key_spec,
    is_host_key_changed,
    is_port_free,
    purge_known_host_entry,
)

logger = launcher_logging.get_logger("minimax-launcher.orchestrator")

# Tunnel establishment retry budget. A freshly created pod reports RUNNING and
# a public SSH mapping before its SSH endpoint actually accepts connections:
# on a cold start RunPod pulls the template's docker image (observed ~10 min)
# before sshd is reachable, so the first ``ssh -N -L`` attempt can fail with
# "connection refused" for a long while. The budget is generous enough to ride
# out that pull; the loop aborts early on credential/host-key failures that
# waiting cannot fix.
_TUNNEL_ESTABLISH_BUDGET = 1200.0
_TUNNEL_ESTABLISH_INTERVAL = 5.0
_TUNNEL_WAIT_ALIVE_TIMEOUT = 15.0
# Substrings in ssh stderr that indicate a credential/host-key problem rather
# than "the pod is still booting". These abort immediately instead of retrying.
_AUTH_FAILURE_MARKERS = (
    "permission denied",
    "publickey",
    "authentication",
    "host key verification failed",
    "too many authentication failures",
)

# Cadence for the pod-unavailability retry loop. RunPod "no capacity" is a
# time-based, transient condition (GPUs free up as other jobs finish), so the
# launcher keeps asking the scheduler on a fixed cadence instead of failing
# the whole startup; the user is told (discreetly) that retries are running.
_POD_RETRY_INTERVAL = 30.0


def _sleep(seconds: float) -> None:
    """Sleep indirection so tests can collapse the retry cadence."""
    time.sleep(seconds)


# Substrings identifying a pod-unavailability (capacity) failure. Kept in one
# place: the GUI renders this error family discreetly (warning, no popup) and
# the CLI downgrades it to a short warning.
_POD_UNAVAILABLE_MARKERS = ("free gpu", "cannot place the pod", "no capacity")


def is_pod_unavailable_error(text: str) -> bool:
    """Return True when *text* is a pod-unavailability (capacity) failure."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _POD_UNAVAILABLE_MARKERS)


# RunPod indexes a freshly created pod with a short delay: ``get_pod`` can
# answer 404 for a few seconds while the pod is alive and already billing.
# A 404 is therefore only trusted as "the pod is gone" after a short retry
# window — and only for a record young enough for that delay to be plausible
# (an old record that 404s really is gone).
POD_PRESENCE_RETRY_WINDOW_S = 600.0
_POD_PRESENCE_ATTEMPTS = 3
_POD_PRESENCE_DELAY = 2.5


def pod_presence(
    runpod: RunPodClientLike,
    pod_id: str,
    *,
    created_at: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Optional[object], str]:
    """Resolve whether *pod_id* exists: ``(pod, "found" | "gone" | "unknown")``.

    A single 404 is NOT proof of deletion. This is the bug behind "the pod
    cannot be terminated from the launcher": the pod was created, RunPod had
    not indexed it yet, ``get_pod`` answered 404, the registry record was
    cleared — and the record was the launcher's only link to a pod that was
    running and billing. The check now retries for a fresh pod, falls back to
    the list endpoint, and reports ``"unknown"`` (never ``"gone"``) when the
    API is unreachable, so callers keep the record instead of dropping it.
    """
    fresh = (
        created_at is not None
        and (time.time() - float(created_at)) < POD_PRESENCE_RETRY_WINDOW_S
    )
    attempts = _POD_PRESENCE_ATTEMPTS if fresh else 1
    for attempt in range(1, attempts + 1):
        try:
            return runpod.get_pod(pod_id), "found"
        except PodNotFoundError:
            if attempt >= attempts:
                break
            logger.info(
                "Pod %s not found yet (attempt %d/%d): RunPod may still be "
                "indexing it — retrying in %.1fs",
                pod_id, attempt, attempts, _POD_PRESENCE_DELAY,
            )
            sleep(_POD_PRESENCE_DELAY)
        except Exception as exc:  # noqa: BLE001 - unverifiable, not gone
            logger.info("Pod %s could not be verified (%s)", pod_id, exc)
            return None, "unknown"
    # Last resort: the list endpoint sometimes exposes a pod that get_pod
    # still 404s on. Only conclude "gone" when both agree — and a client that
    # cannot list (or a failing list call) leaves the get_pod verdict standing
    # rather than turning a clear 404 into "unknown".
    #
    # An empty list IS now meaningful: ``RunPodClient.list_pods`` raises for a
    # payload it cannot parse instead of silently returning ``[]``, so "no
    # pods" here means the API answered and the pod really is absent.
    try:
        pods = runpod.list_pods()
    except Exception as exc:  # noqa: BLE001 - no list endpoint: trust get_pod
        logger.info("Pod list unavailable (%s); trusting the get_pod verdict", exc)
        return None, "gone"
    for pod in pods:
        if getattr(pod, "id", None) == pod_id:
            return pod, "found"
    return None, "gone"


# A ComfyUI pod whose image is a community build has no SSH daemon inside, so
# its exposed TCP 22 never accepts connections and the tunnel can never come
# up — regardless of how long we wait. An image built from this repo's
# Dockerfile starts sshd from its entrypoint. This hint makes that
# distinction actionable instead of retrying in silence.
_COMFY_NO_SSHD_HINT = (
    "if the ComfyUI image is a community build without an SSH daemon, "
    "its exposed TCP 22 never answers — use an image built from this "
    "repository (it starts sshd), or the RunPod web terminal instead"
)

# The training image deliberately leaves port 22 closed unless PUBLIC_KEY is
# set at boot (upstream's security default), so a RunPod template that does not
# enable SSH access produces a pod whose tunnel can never come up. That is by
# far the likeliest cause of a dead train tunnel, so it is named explicitly
# rather than left to a generic timeout message.
_TRAIN_NO_SSHD_HINT = (
    "the training image only starts sshd when PUBLIC_KEY is set, so the RunPod "
    "template must enable SSH access (RunPod's 'SSH Terminal Access' toggle "
    "sets it) — verify the template with scripts/train/verify_template.py"
)

#: Tunnel-name -> actionable hint appended to the "tunnel never came alive"
#: error. Keyed by tunnel name so each stack explains its own failure mode.
_NO_SSHD_HINTS = {
    "comfy": _COMFY_NO_SSHD_HINT,
    "train": _TRAIN_NO_SSHD_HINT,
}


class OrchestrationError(RuntimeError):
    """Raised when the startup sequence fails at any stage."""


# Protocols for the injected dependencies (kept minimal for testability).
class RunPodClientLike(Protocol):
    def get_pod(self, pod_id: str): ...
    def start_pod(self, pod_id: str): ...
    def stop_pod(self, pod_id: str): ...
    def terminate_pod(self, pod_id: str): ...
    def update_pod_env(self, pod_id: str, env): ...
    def wait_ready(self, pod_id: str, timeout: float, interval: float): ...
    def wait_ssh_endpoint(self, pod_id: str, timeout: float, interval: float): ...
    def wait_public_port(
        self, pod_id: str, private_port: int, timeout: float, interval: float
    ): ...
    def create_pod(
        self,
        name: str,
        *,
        template_id: str,
        gpu_id: str,
        gpu_count: int = 1,
        data_center_ids=(),
        env=None,
    ): ...


class TunnelManagerLike(Protocol):
    def start(self, name: str, target, endpoint, key_path, connect_timeout): ...
    def start_many(self, name: str, targets, endpoint, key_path, connect_timeout): ...
    def is_alive(self, name: str, target=None) -> bool: ...
    def wait_alive(self, name: str, target=None, timeout: float = 15.0, interval: float = 0.5) -> bool: ...
    def targets(self, name: str): ...
    def stop(self, name: str) -> None: ...
    def stop_all(self) -> None: ...


def _resolve_direct_endpoint(pod) -> SshEndpoint:
    endpoint = pod.ssh_tunnel_endpoint()
    if endpoint is None:
        raise OrchestrationError(
            "Pod has no usable SSH endpoint for '-L' port forwarding. "
            "RunPod's ssh.proxy does not support port forwarding. Configure the "
            "pod to expose TCP 22 (SSH over exposed TCP) or a direct SSH endpoint, "
            "and ensure it is RUNNING."
        )
    return from_runpod_endpoint(endpoint)


def _ssh_endpoint_refresher(
    runpod: RunPodClientLike, pod_id: str, fallback: SshEndpoint
) -> Callable[[], Optional[SshEndpoint]]:
    """Return a callable that re-reads the pod's current SSH endpoint.

    Used by the tunnel loop: RunPod can reassign the public 22/tcp port when a
    pod is restarted, so a stale endpoint must be re-resolved rather than
    retried forever. Any failure returns *fallback* (the caller keeps its
    previous endpoint and the loop simply retries).
    """

    def refresh() -> Optional[SshEndpoint]:
        try:
            pod = runpod.get_pod(pod_id)
        except RunPodError:
            return fallback
        endpoint = pod.ssh_tunnel_endpoint()
        if endpoint is None:
            return fallback
        return from_runpod_endpoint(endpoint)

    return refresh


def _recorded_pid_alive(state: runtime_state.RuntimeState, key: str) -> Optional[int]:
    """Return the recorded PID for *key* if it is alive, else None."""
    entry = state.load().get(key)
    if entry is None:
        return None
    return entry.pid if runtime_state.pid_is_alive(entry.pid) else None


def _assert_owned_port(
    port: int,
    key: str,
    state: runtime_state.RuntimeState,
    what: str,
    is_occupied: Callable[[int], bool],
) -> None:
    """Enforce strict ownership: an occupied local port must be launcher-owned.

    If *port* is occupied and the recorded PID for *key* is not alive, the
    occupying process is external and must NOT be adopted or terminated — raise
    an actionable conflict.
    """
    if is_occupied(port):
        if _recorded_pid_alive(state, key) is None:
            raise OrchestrationError(
                f"{what} port {port} is occupied by a process not started by "
                "the launcher. Stop that process or change the configured port. "
                "The launcher will not adopt or terminate it."
            )


def _is_auth_failure(stderr: str) -> bool:
    """Return True if ssh stderr indicates a credential/host-key failure.

    "Connection refused" / "Connection timed out" mean the pod's sshd is still
    coming up (docker image pulling) and retrying will eventually succeed;
    "Permission denied" / "publickey" / "Host key verification failed" mean a
    misconfiguration that waiting cannot fix, so the loop should abort fast.
    """
    lowered = stderr.lower()
    return any(marker in lowered for marker in _AUTH_FAILURE_MARKERS)


def _establish_tunnel(
    name: str,
    target: Union[TunnelTarget, Sequence[TunnelTarget]],
    endpoint: SshEndpoint,
    tunnels: TunnelManagerLike,
    key_path: Optional[str],
    connect_timeout: int,
    refresh_endpoint: Optional[Callable[[], Optional[SshEndpoint]]] = None,
    budget: Optional[float] = None,
) -> None:
    """Start tunnel *name* and retry until it forwards or the budget expires.

    *target* is a single :class:`TunnelTarget` or a sequence of them. Several
    targets are forwarded by ONE ``ssh`` process (see
    :func:`launcher.tunnel.build_tunnel_args_multi`), which is what the ``train``
    stack needs: its desktop and its file manager are two ports on one pod, and
    two ssh processes would mean two authentications and two things to reap.

    A freshly created pod reports RUNNING and a public SSH (22/tcp) mapping
    before its in-container sshd actually accepts connections: RunPod exposes
    the TCP port at the hypervisor level immediately, but the container is
    still pulling its docker image (~10 min), so ``ssh`` connects at the TCP
    layer yet receives no SSH banner and silently hangs until it is killed.
    The loop therefore re-spawns on a generous budget, probing TCP reachability
    first to distinguish "endpoint unreachable" from "container still booting",
    and throttles its log output so a long pull does not flood the journal.

    ``refresh_endpoint`` (optional) re-resolves the pod's SSH endpoint from the
    API. It is consulted whenever the TCP probe fails, because RunPod **can
    reassign the public 22/tcp port when a pod is restarted** — an endpoint
    captured before a stop/start then points at a dead port forever (observed
    live: 22165 -> 22081 after an env-sync restart).
    """
    targets = (target,) if isinstance(target, TunnelTarget) else tuple(target)
    if not targets:
        raise OrchestrationError(f"Tunnel {name!r} needs at least one target")
    # With one target the liveness probe is explicit (and matches the
    # long-standing single-port behaviour); with several, omitting it makes
    # wait_alive/is_alive check EVERY forwarded port, which is the only useful
    # definition for a multi-port tunnel.
    wait_target = targets[0] if len(targets) == 1 else None
    effective_budget = _TUNNEL_ESTABLISH_BUDGET if budget is None else budget
    deadline = time.monotonic() + effective_budget
    attempt = 0
    failures: list[str] = []
    last_log_at = 0.0
    logged_first = False
    #: Host keys already dropped in this call. RunPod recycles public
    #: ``host:port`` pairs across pods, so the first attempt against a fresh pod
    #: can fail on a stale key; purging it lets the next attempt re-add the new
    #: one. The set keeps that from turning into an endless purge loop.
    purged_host_keys: set[str] = set()
    while True:
        attempt += 1
        tcp_ok = can_connect(endpoint.port, endpoint.host, timeout=3.0)
        if not logged_first:
            logger.info(
                "SSH tunnel %s -> %s:%d (TCP %s) forwarding %s",
                name, endpoint.host, endpoint.port,
                "reachable" if tcp_ok else "unreachable",
                ", ".join(t.forwarding_spec for t in targets),
            )
            logged_first = True
        if not tcp_ok and refresh_endpoint is not None:
            try:
                refreshed = refresh_endpoint()
            except Exception as exc:  # defensive: never abort the tunnel loop
                refreshed = None
                logger.warning("Could not refresh the SSH endpoint: %s", exc)
            if refreshed is not None and (refreshed.host, refreshed.port) != (
                endpoint.host, endpoint.port
            ):
                logger.info(
                    "SSH endpoint changed after a pod restart: %s:%d -> %s:%d",
                    endpoint.host, endpoint.port, refreshed.host, refreshed.port,
                )
                endpoint = refreshed
        try:
            if len(targets) == 1 or not hasattr(tunnels, "start_many"):
                tunnels.start(name, targets[0], endpoint, key_path, connect_timeout)
            else:
                tunnels.start_many(
                    name, targets, endpoint, key_path, connect_timeout
                )
        except TunnelError as exc:
            # e.g. local port still held from a previous attempt's ssh not yet
            # released — transient, retry rather than abort.
            failures.append(f"attempt {attempt}: {exc}")
            if time.monotonic() >= deadline:
                break
            logger.warning(
                "SSH tunnel %s attempt %d failed to start: %s; retrying",
                name, attempt, exc,
            )
            time.sleep(_TUNNEL_ESTABLISH_INTERVAL)
            continue
        if hasattr(tunnels, "wait_alive"):
            try:
                alive = tunnels.wait_alive(
                    name, wait_target, timeout=_TUNNEL_WAIT_ALIVE_TIMEOUT
                )
            except TypeError:
                # A tunnel manager whose wait_alive still takes a mandatory
                # target (older fakes in tests).
                alive = tunnels.wait_alive(name, targets[0])
        else:
            alive = tunnels.is_alive(name, wait_target)
        if alive:
            logger.ok("SSH tunnel %s established (attempt %d)", name, attempt)
            return
        stderr = tunnels.stderr_text(name) if hasattr(tunnels, "stderr_text") else ""
        failures.append(
            f"attempt {attempt}: not alive" + (f" — {stderr}" if stderr else "")
        )
        if hasattr(tunnels, "stop"):
            tunnels.stop(name)
        if stderr and is_host_key_changed(stderr):
            # A recycled RunPod endpoint: the pod behind this host:port is not
            # the one that key was recorded for. Waiting cannot help, but
            # dropping our own stale entry can — ssh re-adds the new key on the
            # next attempt (StrictHostKeyChecking=accept-new).
            spec = host_key_spec(endpoint)
            if spec not in purged_host_keys:
                purged_host_keys.add(spec)
                if purge_known_host_entry(spec):
                    logger.warning(
                        "Stale SSH host key for %s cleared (RunPod reuses "
                        "public endpoints across pods); retrying", spec,
                    )
                    failures.append(
                        f"attempt {attempt}: cleared the stale host key for {spec}"
                    )
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_TUNNEL_ESTABLISH_INTERVAL)
                    continue
        if stderr and _is_auth_failure(stderr):
            # Credential/host-key failure: waiting longer cannot help.
            raise OrchestrationError(
                "SSH tunnel failed authentication: the SSH key or host key is "
                "rejected by the pod. Verify the configured SSH key matches the "
                f"pod and that the endpoint is correct. ssh said: {stderr}"
            )
        if time.monotonic() >= deadline:
            break
        last_log_at = _log_throttled(name, attempt, tcp_ok, stderr, last_log_at)
        time.sleep(_TUNNEL_ESTABLISH_INTERVAL)

    detail = "; ".join(failures) or "unknown cause"
    hint = _NO_SSHD_HINTS.get(name, "")
    raise OrchestrationError(
        f"SSH tunnel did not become alive after {attempt} attempt(s) over "
        f"{effective_budget:.0f}s. The pod's SSH endpoint never accepted "
        "a connection — it may still be pulling its docker image"
        f"{'; ' + hint if hint else ''}. Verify ssh.direct reachability, the "
        f"SSH key, and that port 22 is exposed on the pod. Failures: {detail}"
    )


# Log a retry at most once per this many seconds (long docker pulls must not
# flood the journal with one WARNING per ~20s attempt).
_TUNNEL_RETRY_LOG_INTERVAL = 30.0


def _log_throttled(
    name: str,
    attempt: int,
    tcp_ok: bool,
    stderr: str,
    last_log_at: float,
) -> float:
    """Log a retry at most every ``_TUNNEL_RETRY_LOG_INTERVAL`` seconds.

    A cold pod pulls its docker image for ~10 minutes, so one WARNING per
    ~20s attempt floods the journal. Returns the (possibly updated) timestamp
    of the last emitted log line.
    """
    now = time.monotonic()
    if now - last_log_at >= _TUNNEL_RETRY_LOG_INTERVAL:
        if not tcp_ok:
            cause = "TCP unreachable"
        elif stderr:
            # ssh gave up: say what it said instead of guessing at a boot delay.
            cause = "ssh refused the connection"
        else:
            # ssh is still running and silent: it is handshaking, not failing —
            # the pod's container is most likely still booting.
            cause = "sshd not answering yet (container still booting)"
        hint = ""
        if not tcp_ok and name == "comfy":
            hint = f" — {_COMFY_NO_SSHD_HINT}"
        logger.warning(
            "SSH tunnel %s still not alive after %d attempt(s) — %s%s%s; "
            "retrying (the pod may still be pulling its docker image)",
            name, attempt, cause, hint,
            f" ({stderr})" if stderr else " (no ssh stderr yet)",
        )
        return now
    return last_log_at


def _resolve_pod(config, runpod, registry, lock, created, stats=None):
    """Resolve the pod to start, in priority order.

    Returns ``(pod, pod_id, created_by_us)``. An environment pod that does not
    exist is a hard error (no fallthrough); a missing/TERMINATED registered
    pod is cleared and replaced by a freshly created one.
    """
    if config.runpod.pod_id:
        logger.info("Checking RunPod pod %s", config.runpod.pod_id)
        try:
            pod = runpod.get_pod(config.runpod.pod_id)
        except PodNotFoundError as exc:
            raise OrchestrationError(
                f"Pod {config.runpod.pod_id} not found on RunPod. Verify "
                "RUNPOD_POD_ID, or remove it to let the launcher provision a "
                "pod instead."
            ) from exc
        return pod, config.runpod.pod_id, False

    record = registry.load(config.stack)
    if record is not None:
        pod, presence = pod_presence(runpod, record.pod_id, created_at=record.created_at)
        if presence == "gone":
            logger.info(
                "Registered %s pod %s no longer exists; clearing the record",
                config.stack, record.pod_id,
            )
            registry.clear(config.stack)
        elif presence == "unknown":
            raise OrchestrationError(
                f"Registered {config.stack} pod {record.pod_id} is unreachable "
                "(the RunPod API could not be queried); refusing to provision "
                "a new pod. Retry when the RunPod API is reachable, or delete "
                "the record to force a fresh provision, or set RUNPOD_POD_ID "
                "to adopt a different pod."
            )
        elif pod.status == "TERMINATED":
            logger.info(
                "Registered %s pod %s is TERMINATED; provisioning a new pod",
                config.stack, record.pod_id,
            )
            registry.clear(config.stack)
        else:
            return pod, record.pod_id, False

    template_id, template_var = _stack_template(config)
    if not template_id:
        raise OrchestrationError(
            f"RUNPOD_POD_ID is not set and no registered {config.stack} pod "
            f"exists. Set RUNPOD_POD_ID to use an existing pod, or configure "
            f"RUNPOD_API_KEY, {template_var}, and RUNPOD_GPU_ID to let the "
            "launcher provision a pod."
        )
    pod, created_by_us = _create_pod_guarded(
        config, runpod, registry, lock, created, stats
    )
    return pod, pod.id, created_by_us


def _stack_template(config: Config) -> tuple[Optional[str], str]:
    """(template id, env var name) for *config*'s stack."""
    if config.stack == "comfy":
        return config.secrets.comfy_template_id, "RUNPOD_COMFY_TEMPLATE_ID"
    if config.stack == "train":
        return config.secrets.train_template_id, "RUNPOD_TRAIN_TEMPLATE_ID"
    return config.secrets.runpod_template_id, "RUNPOD_TEMPLATE_ID"


def _resolved_stack_pod_env(config: Config) -> dict[str, str]:
    """Container env for the active stack, before keys and user overrides.

    ``comfy``: the preset/tier/workflow/flag values; ``train``: the
    training image flags. Both are then merged with the managed extra API
    keys and ``RUNPOD_POD_ENV`` (explicit user overrides always win). The
    launcher adds/overrides managed values but never deletes pod-side keys.
    """
    if config.stack == "comfy":
        env = dict(config.resolved_comfy_pod_env())
    else:
        env = dict(config.resolved_train_pod_env())
        # The desktop credential is a SECRET, so it is injected here rather
        # than through TrainConfig (a non-secret dataclass). Left unset, the
        # image generates one and prints it in the pod log — workable, but
        # the user would have to read the log to log in, and a pod restart
        # would change it.
        if config.secrets.vnc_password:
            env["VNC_PASSWORD"] = config.secrets.vnc_password
    env.update(config.secrets.extra)
    env.update(config.runpod.pod_env)
    return env


def _extra_secret_diffs(pod, config: Config) -> list[str]:
    """Names of launcher-managed extra secrets the pod env does not match.

    A managed key counts as stale when it is absent from the pod env or holds
    a different value. Only key NAMES are ever used here or logged — never
    values.
    """
    env = getattr(pod, "env", None) or {}
    return [
        key
        for key in config.secrets.extra
        if env.get(key) != config.secrets.extra[key]
    ]


def _create_pod_guarded(config, runpod, registry, lock, created, stats=None):
    """Create a pod under the provisioning lock and record it immediately.

    Returns ``(pod, created_by_us)``. The registry is re-checked while holding
    the lock so a competing launcher that finished first wins and is reused.
    The record is saved before any wait so a crash can never leave an
    untracked pod behind.

    A placement failure (no capacity) is retried on a fixed cadence
    (:data:`_POD_RETRY_INTERVAL`) until it succeeds: the scheduler's capacity
    is time-based, and failing the whole startup on a transient condition
    would leave the user starting over from scratch. Every failed attempt is
    counted in *stats* and announced as a discreet warning.
    """
    name = config.runpod.pod_name or (
        "minimax-launcher-" + config.stack + "-" + time.strftime("%Y%m%d-%H%M%S")
    )
    lock.acquire(intent="provision", pod_name=name)
    try:
        record = registry.load(config.stack)
        if record is not None:
            pod, presence = pod_presence(
                runpod, record.pod_id, created_at=record.created_at
            )
            if presence == "found" and pod.status != "TERMINATED":
                logger.ok(
                    "Another launcher instance already provisioned pod %s; reusing it",
                    record.pod_id,
                )
                return pod, False
            if presence == "gone" or (
                presence == "found" and pod.status == "TERMINATED"
            ):
                registry.clear(config.stack)
            elif presence == "unknown":
                raise OrchestrationError(
                    f"Registered {config.stack} pod {record.pod_id} could not "
                    "be verified (the RunPod API is unreachable); refusing to "
                    "provision a second pod."
                )

        template_id, _template_var = _stack_template(config)
        logger.info(
            "Creating RunPod %s pod %s (gpu=%s x%d)",
            config.stack, name, config.runpod.gpu_id, config.runpod.gpu_count,
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                pod = runpod.create_pod(
                    name,
                    template_id=template_id,
                    gpu_id=config.runpod.gpu_id,
                    gpu_count=config.runpod.gpu_count,
                    data_center_ids=config.runpod.data_centers,
                    env=_resolved_stack_pod_env(config),
                )
            except PlacementError:
                if stats is not None:
                    stats["placement_failures"] = (
                        stats.get("placement_failures", 0) + 1
                    )
                logger.warning(
                    "Pod not available (no free GPU at RunPod) — retrying in "
                    "%.0fs (attempt %d)",
                    _POD_RETRY_INTERVAL, attempt,
                )
                _sleep(_POD_RETRY_INTERVAL)
                continue
            break
        # Track the pod before recording it: if the record write fails, the
        # rollback still knows which pod to stop.
        created.append(f"pod:{pod.id}")
        registry.save(
            PodRecord(
                pod_id=pod.id,
                name=name,
                created_at=time.time(),
                gpu_id=config.runpod.gpu_id,
                gpu_count=config.runpod.gpu_count,
                data_center_id=pod.data_center_id,
                stack=config.stack,
            ),
            config.stack,
        )
        return pod, True
    finally:
        lock.release()


def _reconfigure_pod(
    config: Config,
    runpod: RunPodClientLike,
    pod,
    pod_id: str,
    why: str,
):
    """stop -> update-env -> start (NOT ``restart``) on an adopted pod.

    ``stop`` keeps the ephemeral container disk (the model cache), so the
    pod restarts against its cached model; ``restart`` would wipe it and
    force a re-download. The env update is a read-then-full-replace (see
    :meth:`RunPodClient.update_pod_env`) with :func:`_resolved_pod_env`, so
    no pod-side key is ever deleted. ``why`` is a short log-only reason;
    secret values are never logged.
    """
    if pod.is_running and "stop" in (pod.actions or ()):
        logger.info("Stopping pod %s (disk kept) to %s", pod_id, why)
        runpod.stop_pod(pod_id)
        pod = runpod.get_pod(pod_id)

    new_env = _resolved_stack_pod_env(config)
    logger.info("Updating pod %s env (read-then-full-replace) to %s", pod_id, why)
    pod = runpod.update_pod_env(pod_id, new_env)

    if pod.is_startable:
        logger.info("Starting pod %s", pod_id)
        pod = runpod.start_pod(pod_id)
    if not pod.is_running:
        logger.info("Waiting for pod to become RUNNING")
        pod = runpod.wait_ready(pod_id, timeout=600.0, interval=10.0)
    logger.ok("Pod is RUNNING")
    return pod


def _comfy_env_diffs(pod, config: Config) -> list[str]:
    """Names of launcher-managed ComfyUI env keys the pod does not match.

    Only the keys the launcher itself manages (preset/tier/workflows/flags,
    see :meth:`Config.resolved_comfy_pod_env`) are compared; image defaults
    for other ``H3_*`` keys are left alone. Only key NAMES are used here or
    logged — never values.
    """
    env = getattr(pod, "env", None) or {}
    managed = config.resolved_comfy_pod_env()
    return [key for key in managed if env.get(key) != managed[key]]


def _sync_comfy_env(
    config: Config,
    runpod: RunPodClientLike,
    pod,
    pod_id: str,
):
    """Re-sync an adopted ComfyUI pod with the selected preset/flags/keys.

    A preset change (e.g. switching ``dasiwa_mmh3v12`` to another preset) or
    an out-of-sync managed key triggers stop -> update-env -> start; the
    stop keeps the disk (already-downloaded model cache), so the restart
    reuses what the pod already has.
    """
    names = ", ".join(_comfy_env_diffs(pod, config))
    logger.info(
        "Pod %s ComfyUI settings out of sync with the launcher (%s); syncing pod env",
        pod_id,
        names,
    )
    return _reconfigure_pod(config, runpod, pod, pod_id, "sync ComfyUI preset/keys")


def _train_env_diffs(pod, config: Config) -> list[str]:
    """Names of launcher-managed training env keys the pod does not match.

    Only the keys the launcher itself manages (fetch_models / image version /
    telemetry, see :meth:`Config.resolved_train_pod_env`, plus the desktop
    credential) are compared; image defaults for anything else are left alone.

    ``VNC_PASSWORD`` is compared by value here like any other managed key, but
    it is the one managed key that is a secret — so this function returns NAMES
    only and the caller must never log the values. The log line built by
    :func:`_sync_train_env` uses names exclusively, which is why the return
    type is a list of names rather than a mapping.
    """
    env = getattr(pod, "env", None) or {}
    managed = config.resolved_train_pod_env()
    if config.secrets.vnc_password:
        managed = {**managed, "VNC_PASSWORD": config.secrets.vnc_password}
    return [key for key in managed if env.get(key) != managed[key]]


def _sync_train_env(
    config: Config,
    runpod: RunPodClientLike,
    pod,
    pod_id: str,
):
    """Re-sync an adopted training pod with the selected fetch/version/credential.

    Changing which model families the pod pre-downloads, moving to a new image
    tag, or rotating the desktop password triggers stop -> update-env -> start.
    The stop keeps the disk, so already-downloaded weights (~45 GB for MiniMax
    H3) are reused rather than re-fetched.
    """
    names = ", ".join(_train_env_diffs(pod, config))
    logger.info(
        "Pod %s training settings out of sync with the launcher (%s); "
        "syncing pod env",
        pod_id,
        names,
    )
    return _reconfigure_pod(config, runpod, pod, pod_id, "sync training settings")


def _ensure_pod_running(runpod: RunPodClientLike, pod, pod_id: str):
    """Start a startable pod and wait until it is RUNNING (returns the pod)."""
    if pod.is_startable:
        logger.info("Pod is %s — starting", pod.status)
        pod = runpod.start_pod(pod_id)
    if not pod.is_running:
        logger.info("Waiting for pod to become RUNNING")
        pod = runpod.wait_ready(pod_id, timeout=600.0, interval=10.0)
    logger.ok("Pod is RUNNING")
    return pod


def _start_comfy(
    config: Config,
    pod,
    endpoint: Optional[SshEndpoint],
    runpod: RunPodClientLike,
    tunnels: TunnelManagerLike,
    open_browser: Callable[[str], None],
    state: runtime_state.RuntimeState,
    created: list[str],
) -> None:
    """ComfyUI stack post-RUNNING phase.

    ``tunnel`` (default): 127.0.0.1:<local> forwards to the pod's ComfyUI
    port over SSH — the UI runs in the local browser, all compute stays on
    the pod. ``direct`` (explicit option only): no SSH at all — wait for the
    pod's public mapping of the ComfyUI port, health-check the public URL,
    and open it in the browser (the ComfyUI endpoint has no authentication,
    so a security warning is always logged).
    """
    if config.comfy.access == "direct":
        url = pod.public_url_for_port(config.comfy.remote_port)
        if url is None:
            logger.info(
                "Waiting for RunPod to publish the public HTTP mapping for "
                "port %d",
                config.comfy.remote_port,
            )
            try:
                pod = runpod.wait_public_port(
                    pod.id, config.comfy.remote_port, timeout=600.0, interval=5.0
                )
            except RunPodError:
                pass
            url = pod.public_url_for_port(config.comfy.remote_port)
        if url is None:
            raise OrchestrationError(
                f"Pod {pod.id} has no public URL for port "
                f"{config.comfy.remote_port} (direct mode requires the pod "
                "template to expose the port as HTTP, e.g. '8188/http'). "
                "Use tunnel mode instead (COMFY_ACCESS=tunnel)."
            )
        logger.warning(
            "Direct access: the ComfyUI endpoint at %s has NO "
            "authentication — keep it private",
            url,
        )
        if getattr(config.comfy, "local_assets", ()):
            # No SSH in direct mode, and a local file can only reach the pod
            # over the tunnel. Say so instead of failing the whole start.
            logger.warning(
                "%d local library entry(ies) ignored: direct access opens no SSH "
                "tunnel. Use tunnel access to upload local files.",
                len(config.comfy.local_assets),
            )
        logger.info("Waiting for ComfyUI readiness at %s", url)
        wait_comfy(url)
        logger.ok("ComfyUI ready (preset %s)", config.comfy.preset)
        open_browser(url)
        return

    target = TunnelTarget(
        local_port=config.comfy.local_port,
        remote_host="127.0.0.1",
        remote_port=config.comfy.remote_port,
    )

    # Strict ownership: an occupied ComfyUI local port must be launcher-owned.
    _assert_owned_port(
        config.comfy.local_port,
        "tunnels:comfy",
        state,
        "ComfyUI SSH tunnel",
        lambda p: not is_port_free(p),
    )

    logger.info("Establishing SSH tunnel (%s)", target.forwarding_spec)
    created.append("tunnels:comfy")
    _establish_tunnel(
        "comfy", target, endpoint, tunnels,
        config.ssh.key_path, config.ssh.connect_timeout,
        refresh_endpoint=_ssh_endpoint_refresher(runpod, pod.id, endpoint),
    )
    logger.ok("SSH tunnel established")

    base_url = config.comfy_base_url()
    logger.info("Waiting for ComfyUI readiness at %s", base_url)
    wait_comfy(base_url)
    logger.ok("ComfyUI ready (preset %s)", config.comfy.preset)
    open_browser(base_url)
    # From here on, everything is either advisory or user-visible work: the
    # browser is already open, so neither the version probe nor the upload can
    # make the start look stuck.
    _probe_comfy_version_drift(config, endpoint)
    # The browser is opened first on purpose: uploading a 2 GB LoRA can take
    # minutes, and the user should be able to work (or watch) meanwhile.
    _upload_local_assets(config, endpoint)


def _probe_comfy_version_drift(config: Config, endpoint: Optional[SshEndpoint]) -> None:
    """Run :func:`_warn_comfy_version_drift` off the start path, best-effort.

    The probe is five read-only SSH round-trips against the pod. Worth a
    warning line in the journal, never worth delaying the browser — or the
    local-asset upload — after it: a pod whose sshd is unreachable burns
    ``5 × ssh.connect_timeout`` there, and the launcher would look frozen
    right after opening the UI. The thread is a daemon, so a start never
    waits for it and never fails because of it.
    """
    if endpoint is None:
        return

    def run() -> None:
        try:
            _warn_comfy_version_drift(config, endpoint)
        except Exception as exc:  # noqa: BLE001 - advisory only
            logger.info("ComfyUI version check skipped (%s)", exc)

    threading.Thread(target=run, daemon=True, name="comfy-version-probe").start()


def _warn_comfy_version_drift(config: Config, endpoint: Optional[SshEndpoint]) -> None:
    """Report a ComfyUI version / manager drift on the pod, best-effort.

    Observed live on image 2.0.3: the launcher asked for v0.36.0, the pod ran
    v0.35.1 (the installer's own version marker made the work tree look dirty,
    so every pinned checkout was refused), and ``--enable-manager`` was
    silently disabled because the ``comfyui_manager`` *package* was missing
    while the ``custom_nodes`` clone sat at 3.41.

    Nothing here changes the pod, delays the start, or raises: a version check
    must never be the reason a start fails. ``minimax-launcher comfy repair
    --apply`` is the fix.
    """
    if endpoint is None:
        return
    try:
        from . import comfy_version
        from .comfy_ops import ComfyOps

        ops = ComfyOps(
            endpoint=endpoint,
            key_path=config.ssh.key_path,
            connect_timeout=config.ssh.connect_timeout,
            base_url=config.comfy_base_url(),
        )
        state = comfy_version.read_state(ops)
        pinned = config.comfy.comfyui_version or ""
        report = comfy_version.diagnose(
            state,
            comfy_version.expected_target(pinned),
            pinned_version=pinned,
        )
        if report.ok:
            logger.ok(
                "ComfyUI %s on the pod matches the requested target",
                state.version,
            )
            return
        for finding in report.findings:
            logger.warning("ComfyUI pod : %s", finding.message)
        logger.warning(
            "Fix: `minimax-launcher comfy repair --apply` (then "
            "restart the comfy stack)."
        )
    except Exception as exc:  # noqa: BLE001 - advisory only
        logger.info("ComfyUI version check skipped (%s)", exc)


def _upload_local_assets(config: Config, endpoint: Optional[SshEndpoint]) -> None:
    """Send the library's **local** entries to the pod, once ComfyUI is up.

    The pod cannot read this machine, and its own installer only understands
    URLs (``lib/annuaire.sh`` rejects anything that is not http(s)), so a
    local LoRA / workflow / node is uploaded here, over the same SSH endpoint
    the tunnel uses, into the very folder the URL path targets.

    **Best-effort by construction**: a missing file, a dead connection or a
    failed transfer is logged and the next asset is tried. A start that
    brought ComfyUI up is a success even if an asset did not make it — the
    user can retry with the ⬇ button without paying for another boot.
    """
    assets = tuple(getattr(config.comfy, "local_assets", ()) or ())
    if not assets:
        return
    if endpoint is None:
        logger.warning(
            "%d local entry(ies) not uploaded: no SSH endpoint.",
            len(assets),
        )
        return
    from .comfy_ops import ComfyOps, ComfyOpsError

    ops = ComfyOps(
        endpoint,
        config.ssh.key_path,
        config.ssh.connect_timeout,
        base_url=config.comfy_base_url(),
        secret_env=dict(getattr(config.secrets, "extra", None) or {}),
    )
    logger.info(
        "Uploading %d local library entry(ies) to the pod",
        len(assets),
    )
    sent = 0
    for asset in assets:
        label = asset.name or Path(asset.path).name or asset.path
        try:
            message = ops.upload_asset(asset.kind, asset.path, name=asset.name)
        except ComfyOpsError as exc:
            logger.warning("%r not uploaded: %s", label, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - never fail a started stack
            logger.warning("%r not uploaded: %s", label, exc)
            continue
        sent += 1
        logger.ok("%s", message)
    if sent == len(assets):
        logger.ok("%d/%d local entry(ies) in place", sent, len(assets))
    else:
        logger.warning(
            "%d/%d local entry(ies) uploaded — retry with the "
            "library ⬇ button",
            sent,
            len(assets),
        )


def _start_train(
    config: Config,
    pod,
    endpoint: Optional[SshEndpoint],
    runpod: RunPodClientLike,
    tunnels: TunnelManagerLike,
    open_browser: Callable[[str], None],
    state: runtime_state.RuntimeState,
    created: list[str],
) -> None:
    """Training stack post-RUNNING phase.

    TWO ports over ONE ssh process: the KasmVNC desktop (the Fizgig UI on
    :data:`TrainConfig.local_port`) and filebrowser (datasets in, trained LoRAs
    out, on :data:`TrainConfig.files_local_port`). Two separate ssh processes
    would mean two authentications, two failure modes and two things to reap
    for a single pod.

    **Nothing is published.** The pod's public URL is not used at all for this
    stack — a deliberate difference from the stock Fizgig template, which
    exposes both ports publicly (password-gated, but public). Reaching them
    requires the SSH tunnel the launcher owns.

    There is no ``direct`` access mode, and there must not be one: unlike
    ComfyUI, this endpoint is a full desktop running as root.
    """
    if endpoint is None:
        raise OrchestrationError(
            "The training stack needs an SSH endpoint for its tunnel, and the "
            "pod has none. RunPod's ssh.proxy does not support port "
            "forwarding — the pod must expose TCP 22 (SSH over exposed TCP). "
            "Check the RunPod template enables SSH access."
        )

    targets = [
        TunnelTarget(
            local_port=config.train.local_port,
            remote_host="127.0.0.1",
            remote_port=config.train.remote_port,
        ),
        TunnelTarget(
            local_port=config.train.files_local_port,
            remote_host="127.0.0.1",
            remote_port=config.train.files_remote_port,
        ),
    ]

    # Strict ownership on BOTH ports: either one being held by a foreign
    # process would make ssh abort the whole forward (ExitOnForwardFailure).
    _assert_owned_port(
        config.train.local_port,
        "tunnels:train",
        state,
        "Training desktop SSH tunnel",
        lambda p: not is_port_free(p),
    )
    _assert_owned_port(
        config.train.files_local_port,
        "tunnels:train",
        state,
        "Training file-manager SSH tunnel",
        lambda p: not is_port_free(p),
    )

    logger.info(
        "Establishing SSH tunnel (%s)",
        ", ".join(t.forwarding_spec for t in targets),
    )
    created.append("tunnels:train")
    _establish_tunnel(
        "train", targets, endpoint, tunnels,
        config.ssh.key_path, config.ssh.connect_timeout,
        refresh_endpoint=_ssh_endpoint_refresher(runpod, pod.id, endpoint),
    )
    logger.ok("SSH tunnel established")

    if not config.secrets.vnc_password:
        # Not fatal: the image generates one and prints it in the pod log. Said
        # out loud because the alternative is a user staring at a login prompt
        # with no idea what to type.
        logger.warning(
            "No desktop password configured. The pod generated one and printed "
            "it in its log (the 'Ready' banner). Set it under Credentials to "
            "make it stable across restarts and visible here."
        )

    base_url = config.train_base_url()
    logger.info("Waiting for the training desktop at %s", base_url)
    wait_train(base_url)
    logger.ok(
        "Training desktop ready (image %s) — files at %s",
        config.train.image_tag,
        config.train_files_url(),
    )
    open_browser(base_url)


def _recreate_on_no_capacity(
    config: Config,
    runpod: RunPodClientLike,
    pod_id: str,
    on_no_capacity: Optional[Callable[[str], bool]],
    registry: PodRegistry,
    lock: ProvisioningLock,
    created: list[str],
) -> bool:
    """Ask whether to recreate an adopted pod whose host has no free GPU.

    Returns True only when the (optional, thread-safe) *on_no_capacity* hook
    confirms the user wants to drop the stuck pod and provision a fresh one
    (which RunPod may place on a different host with capacity). On True, the
    old pod is terminated, its registry record cleared, and the new pod is
    recorded so failure rollback still tracks it. On False or with no hook,
    returns False and the caller re-raises the original PlacementError.
    """
    if on_no_capacity is None:
        return False
    try:
        approve = bool(on_no_capacity(pod_id))
    except Exception:
        approve = False
    if not approve:
        return False
    logger.info(
        "No free GPU on the pod's host; terminating pod %s to provision a fresh one",
        pod_id,
    )
    try:
        runpod.terminate_pod(pod_id)
    except RunPodError as exc:
        logger.warning("Could not terminate pod %s: %s", pod_id, exc)
    registry.clear(config.stack)
    return True


def _pod_ready_alert_if_waited(stats: Optional[dict]) -> None:
    """Sound alert when the pod finally started after unavailability(ies).

    A pod that came up on the first try needs no alarm; a pod that required
    repeated placement attempts (capacity was missing) gets a short chime and
    a prominent journal line so the user, who may have been waiting, knows
    the stack is finally up.
    """
    count = (stats or {}).get("placement_failures", 0)
    if count <= 0:
        return
    played = alerts.play_pod_ready()
    logger.ok(
        "Pod started after %d availability failure(s)%s",
        count,
        " — alert played" if played else "",
    )


def _run_start(
    config: Config,
    runpod: RunPodClientLike,
    tunnels: TunnelManagerLike,
    open_browser: Callable[[str], None],
    state: runtime_state.RuntimeState,
    created: list[str],
    registry: PodRegistry,
    lock: ProvisioningLock,
    on_no_capacity: Optional[Callable[[str], bool]] = None,
    stats: Optional[dict] = None,
) -> None:
    pod, pod_id, created_by_us = _resolve_pod(
        config, runpod, registry, lock, created, stats
    )

    if created_by_us:
        # A freshly created pod must never be started; it is provisioning
        # (or already running) on its own. wait_ready is the readiness gate
        # in either case — the create response returns before the pod's
        # ports are usable.
        logger.info("Waiting for new pod %s to become RUNNING", pod_id)
        pod = runpod.wait_ready(
            pod_id, timeout=float(config.runpod.provision_timeout), interval=10.0
        )
        logger.ok("Pod is RUNNING")
        _pod_ready_alert_if_waited(stats)
    else:
        logger.info("Checking RunPod %s pod %s", config.stack, pod_id)
        try:
            if config.stack == "comfy":
                if _comfy_env_diffs(pod, config) or _extra_secret_diffs(pod, config):
                    pod = _sync_comfy_env(config, runpod, pod, pod_id)
                else:
                    pod = _ensure_pod_running(runpod, pod, pod_id)
            elif config.stack == "train":
                if _train_env_diffs(pod, config) or _extra_secret_diffs(pod, config):
                    pod = _sync_train_env(config, runpod, pod, pod_id)
                else:
                    pod = _ensure_pod_running(runpod, pod, pod_id)
        except PlacementError:
            if stats is not None:
                stats["placement_failures"] = (
                    stats.get("placement_failures", 0) + 1
                )
            if not _recreate_on_no_capacity(
                config, runpod, pod_id, on_no_capacity, registry, lock, created,
            ):
                raise
            pod, pod_id, created_by_us = _resolve_pod(
                config, runpod, registry, lock, created, stats
            )
            logger.info("Waiting for new pod %s to become RUNNING", pod_id)
            pod = runpod.wait_ready(
                pod_id, timeout=float(config.runpod.provision_timeout), interval=10.0
            )
            logger.ok("Pod is RUNNING")
            _pod_ready_alert_if_waited(stats)

    # ComfyUI in ``direct`` mode needs no SSH at all: the pod's public HTTP
    # mapping of the ComfyUI port is the endpoint (see _start_comfy).
    comfy_direct = config.stack == "comfy" and config.comfy.access == "direct"

    # RUNNING fires before RunPod assigns the public 22/tcp mapping (observed
    # anywhere from ~17s to several minutes later). Poll for a direct SSH
    # endpoint before resolving it.
    if not comfy_direct and pod.ssh_tunnel_endpoint() is None:
        logger.info("Waiting for RunPod to assign the public SSH (22/tcp) mapping")
        pod = runpod.wait_ssh_endpoint(pod_id, timeout=600.0, interval=5.0)

    endpoint = None if comfy_direct else _resolve_direct_endpoint(pod)

    if config.stack == "comfy":
        _start_comfy(config, pod, endpoint, runpod, tunnels, open_browser, state, created)
        return

    if config.stack == "train":
        _start_train(
            config, pod, endpoint, runpod, tunnels, open_browser, state, created,
        )
        return


def start(
    config: Config,
    runpod: Optional[RunPodClientLike] = None,
    tunnels: Optional[TunnelManagerLike] = None,
    open_browser: Optional[Callable[[str], None]] = None,
    state: Optional[runtime_state.RuntimeState] = None,
    registry: Optional[PodRegistry] = None,
    lock: Optional[ProvisioningLock] = None,
    on_no_capacity: Optional[Callable[[str], bool]] = None,
) -> None:
    """Run the full startup sequence.

    Dependencies default to the real implementations; inject fakes in tests.
    Raises :class:`OrchestrationError` (or a more specific error) on failure,
    and stops only the resources created by this invocation on error —
    resources from a previous successful invocation are left untouched. A
    pod created by this invocation is additionally stopped on failure so a
    crashed startup does not leave a charging pod behind (the registry record
    is kept so the pod stays manageable).

    ``on_no_capacity`` (optional, thread-safe) is called with the pod id when
    starting an adopted pod fails because its host has no free GPU. Returning
    True terminates that pod and provisions a fresh one; False (or no hook)
    propagates the error.

    Pod creation failures for lack of capacity are retried automatically on
    a 30 s cadence; when the pod finally starts after such failure(s), a
    short sound alert is played.
    """
    state = state or runtime_state.RuntimeState()
    registry = registry or PodRegistry()
    lock = lock or ProvisioningLock()

    try:
        runpod = runpod or _build_runpod(config)
    except OrchestrationError:
        raise
    tunnels = tunnels or TunnelManager(state=state)
    open_browser = open_browser or _default_open_browser

    created: list[str] = []
    stats: dict = {}
    try:
        _run_start(
            config, runpod, tunnels, open_browser, state, created,
            registry, lock,
            on_no_capacity=on_no_capacity,
            stats=stats,
        )
    except (RunPodError, TunnelError, HealthError, OrchestrationError) as exc:
        logger.error("%s", exc)
        _rollback_created_pod(runpod, registry, created, config.stack)
        _rollback_failed_start(tunnels=tunnels, created=created)
        raise OrchestrationError(str(exc)) from exc


def _rollback_created_pod(
    runpod: Optional[RunPodClientLike],
    registry: PodRegistry,
    created: Sequence[str],
    stack: str = "agent",
) -> None:
    """Best-effort stop of pods this invocation created.

    The pod's live state is fetched once to decide whether ``stop`` is a
    valid action (RunPod's action matrix depends on status). A verification
    failure NEVER masks the original error, never retries, and never touches
    the pod: when the pod cannot be verified it is left as-is, the registry
    record is preserved, and the situation is reported for manual recovery.
    Only ``stop`` is ever attempted — never ``terminate``.
    """
    for entry in created:
        if not entry.startswith("pod:"):
            continue
        pod_id = entry[len("pod:"):]
        # These pods were created seconds ago: a 404 here is almost always
        # RunPod still indexing them, so the presence check retries before
        # concluding anything (a single 404 used to clear the registry and
        # orphan a running pod).
        pod, presence = pod_presence(runpod, pod_id, created_at=time.time())
        if presence == "gone":
            logger.warning(
                "Created pod %s is already gone; clearing the pod registry", pod_id
            )
            registry.clear(stack)
            continue
        if presence == "unknown":
            logger.warning(
                "Created pod %s could not be verified; it was left as-is. "
                "Recover it via the RunPod console or by setting RUNPOD_POD_ID.",
                pod_id,
            )
            continue
        if "stop" not in (pod.actions or ()):
            logger.warning(
                "Created pod %s (status %s) does not offer a stop action; it "
                "was left as-is. Recover it via the RunPod console or by "
                "setting RUNPOD_POD_ID.",
                pod_id,
                getattr(pod, "status", "UNKNOWN"),
            )
            continue
        try:
            runpod.stop_pod(pod_id)
            logger.warning("Stopped newly created pod %s after a failed startup", pod_id)
        except Exception as exc:
            logger.warning(
                "Could not stop newly created pod %s: %s. Recover it via the "
                "RunPod console or by setting RUNPOD_POD_ID.",
                pod_id,
                exc,
            )


def _rollback_failed_start(
    tunnels: Optional[TunnelManagerLike],
    created: Sequence[str],
) -> None:
    """Stop only the resources created by the failed start() invocation.

    Each manager's ``stop`` terminates the process it spawned in this process
    and unrecords its own RuntimeState entry, so no previous-invocation or
    external resource is touched. This is intentionally narrower than
    :func:`stop` / :func:`cleanup`, which remain the full shutdown path for
    the explicit ``stop`` command and Ctrl+C.
    """
    if tunnels is not None and hasattr(tunnels, "stop"):
        for entry in created:
            if entry.startswith("tunnels:"):
                tunnels.stop(entry[len("tunnels:"):])


#: Local tunnel names each workload stack owns. A targeted ``stop(stack=…)``
#: only tears down the tunnels listed here, so stopping one stack never takes
#: down another stack's tunnel (or the text agent pointed at it).
STACK_TUNNELS: dict[str, tuple[str, ...]] = {
    "comfy": ("comfy",),
    "train": ("train",),
}


def stop(
    tunnels: Optional[TunnelManagerLike] = None,
    state: Optional[runtime_state.RuntimeState] = None,
    runpod: Optional[RunPodClientLike] = None,
    registry: Optional[PodRegistry] = None,
    config: Optional[Config] = None,
    terminate: bool = False,
    stack: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """Clean shutdown of launcher-owned processes and the registered pod(s).

    The local phase terminates ONLY PIDs recorded in the runtime state (never
    unrelated ``ssh.exe``). With ``stack`` omitted it tears down every recorded
    local process and clears the state file (the historical behaviour). With
    ``stack`` given it is **scoped**: only that stack's tunnels (see
    :data:`STACK_TUNNELS`) are stopped, so the other stack's tunnel keeps
    running. This is what makes concurrent stacks (ComfyUI and LoRA training)
    independently stoppable.

    The pod phase (only when *runpod* and *registry* are provided) manages
    the launcher-created pods recorded in the registry:

    * ``stack`` selects which workload stack's pod to manage (``"agent"`` /
      ``"comfy"``); when omitted, every registered pod is managed — both
      stacks can therefore be stopped independently, or all at once.
    * default (``terminate=False``): ``stop`` each pod if that is a valid
      action for its current status — the disk is kept and the record
      survives, so the pod can be started again later. A pod that no longer
      exists clears the record; an unreachable API keeps it.
    * ``terminate=True``: terminate the pod(s) (destructive) and clear the
      record(s). Refused for the stack of an explicitly configured
      ``RUNPOD_POD_ID`` (such a pod is never terminated by the launcher) or
      when no registered pod exists.

    Returns the pod phase outcomes as ``{stack: outcome}`` (``"stopped"``/
    ``"cleared"``/``"unavailable"``/``"skipped"``/``"terminated"``/
    ``"already_stopped"``) or ``None`` when no pod phase ran.
    """
    state = state or runtime_state.RuntimeState()
    entries = state.load()

    scoped = stack is not None
    tunnel_names = STACK_TUNNELS.get(stack, ()) if scoped else ()

    for key in list(entries):
        if not key.startswith("tunnels:"):
            continue
        if scoped and key[len("tunnels:"):] not in tunnel_names:
            continue
        entry = entries.pop(key, None)
        if entry is not None and runtime_state.pid_is_alive(entry.pid):
            try:
                runtime_state.terminate_recorded_pid(entry)
            except OSError:
                pass

    # Stop any in-memory processes held by injected managers (same process).
    if tunnels is not None:
        if scoped:
            for name in tunnel_names:
                try:
                    tunnels.stop(name)
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass
        else:
            tunnels.stop_all()
    if scoped:
        # Other stacks' entries survive: save what is left (managers may have
        # unrecorded their own entries already, so re-read before writing).
        remaining = state.load()
        for key in list(remaining):
            if key.startswith("tunnels:") and key[len("tunnels:"):] in tunnel_names:
                remaining.pop(key, None)
        state.save(remaining)
    else:
        state.clear()

    return _stop_registered_pod(
        runpod=runpod, registry=registry, config=config, terminate=terminate,
        stack=stack,
    )


def _stop_one_registered_pod(
    runpod: RunPodClientLike,
    registry: PodRegistry,
    stack: str,
    terminate: bool,
) -> str:
    """Stop or terminate the registered pod of *stack*; returns the outcome."""
    record = registry.load(stack)
    if record is None:
        raise OrchestrationError(
            f"No registered {stack} pod to {'terminate' if terminate else 'stop'}"
        )

    pod_id = record.pod_id
    if terminate:
        # Do not trust a single 404 here either: a pod created seconds ago can
        # still be missing from the API while it is alive and billing.
        pod, presence = pod_presence(
            runpod, pod_id, created_at=record.created_at
        )
        if presence == "gone":
            registry.clear(stack)
            logger.ok("Registered %s pod %s already gone; record cleared", stack, pod_id)
            return "cleared"
        # "found" or "unknown" (unverifiable): attempt the termination and let
        # the API answer — a genuine 404 below is then a real "already gone".
        try:
            runpod.terminate_pod(pod_id)
        except PodNotFoundError:
            registry.clear(stack)
            logger.ok("Registered %s pod %s already gone; record cleared", stack, pod_id)
            return "cleared"
        except RunPodError as exc:
            raise OrchestrationError(
                f"Failed to terminate pod {pod_id}: {exc}"
            ) from exc
        registry.clear(stack)
        logger.ok("Terminated RunPod %s pod %s", stack, pod_id)
        return "terminated"

    pod, presence = pod_presence(runpod, pod_id, created_at=record.created_at)
    if presence == "unknown":
        return "unavailable"
    if presence == "gone":
        registry.clear(stack)
        logger.ok("Registered %s pod %s no longer exists; record cleared", stack, pod_id)
        return "cleared"
    if "stop" not in (pod.actions or ()):
        status = (pod.status or "").strip().upper()
        if status == STATUS_TERMINATED:
            registry.clear(stack)
            logger.ok(
                "Registered %s pod %s already terminated; record cleared",
                stack, pod_id,
            )
            return "cleared"
        if status in (STATUS_EXITED, STATUS_ERROR):
            logger.ok(
                "Registered %s pod %s already stopped (status %s); "
                "nothing to stop",
                stack, pod_id, pod.status,
            )
            return "already_stopped"
        logger.info(
            "Registered %s pod %s (status %s) does not offer a stop action; "
            "nothing to stop",
            stack,
            pod_id,
            pod.status,
        )
        return "skipped"
    try:
        runpod.stop_pod(pod_id)
    except RunPodError as exc:
        raise OrchestrationError(f"Failed to stop pod {pod_id}: {exc}") from exc
    logger.ok("Stopped RunPod %s pod %s (disk kept)", stack, pod_id)
    return "stopped"


def _stop_registered_pod(
    runpod: Optional[RunPodClientLike],
    registry: Optional[PodRegistry],
    config: Optional[Config],
    terminate: bool,
    stack: Optional[str] = None,
) -> Optional[dict[str, str]]:
    if registry is None:
        return None
    records = registry.all()
    if stack is not None:
        targets = [stack] if stack in records else []
    else:
        targets = [s for s in STACKS if s in records]
    if not targets:
        if terminate:
            raise OrchestrationError(
                "No registered pod to terminate. Run `minimax-launcher start` "
                "to provision a pod first, or set RUNPOD_POD_ID to manage an "
                "existing pod."
            )
        return None
    if terminate and config is not None and config.runpod.pod_id:
        # An explicitly configured (environment) pod belongs to the stack
        # this invocation configured and is never terminated by the launcher.
        if stack is None or stack == config.stack:
            raise OrchestrationError(
                "RUNPOD_POD_ID is set; the launcher never terminates an "
                "explicitly configured pod. Clear RUNPOD_POD_ID first, or "
                "terminate the pod via the RunPod console."
            )
    if runpod is None:
        if terminate:
            raise OrchestrationError(
                "RUNPOD_API_KEY is not set; the registered pod cannot be "
                "terminated. Configure the key and retry."
            )
        return None

    outcomes: dict[str, str] = {}
    for target in targets:
        outcomes[target] = _stop_one_registered_pod(runpod, registry, target, terminate)
    return outcomes


def cleanup(
    tunnels: Optional[TunnelManagerLike] = None,
    state: Optional[runtime_state.RuntimeState] = None,
) -> None:
    """Stop launcher-owned processes and clear runtime state (Ctrl+C / failure)."""
    stop(tunnels=tunnels, state=state)


def _recorded_live_entries(
    state: runtime_state.RuntimeState,
) -> dict[str, runtime_state.ProcessEntry]:
    """Return RuntimeState entries whose PID is currently alive."""
    return {
        name: entry
        for name, entry in state.load().items()
        if runtime_state.pid_is_alive(entry.pid)
    }


def status(state: Optional[runtime_state.RuntimeState] = None) -> dict[str, dict]:
    """Return launcher-owned process status (pid, label, port, alive).

    Reports whether the recorded SSH tunnel(s) are still alive, even across
    CLI invocations (reads the persistent runtime state).
    """
    state = state or runtime_state.RuntimeState()
    entries = state.load()
    result = {}
    for name, entry in entries.items():
        result[name] = {
            "pid": entry.pid,
            "label": entry.label,
            "port": entry.port,
            "alive": runtime_state.pid_is_alive(entry.pid),
        }
    return result


def operational_status(
    config: Optional[Config] = None,
    runpod: Optional[RunPodClientLike] = None,
    tunnels: Optional[TunnelManagerLike] = None,
    state: Optional[runtime_state.RuntimeState] = None,
    check_comfy_fn=None,
    check_train_fn=None,
    registry: Optional[PodRegistry] = None,
) -> dict[str, str]:
    """Return a concise operational status summary for the active stack.

    Each field is a small human-readable value. Live checks use bounded timeouts
    and degrade to ``UNKNOWN``/``STOPPED``/``NOT READY`` rather than raising.
    ``pod`` is the pod ID actually in use (environment first, then the
    registered pod of the active stack) or ``NONE`` — resolving it never
    performs an API call.

    For the ``comfy`` stack the serving fields describe the ComfyUI server:
    ``vllm`` is the ComfyUI readiness, ``model`` is the active preset,
    ``profile`` the tier, ``access`` the configured access mode
    (``tunnel``/``direct``), and ``url`` the local tunnel URL or the public
    direct URL.

    For the ``train`` stack the serving fields describe the Fizgig desktop:
    ``vllm`` is the desktop readiness (see :func:`launcher.health.check_train`
    — KasmVNC answers 401 to an anonymous probe, which is the READY signal),
    ``model`` the image name, ``profile`` the image tag, ``access`` is
    ``N/A``, and ``url`` the local desktop URL (the file manager has its own
    URL on the same tunnel).
    """
    state = state or runtime_state.RuntimeState()
    recorded = _recorded_live_entries(state)

    pod_id = None
    if config is not None:
        pod_id = config.runpod.pod_id
        if not pod_id and registry is not None:
            record = registry.load(config.stack)
            if record is not None:
                pod_id = record.pod_id

    runpod_status = "UNKNOWN"
    pod = None
    if pod_id and runpod is not None:
        try:
            pod = runpod.get_pod(pod_id)
            runpod_status = pod.status
        except RunPodError:
            runpod_status = "UNKNOWN"

    if config is not None and config.stack == "train":
        # One tunnel, two ports (desktop + file manager): no single target, so
        # ``is_alive`` probes every forwarded port and both must answer.
        tunnel_name = "train"
        target = None
        check_url = config.train_base_url()
        health_fn = check_train_fn
    else:
        tunnel_name = "comfy"
        target = TunnelTarget(
            config.comfy.local_port, "127.0.0.1", config.comfy.remote_port
        ) if config is not None else TunnelTarget(8188, "127.0.0.1", 8188)
        if config is not None and config.comfy.access == "direct" and pod is not None:
            check_url = (
                pod.public_url_for_port(config.comfy.remote_port)
                or config.comfy_base_url()
            )
        else:
            check_url = (
                config.comfy_base_url() if config is not None
                else "http://127.0.0.1:8188"
            )
        health_fn = check_comfy_fn

    # Scope the tunnel verdict to THIS stack's tunnel: with several stacks
    # running at once, "some tunnel is alive" must not make every stack look
    # connected.
    tunnel_status = "STOPPED"
    if f"tunnels:{tunnel_name}" in recorded:
        if tunnels is not None:
            tunnel_status = "CONNECTED" if tunnels.is_alive(tunnel_name, target) else "UNKNOWN"
        else:
            tunnel_status = "CONNECTED"

    health_status = "UNKNOWN"
    if health_fn is not None:
        try:
            health_status = "READY" if health_fn(check_url) else "NOT READY"
        except Exception:
            health_status = "NOT READY"

    if config is not None and config.stack == "train":
        model_status = config.train.image_name
        profile_label = f"image {config.train.image_tag}"
        access_status = "N/A"
        url = config.train_base_url()
    else:
        model_status = config.comfy.preset if config is not None else DEFAULT_COMFY_PRESET
        profile_label = config.comfy.tier if config is not None else DEFAULT_COMFY_TIER
        access_status = config.comfy.access if config is not None else "tunnel"
        if config is not None and config.comfy.access == "direct":
            url = (
                (pod.public_url_for_port(config.comfy.remote_port) if pod else None)
                or config.comfy_base_url()
            )
        else:
            url = (
                config.comfy_base_url() if config is not None
                else "http://127.0.0.1:8188"
            )

    return {
        "stack": config.stack if config else DEFAULT_STACK,
        "runpod": runpod_status,
        "pod": pod_id or "NONE",
        "tunnel": tunnel_status,
        "vllm": health_status,
        "model": model_status,
        "profile": profile_label,
        "access": access_status,
        "url": url,
    }


def active_stacks(
    registry: Optional[PodRegistry] = None,
    state: Optional[runtime_state.RuntimeState] = None,
) -> list[str]:
    """Workload stacks that currently have a pod record or a live local process.

    A stack counts as active when the registry holds a record for it (a pod the
    launcher created, running or stopped) **or** one of its local processes is
    recorded and still alive. Returned in :data:`STACKS` order; degrades to an
    empty list on any failure (never raises — this feeds the dashboard).
    """
    registry = registry or PodRegistry()
    state = state or runtime_state.RuntimeState()
    active: set[str] = set()
    try:
        active.update(registry.all().keys())
    except Exception:  # noqa: BLE001 - a broken registry means "unknown"
        pass
    try:
        entries = state.load()
    except Exception:  # noqa: BLE001
        entries = {}
    for name in entries:
        if not name.startswith("tunnels:"):
            continue
        tunnel = name[len("tunnels:"):]
        for stack, names in STACK_TUNNELS.items():
            if tunnel in names:
                active.add(stack)
    return [stack for stack in STACKS if stack in active]


def operational_status_all(
    configs: dict,
    runpod: Optional[RunPodClientLike] = None,
    tunnels: Optional[TunnelManagerLike] = None,
    state: Optional[runtime_state.RuntimeState] = None,
    check_comfy_fn=None,
    check_train_fn=None,
    registry: Optional[PodRegistry] = None,
) -> dict:
    """Per-stack operational status for every stack in *configs*.

    ``configs`` maps a stack name to its :class:`Config`. Each entry is the
    :func:`operational_status` summary for that stack, computed with the same
    injectables. A stack whose config is missing is skipped; a stack whose
    probe raises yields a degraded ``UNKNOWN`` summary rather than aborting the
    whole report (a single broken stack must never hide the others).
    """
    result: dict = {}
    for stack, config in (configs or {}).items():
        try:
            result[stack] = operational_status(
                config=config,
                runpod=runpod,
                tunnels=tunnels,
                state=state,
                check_comfy_fn=check_comfy_fn,
                check_train_fn=check_train_fn,
                registry=registry,
            )
        except Exception as exc:  # noqa: BLE001 - one stack must not hide others
            result[stack] = {
                "stack": stack,
                "runpod": "UNKNOWN",
                "pod": "NONE",
                "tunnel": "UNKNOWN",
                "vllm": "UNKNOWN",
                "model": "—",
                "profile": "—",
                "access": "UNKNOWN",
                "url": "—",
                "error": f"{type(exc).__name__}: {exc}",
            }
    return result


def _build_runpod(config: Config) -> RunPodClient:
    api_key = config.secrets.runpod_api_key
    if not api_key:
        raise OrchestrationError(
            "RUNPOD_API_KEY is not set. Configure it before starting the launcher."
        )
    return RunPodClient(api_key)


def _default_open_browser(url: str) -> None:
    import webbrowser

    webbrowser.open(url)

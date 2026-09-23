"""Infrastructure recovery engine for the MiniMax H3 Launcher tool layer.

Implements the write-risk recovery actions (``start_runpod``,
``reconnect_ssh``, ``restart_comfy``, ``restart_train``) as a strict,
authorization-gated, single-action-per-call engine. It:

* reuses the existing lifecycle managers (RunPod client, tunnel manager) —
  it does NOT re-implement pod/tunnel logic;
* is gated by an explicit :class:`RecoveryAuthorizer` (operator enablement +
  per-call ``confirm``) — ``risk="write"`` alone is not sufficient;
* performs a fresh live precondition probe immediately before every mutation
  (a previous ``forge_infra_diagnose`` result is never trusted);
* uses a concurrency lock so two turns cannot act simultaneously;
* applies bounded timeouts and no automatic retries;
* verifies the post-condition and reports a compact, classified result.

It never executes arbitrary shell commands, never accepts arbitrary pod IDs /
URLs / paths / commands / PIDs, never kills external processes, and never
exposes secrets or raw logs.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from . import logging as launcher_logging
from .config import Config
from .runpod import RunPodError
from .tunnel import TunnelError, TunnelTarget, from_runpod_endpoint

logger = launcher_logging.get_logger("minimax-launcher.infra_recover")

# Closed action set — the only actions this engine understands.
RECOVERY_ACTIONS = (
    "start_runpod",
    "reconnect_ssh",
    "restart_comfy",
    "restart_train",
)

# Action-specific bounded timeouts (seconds).
POD_TIMEOUT = 120.0
SSH_TIMEOUT = 30.0
# A stopped ComfyUI pod must pull its (multi-GB) image before it reaches
# RUNNING, so the pod phase of ``restart_comfy`` allows more time.
COMFY_POD_TIMEOUT = 300.0
# The training image is larger still (KasmVNC desktop + torch), and with
# FETCH_MODELS set the pod downloads tens of GB of weights before the
# desktop is up — but recovery only waits for the POD, not for that.
TRAIN_POD_TIMEOUT = 300.0


class RecoveryError(RuntimeError):
    """Base class for recovery-engine errors."""


@dataclass(frozen=True)
class RecoveryResult:
    """Compact, classified outcome of a recovery attempt."""

    action: str
    ok: bool
    before: str
    after: str
    error_class: Optional[str]
    message: str

    def render(self) -> str:
        lines = [
            f"action: {self.action}",
            f"ok: {str(self.ok).lower()}",
            f"before: {self.before}",
            f"after: {self.after}",
        ]
        if self.error_class:
            lines.append(f"error: {self.error_class}")
        lines.append(f"message: {self.message}")
        return "\n".join(lines)


class RecoveryAuthorizer:
    """Authorization gate: operator enablement + per-call confirmation.

    Returns ``None`` when the action is authorized, or a
    ``(error_class, message)`` tuple describing why it was refused.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode  # "disabled" | "confirm"

    def authorize(self, action: str, confirm: bool):
        if self.mode == "disabled":
            return ("denied", "infrastructure recovery is disabled (LAUNCHER_INFRA_RECOVERY)")
        if action not in RECOVERY_ACTIONS:
            return ("invalid_action", f"unknown action {action!r}")
        if not confirm:
            return ("preview", "recovery requires confirm=true")
        return None


# A recovery lock older than this is considered stale regardless of content:
# it covers a holder that crashed between creating the file and writing its
# pid (an unparseable file with a live, unknown holder).
_STALE_LOCK_SECONDS = 60.0
#: A recovery lock whose PID looks alive but is older than this is stale too:
#: Windows recycles PIDs, so a crashed holder's number can name any unrelated
#: process — and the lock would then be held for ever. Well above any real
#: recovery (bounded by the ssh/HTTP timeouts).
_RECOVERY_LOCK_MAX_AGE_SECONDS = 1800.0


class FileRecoveryLock:
    """File-based recovery lock at the launcher state dir.

    Shared by the MCP wiring and the GUI wiring so that a GUI-triggered and a
    tool-triggered recovery can never run simultaneously (both touch pod
    lifecycle and tunnels; two concurrent recoveries risk double mutations).

    Safety rules:

    * acquire is an atomic ``O_CREAT | O_EXCL`` create;
    * takeover of an existing lock requires a provably dead holder PID — an
      *unparseable* lock file is only taken over once it is at least
      ``_STALE_LOCK_SECONDS`` old (a fresh partial write may belong to a
      holder whose pid simply has not landed on disk yet);
    * release removes the file only when it is still owned by this process
      (or is stale) — a holder that lost its lock to a takeover must not
      delete the *new* holder's lock on the way out.
    """

    def __init__(self, path=None) -> None:
        from .recovery_audit import state_dir

        self._path = Path(path) if path is not None else state_dir() / "recovery.lock"

    def acquire(self) -> bool:
        import json as _json
        import os as _os

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = _os.open(str(self._path), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
        except FileExistsError:
            if self._takeover_allowed():
                try:
                    self._path.unlink()
                except OSError:
                    return False
                try:
                    fd = _os.open(
                        str(self._path), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY
                    )
                except OSError:
                    return False
            else:
                return False
        except OSError:
            return False

        try:
            _os.write(fd, _json.dumps({"pid": _os.getpid()}).encode("utf-8"))
        finally:
            _os.close(fd)
        return True

    def _takeover_allowed(self) -> bool:
        """True when the existing lock is held by a dead process (or is a
        stale/partial write no one can prove is live)."""
        import json as _json
        import os as _os
        import time as _time

        from . import runtime_state

        try:
            age = _time.time() - self._path.stat().st_mtime
        except OSError:
            return True  # vanished between exists() and stat(): free to take
        try:
            data = _json.loads(self._path.read_text(encoding="utf-8"))
            pid = int(data.get("pid", 0))
        except Exception:
            # Unparseable (partial write / crash mid-write): only a lock that
            # is provably old may be taken over.
            return age >= _STALE_LOCK_SECONDS
        if not pid:
            return age >= _STALE_LOCK_SECONDS
        if not runtime_state.pid_is_alive(pid):
            return True
        # The PID is "alive" — but a crashed holder's PID can be recycled by
        # any other process, and the lock would then look held for ever with no
        # way out from the tool layer. The absolute age bound is the only
        # defence; it is well above any real recovery (which is bounded by the
        # ssh/HTTP timeouts).
        return age >= _RECOVERY_LOCK_MAX_AGE_SECONDS

    def release(self) -> None:
        import json as _json
        import os as _os
        import time as _time

        try:
            if self._path.exists():
                try:
                    data = _json.loads(self._path.read_text(encoding="utf-8"))
                    holder = int(data.get("pid", 0))
                except Exception:
                    holder = 0
                stale = _time.time() - self._path.stat().st_mtime >= _STALE_LOCK_SECONDS
                if holder == _os.getpid() or (holder == 0 and stale):
                    self._path.unlink()
        except OSError:
            pass


# A lock must expose acquire() -> bool and release() -> None.
LockLike = Callable[[], bool]


class RecoveryEngine:
    """Execute a single, authorized recovery action with strict safety checks.

    Parameters are injectable fakes in tests; in production they default to the
    real lifecycle managers.
    """

    def __init__(
        self,
        config: Config,
        authorizer: RecoveryAuthorizer,
        runpod=None,
        tunnels=None,
        state=None,
        lock=None,
        is_port_free=None,
        audit=None,
        registry=None,
        check_comfy_fn=None,
        check_train_fn=None,
    ) -> None:
        self.config = config
        self.authorizer = authorizer
        self.runpod = runpod
        self.tunnels = tunnels
        self.state = state
        self.lock = lock
        self.is_port_free = is_port_free
        self.audit = audit
        self.registry = registry
        self.check_comfy_fn = check_comfy_fn
        #: Train-stack desktop probe: KasmVNC answers 401 to an anonymous
        #: request, which ``check_comfy`` reads as a failure (see
        #: :func:`launcher.health.check_train`).
        self.check_train_fn = check_train_fn

    def _pod_id(self) -> Optional[str]:
        """Resolve the pod id in use: environment first, then the registry.

        The registry lookup is stack-aware: a ``comfy`` config resolves the
        ComfyUI pod, a ``train`` config the training pod.
        """
        if self.config.runpod.pod_id:
            return self.config.runpod.pod_id
        if self.registry is None:
            return None
        try:
            record = self.registry.load(self.config.stack)
        except Exception:
            return None
        if record is None:
            return None
        return record.pod_id

    def recover(
        self,
        action: str,
        confirm: bool = False,
        reason: Optional[str] = None,
    ) -> RecoveryResult:
        started = time.monotonic()
        refusal = self.authorizer.authorize(action, confirm)
        if refusal is not None:
            error_class, message = refusal
            result = RecoveryResult(
                action=action, ok=False, before="", after="",
                error_class=error_class, message=message,
            )
            self._audit(action, confirm, False, started, result)
            return result

        if self.lock is not None:
            if not self.lock.acquire():
                result = RecoveryResult(
                    action=action, ok=False, before="", after="",
                    error_class="lock_held",
                    message="another recovery is already in progress",
                )
                self._audit(action, confirm, True, started, result)
                return result

        try:
            handler = getattr(self, f"_do_{action}")
            result = handler()
        except RecoveryError as exc:
            result = RecoveryResult(
                action=action, ok=False, before="", after="",
                error_class=getattr(exc, "error_class", "action_failed"),
                message=str(exc),
            )
        except Exception as exc:  # defensive — never leak a raw exception
            logger.error(
                "recovery action %s failed: %s: %s",
                action, type(exc).__name__, launcher_logging.redact(str(exc)),
            )
            result = RecoveryResult(
                action=action, ok=False, before="", after="",
                error_class="action_failed", message="recovery failed",
            )
        finally:
            if self.lock is not None:
                self.lock.release()

        self._audit(action, confirm, True, started, result, reason)
        return result

    def _audit(self, action, confirm, authorized, started, result, reason=None):
        """Record the attempt in the audit log.

        An audit write failure must never change the reported outcome (a
        successful recovery is not re-reported as a failure because the log
        could not be appended) nor escape the engine.
        """
        if self.audit is None:
            return
        ended = time.monotonic()
        try:
            self.audit.record(
                action=action,
                authorized=authorized,
                confirm=confirm,
                started_at=started,
                ended_at=ended,
                duration_ms=int((ended - started) * 1000),
                before=result.before,
                after=result.after,
                ok=result.ok,
                error_class=result.error_class,
                reason=reason,
            )
        except Exception as exc:
            logger.warning(
                "recovery audit write failed: %s: %s",
                type(exc).__name__, launcher_logging.redact(str(exc)),
            )

    # ------------------------------------------------------------------
    # Action handlers (each performs fresh preconditions -> act -> verify)
    # ------------------------------------------------------------------

    def _do_start_runpod(self) -> RecoveryResult:
        pod_id = self._pod_id()
        if self.runpod is None or pod_id is None:
            return RecoveryResult(
                action="start_runpod", ok=False, before="", after="",
                error_class="precondition_failed", message="RunPod client or pod id unavailable",
            )
        try:
            pod = self.runpod.get_pod(pod_id)
        except RunPodError:
            return RecoveryResult(
                action="start_runpod", ok=False, before="UNKNOWN", after="UNKNOWN",
                error_class="precondition_failed", message="pod state unavailable",
            )
        before = pod.status
        if pod.is_running:
            return RecoveryResult(
                action="start_runpod", ok=True, before=before, after=before,
                error_class="no_op", message="pod is already running",
            )
        if not pod.is_startable:
            return RecoveryResult(
                action="start_runpod", ok=False, before=before, after=before,
                error_class="precondition_failed",
                message=f"pod status {before!r} cannot be started",
            )
        try:
            self.runpod.start_pod(pod_id)
            final = self.runpod.wait_ready(pod_id, timeout=POD_TIMEOUT)
        except RunPodError:
            return RecoveryResult(
                action="start_runpod", ok=False, before=before, after="UNKNOWN",
                error_class="action_failed", message="pod start failed",
            )
        if not final.is_running:
            return RecoveryResult(
                action="start_runpod", ok=False, before=before, after=final.status,
                error_class="verification_failed", message="pod did not become RUNNING",
            )
        return RecoveryResult(
            action="start_runpod", ok=True, before=before, after=final.status,
            error_class=None, message="pod is now RUNNING",
        )

    def _tunnel_spec(self) -> tuple[str, TunnelTarget]:
        """(tunnel name, local->remote target) for the active stack.

        The ``comfy`` stack tunnels its serving port under the ``comfy``
        tunnel name; the ``train`` stack has two ports and is handled by
        :meth:`_tunnel_targets`.
        """
        return "comfy", TunnelTarget(
            local_port=self.config.comfy.local_port,
            remote_host="127.0.0.1",
            remote_port=self.config.comfy.remote_port,
        )

    def _tunnel_targets(self) -> tuple[str, list[TunnelTarget]]:
        """Every local->remote target the active stack's tunnel forwards.

        The ``train`` stack needs TWO ports over ONE ssh process (the KasmVNC
        desktop and the file manager). It used to fall through to the single-port
        branch: recovery then opened a tunnel to the wrong port under the wrong
        name, ``wait_alive`` only probes the *local* port (which ``ssh -L`` binds
        even when the remote port is closed), so the engine reported
        ``ok=True, after="CONNECTED"`` while the desktop stayed unreachable.
        """
        if self.config.stack == "train":
            return "train", [
                TunnelTarget(
                    local_port=self.config.train.local_port,
                    remote_host="127.0.0.1",
                    remote_port=self.config.train.remote_port,
                ),
                TunnelTarget(
                    local_port=self.config.train.files_local_port,
                    remote_host="127.0.0.1",
                    remote_port=self.config.train.files_remote_port,
                ),
            ]
        name, target = self._tunnel_spec()
        return name, [target]


    def _do_reconnect_ssh(self) -> RecoveryResult:
        if self.runpod is None or self.tunnels is None:
            return RecoveryResult(
                action="reconnect_ssh", ok=False, before="", after="",
                error_class="precondition_failed",
                message="RunPod client or tunnel manager unavailable",
            )
        pod_id = self._pod_id()
        if pod_id is None:
            return RecoveryResult(
                action="reconnect_ssh", ok=False, before="", after="",
                error_class="precondition_failed",
                message="RunPod client, tunnel manager, or SSH key unavailable",
            )
        try:
            pod = self.runpod.get_pod(pod_id)
        except RunPodError:
            return RecoveryResult(
                action="reconnect_ssh", ok=False, before="UNKNOWN", after="UNKNOWN",
                error_class="precondition_failed", message="pod state unavailable",
            )
        if not pod.is_running:
            return RecoveryResult(
                action="reconnect_ssh", ok=False, before=pod.status, after=pod.status,
                error_class="precondition_failed", message="pod is not RUNNING",
            )
        endpoint = pod.ssh_tunnel_endpoint()
        if endpoint is None:
            return RecoveryResult(
                action="reconnect_ssh", ok=False, before=pod.status, after=pod.status,
                error_class="precondition_failed", message="no SSH endpoint available",
            )

        tunnel_name, targets = self._tunnel_targets()
        before = (
            "CONNECTED"
            if all(self.tunnels.is_alive(tunnel_name, t) for t in targets)
            else "STOPPED"
        )
        # Ownership-aware port check: if a local port is occupied, allow the
        # reconnect only when the occupant is the launcher's own tunnel
        # (TunnelManager.start() will release it); refuse external occupants.
        if self.is_port_free is not None:
            for target in targets:
                if self.is_port_free(target.local_port):
                    continue
                if not self._owned_tunnel(tunnel_name):
                    return RecoveryResult(
                        action="reconnect_ssh", ok=False, before=before, after=before,
                        error_class="external_conflict",
                        message=(
                            f"local port {target.local_port} is occupied by an "
                            "external process"
                        ),
                    )

        try:
            endpoint_value = from_runpod_endpoint(endpoint)
            if len(targets) == 1:
                self.tunnels.start(
                    tunnel_name, targets[0], endpoint_value,
                    self.config.ssh.key_path, self.config.ssh.connect_timeout,
                )
            else:
                self.tunnels.start_many(
                    tunnel_name, targets, endpoint_value,
                    self.config.ssh.key_path, self.config.ssh.connect_timeout,
                )
            alive = all(
                self.tunnels.wait_alive(tunnel_name, t, timeout=SSH_TIMEOUT)
                for t in targets
            )
        except TunnelError:
            return RecoveryResult(
                action="reconnect_ssh", ok=False, before=before, after="UNKNOWN",
                error_class="action_failed", message="tunnel start failed",
            )
        if not alive:
            return RecoveryResult(
                action="reconnect_ssh", ok=False, before=before, after="UNKNOWN",
                error_class="verification_failed", message="tunnel did not become alive",
            )
        return RecoveryResult(
            action="reconnect_ssh", ok=True, before=before, after="CONNECTED",
            error_class=None, message="SSH tunnel re-established",
        )


    def _owned_tunnel(self, name: str = "comfy") -> bool:
        """True when the *name* tunnel was created by a live launcher process."""
        if self.state is None:
            return False
        entries = self.state.load()
        entry = entries.get(f"tunnels:{name}")
        if entry is None:
            return False
        from . import runtime_state
        return runtime_state.pid_is_alive(entry.pid)

    def _do_restart_comfy(self) -> RecoveryResult:
        """Restore the ComfyUI stack: pod RUNNING -> SSH endpoint -> tunnel.

        Bounded one-shot recovery for the ``comfy`` stack: if the pod is
        stopped it is started (the image pull dominates the wait), the RunPod
        SSH (22/tcp) mapping is resolved, and the local ComfyUI tunnel
        (127.0.0.1:comfy.local_port -> pod:comfy.remote_port) is
        (re)established. The post-condition is a live tunnel; ComfyUI
        application health is probed (when available) and reported, but an
        app still downloading model weights does not fail the recovery.
        """
        if self.config.stack != "comfy":
            return RecoveryResult(
                action="restart_comfy", ok=False, before="", after="",
                error_class="invalid_action",
                message="restart_comfy only applies to the comfy stack",
            )
        if (
            self.runpod is None
            or self.tunnels is None
        ):
            return RecoveryResult(
                action="restart_comfy", ok=False, before="", after="",
                error_class="precondition_failed",
                message="RunPod client or tunnel manager unavailable",
            )
        pod_id = self._pod_id()
        if pod_id is None:
            return RecoveryResult(
                action="restart_comfy", ok=False, before="", after="",
                error_class="precondition_failed",
                message="no pod id available (RUNPOD_POD_ID or the registry)",
            )
        try:
            pod = self.runpod.get_pod(pod_id)
        except RunPodError:
            return RecoveryResult(
                action="restart_comfy", ok=False, before="UNKNOWN", after="UNKNOWN",
                error_class="precondition_failed", message="pod state unavailable",
            )
        # ``before`` is the PRE-action state: pod status plus tunnel state
        # (a stopped pod can never hold a live tunnel).
        tunnel_state = (
            self._comfy_tunnel_state() if pod.is_running else "STOPPED"
        )
        before = f"{pod.status}/{tunnel_state}"
        if not pod.is_running:
            if not pod.is_startable:
                return RecoveryResult(
                    action="restart_comfy", ok=False, before=before, after=before,
                    error_class="precondition_failed",
                    message=f"pod status {pod.status!r} cannot be started",
                )
            try:
                self.runpod.start_pod(pod_id)
                pod = self.runpod.wait_ready(
                    pod_id, timeout=COMFY_POD_TIMEOUT, interval=10.0
                )
            except RunPodError:
                return RecoveryResult(
                    action="restart_comfy", ok=False, before=before, after="UNKNOWN",
                    error_class="action_failed", message="pod start failed",
                )
            if not pod.is_running:
                return RecoveryResult(
                    action="restart_comfy", ok=False, before=before,
                    after=f"{pod.status}/STOPPED",
                    error_class="verification_failed", message="pod did not become RUNNING",
                )

        endpoint = pod.ssh_tunnel_endpoint()
        if endpoint is None:
            wait_endpoint = getattr(self.runpod, "wait_ssh_endpoint", None)
            if wait_endpoint is not None:
                try:
                    pod = wait_endpoint(pod_id, timeout=60.0, interval=5.0)
                    endpoint = pod.ssh_tunnel_endpoint()
                except RunPodError:
                    pass
        if endpoint is None:
            return RecoveryResult(
                action="restart_comfy", ok=False, before=f"{pod.status}/STOPPED",
                after=f"{pod.status}/STOPPED",
                error_class="precondition_failed", message="no SSH endpoint available",
            )

        target = TunnelTarget(
            local_port=self.config.comfy.local_port,
            remote_host="127.0.0.1",
            remote_port=self.config.comfy.remote_port,
        )

        # Ownership-aware port check: an occupied ComfyUI local port may only
        # be reclaimed from the launcher's own tunnel; external occupants
        # are refused (the tunnel manager never kills foreign processes).
        if self.is_port_free is not None and not self.is_port_free(target.local_port):
            if not self._owned_tunnel("comfy"):
                return RecoveryResult(
                    action="restart_comfy", ok=False, before=before, after=before,
                    error_class="external_conflict",
                    message="ComfyUI local port is occupied by an external process",
                )

        try:
            self.tunnels.start(
                "comfy", target, from_runpod_endpoint(endpoint),
                self.config.ssh.key_path, self.config.ssh.connect_timeout,
            )
            alive = self.tunnels.wait_alive("comfy", target, timeout=SSH_TIMEOUT)
        except TunnelError:
            return RecoveryResult(
                action="restart_comfy", ok=False, before=before, after="UNKNOWN",
                error_class="action_failed", message="ComfyUI tunnel start failed",
            )
        if not alive:
            return RecoveryResult(
                action="restart_comfy", ok=False, before=before, after="UNKNOWN",
                error_class="verification_failed", message="ComfyUI tunnel did not become alive",
            )

        healthy = self._comfy_app_health()
        if healthy is True:
            message = "ComfyUI tunnel re-established and the server is healthy"
        elif healthy is False:
            message = (
                "ComfyUI tunnel re-established; the ComfyUI server is not "
                "answering yet (it may still be downloading model weights)"
            )
        else:
            message = "ComfyUI tunnel re-established"
        return RecoveryResult(
            action="restart_comfy", ok=True, before=before,
            after=f"{pod.status}/CONNECTED",
            error_class=None, message=message,
        )

    def _comfy_tunnel_state(self) -> str:
        if self.tunnels is None:
            return "STOPPED"
        target = TunnelTarget(
            local_port=self.config.comfy.local_port,
            remote_host="127.0.0.1",
            remote_port=self.config.comfy.remote_port,
        )
        return "CONNECTED" if self.tunnels.is_alive("comfy", target) else "STOPPED"

    def _comfy_app_health(self) -> Optional[bool]:
        """Probe ComfyUI through the fresh tunnel (None = not probed)."""
        if self.check_comfy_fn is None:
            return None
        try:
            return bool(self.check_comfy_fn(self.config.comfy_base_url()))
        except Exception:
            return False

    # ------------------------------------------------------------------ train
    # ``restart_train`` shares the SHAPE of ``restart_comfy`` — start the pod,
    # resolve the SSH endpoint, re-establish the tunnel, verify — because the
    # sequence really is the same. It is not folded into one helper because the
    # tunnel step differs in kind, not in parameters: the training stack
    # forwards TWO ports through ONE ssh process (see
    # launcher.tunnel.build_tunnel_args_multi), so it calls start_many and
    # checks both ports, where comfy calls start and checks one.

    def _do_restart_train(self) -> RecoveryResult:
        """Restore the training stack: pod RUNNING -> SSH endpoint -> tunnel.

        Bounded one-shot recovery for the ``train`` stack. The post-condition is
        a live two-port tunnel (desktop + file manager); application health is
        probed when available and reported, but a pod still pulling its image or
        downloading weights does not fail the recovery.
        """
        if self.config.stack != "train":
            return RecoveryResult(
                action="restart_train", ok=False, before="", after="",
                error_class="invalid_action",
                message="restart_train only applies to the train stack",
            )
        if (
            self.runpod is None
            or self.tunnels is None
        ):
            return RecoveryResult(
                action="restart_train", ok=False, before="", after="",
                error_class="precondition_failed",
                message="RunPod client or tunnel manager unavailable",
            )
        pod_id = self._pod_id()
        if pod_id is None:
            return RecoveryResult(
                action="restart_train", ok=False, before="", after="",
                error_class="precondition_failed",
                message="no pod id available (RUNPOD_POD_ID or the registry)",
            )
        try:
            pod = self.runpod.get_pod(pod_id)
        except RunPodError:
            return RecoveryResult(
                action="restart_train", ok=False, before="UNKNOWN", after="UNKNOWN",
                error_class="precondition_failed", message="pod state unavailable",
            )
        tunnel_state = self._train_tunnel_state() if pod.is_running else "STOPPED"
        before = f"{pod.status}/{tunnel_state}"
        if not pod.is_running:
            if not pod.is_startable:
                return RecoveryResult(
                    action="restart_train", ok=False, before=before, after=before,
                    error_class="precondition_failed",
                    message=f"pod status {pod.status!r} cannot be started",
                )
            try:
                self.runpod.start_pod(pod_id)
                pod = self.runpod.wait_ready(
                    pod_id, timeout=TRAIN_POD_TIMEOUT, interval=10.0
                )
            except RunPodError:
                return RecoveryResult(
                    action="restart_train", ok=False, before=before, after="UNKNOWN",
                    error_class="action_failed", message="pod start failed",
                )
            if not pod.is_running:
                return RecoveryResult(
                    action="restart_train", ok=False, before=before,
                    after=f"{pod.status}/STOPPED",
                    error_class="verification_failed",
                    message="pod did not become RUNNING",
                )

        endpoint = pod.ssh_tunnel_endpoint()
        if endpoint is None:
            wait_endpoint = getattr(self.runpod, "wait_ssh_endpoint", None)
            if wait_endpoint is not None:
                try:
                    pod = wait_endpoint(pod_id, timeout=60.0, interval=5.0)
                    endpoint = pod.ssh_tunnel_endpoint()
                except RunPodError:
                    pass
        if endpoint is None:
            return RecoveryResult(
                action="restart_train", ok=False, before=f"{pod.status}/STOPPED",
                after=f"{pod.status}/STOPPED",
                error_class="precondition_failed",
                message=(
                    "no SSH endpoint available — the training image only starts "
                    "sshd when the RunPod template enables SSH access"
                ),
            )

        targets = [
            TunnelTarget(
                local_port=self.config.train.local_port,
                remote_host="127.0.0.1",
                remote_port=self.config.train.remote_port,
            ),
            TunnelTarget(
                local_port=self.config.train.files_local_port,
                remote_host="127.0.0.1",
                remote_port=self.config.train.files_remote_port,
            ),
        ]

        # Ownership-aware port check on BOTH ports: an occupied local port may
        # only be reclaimed from the launcher's own tunnel, and either one being
        # held externally makes ssh abort the whole forward.
        if self.is_port_free is not None:
            for target in targets:
                if not self.is_port_free(target.local_port):
                    if not self._owned_tunnel("train"):
                        return RecoveryResult(
                            action="restart_train", ok=False, before=before,
                            after=before,
                            error_class="external_conflict",
                            message=(
                                f"training local port {target.local_port} is "
                                "occupied by an external process"
                            ),
                        )

        try:
            self.tunnels.start_many(
                "train", targets, from_runpod_endpoint(endpoint),
                self.config.ssh.key_path, self.config.ssh.connect_timeout,
            )
            # No target argument: every forwarded port must answer.
            alive = self.tunnels.wait_alive("train", timeout=SSH_TIMEOUT)
        except TunnelError:
            return RecoveryResult(
                action="restart_train", ok=False, before=before, after="UNKNOWN",
                error_class="action_failed", message="training tunnel start failed",
            )
        if not alive:
            return RecoveryResult(
                action="restart_train", ok=False, before=before, after="UNKNOWN",
                error_class="verification_failed",
                message="training tunnel did not become alive on both ports",
            )

        healthy = self._train_app_health()
        if healthy is True:
            message = "Training tunnel re-established and the desktop is answering"
        elif healthy is False:
            message = (
                "Training tunnel re-established; the desktop is not answering yet "
                "(the pod may still be pulling its image or downloading weights)"
            )
        else:
            message = "Training tunnel re-established"
        return RecoveryResult(
            action="restart_train", ok=True, before=before,
            after=f"{pod.status}/CONNECTED",
            error_class=None, message=message,
        )

    def _train_tunnel_state(self) -> str:
        if self.tunnels is None:
            return "STOPPED"
        # No target argument: the tunnel is only useful when BOTH ports work.
        return "CONNECTED" if self.tunnels.is_alive("train") else "STOPPED"

    def _train_app_health(self) -> Optional[bool]:
        """Probe the training desktop through the fresh tunnel (None = not probed).

        Uses the train probe (ANY HTTP answer is healthy — KasmVNC answers
        401 to an anonymous request), never the ComfyUI one.
        """
        if self.check_train_fn is None:
            return None
        try:
            return bool(self.check_train_fn(self.config.train_base_url()))
        except Exception:
            return False

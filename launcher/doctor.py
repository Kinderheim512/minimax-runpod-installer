"""Non-destructive diagnostics for the MiniMax H3 Launcher (``doctor``).

Runs a series of local, bounded checks and returns a structured report. Each
check degrades to a clear status (``OK``/``WARN``/``ERROR``/``SKIP``) with an
actionable detail string, never raising for a single failed check.

This module reuses the existing primitives (config, runpod, tunnel, health)
and does not start anything, mutate state, or consume RunPod credits.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

from . import runtime_state
from .config import Config, ConfigError
from .health import HealthError, check_comfy, check_train, get_comfy_stats
from .runpod import RunPodClient, RunPodError, TemplateNotFoundError
from .tunnel import is_port_free


@dataclass(frozen=True)
class Diagnostic:
    """Outcome of a single diagnostic check."""

    name: str
    status: str  # OK | WARN | ERROR | SKIP
    detail: str


def run_diagnostics(
    config: Config,
    runpod: Optional[RunPodClient] = None,
    base_url: Optional[str] = None,
    credentials=None,
    registry=None,
) -> list[Diagnostic]:
    """Run all local diagnostics and return the ordered results.

    Parameters
    ----------
    config:
        Loaded configuration.
    runpod:
        Optional RunPod client. If None, RunPod-dependent checks are ``SKIP``.
    base_url:
        Optional service base URL for health checks. Defaults to the active
        stack's local URL.
    credentials:
        Optional credential store (anything with a ``status()`` method
        returning a value-free status). If None, the credential check is
        ``SKIP``. Only value-free state is ever reported.
    registry:
        Optional pod registry. When ``RUNPOD_POD_ID`` is not set, the
        registered pod (created by this launcher on this machine) is used.
    """
    results: list[Diagnostic] = []

    # 1. Configuration is always available (already validated by caller).
    results.append(Diagnostic("configuration", "OK", "Configuration loaded and validated"))

    # 1b. Selected workload verification status.
    if config.stack == "comfy":
        results.append(
            Diagnostic(
                "preset",
                "OK",
                f"ComfyUI preset {config.comfy.preset!r} (tier {config.comfy.tier})",
            )
        )
    elif config.stack == "train":
        results.append(
            Diagnostic(
                "train",
                "OK",
                f"LoRA training image {config.train.image_name!r} "
                f"(desktop {config.train_base_url()}, "
                f"files {config.train_files_url()})",
            )
        )

    # 2. OpenSSH client presence: the whole tunnel stack is built on it.
    ssh_binary = shutil.which("ssh")
    if ssh_binary is None:
        results.append(
            Diagnostic(
                "openssh",
                "ERROR",
                "The OpenSSH client ('ssh') is not on PATH. Install it "
                "(Windows: 'Add an optional feature' > OpenSSH Client; "
                "macOS/Linux: your package manager) and retry.",
            )
        )
    else:
        results.append(Diagnostic("openssh", "OK", f"OpenSSH client at {ssh_binary}"))

    # 2b. SSH key presence.
    key = config.ssh.key_path
    if not key:
        results.append(Diagnostic("ssh_key", "WARN", "SSH_KEY_PATH is not set"))
    else:
        import os

        results.append(
            Diagnostic(
                "ssh_key",
                "OK" if os.path.isfile(key) else "ERROR",
                f"SSH key {key}" + (" exists" if os.path.isfile(key) else " not found"),
            )
        )

    # 3. RunPod API + pod (environment first, then the local pod registry
    # record of the active stack).
    pod_id = config.runpod.pod_id
    if not pod_id and registry is not None:
        try:
            record = registry.load(config.stack)
        except Exception:
            record = None
        if record is not None:
            pod_id = record.pod_id

    if runpod is None:
        results.append(Diagnostic("runpod", "SKIP", "RunPod client not provided"))
    elif not pod_id:
        results.append(
            Diagnostic(
                "runpod",
                "WARN",
                "RUNPOD_POD_ID is not set and no registered pod exists",
            )
        )
    else:
        try:
            pod = runpod.get_pod(pod_id)
            results.append(Diagnostic("runpod", "OK", f"Pod {pod.id} status {pod.status}"))
            if pod.ssh_tunnel_endpoint() is None:
                results.append(
                    Diagnostic("ssh_endpoint", "ERROR", "No usable SSH endpoint (direct/TCP 22)")
                )
            else:
                results.append(Diagnostic("ssh_endpoint", "OK", "SSH endpoint available"))
        except RunPodError as exc:
            results.append(Diagnostic("runpod", "ERROR", str(exc)))

    # 3b. Secure credential store (value-free state only, never values).
    if credentials is None:
        results.append(Diagnostic("runpod_credentials", "SKIP", "credential store not provided"))
    else:
        try:
            cred_status = credentials.status()
        except Exception:
            results.append(Diagnostic("runpod_credentials", "ERROR", "credential store unavailable"))
        else:
            if not cred_status.file_present:
                if config.secrets.runpod_api_key:
                    results.append(
                        Diagnostic(
                            "runpod_credentials",
                            "SKIP",
                            "provided via environment",
                        )
                    )
                else:
                    results.append(
                        Diagnostic(
                            "runpod_credentials",
                            "WARN",
                            "RunPod credentials are not stored (run 'minimax-launcher credentials set')",
                        )
                    )
            elif not cred_status.readable:
                results.append(
                    Diagnostic(
                        "runpod_credentials",
                        "ERROR",
                        "stored RunPod credentials are unreadable (run 'minimax-launcher credentials set')",
                    )
                )
            else:
                results.append(
                    Diagnostic(
                        "runpod_credentials",
                        "OK",
                        "RunPod credentials and template configured",
                    )
                )

    # 3c. Provisioning catalog (account-level, advisory — never reserves GPU).
    if runpod is None:
        for name in ("pod_template", "ssh_keys", "gpu_availability"):
            results.append(Diagnostic(name, "SKIP", "RunPod client not provided"))
    else:
        results.append(_template_check(config, runpod))
        results.append(_ssh_keys_check(runpod))
        results.append(_gpu_availability_check(config, runpod))

    # 4. Local port availability (the active stack's local port).
    if config.stack == "comfy":
        local_port = config.comfy.local_port
        port_label = "ComfyUI"
        results.append(
            Diagnostic(
                "local_port",
                "OK" if is_port_free(local_port) else "WARN",
                f"Local {port_label} port {local_port} "
                + ("free" if is_port_free(local_port) else "occupied"),
            )
        )
    elif config.stack == "train":
        # TWO ports for this stack, and either one being occupied makes
        # ssh abort the whole forward (ExitOnForwardFailure) — so both are
        # reported rather than just the first.
        for _port, _label in (
            (config.train.local_port, "training desktop"),
            (config.train.files_local_port, "training file manager"),
        ):
            _free = is_port_free(_port)
            results.append(
                Diagnostic(
                    f"local_port_{_port}",
                    "OK" if _free else "WARN",
                    f"Local {_label} port {_port} "
                    + ("free" if _free else "occupied"),
                )
            )
    # 5. Serving health: ComfyUI (comfy) or the training desktop (train).
    if config.stack == "comfy":
        base = base_url or config.comfy_base_url()
        try:
            if check_comfy(base):
                stats = get_comfy_stats(base)
                vram = stats.get("devices", [{}])[0].get("name") if stats.get("devices") else None
                results.append(Diagnostic("comfyui", "OK", f"ComfyUI /system_stats healthy" + (f" ({vram})" if vram else "")))
            else:
                results.append(Diagnostic("comfyui", "WARN", "ComfyUI /system_stats not answering (pod still starting or not tunneled)"))
        except HealthError as exc:
            results.append(Diagnostic("comfyui", "WARN", str(exc)))
    elif config.stack == "train":
        # Both ports are probed: a tunnel forwarding two ports is only
        # useful when both work, and 'the desktop answers but the file
        # manager does not' is a state worth seeing explicitly.
        for _url, _name, _label in (
            (config.train_base_url(), "train_desktop", "Training desktop"),
            (config.train_files_url(), "train_files", "Training file manager"),
        ):
            try:
                if check_train(_url):
                    results.append(
                        Diagnostic(_name, "OK", f"{_label} answers at {_url}")
                    )
                else:
                    results.append(
                        Diagnostic(
                            _name,
                            "WARN",
                            f"{_label} not answering at {_url} "
                            "(pod still booting/downloading, or not tunneled)",
                        )
                    )
            except HealthError as exc:
                results.append(Diagnostic(_name, "WARN", str(exc)))

    return results


def _template_check(config: Config, runpod: RunPodClient) -> Diagnostic:
    """Verify the active stack's configured template still exists (advisory)."""
    if config.stack == "comfy":
        template_id = config.secrets.comfy_template_id
        env_name = "RUNPOD_COMFY_TEMPLATE_ID"
    elif config.stack == "train":
        template_id = config.secrets.train_template_id
        env_name = "RUNPOD_TRAIN_TEMPLATE_ID"
    else:
        template_id = config.secrets.runpod_template_id
        env_name = "RUNPOD_TEMPLATE_ID"
    if not template_id:
        return Diagnostic("pod_template", "SKIP", f"{env_name} is not set")
    try:
        runpod.get_template(template_id)
    except TemplateNotFoundError as exc:
        return Diagnostic("pod_template", "ERROR", str(exc))
    except RunPodError as exc:
        return Diagnostic("pod_template", "SKIP", f"template check unavailable: {exc}")
    return Diagnostic("pod_template", "OK", f"Template is available ({env_name})")


def _ssh_keys_check(runpod: RunPodClient) -> Diagnostic:
    """Verify the account has at least one registered SSH key."""
    try:
        keys = runpod.list_ssh_keys()
    except RunPodError as exc:
        return Diagnostic("ssh_keys", "SKIP", f"SSH key check unavailable: {exc}")
    if not keys:
        return Diagnostic(
            "ssh_keys",
            "WARN",
            "no SSH keys registered on the RunPod account "
            "(run 'runpod ssh-key add <key>' to register one)",
        )
    return Diagnostic("ssh_keys", "OK", f"{len(keys)} SSH key(s) registered")


def _gpu_availability_check(config: Config, runpod: RunPodClient) -> Diagnostic:
    """Report catalog availability for the configured GPU (advisory only)."""
    gpu_id = config.runpod.gpu_id
    try:
        gpus = runpod.get_gpu_types()
    except RunPodError as exc:
        return Diagnostic("gpu_availability", "SKIP", f"GPU catalog unavailable: {exc}")
    gpu = None
    for item in gpus:
        if isinstance(item, Mapping) and item.get("id") == gpu_id:
            gpu = item
            break
    if gpu is None:
        return Diagnostic(
            "gpu_availability",
            "WARN",
            f"GPU {gpu_id} not found in the RunPod catalog (advisory only)",
        )
    availability = gpu.get("availability")
    if availability is None:
        return Diagnostic(
            "gpu_availability",
            "WARN",
            f"GPU {gpu_id} availability not reported (advisory only)",
        )
    if isinstance(availability, Mapping):
        values = [str(v) for v in availability.values()]
    else:
        values = [str(availability)]
    if all(v == "NONE" for v in values):
        return Diagnostic(
            "gpu_availability",
            "WARN",
            f"GPU {gpu_id} has no current availability (advisory only)",
        )
    return Diagnostic("gpu_availability", "OK", f"GPU {gpu_id} available (advisory only)")

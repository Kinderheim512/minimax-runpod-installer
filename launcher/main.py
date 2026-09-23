"""Entry point for the MiniMax H3 Launcher.

Provides CLI subcommands backed by the M3/M4 orchestrator:

* ``start``       — run the full startup sequence (RunPod -> SSH tunnel -> the
                    stack's service).
* ``stop``        — clean shutdown (tunnels + the registered RunPod pod;
                    ``--terminate`` deletes the pod instead of stopping it).
* ``status``      — concise operational status.
* ``doctor``      — non-destructive local diagnostics.
* ``credentials`` — manage the secure credential store (``set`` /
  ``status`` / ``clear``; bare ``credentials`` reports status).
* ``gui``         — launch the Windows GUI (tkinter) front-end.

Ctrl+C during ``start`` cleans up launcher-owned resources via the orchestrator
cleanup path (terminates only RuntimeState-recorded PIDs).
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Optional

from . import comfy_ops
from . import credentials
from . import logging as launcher_logging
from . import orchestrator
from .config import (
    COMFY_ACCESS_MODES,
    COMFY_SAGE_MODES,
    COMFY_TIERS,
    DEFAULT_STACK,
    ConfigError,
    STACKS,
    load_config,
)
from .doctor import run_diagnostics
from .health import check_comfy, check_train
from .pod_registry import PodRegistry
from .runpod import RunPodClient
from .tunnel import TunnelManager

logger = launcher_logging.get_logger("minimax-launcher")


def _load_config(
    stack: str | None = None,
    comfy: dict | None = None,
    train: dict | None = None,
    gpu: str | None = None,
) -> tuple:
    try:
        return (
            load_config(
                stack=stack, comfy=comfy, train=train, gpu=gpu,
                store=credentials.CredentialStore(),
            ),
            None,
        )
    except ConfigError as exc:
        return None, exc


def _credentials_state() -> str:
    """Value-free credential store state for status output."""
    try:
        st = credentials.CredentialStore().status()
    except credentials.CredentialStoreError:
        return "UNKNOWN"
    if not st.file_present:
        return "NOT CONFIGURED"
    return "CONFIGURED" if st.readable else "CORRUPT"


def _status(all_stacks: bool = False) -> int:
    launcher_logging.setup_logging()
    if all_stacks:
        return _status_all()
    config, err = _load_config()
    if err is not None:
        logger.error("Configuration is invalid: %s", err)
        return 1

    logger.info("Loading configuration")
    logger.ok("Configuration validated")

    # Rich operational summary (bounded live checks).
    runpod = None
    if config.secrets.runpod_api_key:
        runpod = RunPodClient(config.secrets.runpod_api_key)
    summary = orchestrator.operational_status(
        config=config,
        runpod=runpod,
        check_comfy_fn=check_comfy,
        check_train_fn=check_train,
        registry=PodRegistry(),
    )

    if config.stack == "comfy":
        serving, model_label, profile_label = "ComfyUI", "Preset", "Tier"
        access_label = "Access"
        access_value = config.comfy.access
        model_value = summary["model"]
        extra_rows: list[tuple[str, str]] = []
    else:
        # The training pod serves a desktop + a file manager, not an LLM:
        # naming the Fizgig/KasmVNC surfaces is what the operator sees in the
        # GUI too (docs/train-stack.md).
        serving, model_label, profile_label = "Desktop", "Image", "Tag"
        model_value = summary["model"]
        extra_rows = []
        access_label, access_value = "Files", config.train_files_url()

    print("")
    print(f"MiniMax H3 Launcher Status ({config.stack} stack)")
    print("--------------------")
    print(f"RunPod       {summary['runpod']}")
    print(f"Pod          {summary['pod']}")
    print(f"RunPod creds {_credentials_state()}")
    print(f"SSH tunnel   {summary['tunnel']}")
    print(f"{serving:<11} {summary['vllm']}")
    print(f"{model_label:<11} {model_value}")
    for extra_label, extra_value in extra_rows:
        print(f"{extra_label:<11} {extra_value}")
    print(f"{profile_label:<11} {summary['profile']}")
    print(f"{access_label:<11} {access_value}")
    print("--------------------")
    print(summary["url"])
    print("")
    return 0


#: Short label of what each stack serves, for the `status --all` table.
_STACK_SERVING_LABEL = {
    "comfy": "ComfyUI",
    "train": "Desktop",
}


def _status_all() -> int:
    """Report every active workload stack at once (concurrent pods).

    A stack is listed when the registry holds a pod record for it or one of
    its local processes is recorded and alive (see
    :func:`launcher.orchestrator.active_stacks`). Never fails on a single
    stack: a stack whose config or probe breaks is shown as degraded.
    """
    registry = PodRegistry()
    stacks = orchestrator.active_stacks(registry=registry)
    if not stacks:
        print("")
        print("MiniMax H3 Launcher Status (no active stack)")
        print("--------------------")
        print("No pod registered and no local process running.")
        print("")
        return 0

    configs: dict = {}
    for stack in stacks:
        config, err = _load_config(stack=stack)
        if config is not None:
            configs[stack] = config
        else:
            logger.warning("Invalid configuration for the %s stack: %s", stack, err)

    runpod = None
    api_key = next(
        (c.secrets.runpod_api_key for c in configs.values() if c.secrets.runpod_api_key),
        None,
    )
    if api_key:
        runpod = RunPodClient(api_key)

    summaries = orchestrator.operational_status_all(
        configs,
        runpod=runpod,
        check_comfy_fn=check_comfy,
        check_train_fn=check_train,
        registry=registry,
    )

    print("")
    print(f"MiniMax H3 Launcher Status — {len(stacks)} active stack(s)")
    print("--------------------")
    header = f"{'Stack':<10}{'Serves':<11}{'Pod':<18}{'RunPod':<11}{'Tunnel':<11}{'Ready':<11}"
    print(header)
    for stack in stacks:
        summary = summaries.get(stack)
        if summary is None:
            print(f"{stack:<10}{'—':<11}{'—':<18}{'—':<11}{'—':<11}{'—':<11}")
            continue
        print(
            f"{stack:<10}{_STACK_SERVING_LABEL.get(stack, '—'):<11}"
            f"{(summary.get('pod') or 'NONE'):<18}"
            f"{(summary.get('runpod') or '—'):<11}"
            f"{(summary.get('tunnel') or '—'):<11}"
            f"{(summary.get('vllm') or '—'):<11}"
        )
    print("--------------------")
    for stack in stacks:
        summary = summaries.get(stack)
        if summary is None:
            continue
        print(f"{stack:<10} {summary.get('url') or '—'}")
    print("")
    return 0


def _comfy_overrides(args) -> dict | None:
    """Explicit ComfyUI values from the CLI (only the ones actually given)."""
    overrides = {}
    if getattr(args, "preset", None):
        overrides["preset"] = args.preset
    if getattr(args, "tier", None):
        overrides["tier"] = args.tier
    if getattr(args, "workflows", None):
        overrides["workflows"] = args.workflows
    if getattr(args, "access", None):
        overrides["access"] = args.access
    if getattr(args, "sage_attention", None):
        overrides["sage_attention"] = args.sage_attention
    if getattr(args, "no_turbo_lora", False):
        overrides["turbo_lora"] = False
    if getattr(args, "no_spectrum", False):
        overrides["spectrum"] = False
    return overrides or None


def _gui_stack_overrides(stack: str) -> tuple[dict, str | None]:
    """Per-stack launch values persisted by the GUI (``gui.json``).

    Returns ``(overrides, gpu)``. Degrades to ``({}, None)`` when the GUI
    settings are unreadable — a missing ``gui.json`` is normal on a
    CLI-only install and must never fail a start.
    """
    try:
        from .gui import load_settings, stack_overrides_from_settings

        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - never fail a start over settings
        logger.warning("gui.json unreadable (%s): GUI values ignored", exc)
        return {}, None
    return (
        stack_overrides_from_settings(settings, stack),
        (settings.runpod_gpu or None),
    )


def _start(
    stack: str | None = None,
    comfy: dict | None = None,
    train: dict | None = None,
    gpu: str | None = None,
) -> int:
    launcher_logging.setup_logging()
    config, err = _load_config(
        stack=stack, comfy=comfy, train=train, gpu=gpu,
    )
    if err is not None:
        logger.error("Configuration is invalid: %s", err)
        return 1
    try:
        orchestrator.start(config)
    except KeyboardInterrupt:
        logger.warn("Interrupted — cleaning up launcher-owned resources")
        orchestrator.cleanup()
        return 130
    except orchestrator.OrchestrationError as exc:
        if orchestrator.is_pod_unavailable_error(str(exc)):
            # Pod unavailability is transient and already narrated in the
            # journal during the retries: keep the final failure discreet.
            logger.warning(
                "Pod is currently unavailable (no free GPU at RunPod) — start "
                "aborted. Re-run 'start' when capacity is back."
            )
        else:
            logger.error("Startup failed: %s", exc)
        return 1
    return 0


_GOOD_STOP_OUTCOMES = ("stopped", "terminated", "cleared", "already_stopped")


def _stop(terminate: bool = False, stack: str | None = None) -> int:
    launcher_logging.setup_logging()
    # Load the config of the stack being stopped: the RUNPOD_POD_ID guard in
    # ``orchestrator.stop`` compares ``stack`` with ``config.stack``, and the
    # default stack made ``stop --stack comfy --terminate`` skip the guard —
    # terminating a pod the launcher promises never to terminate.
    config, err = _load_config(stack=stack or DEFAULT_STACK)
    if err is not None:
        # A local-only stop does not need a valid config, but terminating
        # the pod does (the RUNPOD_POD_ID guard needs it).
        if terminate:
            logger.error("Configuration is invalid: %s", err)
            return 1
        config = None
    runpod = None
    if config is not None and config.secrets.runpod_api_key:
        runpod = RunPodClient(config.secrets.runpod_api_key)
    try:
        phase = orchestrator.stop(
            runpod=runpod,
            registry=PodRegistry(),
            config=config,
            terminate=terminate,
            stack=stack,
        )
    except orchestrator.OrchestrationError as exc:
        logger.error("Shutdown failed: %s", exc)
        return 1
    ok = True
    if phase:
        for name, outcome in phase.items():
            # ``unavailable`` (the API could not be queried) and ``skipped``
            # (no stop action) both leave a GPU pod that may still be billing:
            # reporting them as OK made a failed shutdown indistinguishable
            # from a successful one, and the command still exited 0.
            if outcome in _GOOD_STOP_OUTCOMES:
                logger.ok("RunPod %s pod: %s", name, outcome)
            else:
                logger.warning("RunPod %s pod: %s", name, outcome)
                ok = False
    if not ok:
        logger.warning(
            "Shutdown incomplete: a pod may still be running and billing "
            "(check the RunPod console)."
        )
        return 1
    logger.ok("Shutdown complete")
    return 0


def _doctor() -> int:
    launcher_logging.setup_logging()
    config, err = _load_config()
    if err is not None:
        logger.error("Configuration is invalid: %s", err)
        return 1

    runpod = None
    if config.secrets.runpod_api_key:
        runpod = RunPodClient(config.secrets.runpod_api_key)

    results = run_diagnostics(
        config,
        runpod=runpod,
        credentials=credentials.CredentialStore(),
        registry=PodRegistry(),
    )
    print("")
    print("MiniMax H3 Launcher Diagnostics")
    print("-------------------------")
    for r in results:
        print(f"[{r.status:<5}] {r.name:<20} {r.detail}")
    print("")
    # A doctor run that finds ERRORs must not exit 0: `minimax-launcher doctor &&
    # start`-style automation would otherwise proceed on a broken install.
    return 1 if any(r.status == "ERROR" for r in results) else 0


def _gui() -> int:
    from . import gui

    return gui.run_gui()


def _credentials(action: str) -> int:
    launcher_logging.setup_logging()
    if action == "set":
        return _credentials_set()
    if action == "clear":
        return _credentials_clear()
    return _credentials_status()


def _credentials_set() -> int:
    prompts = (
        "RunPod API key: ",
        "Private RunPod template ID: ",
        "ComfyUI/MiniMax H3 template ID (optional, empty keeps existing): ",
        "LoRA training template ID (optional, empty keeps existing): ",
        "Hugging Face token (optional, empty keeps existing): ",
        "Civitai API key (optional, empty keeps existing): ",
        "Training desktop password, 12+ chars (optional, empty keeps existing): ",
    )
    try:
        answers = [getpass.getpass(prompt) for prompt in prompts]
    except (EOFError, KeyboardInterrupt):
        logger.error("Credential input was interrupted")
        return 1
    (
        api_key,
        template_id,
        comfy_template_id,
        train_template_id,
        hf_token,
        civitai_key,
        vnc_password,
    ) = answers
    # Empty answer means "keep the existing stored value" (None = preserve).
    comfy_id: Optional[str] = comfy_template_id.strip() or None
    train_id: Optional[str] = train_template_id.strip() or None
    vnc_pw: Optional[str] = vnc_password.strip() or None
    if vnc_pw is not None and len(vnc_pw) < 12:
        logger.error(
            "The training desktop password must be at least 12 characters "
            "(filebrowser refuses shorter ones)."
        )
        return 1
    store = credentials.CredentialStore()
    existing_extra: dict[str, str] = {}
    try:
        existing_extra = dict(store.get().extra)
    except credentials.CredentialStoreError:
        pass
    extra = dict(existing_extra)
    for key, raw in (("HF_TOKEN", hf_token), ("CIVITAI_API_KEY", civitai_key)):
        value = raw.strip()
        if value:
            extra[key] = value
    try:
        store.set(
            api_key,
            template_id,
            extra=extra,
            comfy_template_id=comfy_id,
            train_template_id=train_id,
            vnc_password=vnc_pw,
        )
    except credentials.CredentialStoreError as exc:
        logger.error(str(exc))
        return 1
    logger.ok("RunPod credentials stored")
    return 0


def _credentials_status() -> int:
    from .config import EXTRA_SECRET_KEYS

    store = credentials.CredentialStore()
    try:
        st = store.status()
    except credentials.CredentialStoreError:
        logger.error("The secure credential store is not available on this platform")
        return 1
    state = "NOT CONFIGURED"
    if st.file_present:
        state = "CONFIGURED" if st.readable else "CORRUPT"
    comfy_state = "ABSENT"
    if st.file_present and st.readable:
        comfy_state = "SET" if st.comfy_template_configured else "ABSENT"
    train_state = "ABSENT"
    if st.file_present and st.readable:
        train_state = "SET" if st.train_template_configured else "ABSENT"
    desktop_state = "ABSENT"
    if st.file_present and st.readable:
        desktop_state = "SET" if st.vnc_password_configured else "ABSENT"
    print("")
    print("RunPod credentials  " + state)
    # ``template_configured`` is the RunPod template, distinct from the
    # per-stack ones below — this line used to repeat ``state`` verbatim.
    print(
        "RunPod template     "
        + ("SET" if (st.file_present and st.readable and st.template_configured) else "ABSENT")
    )
    print(f"ComfyUI template    {comfy_state}")
    print(f"train template      {train_state}")
    print(f"desktop password    {desktop_state}")
    if st.file_present and st.readable:
        for key in sorted(set(EXTRA_SECRET_KEYS) | set(st.extra_keys)):
            mark = "SET" if key in st.extra_keys else "ABSENT"
            print(f"{key:<20} {mark}")
    print("")
    return 0


def _credentials_clear() -> int:
    store = credentials.CredentialStore()
    try:
        removed = store.clear()
    except credentials.CredentialStoreError as exc:
        logger.error(str(exc))
        return 1
    if removed:
        logger.ok("Stored RunPod credentials removed")
    else:
        logger.info("No stored RunPod credentials to remove")
    return 0


def _comfy_ops() -> tuple:
    """(config, ComfyOps) bound to the registered comfy pod's SSH endpoint."""
    config, err = _load_config(stack="comfy")
    if err is not None:
        raise orchestrator.OrchestrationError(f"Configuration is invalid: {err}")
    ops = comfy_ops.build_comfy_ops(config)
    return config, ops


def _comfy_version_report(config, ops) -> str:
    """Read the pod's ComfyUI state and name every divergence."""
    from . import comfy_version

    state = comfy_version.read_state(ops)
    expected = comfy_version.expected_target(
        config.comfy.comfyui_version or "",
        latest_release=_latest_comfyui_release(),
    )
    return comfy_version.diagnose(
        state, expected, pinned_version=config.comfy.comfyui_version or ""
    ).render()


def _latest_comfyui_release() -> str:
    """Newest stable ComfyUI release, or "" when GitHub is unreachable.

    Only used to phrase the expected target when nothing is pinned — the pod
    resolves it itself at boot, so a failure here must stay harmless.
    """
    try:
        from . import gui as gui_module

        releases = gui_module.fetch_comfyui_releases()
        return releases[0] if releases else ""
    except Exception:  # noqa: BLE001 - advisory only
        return ""


def _comfy_repair(config, ops, apply: bool = False) -> str:
    """Verify the pod's ComfyUI, and fix it with ``--apply``.

    Without ``--apply`` this is a pure read: it prints exactly what is wrong
    and what would be run. With it, the marker stops dirtying its own work
    tree, the requested tag is checked out, and the ``comfyui-manager``
    package is installed from the version this ComfyUI core pins.
    """
    from . import comfy_version

    state = comfy_version.read_state(ops)
    expected = comfy_version.expected_target(
        config.comfy.comfyui_version or "",
        latest_release=_latest_comfyui_release(),
    )
    report = comfy_version.diagnose(
        state, expected, pinned_version=config.comfy.comfyui_version or ""
    )
    lines = [report.render()]

    if report.ok:
        return "\n".join(lines)

    if not apply:
        lines.append("")
        lines.append("Re-run with `--apply` to fix them.")
        return "\n".join(lines)

    steps = comfy_version.repair(ops, state, target=expected)
    lines.append("")
    lines.append("Fixes applied:")
    lines.extend(f"  - {step}" for step in steps)
    after = comfy_version.read_state(ops)
    lines.append("")
    lines.append("After the fix:")
    lines.append(
        comfy_version.diagnose(
            after, expected, pinned_version=config.comfy.comfyui_version or ""
        ).render()
    )
    lines.append("")
    lines.append(
        "ComfyUI is PID 1 in the image: restart the comfy stack "
        "(`minimax-launcher stop --stack comfy` then `start --stack comfy`) for "
        "the version and the manager to take effect."
    )
    return "\n".join(lines)


def _comfy(action: str, args) -> int:
    launcher_logging.setup_logging()
    try:
        if action == "lora-install":
            _, ops = _comfy_ops()
            out = ops.install_lora(
                args.url, personal=args.personal, force=args.force,
                filename=args.filename,
            )
        elif action == "lora-list":
            _, ops = _comfy_ops()
            out = ops.list_loras(personal=args.personal)
        elif action == "lora-remove":
            _, ops = _comfy_ops()
            out = ops.remove_lora(args.name, personal=args.personal)
        elif action == "vault":
            _, ops = _comfy_ops()
            out = ops.sync_vault()
        elif action == "repair":
            config, ops = _comfy_ops()
            out = _comfy_repair(config, ops, apply=args.apply)
        elif action == "version":
            config, ops = _comfy_ops()
            out = _comfy_version_report(config, ops)
        else:  # outputs
            config, ops = _comfy_ops()
            local_dir = Path(args.dir).expanduser() if args.dir else (
                Path.home() / "Downloads" / "comfy-outputs"
            )
            # Recursive listing: the H3 templates pin nested prefixes such as
            # "video/<date>/<time>", so a root-only `ls -1` returned the
            # *directory* "video" — and `/view?filename=video` answers 404.
            files = ops.list_outputs(subfolders=True)
            if args.name:
                out = _download_one_output(ops, args.name, local_dir)
            elif args.all:
                if not files:
                    out = "(no outputs)"
                else:
                    written = []
                    failures = []
                    for entry in files:
                        try:
                            written.append(str(_download_output(ops, entry, local_dir)))
                        except comfy_ops.ComfyOpsError as exc:
                            failures.append(f"{entry}: {exc}")
                    lines = [f"{len(written)} file(s) downloaded to {local_dir}"]
                    lines.extend(written)
                    if failures:
                        lines.append(f"{len(failures)} failure(s):")
                        lines.extend(failures)
                    out = "\n".join(lines)
            else:
                out = "\n".join(files) if files else "(no outputs)"
    except (orchestrator.OrchestrationError, comfy_ops.ComfyOpsError) as exc:
        logger.error(str(exc))
        return 1
    if out:
        print(out)
    return 0


def _download_output(ops, entry: str, local_dir: Path) -> Path:
    """Download one pod-relative output path (``video/x.mp4``)."""
    subfolder, _, filename = entry.rpartition("/")
    return ops.download_output(
        filename, local_dir, subfolder=subfolder, mirror_subfolder=True
    )


def _download_one_output(ops, name: str, local_dir: Path) -> str:
    """Download the output named *name* (a path, or a bare file name)."""
    known = ops.list_outputs(subfolders=True)
    match = next(
        (
            entry
            for entry in known
            if entry == name or entry.rpartition("/")[2] == name
        ),
        None,
    )
    if match is None:
        raise comfy_ops.ComfyOpsError(
            f"{name!r} is not one of the {len(known)} outputs on the pod "
            "(run `minimax-launcher comfy outputs` to list them)"
        )
    return f"Downloaded: {_download_output(ops, match, local_dir)}"


def _package_version() -> str:
    """The launcher's version, from the package (``unknown`` if unreadable)."""
    try:
        from . import __version__

        return __version__
    except Exception:  # noqa: BLE001 - a version string is never fatal
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch to the requested subcommand.

    When called programmatically with no explicit ``argv``, defaults to the
    ``status`` behavior (backward compatible with the M0 entry point).
    """
    if argv is None:
        return _status()

    parser = argparse.ArgumentParser(prog="minimax-launcher")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    sub = parser.add_subparsers(dest="command")

    start_parser = sub.add_parser(
        "start", help="Start the launcher for a workload stack (comfy or train)"
    )
    start_parser.add_argument(
        "--stack",
        choices=tuple(STACKS),
        default=None,
        help="Workload stack (default: LAUNCHER_STACK or comfy)",
    )
    start_parser.add_argument(
        "--gui-settings",
        action="store_true",
        dest="gui_settings",
        help=(
            "Merge the values persisted by the GUI (gui.json) for the selected "
            "stack — preset, tier, library, GPU — so a non-GUI start launches "
            "exactly what the Start button would have launched"
        ),
    )
    start_parser.add_argument(
        "--preset",
        default=None,
        help="ComfyUI preset (comfy stack; default: dasiwa_mmh3v12)",
    )
    start_parser.add_argument(
        "--tier",
        choices=tuple(COMFY_TIERS),
        default=None,
        help="ComfyUI model tier (comfy stack; default: auto)",
    )
    start_parser.add_argument(
        "--workflows",
        default=None,
        help="ComfyUI workflows: 'all' or a comma-separated subset of t2v,i2v,r2v",
    )
    start_parser.add_argument(
        "--access",
        choices=tuple(COMFY_ACCESS_MODES),
        default=None,
        help="ComfyUI access mode: tunnel (default) or direct (public URL)",
    )
    start_parser.add_argument(
        "--sage-attention",
        dest="sage_attention",
        choices=tuple(COMFY_SAGE_MODES),
        default=None,
        help="ComfyUI sage attention: auto (default) / true / false",
    )
    start_parser.add_argument(
        "--no-turbo-lora",
        dest="no_turbo_lora",
        action="store_true",
        help="Do not auto-download the Turbo LoRA (comfy stack)",
    )
    start_parser.add_argument(
        "--no-spectrum",
        dest="no_spectrum",
        action="store_true",
        help="Do not install the spectrum custom node (comfy stack)",
    )
    start_parser.add_argument(
        "--gpu",
        default=None,
        help=(
            "RunPod GPU id to provision (any stack; default: RUNPOD_GPU_ID or "
            "'NVIDIA RTX A6000'). Use it when the default card has no capacity, "
            "e.g. --gpu 'NVIDIA A40'"
        ),
    )
    stop_parser = sub.add_parser(
        "stop", help="Stop the SSH tunnels and the registered RunPod pod(s)"
    )
    stop_parser.add_argument(
        "--stack",
        choices=tuple(STACKS),
        default=None,
        help="Only stop this stack's pod (default: all registered pods)",
    )
    stop_parser.add_argument(
        "--terminate",
        action="store_true",
        help="Terminate the registered RunPod pod (default stop only stops it)",
    )
    sub.add_parser("status", help="Show operational status").add_argument(
        "--all",
        action="store_true",
        dest="all_stacks",
        help="Report every active workload stack (concurrent pods), not just one",
    )
    sub.add_parser("doctor", help="Run non-destructive diagnostics")
    sub.add_parser("gui", help="Launch the Windows GUI (tkinter)")
    cred_parser = sub.add_parser(
        "credentials", help="Manage the secure credential store (RunPod + extra API keys)"
    )
    cred_sub = cred_parser.add_subparsers(dest="credentials_action")
    cred_sub.add_parser(
        "set", help="Store (or rotate) RunPod credentials + optional API keys"
    )
    cred_sub.add_parser("status", help="Show credential store state (no values)")
    cred_sub.add_parser("clear", help="Remove stored credentials")

    comfy_parser = sub.add_parser(
        "comfy", help="Manage the ComfyUI pod (LoRA, personal vault, outputs)"
    )
    comfy_sub = comfy_parser.add_subparsers(dest="comfy_action")
    lora_install = comfy_sub.add_parser(
        "lora-install", help="Install a LoRA on the pod from an HF/CivitAI/direct URL"
    )
    lora_install.add_argument("url")
    lora_install.add_argument(
        "--personal", action="store_true",
        help="Install into the personal LoRA folder (backed up by the vault)",
    )
    lora_install.add_argument(
        "--force", action="store_true", help="Reinstall even if already present"
    )
    lora_install.add_argument(
        "--filename", default=None, help="Explicit local file name"
    )
    lora_list = comfy_sub.add_parser(
        "lora-list", help="List the LoRAs installed on the pod"
    )
    lora_list.add_argument(
        "--personal", action="store_true", help="Personal folder only"
    )
    lora_remove = comfy_sub.add_parser(
        "lora-remove", help="Remove a LoRA from the pod"
    )
    lora_remove.add_argument("name")
    lora_remove.add_argument(
        "--personal", action="store_true", help="Personal folder only"
    )
    comfy_sub.add_parser(
        "vault", help="Sync personal LoRAs/presets/outputs to the HF vault"
    )
    comfy_sub.add_parser(
        "version",
        help="Report the ComfyUI version the pod actually runs (read-only)",
    )
    repair_parser = comfy_sub.add_parser(
        "repair",
        help=(
            "Verify the pod's ComfyUI version and manager; --apply fixes them "
            "(the marker that blocks every checkout, the pinned tag, the "
            "comfyui-manager package)"
        ),
    )
    repair_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the fixes (without it, only reports what would be done)",
    )
    outputs = comfy_sub.add_parser(
        "outputs", help="List (or download) the generated outputs on the pod"
    )
    outputs.add_argument("name", nargs="?", default=None, help="File to download")
    outputs.add_argument(
        "--all", action="store_true", help="Download every output file"
    )
    outputs.add_argument(
        "--dir", default=None,
        help="Local download directory (default: ~/Downloads/comfy-outputs)",
    )

    args = parser.parse_args(argv)

    if args.command == "start":
        stack = getattr(args, "stack", None)
        effective_stack = (
            stack
            or (os.environ.get("LAUNCHER_STACK") or DEFAULT_STACK).strip().lower()
        )
        comfy = _comfy_overrides(args) if effective_stack == "comfy" else None
        if comfy is None and (
            args.preset or args.tier or args.workflows or args.access
            or args.sage_attention or args.no_turbo_lora or args.no_spectrum
        ):
            logger.warning(
                "Ignoring ComfyUI options (--preset/--tier/...) — the selected "
                "stack is not 'comfy'"
            )
        train = None
        gpu = getattr(args, "gpu", None)
        if getattr(args, "gui_settings", False):
            gui_overrides, gui_gpu = _gui_stack_overrides(effective_stack)
            if gui_overrides:
                if effective_stack == "comfy":
                    comfy = {**gui_overrides, **(comfy or {})}
                elif effective_stack == "train":
                    train = {**gui_overrides, **(train or {})}
                logger.info(
                    "gui.json values applied to the %s stack", effective_stack
                )
            if not gpu and gui_gpu:
                gpu = gui_gpu
        return _start(
            stack=stack,
            comfy=comfy,
            train=train,
            gpu=gpu,
        )
    if args.command == "stop":
        return _stop(terminate=args.terminate, stack=getattr(args, "stack", None))
    if args.command == "doctor":
        return _doctor()
    if args.command == "gui":
        return _gui()
    if args.command == "credentials":
        return _credentials(getattr(args, "credentials_action", None) or "status")
    if args.command == "comfy":
        action = getattr(args, "comfy_action", None)
        if action is None:
            comfy_parser.print_help()
            return 1
        return _comfy(action, args)
    # "status" (or missing subcommand) validates and reports status.
    return _status(all_stacks=bool(getattr(args, "all_stacks", False)))


def cli() -> int:
    """Console-script entry point: dispatch on the real command line."""
    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

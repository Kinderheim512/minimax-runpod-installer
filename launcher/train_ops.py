"""Pod-side operations for the `train` stack (LoRA training).

Mirrors :mod:`launcher.comfy_ops`: a frozen dataclass bound to one pod's SSH
endpoint, a ``build_train_ops`` factory that resolves the registered pod, and
methods that shell out over SSH. Nothing here duplicates the RunPod client, the
pod registry or the tunnel manager — it reuses all three.

WHY THE LORA COMES BACK OVER SCP
    The training pod writes finished LoRAs to ``/workspace/output_loras``. The
    obvious alternative — the filebrowser instance on port 8080 — needs its
    HTTP API and its credential, and reimplements a file transfer that ssh
    already does. ``scp`` reuses the exact connection the launcher's tunnel is
    built on, so there is one credential and one failure mode instead of two.

    Downloading to a LOCAL folder is deliberate: the launcher never routes a
    trained LoRA through a third-party service (a HuggingFace repo, a paste
    service) on the way to the user's machine. The bytes go pod -> host over
    the tunnel and nowhere else.

WHAT THIS MODULE NEVER DOES
    It never uploads anything, never deletes anything on the pod, and never
    touches the pod's dataset folder. Training is Fizgig's job; this module only
    reads what Fizgig produced.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import logging as launcher_logging
from . import winproc
from .tunnel import SshEndpoint, build_ssh_exec_args

logger = launcher_logging.get_logger("minimax-launcher.train_ops")


def _unlink_quietly(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass

#: Where the image keeps the things a user wants back.
DEFAULT_LORA_DIR = "/workspace/output_loras"
DEFAULT_DATASET_DIR = "/workspace/datasets"
DEFAULT_MODELS_DIR = "/workspace/models"

#: A LoRA is small (tens of MB); a dataset can be GBs. Different budgets.
LIST_OP_TIMEOUT = 30.0
DOWNLOAD_OP_TIMEOUT = 1800.0
STATUS_OP_TIMEOUT = 30.0

#: Extension filter for "collect the LoRAs". Fizgig writes kohya .safetensors;
#: checkpoints from the experimental fine-tune path are also .safetensors, so
#: there is nothing else to include.
LORA_SUFFIXES = (".safetensors",)


class TrainOpsError(RuntimeError):
    """Raised when a pod-side training operation cannot be completed."""


def _excerpt(text: str, limit: int = 300) -> str:
    """Trim a pod-side message so it stays readable in a log line."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def build_train_ops(config, runpod=None, registry=None) -> "TrainOps":
    """Bind a :class:`TrainOps` to the registered training pod's SSH endpoint.

    Shared by the CLI and the GUI so there is one wiring, not two. Raises
    :class:`TrainOpsError` with an actionable message when no usable SSH
    endpoint exists — which is the common case, since the training image only
    starts sshd when the RunPod template enables SSH access.
    """
    from . import runpod as _runpod_module
    from .pod_registry import PodRegistry as _PodRegistry

    if not config.secrets.runpod_api_key:
        raise TrainOpsError(
            "RUNPOD_API_KEY is not configured; it is needed to reach the "
            "training pod. Run `minimax-launcher credentials set`."
        )
    registry = registry or _PodRegistry()
    record = registry.load("train")
    if record is None:
        if not config.runpod.pod_id:
            raise TrainOpsError(
                "No training pod is registered on this machine. Run "
                "`minimax-launcher start --stack train` first."
            )
        pod_id = config.runpod.pod_id
    else:
        pod_id = record.pod_id
    runpod = runpod or _runpod_module.RunPodClient(config.secrets.runpod_api_key)
    pod = runpod.get_pod(pod_id)
    endpoint = pod.ssh_tunnel_endpoint()
    if endpoint is None:
        raise TrainOpsError(
            f"Training pod {pod_id} (status {pod.status}) has no usable SSH "
            "endpoint. The image only starts sshd when PUBLIC_KEY is set, so "
            "the RunPod template must enable SSH access — verify it with "
            "`python scripts/train/verify_template.py`."
        )
    return TrainOps(endpoint, config.ssh.key_path, config.ssh.connect_timeout)


@dataclass(frozen=True)
class TrainOps:
    """Pod-side training operations bound to one SSH endpoint."""

    endpoint: SshEndpoint
    key_path: Optional[str]
    connect_timeout: int = 10
    lora_dir: str = DEFAULT_LORA_DIR
    dataset_dir: str = DEFAULT_DATASET_DIR
    models_dir: str = DEFAULT_MODELS_DIR

    # ------------------------------------------------------------------ exec
    def _exec(self, command: str, timeout: float) -> str:
        """Run *command* on the pod; returns stdout (raises on failure)."""
        args = build_ssh_exec_args(
            self.endpoint, self.key_path, command, self.connect_timeout
        )
        try:
            proc = winproc.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise TrainOpsError(
                f"Pod-side operation timed out after {timeout:.0f}s"
            ) from None
        except OSError as exc:
            raise TrainOpsError(f"Could not run the SSH client: {exc}") from exc
        if proc.returncode != 0:
            raise TrainOpsError(
                f"Pod-side operation failed (exit {proc.returncode}): "
                f"{_excerpt(proc.stderr or proc.stdout)}"
            )
        return proc.stdout

    # ------------------------------------------------------------------ read
    def list_loras(self) -> list[str]:
        """Names of the LoRA files in the pod's output folder.

        Only the immediate folder is listed: Fizgig writes ``<name>.safetensors``
        plus per-epoch ``<name>-NNNNNN.safetensors`` checkpoints side by side,
        so a recursive walk would only add noise. A missing folder is not an
        error — it just means nothing has been trained yet.
        """
        command = (
            f"ls -1 {shlex.quote(self.lora_dir)} 2>/dev/null || true"
        )
        out = self._exec(command, LIST_OP_TIMEOUT)
        names = []
        for line in out.splitlines():
            name = line.strip()
            if not name or name.startswith("."):
                continue
            if name.endswith(LORA_SUFFIXES):
                names.append(name)
        return sorted(names)

    def list_datasets(self) -> list[str]:
        """Names of the dataset folders on the pod (one folder per LoRA)."""
        command = (
            f"find {shlex.quote(self.dataset_dir)} -mindepth 1 -maxdepth 1 "
            "-type d -printf '%f\\n' 2>/dev/null || true"
        )
        out = self._exec(command, LIST_OP_TIMEOUT)
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    def storage_report(self) -> str:
        """Human-readable disk usage of the pod's volume.

        Surfaced in the GUI because the failure mode it prevents is expensive
        and silent: a pod whose /workspace is the CONTAINER disk rather than a
        volume loses every downloaded weight when it stops.
        """
        command = (
            f"df -h {shlex.quote(self.models_dir)} 2>/dev/null | tail -1; "
            f"du -sh {shlex.quote(self.lora_dir)} 2>/dev/null || true"
        )
        return self._exec(command, STATUS_OP_TIMEOUT).strip()

    def pod_fizgig_version(self) -> str:
        """What Fizgig revision the pod is actually running.

        Upstream's entrypoint pulls ``FIZGIG_REF`` at every boot, so the pod's
        revision is whatever that ref pointed at when it last started — not a
        value the launcher chose and not something the launcher can assume. It
        has to be read back off the pod.

        ``git describe`` reports it relative to the nearest release tag:
        ``6.0.1`` when the tree sits exactly on one, ``6.0.1-3-gabc1234`` when
        ``master`` has moved past it. That is what
        :func:`launcher.fizgig_version.check` compares against upstream's newest
        release.

        Never raises: an unreachable pod, or a tree with no git metadata, reports
        ``(unknown)`` so the caller can say so rather than fail.
        """
        command = (
            "git -C /workspace/Fizgig describe --tags --always 2>/dev/null "
            "|| git -C /workspace/Fizgig rev-parse --short HEAD 2>/dev/null "
            "|| echo '(unknown)'"
        )
        return self._exec(command, STATUS_OP_TIMEOUT).strip()

    # ------------------------------------------------------------------ write
    @staticmethod
    def _safe_remote_name(name: str) -> str:
        """Validate a pod-supplied file name before it becomes a local path.

        The name comes from the pod's own ``ls -1`` output, so it is
        "trustworthy" as a *remote* path — but a Linux file name may legally
        contain ``\\`` (a path separator on Windows) or ``:`` (an NTFS
        alternate data stream). ``dest_dir / '..\\..\\evil.safetensors'``
        escapes the destination folder and writes wherever it likes, so the
        name is rejected unless it is a bare, relative file name.
        """
        if not name or name in (".", ".."):
            raise TrainOpsError(f"invalid pod file name {name!r}")
        if len(name) > 200:
            raise TrainOpsError(f"pod file name is too long ({len(name)} chars)")
        forbidden = set('\\/:*?"<>|')
        if any(char in forbidden for char in name):
            raise TrainOpsError(
                f"pod file name {name!r} contains a path separator or a "
                "reserved character; refusing to write it locally"
            )
        if any(ord(char) < 32 for char in name):
            raise TrainOpsError(f"pod file name {name!r} contains control characters")
        return name

    @staticmethod
    def _local_target(dest_dir: Path, name: str) -> Path:
        """Resolve *name* inside *dest_dir*, or refuse."""
        safe = TrainOps._safe_remote_name(name)
        root = Path(dest_dir).resolve()
        target = (root / safe).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise TrainOpsError(
                f"pod file name {name!r} would escape {root}"
            ) from None
        return target

    def download_lora(self, name: str, dest_dir: Path) -> Path:
        """Copy one LoRA from the pod into *dest_dir*; returns the local path.

        ``name`` is validated against the pod's own listing rather than
        interpolated into a remote path: a name arriving from a listing is
        trustworthy, one arriving from a text field is not, and this keeps the
        remote path out of the user's hands entirely. The local destination is
        additionally confined to *dest_dir*.
        """
        known = self.list_loras()
        if name not in known:
            raise TrainOpsError(
                f"{name!r} is not in the pod's output folder "
                f"({len(known)} file(s) there). Refresh the list and retry."
            )
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = self._local_target(dest_dir, name)
        remote = f"{self.lora_dir.rstrip('/')}/{name}"
        self._scp_download(remote, target)
        logger.ok("Downloaded %s to %s", name, target)
        return target

    def collect_loras(self, dest_dir: Path) -> list[Path]:
        """Download every LoRA in the pod's output folder.

        Skips a destination that already exists and is non-empty (a failed or
        timed-out transfer is unlinked by :meth:`_scp_download`), so re-running
        after a partial collection resumes instead of re-fetching tens of MB
        per epoch checkpoint.
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        for name in self.list_loras():
            target = self._local_target(dest_dir, name)
            if target.is_file() and target.stat().st_size > 0:
                logger.info("Already collected: %s", name)
                continue
            remote = f"{self.lora_dir.rstrip('/')}/{name}"
            self._scp_download(remote, target)
            downloaded.append(target)
            logger.ok("Downloaded %s", name)
        return downloaded

    def _scp_download(self, remote_path: str, local_path: Path) -> None:
        """Fetch one remote file with scp over the same SSH connection.

        ``scp`` takes ``-P`` for the port (ssh takes ``-p`` for something else
        entirely), and the same reliability flags as the tunnel: BatchMode so it
        can never hang on a prompt, accept-new for the host key.

        The transfer lands in a ``.part`` sibling and is renamed only once
        ``scp`` reports success. Writing straight to the destination left a
        *truncated* file behind when the launcher itself was killed mid-transfer
        (the timeout/exit paths unlink, a killed process does not), and
        ``collect_loras`` accepts any non-empty file as complete — so the
        truncated LoRA was reported as collected and never re-fetched.
        """
        target = Path(local_path)
        staging = target.with_name(target.name + ".part")
        args = [
            "scp",
            "-P", str(self.endpoint.port),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={self.connect_timeout}",
        ]
        if self.key_path:
            args.extend(["-i", self.key_path])
        args.append(f"{self.endpoint.username}@{self.endpoint.host}:{remote_path}")
        args.append(str(staging))
        try:
            proc = winproc.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=DOWNLOAD_OP_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # Remove the half-written file: collect_loras only skips a
            # destination that is non-empty, so a truncated file left behind
            # would otherwise never be re-fetched.
            _unlink_quietly(staging)
            raise TrainOpsError(
                f"Download of {target.name} timed out after "
                f"{DOWNLOAD_OP_TIMEOUT:.0f}s"
            ) from None
        except OSError as exc:
            raise TrainOpsError(f"Could not run the scp client: {exc}") from exc
        if proc.returncode != 0:
            _unlink_quietly(staging)
            raise TrainOpsError(
                f"scp failed for {remote_path} (exit {proc.returncode}): "
                f"{_excerpt(proc.stderr or proc.stdout)}"
            )
        try:
            os.replace(staging, target)
        except OSError as exc:
            _unlink_quietly(staging)
            raise TrainOpsError(
                f"scp reported success but the downloaded file could not be "
                f"moved into place: {exc}"
            ) from exc

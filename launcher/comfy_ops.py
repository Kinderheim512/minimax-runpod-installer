"""Pod-side ComfyUI operations for the comfy stack.

The launcher itself never runs GPU code: every operation here either runs a
script from the pod-side installer (``/opt/minimax-runpod-installer``) over a
one-shot SSH exec channel, or talks to the pod's ComfyUI HTTP API through the
local SSH tunnel.

Operations:

* LoRA management — ``install_lora.sh`` (install / list / remove, with
  ``--personal`` for the vault-backed personal LoRA folder).
* Personal vault sync — ``sync_push.sh`` pushes personal LoRAs/presets/outputs
  to ``PERSONAL_STORAGE_HF_REPO``.
* Output download — generated files are listed on the pod and fetched through
  ComfyUI's ``/view`` endpoint via the tunnel (local machine only).

**Secrets and SSH sessions.** The scripts read ``CIVITAI_API_KEY`` /
``HF_TOKEN`` from their environment, but a RunPod pod's container environment
is *not* inherited by sshd sessions (the same mechanism that made
``INSTALL_DIR`` invisible, see ``docs/comfy-image-analysis.md`` §17.8 #4).
Without help, a gated CivitAI download ran anonymously and failed with a
401/403 — the reported "Authentication failed with CivitAI" bug. The launcher
therefore forwards its own stored keys (``config.secrets.extra``) as
``KEY=value`` prefixes on the remote command of the scripts that need them:
nothing is written to the pod's disk, and no value is ever logged.

Long operations (multi-GB LoRA downloads) get a generous subprocess timeout;
failures raise :class:`ComfyOpsError` with a bounded excerpt of the script
output (no secrets: the scripts never echo the API keys).
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from . import logging as launcher_logging
from . import winproc
from .tunnel import SshEndpoint, build_ssh_exec_args


def _unlink_quietly(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


#: Characters a launcher-written pod file name may contain. An allowlist, not a
#: denylist: the name is interpolated into a shell fragment and used as a path,
#: so ``$``, a backtick, a quote, a separator or a redirection must all be
#: impossible — while spaces (legal in a file name, handled by quoting) stay
#: allowed.
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9 ._+@-]+")


def _safe_leaf_name(name: str, what: str) -> str:
    """Validate a bare file name, allowing spaces but nothing shell-active."""
    if not name or len(name) > 200 or name in (".", ".."):
        raise ComfyOpsError(f"invalid {what} name {name!r}")
    if not _SAFE_NAME_RE.fullmatch(name):
        raise ComfyOpsError(
            f"invalid {what} name {name!r} (letters, digits, space, '.', '_', "
            "'+', '@' and '-' only)"
        )
    return name

logger = launcher_logging.get_logger("minimax-launcher.comfy_ops")

#: Env keys the launcher is allowed to forward to the pod-side scripts. An
#: allow-list (not the raw ``secrets.extra`` mapping): a key name is injected
#: into a remote shell command line, so it must be known-safe by construction.
FORWARDED_SECRET_KEYS = ("HF_TOKEN", "CIVITAI_API_KEY")

#: Hosts whose downloads are gated by an API key.
CIVITAI_HOSTS = ("civitai.com", "civitai.red")

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Single source of truth for the pod-side layout. These values MUST match
#: ``comfy/image-contract.json``; ``tests/test_comfy_image_contract.py``
#: enforces it. Before 2.0.0 the paths were inconsistent (outputs hardcoded to
#: /opt/ComfyUI while custom nodes fell back to ${INSTALL_DIR:-/workspace/
#: ComfyUI}), which is exactly the drift the contract test now catches — see
#: docs/comfy-image-analysis.md §9.
DEFAULT_INSTALL_DIR = "/opt/ComfyUI"
DEFAULT_SCRIPTS_DIR = "/opt/minimax-runpod-installer"
DEFAULT_OUTPUT_DIR = f"{DEFAULT_INSTALL_DIR}/output"
DEFAULT_WORKFLOWS_DIR = f"{DEFAULT_INSTALL_DIR}/user/default/workflows"
DEFAULT_PERSONAL_WORKFLOWS_DIR = f"{DEFAULT_WORKFLOWS_DIR}/personal"
DEFAULT_PERSONAL_LORAS_DIR = f"{DEFAULT_INSTALL_DIR}/models/loras/personal"
DEFAULT_CUSTOM_NODES_DIR = f"{DEFAULT_INSTALL_DIR}/custom_nodes"
#: Python environment baked into the comfy image (``image-contract.json``).
DEFAULT_VENV_DIR = f"{DEFAULT_INSTALL_DIR}/venv"
LONG_OP_TIMEOUT = 7200.0
LIST_OP_TIMEOUT = 60.0
DEFAULT_DOWNLOAD_TIMEOUT = 3600.0
#: A local LoRA can be 1-2 GB and the upload goes through the user's uplink.
DEFAULT_UPLOAD_TIMEOUT = 3600.0
_WORKFLOW_OP_TIMEOUT = 120.0
_API_TIMEOUT = 60.0
_PROMPT_POST_TIMEOUT = 30.0
_OUTPUT_EXCERPT_LIMIT = 2000

#: Library kind -> destination folder on the pod (under ``install_dir``).
#: The URL path lands in exactly these folders (``install_lora.sh --personal``,
#: ``install_workflow``, ``custom_nodes``), so a local upload is
#: indistinguishable from a downloaded one once it is there.
ASSET_KIND_DIRS = {
    "lora": "models/loras/personal",
    "workflow": "user/default/workflows/personal",
    "node": "custom_nodes",
}


class ComfyOpsError(RuntimeError):
    """A pod-side ComfyUI operation failed (bounded, secret-free detail)."""


def _excerpt(output: str, limit: int = _OUTPUT_EXCERPT_LIMIT) -> str:
    text = (output or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:] + f" … ({len(text)} chars total, head truncated)"


def is_civitai_url(url: str) -> bool:
    """True when *url* targets CivitAI (the hosts that honour the API key)."""
    lowered = (url or "").lower()
    return any(host in lowered for host in CIVITAI_HOSTS)


def _human_size(size: int) -> str:
    """Compact, locale-neutral size for the journal (1.2 Go / 340 Mo / 12 Ko)."""
    value = float(size)
    for unit in ("o", "Ko", "Mo", "Go"):
        if value < 1024 or unit == "Go":
            if unit == "o":
                return f"{int(value)} o"
            return f"{value:.1f} {unit}".replace(".0 ", " ")
        value /= 1024
    return f"{value:.1f} Go"


def _human_duration(seconds: float) -> str:
    """Compact duration for the journal (45 s / 3 min 20 s / 1 h 05)."""
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} min {rest:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d}"


def build_comfy_ops(
    config,
    runpod=None,
    registry=None,
) -> "ComfyOps":
    """Bind a :class:`ComfyOps` to the registered comfy pod's SSH endpoint.

    Shared by the CLI ``comfy`` commands and the GUI LoRA panel (one
    primitive, no duplicated wiring). The pod is the registry's ``comfy``
    record, or ``RUNPOD_POD_ID`` when no record exists. Raises
    :class:`ComfyOpsError` with an actionable message when no usable SSH
    endpoint exists.
    """
    from . import runpod as _runpod_module
    from .pod_registry import PodRegistry as _PodRegistry

    if not config.secrets.runpod_api_key:
        raise ComfyOpsError(
            "RUNPOD_API_KEY is not configured; it is needed to reach the "
            "comfy pod. Run `minimax-launcher credentials set`."
        )
    registry = registry or _PodRegistry()
    record = registry.load("comfy")
    if record is None:
        if not config.runpod.pod_id:
            raise ComfyOpsError(
                "No comfy pod is registered on this machine. Run "
                "`minimax-launcher start --stack comfy` first."
            )
        pod_id = config.runpod.pod_id
    else:
        pod_id = record.pod_id
    runpod = runpod or _runpod_module.RunPodClient(config.secrets.runpod_api_key)
    pod = runpod.get_pod(pod_id)
    endpoint = pod.ssh_tunnel_endpoint()
    if endpoint is None:
        raise ComfyOpsError(
            f"Comfy pod {pod_id} (status {pod.status}) has no usable SSH "
            "endpoint. Start it with `minimax-launcher start --stack comfy`."
        )
    return ComfyOps(
        endpoint,
        config.ssh.key_path,
        config.ssh.connect_timeout,
        base_url=config.comfy_base_url(),
        secret_env=dict(getattr(config.secrets, "extra", None) or {}),
    )


@dataclass(frozen=True)
class ComfyOps:
    """ComfyUI pod-side operations bound to one SSH endpoint.

    ``endpoint`` is the pod's SSH endpoint (the same one the tunnel uses);
    ``base_url`` is the local ComfyUI URL (through the tunnel) used for
    output downloads.
    """

    endpoint: SshEndpoint
    key_path: Optional[str]
    connect_timeout: int = 10
    base_url: str = ""
    scripts_dir: str = DEFAULT_SCRIPTS_DIR
    output_dir: str = DEFAULT_OUTPUT_DIR
    workflows_dir: str = DEFAULT_WORKFLOWS_DIR
    install_dir: str = DEFAULT_INSTALL_DIR
    venv_dir: str = DEFAULT_VENV_DIR
    #: Launcher-managed API keys (``HF_TOKEN`` / ``CIVITAI_API_KEY``) forwarded
    #: to the pod-side scripts that need them. Docker ENV is not inherited by
    #: sshd sessions, so without this a gated CivitAI download fails 401/403.
    secret_env: Mapping[str, str] = field(default_factory=dict)

    def _secret_prefix(self) -> str:
        """``KEY='value' `` assignments to prepend to a remote command.

        Only :data:`FORWARDED_SECRET_KEYS` are eligible, values are shell
        quoted, and blank values are dropped — an empty key must never be
        exported (the scripts treat an empty variable as "not configured",
        which is the correct behaviour).
        """
        assignments = []
        for key in FORWARDED_SECRET_KEYS:
            value = (self.secret_env or {}).get(key)
            value = value.strip() if isinstance(value, str) else ""
            if not value or not _ENV_NAME_RE.match(key):
                continue
            assignments.append(f"{key}={shlex.quote(value)}")
        if not assignments:
            return ""
        return " ".join(assignments) + " "

    def _exec(self, command: str, timeout: float) -> str:
        """Run *command* on the pod; returns stdout (raises on failure)."""
        args = build_ssh_exec_args(self.endpoint, self.key_path, command, self.connect_timeout)
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
            raise ComfyOpsError(
                f"Pod-side operation timed out after {timeout:.0f}s"
            ) from None
        except OSError as exc:
            raise ComfyOpsError(f"Could not run the SSH client: {exc}") from exc
        if proc.returncode != 0:
            raise ComfyOpsError(
                f"Pod-side operation failed (exit {proc.returncode}): "
                f"{_excerpt(proc.stderr or proc.stdout)}"
            )
        return proc.stdout

    def run_shell(self, command: str, timeout: float = LONG_OP_TIMEOUT) -> str:
        """Run one shell *command* on the pod; returns stdout.

        Used by :mod:`launcher.comfy_version` to read (and, on repair, to
        adjust) what the pod actually has installed. The command is built by
        the caller from constants and validated names — never from user text —
        and travels inside the encrypted SSH channel. Raises
        :class:`ComfyOpsError` on failure.
        """
        return self._exec(command, timeout)

    def run_script(self, script: str, *args: str, timeout: float = LONG_OP_TIMEOUT) -> str:
        """Run one installer script on the pod with *args* (one string each).

        The launcher's stored API keys are prefixed onto the remote command:
        sshd does not inherit the container's Docker ENV, so this is the only
        way ``install_lora.sh`` / ``sync_push.sh`` can authenticate a gated
        CivitAI or Hugging Face download. The values travel inside the
        encrypted SSH channel; they are never written to the pod's disk and
        never logged (only the script name and its own arguments are).
        """
        command = self._secret_prefix() + " ".join(
            [f"bash {self.scripts_dir}/{script}"] + [shlex.quote(a) for a in args]
        )
        logger.info("Running pod-side script %s %s", script, " ".join(args))
        return self._exec(command, timeout)

    def install_lora(
        self,
        url: str,
        personal: bool = False,
        force: bool = False,
        filename: Optional[str] = None,
    ) -> str:
        """Install a LoRA from an HF/CivitAI/direct URL on the pod."""
        if not url:
            raise ComfyOpsError("LoRA URL must not be empty")
        if not url.startswith(("http://", "https://")):
            # ``url`` is the script's last positional argument, so a value
            # starting with ``-`` would be parsed as an option (``--list``
            # makes the script print its listing, exit 0, and the caller
            # reports a successful install that never happened).
            raise ComfyOpsError("LoRA URL must be http(s)")
        if is_civitai_url(url) and not (self.secret_env or {}).get("CIVITAI_API_KEY"):
            # Actionable instead of a generic "Authentication failed with
            # CivitAI": a gated CivitAI file can only be fetched with a key.
            logger.warning(
                "No CivitAI API key stored: if this file is gated, the download will "
                "fail with 401/403. Store one with `minimax-launcher credentials set`."
            )
        args: list[str] = []
        if personal:
            args.append("--personal")
        if force:
            args.append("--force")
        if filename:
            # The script uses ``--filename`` verbatim as ``dest_file="${dir}/${filename}"``,
            # so a name with a path separator writes outside the LoRA folder.
            args.extend(["--filename", self._safe_name(filename, "LoRA file")])
        args.append(url)
        out = self.run_script("install_lora.sh", *args)
        logger.ok("LoRA installed on the pod (%s)", url)
        return out

    def list_loras(self, personal: bool = False) -> str:
        """List the LoRAs installed on the pod (personal folder only if set)."""
        args = ["--list"]
        if personal:
            args.append("--personal")
        return self.run_script("install_lora.sh", *args, timeout=LIST_OP_TIMEOUT)

    def list_lora_names(self, personal: bool = False) -> list[str]:
        """Same listing as :meth:`list_loras`, as one name per entry.

        The tool layer needs the individual names to number them: every tool
        result is redacted, so a 24+ character LoRA name comes back masked and
        cannot be quoted into a later ``remove`` call. Numbering gives the
        caller a redaction-proof handle.
        """
        out = self.list_loras(personal)
        return [line.strip() for line in out.splitlines() if line.strip()]

    def remove_lora(self, name: str, personal: bool = False) -> str:
        """Remove a LoRA file from the pod (personal folder only if set)."""
        if not name:
            raise ComfyOpsError("LoRA file name must not be empty")
        name = self._safe_name(name, "LoRA file")
        args = ["--remove"]
        if personal:
            args.append("--personal")
        args.append(name)
        return self.run_script("install_lora.sh", *args, timeout=LIST_OP_TIMEOUT)

    def install_node(self, repo_url: str) -> str:
        """Clone a custom-node git repo into ComfyUI's custom_nodes on the pod.

        Idempotent (skips if the target dir already exists). A ``#<ref>`` pin
        suffix is accepted but ignored here — the startup path
        (``lib/annuaire.sh``) applies pins; this one-shot install clones the
        default branch only.
        """
        if not repo_url:
            raise ComfyOpsError("Node repo URL must not be empty")
        base_url = repo_url.split("#", 1)[0].strip()
        if not base_url.startswith(("http://", "https://")):
            raise ComfyOpsError("Node repo URL must be http(s)")
        name = base_url.rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if not name:
            raise ComfyOpsError("Could not derive a node name from the repo URL")
        # The name is derived from a user-supplied URL and interpolated into a
        # double-quoted shell fragment: a URL ending in ``$(…)`` or a backtick
        # would be command-substituted on the pod.
        name = self._safe_name(name, "custom node")
        nodes_dir = f"{self.install_dir}/custom_nodes"
        command = (
            f'nodes_dir="{nodes_dir}"; mkdir -p "$nodes_dir"; '
            f'target="$nodes_dir/{name}"; '
            f'if [ -d "$target" ]; then echo "node already present: {name}"; '
            f'else git clone {shlex.quote(base_url)} "$target" '
            f'&& echo "node installed: {name}"; fi'
        )
        out = self._exec(command, timeout=LIST_OP_TIMEOUT)
        logger.ok("Custom node installed on the pod (%s)", name)
        return out

    def asset_destination(self, kind: str) -> str:
        """Absolute pod folder a local asset of *kind* is uploaded into."""
        relative = ASSET_KIND_DIRS.get(kind)
        if relative is None:
            raise ComfyOpsError(
                f"unknown asset kind {kind!r} (expected one of "
                f"{sorted(ASSET_KIND_DIRS)})"
            )
        return f"{self.install_dir}/{relative}"

    def remote_file_size(self, remote_path: str) -> Optional[int]:
        """Size of *remote_path* in bytes, or None when it does not exist."""
        out = self._exec(
            f"stat -c %s {shlex.quote(remote_path)} 2>/dev/null || true",
            timeout=LIST_OP_TIMEOUT,
        )
        text = (out or "").strip()
        return int(text) if text.isdigit() else None

    def upload_asset(
        self,
        kind: str,
        local_path,
        *,
        name: str = "",
        force: bool = False,
        timeout: float = DEFAULT_UPLOAD_TIMEOUT,
    ) -> str:
        """Upload a **local** file or folder to the pod.

        The pod cannot read this machine, so unlike a URL entry (which travels
        as ``H3_CUSTOM_*`` pod env and is downloaded by the pod-side installer)
        a local asset is transferred here, over the same SSH endpoint the
        tunnel uses, into the very folder the URL path targets — a LoRA lands
        in ``models/loras/personal/``, a workflow in
        ``user/default/workflows/personal/``, a node folder in
        ``custom_nodes/<name>/``.

        A file already present with the **same size** is skipped (a 2 GB LoRA
        must not be re-sent on every start); ``force`` uploads anyway. A node
        folder is compared on (file count, total bytes) for the same reason.

        Returns a one-line summary for the journal. Raises
        :class:`ComfyOpsError` on any failure — the caller decides whether that
        is fatal (the startup path treats it as non-fatal).
        """
        source = Path(local_path)
        label = name or source.name or str(source)
        if not source.exists():
            raise ComfyOpsError(f"chemin local introuvable : {source}")
        destination = self.asset_destination(kind)
        started = time.monotonic()

        if kind == "node":
            if not source.is_dir():
                raise ComfyOpsError(
                    f"a local node must be a folder: {source}"
                )
            if not force and self._remote_tree_signature(
                f"{destination}/{source.name}"
            ) == self._local_tree_signature(source):
                return f"{label!r} already on the pod ({source.name})"
            self._upload_tree(source, destination, timeout)
            pip_note = self._pip_install_node(f"{destination}/{source.name}")
            elapsed = _human_duration(time.monotonic() - started)
            return (
                f"{label!r} uploaded ({source.name}/, {elapsed}) — {pip_note}"
            )

        if not source.is_file():
            raise ComfyOpsError(f"a file is expected for {kind}: {source}")
        remote_path = f"{destination}/{source.name}"
        local_size = source.stat().st_size
        if not force and self.remote_file_size(remote_path) == local_size:
            return (
                f"{label!r} already on the pod "
                f"({source.name}, {_human_size(local_size)})"
            )
        self._upload_file(source, remote_path, timeout)
        uploaded = self.remote_file_size(remote_path)
        if uploaded is not None and uploaded != local_size:
            raise ComfyOpsError(
                f"transfert incomplet pour {source.name} : "
                f"{uploaded} bytes on the pod, {local_size} locally"
            )
        elapsed = _human_duration(time.monotonic() - started)
        logger.ok(
            "Uploaded %s (%s) in %s", source.name, _human_size(local_size), elapsed
        )
        return (
            f"{label!r} uploaded ({source.name}, {_human_size(local_size)}, "
            f"{elapsed})"
        )

    def _upload_file(self, local: Path, remote_path: str, timeout: float) -> None:
        """Stream one local file into *remote_path* over the SSH channel.

        ``cat > file`` on the remote shell rather than ``scp``: a Windows path
        like ``C:\\...\\x.safetensors`` is read by scp as ``host:path`` (the
        drive letter looks like a host name), and this form needs no sftp
        subsystem either — only the exec channel the launcher already uses
        everywhere.
        """
        remote_dir = remote_path.rsplit("/", 1)[0]
        command = (
            f"mkdir -p {shlex.quote(remote_dir)} && "
            f"cat > {shlex.quote(remote_path)}"
        )
        args = build_ssh_exec_args(
            self.endpoint, self.key_path, command, self.connect_timeout
        )
        logger.info(
            "Uploading %s (%s) to %s", local.name, _human_size(local.stat().st_size),
            remote_path,
        )
        try:
            with local.open("rb") as handle:
                proc = winproc.run(
                    args,
                    stdin=handle,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
        except subprocess.TimeoutExpired:
            raise ComfyOpsError(
                f"upload of {local.name} interrupted after {timeout:.0f}s"
            ) from None
        except OSError as exc:
            raise ComfyOpsError(f"envoi impossible : {exc}") from exc
        if proc.returncode != 0:
            raise ComfyOpsError(
                f"upload of {local.name} failed (exit {proc.returncode}): "
                f"{_excerpt(proc.stderr or proc.stdout)}"
            )

    def _upload_tree(self, local: Path, remote_dir: str, timeout: float) -> None:
        """Stream a whole local folder into ``remote_dir/<folder name>``.

        A tar stream piped through the SSH channel: one connection for the
        whole tree (a node can hold dozens of files) and no per-file
        round-trip. ``tar`` ships with Windows 10+ (bsdtar) and every Linux
        base image.
        """
        target = f"{remote_dir}/{local.name}"
        command = (
            f"mkdir -p {shlex.quote(target)} && "
            f"tar -xf - -C {shlex.quote(target)}"
        )
        ssh_args = build_ssh_exec_args(
            self.endpoint, self.key_path, command, self.connect_timeout
        )
        logger.info("Uploading folder %s to %s", local.name, target)
        try:
            packer = winproc.Popen(
                ["tar", "-C", str(local), "-cf", "-", "."],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ComfyOpsError(
                f"tar is not available locally ({exc}) — required to send "
                "a folder"
            ) from exc
        try:
            sender = winproc.Popen(
                ssh_args,
                stdin=packer.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            packer.kill()
            raise ComfyOpsError(f"envoi impossible : {exc}") from exc
        assert packer.stdout is not None
        packer.stdout.close()  # the ssh process owns the read end now
        try:
            _out, err = sender.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            sender.kill()
            packer.kill()
            raise ComfyOpsError(
                f"upload of {local.name}/ interrupted after {timeout:.0f}s"
            ) from None
        packer.wait()
        if sender.returncode != 0:
            raise ComfyOpsError(
                f"upload of {local.name}/ failed (exit {sender.returncode}): "
                f"{_excerpt(err or '')}"
            )

    def _local_tree_signature(self, local: Path) -> tuple[int, int]:
        """(file count, total bytes) of a local folder."""
        count = 0
        total = 0
        for path in local.rglob("*"):
            if path.is_file():
                count += 1
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return (count, total)

    def _remote_tree_signature(self, remote_dir: str) -> Optional[tuple[int, int]]:
        """(file count, total bytes) of a pod folder, or None when absent."""
        quoted = shlex.quote(remote_dir)
        command = (
            f"if [ -d {quoted} ]; then "
            f"printf '%s %s' \"$(find {quoted} -type f | wc -l)\" "
            f"\"$(du -sb {quoted} | cut -f1)\"; fi"
        )
        out = (self._exec(command, timeout=LIST_OP_TIMEOUT) or "").strip()
        parts = out.split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return None
        return (int(parts[0]), int(parts[1]))

    def _pip_install_node(self, remote_dir: str) -> str:
        """Install a freshly uploaded node's requirements, best-effort.

        Mirrors what ``lib/nodes.sh`` does for a cloned node: without it a
        local node is just code whose imports fail at the first prompt. A
        failure is reported, never raised — the node is already on the pod.
        """
        pip = f"{self.venv_dir}/bin/pip"
        requirements = f"{remote_dir}/requirements.txt"
        command = (
            f"if [ -f {shlex.quote(requirements)} ]; then "
            f"if [ -x {shlex.quote(pip)} ]; then "
            f"{shlex.quote(pip)} install -q -r {shlex.quote(requirements)} "
            f"&& echo 'deps ok' || echo 'deps KO'; "
            f"else echo 'venv introuvable'; fi; else echo 'pas de requirements'; fi"
        )
        try:
            out = (self._exec(command, timeout=LONG_OP_TIMEOUT) or "").strip()
        except ComfyOpsError as exc:
            logger.warning("node pip install skipped: %s", exc)
            return "dependencies not installed"
        if out.endswith("deps ok"):
            return "dependencies installed"
        if out.endswith("pas de requirements"):
            return "no dependency to install"
        logger.warning("node pip install inconclusive: %s", out[-200:])
        return "dependencies not installed"

    def install_workflow(self, url: str, filename: Optional[str] = None) -> str:
        """Download a workflow JSON into ``user/default/workflows/personal/``.

        *filename* is the local ``.json`` name (sanitized to a bare file name);
        when omitted it is derived from the URL. Idempotent in the sense that
        ``curl -o`` overwrites an existing file of the same name.
        """
        if not url:
            raise ComfyOpsError("Workflow URL must not be empty")
        if not url.startswith(("http://", "https://")):
            raise ComfyOpsError("Workflow URL must be http(s)")
        if filename:
            fname = Path(filename).name.strip()
        else:
            fname = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if not fname or fname in ("/", ".", ".."):
            fname = "workflow.json"
        if not fname.lower().endswith(".json"):
            fname += ".json"
        # A bare file name only: the value is a user-supplied ``--filename``
        # that ends up on the pod's filesystem.
        fname = _safe_leaf_name(fname, "workflow file")
        wf_dir = f"{self.install_dir}/user/default/workflows/personal"
        command = (
            f'wf_dir="{wf_dir}"; mkdir -p "$wf_dir"; '
            f'curl -fsSL -o {shlex.quote(f"{wf_dir}/{fname}")} {shlex.quote(url)} '
            f'&& echo "workflow installed: {fname}"'
        )
        out = self._exec(command, timeout=LIST_OP_TIMEOUT)
        logger.ok("Workflow installed on the pod (%s)", fname)
        return out

    def sync_vault(self) -> str:
        """Push personal LoRAs/presets/outputs to the HF vault (sync_push.sh)."""
        return self.run_script("sync_push.sh")

    def list_outputs(self, subfolders: bool = False) -> list[str]:
        """Names of the generated output files on the pod.

        Without *subfolders* only the output root is listed; with it,
        files are listed recursively as paths relative to the output
        directory — the H3 workflow templates pin nested prefixes such
        as ``video/MiniMax_H3``.
        """
        if not subfolders:
            out = self._exec(
                f"ls -1 {self.output_dir} 2>/dev/null || true", timeout=LIST_OP_TIMEOUT
            )
            return [line.strip() for line in out.splitlines() if line.strip()]
        out = self._exec(
            f"find {self.output_dir} -type f 2>/dev/null || true",
            timeout=LIST_OP_TIMEOUT,
        )
        base = self.output_dir.rstrip("/")
        return [
            line.strip()[len(base) + 1 :]
            for line in out.splitlines()
            if line.strip().startswith(base + "/")
        ]

    @staticmethod
    def _safe_subfolder(subfolder: str) -> str:
        """Validate a pod-reported output *subfolder* (path-traversal safe).

        The subfolder comes from the pod's file listing, i.e. untrusted input
        when it is joined into a local download path:

        * the raw value is still what is sent to the pod's ``/view`` endpoint
          (URL-quoted — HTTP-safe as-is);
        * the *local* mirror path is rebuilt component by component, where
          ``..``/``.`` segments and anything with a path separator, a Windows
          reserved character, or a control character in a component is
          rejected, and an absolute input is degraded to a relative one.

        Raises :class:`ComfyOpsError` when no safe local mirror can be built;
        callers fall back to the download root rather than failing the
        transfer.
        """
        if not subfolder:
            return ""
        parts = [
            part
            for part in subfolder.replace("\\", "/").split("/")
            if part not in ("", ".")
        ]
        if not parts:
            raise ComfyOpsError(f"invalid output subfolder {subfolder!r}")
        for part in parts:
            if part == "..":
                raise ComfyOpsError(
                    f"invalid output subfolder {subfolder!r} (traversal)"
                )
            if len(part) > 200:
                raise ComfyOpsError(
                    f"invalid output subfolder {subfolder!r} (component too long)"
                )
            for char in part:
                if char in '<>:"/\\|?*\x00' or ord(char) < 32:
                    raise ComfyOpsError(
                        f"invalid output subfolder {subfolder!r} "
                        f"(component {part!r} not allowed)"
                    )
        return "/".join(parts)

    def download_output(
        self,
        filename: str,
        local_dir: Path,
        timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
        subfolder: str = "",
        mirror_subfolder: bool = True,
        local_name: Optional[str] = None,
        type: str = "output",
    ) -> Path:
        """Fetch one generated output through ComfyUI's ``/view`` endpoint.

        The file is streamed from the local tunnel URL (never directly
        from the pod's public URL) into *local_dir*, mirroring the
        output *subfolder* when one is given (e.g. ``video``). The subfolder
        is sanitized before being joined into the local path (it comes from
        the pod's file listing); a subfolder with no safe local mirror falls
        back to the download root — the transfer still happens, it just
        cannot be steered outside *local_dir*.

        ``mirror_subfolder=False`` keeps the *subfolder* in the ``/view``
        URL (it is where the pod actually stores the file) but writes the
        result flat in *local_dir*: the automatic collection wants one
        predictable folder, not the pod's internal output tree.

        ``local_name`` overrides the local file name only — the pod-side
        *filename* still drives the ``/view`` URL. The automatic collection
        uses it to avoid overwriting a file collected during an earlier
        session (ComfyUI restarts its output counter on every pod boot).
        """
        if not filename:
            raise ComfyOpsError("Output file name must not be empty")
        if not self.base_url:
            raise ComfyOpsError(
                "No local ComfyUI URL configured; the tunnel must be up "
                "before downloading outputs"
            )
        local_subfolder = ""
        if mirror_subfolder:
            try:
                local_subfolder = self._safe_subfolder(subfolder)
            except ComfyOpsError:
                logger.warning(
                    "refusing unsafe output subfolder %r for the local path; "
                    "falling back to the download root", subfolder,
                )
        url = (
            self.base_url.rstrip("/")
            + "/view?filename="
            + urllib.parse.quote(filename)
            + "&type="
            + urllib.parse.quote(type or "output")
        )
        if subfolder:
            url += "&subfolder=" + urllib.parse.quote(subfolder)
        rel = (
            f"{local_subfolder}/{Path(local_name or filename).name}"
            if local_subfolder else Path(local_name or filename).name
        )
        local_root = Path(local_dir).resolve()
        target = local_root / rel
        # Belt and braces: the rel path must stay inside local_dir.
        try:
            target.relative_to(local_root)
        except ValueError:
            raise ComfyOpsError(
                f"output path {rel!r} escapes the download directory"
            ) from None
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(target.name + ".part")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=min(timeout, 60.0)) as resp:
                with staging.open("wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        if time.monotonic() - started > timeout:
                            raise ComfyOpsError(
                                f"Output download timed out after {timeout:.0f}s"
                            )
        except urllib.error.HTTPError as exc:
            _unlink_quietly(staging)
            raise ComfyOpsError(
                f"ComfyUI refused the output download (HTTP {exc.code}) for "
                f"{filename!r}"
            ) from exc
        except urllib.error.URLError as exc:
            _unlink_quietly(staging)
            raise ComfyOpsError(
                f"could not reach ComfyUI for {filename!r}: {exc.reason}"
            ) from exc
        except OSError as exc:
            # A bare OSError escaped every caller that only catches
            # ComfyOpsError, and left a truncated file that later looked like a
            # real output.
            _unlink_quietly(staging)
            raise ComfyOpsError(
                f"output download failed for {filename!r}: {exc}"
            ) from exc
        except BaseException:
            _unlink_quietly(staging)
            raise
        try:
            os.replace(staging, target)
        except OSError as exc:
            _unlink_quietly(staging)
            raise ComfyOpsError(
                f"could not move the downloaded output into place: {exc}"
            ) from exc
        logger.ok(
            "Downloaded output %s -> %s (%d bytes)",
            filename, target, target.stat().st_size,
        )
        return target

    # ------------------------------------------------------------------
    # Workflow / ComfyUI HTTP API operations (through the local tunnel)
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_name(name: str, what: str) -> str:
        """Validate a bare file name (no path components, no shell metachars)."""
        import re
        if not name or len(name) > 200 or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ComfyOpsError(
                f"invalid {what} name {name!r} (letters, digits, '_', '-', '.' only)"
            )
        return name

    def list_workflows(self) -> list[str]:
        """Names of the workflow files installed on the pod."""
        out = self._exec(
            f"ls -1 {self.workflows_dir} 2>/dev/null || true", timeout=LIST_OP_TIMEOUT
        )
        names = [line.strip() for line in out.splitlines() if line.strip()]
        return [n for n in names if n.endswith(".json")]

    def read_workflow(self, name: str) -> dict:
        """Read one workflow JSON from the pod's workflows directory."""
        name = self._safe_name(name, "workflow")
        out = self._exec(
            f"cat {self.workflows_dir}/{name}", timeout=_WORKFLOW_OP_TIMEOUT
        )
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise ComfyOpsError(
                f"workflow {name!r} on the pod is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ComfyOpsError(f"workflow {name!r} is not a workflow object")
        return data

    def _http_json(self, method: str, path: str, body: Optional[dict] = None,
                   timeout: float = _API_TIMEOUT) -> dict:
        if not self.base_url:
            raise ComfyOpsError(
                "No local ComfyUI URL configured; the tunnel must be up"
            )
        url = self.base_url.rstrip("/") + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ComfyOpsError(
                f"ComfyUI API {method} {path} -> HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ComfyOpsError(
                f"ComfyUI API unreachable at {url}: {exc.reason}"
            ) from exc
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ComfyOpsError(
                f"ComfyUI API {method} {path} returned invalid JSON: {exc}"
            ) from exc
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    def get_object_info(self) -> dict:
        """The ComfyUI ``/object_info`` map (class -> definition)."""
        return self._http_json("GET", "/object_info")

    def get_system_stats(self) -> dict:
        """The ComfyUI ``/system_stats`` mapping ({} when unavailable)."""
        try:
            return self._http_json("GET", "/system_stats", timeout=10.0)
        except ComfyOpsError:
            return {}

    def get_queue(self) -> dict:
        """``{"running": n, "pending": n}`` from ``/api/queue`` ({} on error)."""
        try:
            data = self._http_json("GET", "/api/queue", timeout=10.0)
        except ComfyOpsError:
            return {}
        running = data.get("queue_running") or []
        pending = data.get("queue_pending") or []
        return {
            "running": len(running) if isinstance(running, list) else 0,
            "pending": len(pending) if isinstance(pending, list) else 0,
        }

    def get_history(self, max_items: int = 200, timeout: float = 15.0) -> dict:
        """The ComfyUI execution history: ``{prompt_id: entry}``.

        An entry is registered as soon as the prompt *starts* executing
        (``status.completed`` false) and finalized when it ends, so callers
        must look at the status, not at the key's presence — see
        :mod:`launcher.comfy_watch`. ``/api/history`` is the documented route
        (``comfy/image-contract.json``); older servers only expose
        ``/history``, hence the fallback. Raises :class:`ComfyOpsError` when
        neither answers (tunnel down, ComfyUI not up yet).
        """
        limit = max(1, int(max_items))
        failure: Optional[ComfyOpsError] = None
        for path in (
            f"/api/history?max_items={limit}",
            f"/history?max_items={limit}",
        ):
            try:
                return self._http_json("GET", path, timeout=timeout)
            except ComfyOpsError as exc:
                failure = exc
        raise failure if failure is not None else ComfyOpsError("history unavailable")

    def submit_prompt(self, prompt: dict, client_id: str = "minimax-launcher") -> str:
        """POST an API prompt to ``/prompt``; returns the prompt id.

        Raises :class:`ComfyOpsError` with the server's error list when the
        prompt is rejected (missing class, bad input, ...).
        """
        data = self._http_json(
            "POST", "/prompt", body={"prompt": prompt, "client_id": client_id},
            timeout=_PROMPT_POST_TIMEOUT,
        )
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            errors = data.get("node_errors") or data.get("error") or data
            raise ComfyOpsError(
                f"ComfyUI rejected the prompt: {_excerpt(str(errors), 800)}"
            )
        return str(prompt_id)

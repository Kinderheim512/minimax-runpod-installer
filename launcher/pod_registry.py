"""Persistent registry for the launcher-created RunPod pods.

Records the identity of every pod the launcher created on this machine,
keyed by workload stack (``comfy`` / ``train``), so subsequent CLI
invocations (``start``/``stop``/``status``/``doctor``) can discover and
manage them without requiring ``RUNPOD_POD_ID``. Both stacks can run at
once — a ComfyUI pod and a LoRA-training pod — each with its own template
and lifecycle.

The records are non-secret by design: each holds only the pod ID, name,
creation timestamp, GPU request, and the scheduler-assigned data center.
Secrets (API key, template IDs) are NEVER written here. The file lives in
the launcher home (``<home>\\pod.json``, see :func:`default_home``) and is
written atomically (temp file + ``os.replace``) so a crash can never leave
a partial record.

File format: version 1 (legacy) held a single top-level pod record; version
2 holds a ``pods`` mapping of stack name to record. Version 1 files are
read as the ``comfy`` stack and transparently upgraded to version 2 on the
next save (the existing record is preserved).
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import logging as launcher_logging

logger = launcher_logging.get_logger("minimax-launcher.pod_registry")

CURRENT_VERSION = 2
LEGACY_VERSION = 1


def default_home() -> Path:
    """Return the launcher home directory.

    ``MINIMAX_LAUNCHER_HOME`` when set, otherwise ``%APPDATA%\\MiniMaxH3Launcher``
    (falling back to ``~/.minimax-launcher`` on platforms without APPDATA).
    """
    env_home = os.environ.get("MINIMAX_LAUNCHER_HOME")
    if env_home:
        return Path(env_home)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "MiniMaxH3Launcher"
    return Path.home() / ".minimax-launcher"


@dataclass(frozen=True)
class PodRecord:
    """Non-secret identity record for a launcher-created pod."""

    pod_id: str
    name: str
    created_at: float
    gpu_id: Optional[str]
    gpu_count: int
    data_center_id: Optional[str] = None
    stack: str = "comfy"
    version: int = CURRENT_VERSION


def _record_from_dict(raw: dict, stack: str) -> PodRecord:
    # Validate before stringifying: ``str(None)`` is the string "None", which
    # would be accepted as a perfectly valid pod id and then handed to the
    # RunPod API — failing far from the corrupted record that produced it.
    pod_id = raw.get("pod_id")
    if not isinstance(pod_id, str) or not pod_id.strip():
        raise ValueError(f"pod record for stack {stack!r} has no usable pod_id")
    created_at = raw.get("created_at")
    try:
        created_at_value = float(str(created_at))
    except (TypeError, ValueError):
        raise ValueError(
            f"pod record for stack {stack!r} has no usable created_at"
        ) from None
    return PodRecord(
        pod_id=pod_id,
        name=str(raw.get("name") or ""),
        created_at=created_at_value,
        gpu_id=raw.get("gpu_id") or None,
        gpu_count=int(raw.get("gpu_count", 1)),
        data_center_id=raw.get("data_center_id") or None,
        stack=str(raw.get("stack") or stack),
    )


class PodRegistry:
    """Read/write the pod records at ``<home>\\pod.json`` (atomic writes).

    All methods take an optional *stack* (default ``"comfy"``) so the
    pre-multi-stack call sites keep working unchanged.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else default_home() / "pod.json"

    @property
    def path(self) -> Path:
        return self._path

    def _read_raw(self) -> Optional[dict]:
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Pod registry file is unreadable; ignoring it")
            return None
        if not isinstance(raw, dict):
            logger.warning("Pod registry file is malformed; ignoring it")
            return None
        return raw

    def _read_all_raw(self) -> dict[str, dict]:
        """Current on-disk records as ``{stack: raw record dict}``.

        Legacy version-1 files map to a single ``agent`` entry. Corrupt or
        unknown-version content degrades to an empty mapping (safe warning).
        """
        raw = self._read_raw()
        if raw is None:
            return {}
        version = raw.get("version")
        if version == CURRENT_VERSION:
            pods = raw.get("pods")
            if not isinstance(pods, dict):
                return {}
            return {k: v for k, v in pods.items() if isinstance(v, dict)}
        if version == LEGACY_VERSION:
            return {
                "comfy": {
                    "pod_id": raw.get("pod_id"),
                    "name": raw.get("name"),
                    "created_at": raw.get("created_at"),
                    "gpu_id": raw.get("gpu_id"),
                    "gpu_count": raw.get("gpu_count", 1),
                    "data_center_id": raw.get("data_center_id"),
                    "stack": "comfy",
                }
            }
        logger.warning("Pod registry file has an unknown version; ignoring it")
        return {}

    def load(self, stack: str = "comfy") -> Optional[PodRecord]:
        """Return the stored record for *stack*, or ``None`` when absent.

        Corrupt content, an unknown version, or missing fields degrade to
        ``None`` with a single safe warning (never raise, never print the
        raw file content).
        """
        entry = self._read_all_raw().get(stack)
        if entry is None:
            return None
        try:
            return _record_from_dict(entry, stack)
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "Pod registry record for stack %r is missing required fields; "
                "ignoring it", stack,
            )
            return None

    def all(self) -> dict[str, PodRecord]:
        """Return every stored record keyed by stack (usable entries only)."""
        result: dict[str, PodRecord] = {}
        for stack, entry in self._read_all_raw().items():
            try:
                result[stack] = _record_from_dict(entry, stack)
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "Pod registry record for stack %r is unusable; ignoring it",
                    stack,
                )
        return result

    def _write(self, records: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".pod-", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": CURRENT_VERSION, "pods": records}, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def save(self, record: PodRecord, stack: Optional[str] = None) -> None:
        """Atomically store *record* under *stack* (default ``record.stack``).

        Other stacks' records are preserved; a legacy version-1 file is
        upgraded in place (its single pod becomes the ``agent`` entry).
        """
        stack = stack or record.stack
        records = self._read_all_raw()
        records[stack] = {
            "pod_id": record.pod_id,
            "name": record.name,
            "created_at": record.created_at,
            "gpu_id": record.gpu_id,
            "gpu_count": record.gpu_count,
            "data_center_id": record.data_center_id,
            "stack": stack,
        }
        self._write(records)

    def clear(self, stack: str = "comfy") -> None:
        """Remove the record for *stack* (idempotent; a missing file is fine).

        When the last record is removed the file itself is deleted.
        """
        records = self._read_all_raw()
        if stack not in records:
            return
        records.pop(stack)
        if not records:
            try:
                self._path.unlink()
            except OSError:
                pass
        else:
            self._write(records)

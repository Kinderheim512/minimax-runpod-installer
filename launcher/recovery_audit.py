"""Dedicated JSONL audit log for infrastructure recovery actions.

Every write-risk recovery action appends one JSON line under
``MINIMAX_LAUNCHER_STATE_DIR`` (falling back to the per-user temp directory) at
``recovery-audit.jsonl`` with the shape::

    {"ts", "action", "authorized", "confirm", "started_at", "ended_at",
     "duration_ms", "before", "after", "ok", "error_class", "reason"}

The ``reason`` and error classifications are redacted before writing; no
secrets, raw logs, command output, or diagnostic payloads are stored.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import os
import tempfile

import json
import time
from pathlib import Path
from typing import Optional, Union

from . import logging as launcher_logging
from .logging import redact as _redact


logger = launcher_logging.get_logger("minimax-launcher.recovery_audit")


class RecoveryAudit:
    """Append-only JSONL audit log for recovery actions."""

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self.path = Path(path) if path is not None else state_dir() / "recovery-audit.jsonl"

    def record(
        self,
        action: str,
        authorized: bool,
        confirm: bool,
        started_at: float,
        ended_at: float,
        duration_ms: int,
        before: str,
        after: str,
        ok: bool,
        error_class: Optional[str],
        reason: Optional[str] = None,
    ) -> None:
        entry = {
            "ts": time.time(),
            "action": action,
            "authorized": authorized,
            "confirm": confirm,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "before": _redact(before),
            "after": _redact(after),
            "ok": ok,
            "error_class": error_class,
        }
        if reason is not None and reason != "":
            entry["reason"] = _redact(reason)
        # The audit is best-effort: a failed append (disk full, locked file,
        # AV interference) must never fail the action being audited or the
        # process calling it.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.warning("recovery audit append failed: %s", exc)


def state_dir() -> Path:
    """Return the launcher state directory.

    Honors ``MINIMAX_LAUNCHER_STATE_DIR`` (the same variable the launcher's
    runtime state uses) and falls back to the per-user temp directory.
    """
    base = os.environ.get("MINIMAX_LAUNCHER_STATE_DIR")
    if base:
        return Path(base).resolve()
    return Path(tempfile.gettempdir()) / "minimax-launcher"

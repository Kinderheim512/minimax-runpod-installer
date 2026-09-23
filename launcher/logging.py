"""Logging foundation for the MiniMax H3 Launcher.

Provides a small, structured logging abstraction with:

* clear log levels (DEBUG, INFO, OK, WARN, ERROR);
* human-readable console output;
* English-only messages;
* a secret-redaction filter so no credentials ever reach the log.

The future application is expected to emit messages such as::

    [INFO] Loading configuration
    [OK]   Configuration validated
    [ERROR] Configuration is invalid
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import logging
import re
import sys
from typing import Optional

# Custom level for the "OK" success marker, between INFO and WARNING.
_OK_LEVEL = 25
logging.addLevelName(_OK_LEVEL, "OK")

#: Logger the module-level :func:`ok` writes through. Deliberately NOT the root
#: logger: ``logging.log()`` on the root triggers the stdlib's ``basicConfig``,
#: which installs an *unfiltered* handler the moment the root logger happens to
#: be empty. A named logger never does that, and still propagates to the root
#: handlers (and therefore to the secret filter).
_OK_LOGGER = logging.getLogger("minimax-launcher")

# Recognized secret value shapes. These patterns are intentionally conservative:
# they only match obvious token/key/value shapes so they cannot strip normal
# prose from a message. Patterns with a leading label (e.g. "api_key=") capture
# that label in group 1 so it is preserved in the replacement; patterns that
# match only the token itself have no capture group and are replaced entirely.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bearer tokens / authorization headers.
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+"),
    # Common key/token assignments: KEY=value or "key": "value".
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*)\S+"),
    # Hugging Face tokens: hf_...
    re.compile(r"\bhf_[A-Za-z0-9_-]+"),
    # Generic long-ish hex/base64 token strings (>= 24 chars). The trailing
    # group swallows the dotted/slashed continuation of the same identifier, so
    # a redacted token can never leave a convincing fragment behind
    # ("anime_style_character_v2.safetensors" -> "<redacted>", not
    # "<redacted>.safetensors"). This is deliberately *more* redaction: a
    # half-masked identifier is both a leak and a corrupted handle.
    re.compile(r"\b[A-Za-z0-9+/_-]{24,}(?:\.[A-Za-z0-9+/_-]+)*"),
)


def _replace(match: re.Match[str]) -> str:
    if match.lastindex is not None and match.lastindex >= 1:
        # Preserve the captured label (e.g. "api_key=") and redact only the value.
        return match.group(1) + "<redacted>"
    return "<redacted>"


def redact(text: str) -> str:
    """Return *text* with secret-looking values replaced by ``<redacted>``."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_replace, text)
    return text


class _SecretFilter(logging.Filter):
    """Logging filter that redacts secret-looking values from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            args = []
            for arg in record.args:
                args.append(redact(arg) if isinstance(arg, str) else arg)
            record.args = tuple(args)
        return True


def ok(message: str, *args: object) -> None:
    """Log a success message at the custom ``OK`` level."""
    _OK_LOGGER.log(_OK_LEVEL, message, *args)


def _configure_root(level: int) -> logging.Logger:
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        root.addHandler(handler)
    # The secret filter is attached to EVERY root handler, not only to the one
    # created above. A handler installed by someone else — the stdlib's own
    # ``basicConfig`` (triggered by a stray ``logging.log``), the GUI's
    # ``GuiLogHandler``, a third-party import — would otherwise log in clear
    # text while ``setup_logging`` believed redaction was in place.
    for existing in list(root.handlers):
        if not any(isinstance(f, _SecretFilter) for f in existing.filters):
            existing.addFilter(_SecretFilter())
    root.setLevel(level)
    return root


def _parse_level(name: Optional[str]) -> int:
    if name is None:
        return logging.INFO
    normalized = name.strip().lower()
    levels = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "ok": _OK_LEVEL,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    if normalized not in levels:
        raise ValueError(f"Unknown log level: {name!r}")
    return levels[normalized]


def setup_logging(level: Optional[str] = None) -> None:
    """Configure the root logger with console output and secret redaction.

    Parameters
    ----------
    level:
        Optional log level name (``debug``, ``info``, ``ok``, ``warn``,
        ``error``). Defaults to ``info``.
    """
    _configure_root(_parse_level(level))


def get_logger(name: str) -> logging.Logger:
    """Return a named child logger using the package's formatting.

    The returned logger inherits the root handler (and its secret filter), so
    callers do not need to attach their own handler. It additionally exposes an
    ``ok`` method mirroring the module-level :func:`ok`.
    """
    logger = logging.getLogger(name)
    if not hasattr(logger, "ok"):

        def _ok(message: str, *args: object, _logger: logging.Logger = logger) -> None:
            """Log a success message at the custom ``OK`` level."""
            _logger.log(_OK_LEVEL, message, *args)

        logger.ok = _ok  # type: ignore[attr-defined]
    return logger

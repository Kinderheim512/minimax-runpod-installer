"""Tests for the logging foundation."""

import logging

from launcher import logging as launcher_logging


def test_redact_masks_api_key_assignment() -> None:
    assert launcher_logging.redact("api_key=abcdef") == "api_key=<redacted>"


def test_redact_masks_bearer_token() -> None:
    result = launcher_logging.redact("Authorization: Bearer tok1234567890")
    assert "tok1234567890" not in result
    assert "Bearer" in result


def test_redact_masks_huggingface_token() -> None:
    assert launcher_logging.redact("hf_abcDEF1234567890xyz") == "<redacted>"


def test_redact_masks_hf_token_assignment() -> None:
    result = launcher_logging.redact("syncing HF_TOKEN=hf_abcDEF1234567890xyz")
    assert "hf_abcDEF1234567890xyz" not in result
    assert "HF_TOKEN=" in result


def test_redact_masks_civitai_api_key_assignment() -> None:
    result = launcher_logging.redact("CIVITAI_API_KEY=abc123def456ghi789")
    assert "abc123def456ghi789" not in result
    assert "CIVITAI_API_KEY=" in result


def test_redact_leaves_normal_text_untouched() -> None:
    assert launcher_logging.redact("Loading configuration") == "Loading configuration"


def test_redact_never_emits_a_plausible_partial_identifier() -> None:
    """A redacted run must not leave a convincing fragment behind.

    ``anime_style_character_v2.safetensors`` used to become
    ``<redacted>.safetensors``: a *corrupted* identifier that still looks like a
    real file name, so the operator (and the agent) could act on it.
    """
    result = launcher_logging.redact("installed anime_style_character_v2.safetensors")
    assert result == "installed <redacted>"
    assert "safetensors" not in result


def test_redact_swallows_a_long_path_segment_whole() -> None:
    """The whole run (including ``/``) is masked, leaving no fragment behind."""
    result = launcher_logging.redact("GET /api/v1/models/abcdefghijklmnopqrstuvwx")
    assert result == "GET /<redacted>"
    assert "abcdefghijklmnopqrstuvwx" not in result


def test_setup_logging_installs_the_filter_on_existing_handlers() -> None:
    """Redaction must cover handlers the stdlib/other code installed first.

    ``ok()`` used to reach ``logging.log``, which calls ``basicConfig()`` and
    installs an *unfiltered* stderr handler as soon as the root logger has no
    handlers. ``_configure_root`` then saw a non-empty handler list and never
    added the secret filter — so every later record was logged in clear text.
    """
    import logging

    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    try:
        root.handlers = []
        stray = logging.StreamHandler()
        root.addHandler(stray)
        launcher_logging.setup_logging("info")
        assert any(
            isinstance(f, launcher_logging._SecretFilter) for f in stray.filters
        ), "the pre-existing root handler must be filtered"
    finally:
        root.handlers = saved
        root.setLevel(saved_level)


def test_ok_does_not_install_an_unfiltered_handler() -> None:
    """``ok()`` must never make the stdlib add a handler of its own."""
    import logging

    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    try:
        root.handlers = []
        launcher_logging.ok("stray ok line")
        launcher_logging.setup_logging("info")
        assert len(root.handlers) == 1, root.handlers
        assert all(
            any(isinstance(f, launcher_logging._SecretFilter) for f in h.filters)
            for h in root.handlers
        ), [h.filters for h in root.handlers]
    finally:
        root.handlers = saved
        root.setLevel(saved_level)


def test_ok_reaches_the_root_handler() -> None:
    """``logger.ok`` keeps flowing through the root logger's handlers."""
    import logging

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    try:
        root.handlers = []
        launcher_logging.setup_logging("info")
        collector = _Collect(level=logging.DEBUG)
        root.addHandler(collector)
        launcher_logging.get_logger("test.ok").ok("pod ready")
        assert [r.getMessage() for r in records] == ["pod ready"]
    finally:
        root.handlers = saved
        root.setLevel(saved_level)


def test_setup_logging_accepts_valid_level() -> None:
    launcher_logging.setup_logging("info")
    root = logging.getLogger()
    assert root.level == logging.INFO


def test_setup_logging_rejects_unknown_level() -> None:
    import pytest

    with pytest.raises(ValueError):
        launcher_logging.setup_logging("bogus")


def test_get_logger_returns_named_logger() -> None:
    logger = launcher_logging.get_logger("test.mod")
    assert logger.name == "test.mod"

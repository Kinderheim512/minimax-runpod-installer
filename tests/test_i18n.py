"""Tests for the launcher language layer."""

import pytest

from launcher import i18n


def test_default_language_is_us_english() -> None:
    i18n.set_language(None)
    assert i18n.get_language() == "en_US"


def test_language_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(i18n.LANGUAGE_ENV, "fr")
    i18n.set_language(None)
    assert i18n.get_language() == "fr"
    i18n.set_language("en_US")


def test_unknown_language_falls_back_to_us(monkeypatch) -> None:
    """A typo must never keep the launcher from starting."""
    monkeypatch.setenv(i18n.LANGUAGE_ENV, "klingon")
    i18n.set_language(None)
    assert i18n.get_language() == "en_US"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("en", "en_US"),
        ("EN_US", "en_US"),
        ("us", "en_US"),
        ("  fr  ", "fr"),
        ("FR-FR", "fr"),
        ("", "en_US"),
        (None, "en_US"),
        ("nonsense", "en_US"),
    ],
)
def test_normalize_language(raw, expected) -> None:
    assert i18n.normalize_language(raw) == expected


def test_t_is_the_identity_in_english() -> None:
    i18n.set_language("en_US")
    assert i18n.t("Start the pod") == "Start the pod"


def test_t_translates_to_french() -> None:
    i18n.set_language("fr")
    try:
        assert i18n.t("Start") == "Démarrage"
        assert i18n.t("Library") == "Bibliothèque"
    finally:
        i18n.set_language("en_US")


def test_t_falls_back_to_the_english_source_when_untranslated() -> None:
    """A missing translation shows the English sentence, never a raw key."""
    i18n.set_language("fr")
    try:
        assert i18n.t("Some untranslated sentence") == "Some untranslated sentence"
    finally:
        i18n.set_language("en_US")


def test_t_formats_placeholders() -> None:
    i18n.set_language("en_US")
    assert i18n.t("Stop {0}?", "comfy") == "Stop comfy?"


def test_t_degrades_when_placeholders_do_not_match() -> None:
    """A bad format string must not raise inside a UI callback."""
    i18n.set_language("en_US")
    assert i18n.t("No placeholder here", "extra") == "No placeholder here"


def test_t_tolerates_non_string_input() -> None:
    i18n.set_language("en_US")
    assert i18n.t("") == ""
    assert i18n.t(None) is None  # type: ignore[arg-type]


def test_supported_languages_are_labelled() -> None:
    for code in i18n.SUPPORTED_LANGUAGES:
        assert i18n.LANGUAGE_LABELS.get(code)


def test_french_table_covers_the_launcher_chrome() -> None:
    """The strings a user meets first must all have a French rendering."""
    from launcher.locales import fr

    for key in (
        "Dashboard",
        "ComfyUI",
        "LoRA training",
        "Models & presets",
        "Library",
        "Start",
        "Stop",
        "Terminate the pod",
        "Repair",
        "Quit",
        "Settings saved.",
        "Stack health",
        "Active stacks",
        "Pod & GPU",
        "Journal",
    ):
        assert key in fr.MSG, key

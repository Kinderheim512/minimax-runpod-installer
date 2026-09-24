"""Guard: no accented literal lives outside ``launcher/locales``.

The launcher's source is English; French (and anything else) belongs in a
locale table. This is the machine-checked form of the project rule, and it
exists because the rule is easy to break one comment at a time:

* ``grep -rE "[eéè...]" launcher/*.py`` must only ever match a locale table —
  ``launcher/locales/*`` is where accents are allowed, and required;
* an accented character anywhere else (a comment, a docstring, a UI string)
  fails here.

The check is deliberately a *whole-file* scan, not an AST walk over string
literals: a French comment is just as much of a leftover as a French label,
and the grep the rule is written against cannot tell them apart either.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

from launcher import i18n

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_DIR = REPO_ROOT / "launcher"
LOCALES_DIR = LAUNCHER_DIR / "locales"

#: Latin letters with a diacritic. Broader than the grep in the project rule
#: (which omits ``â``/``ü``/``œ``…): a leftover is a leftover.
_ACCENTED = re.compile(r"[^\x00-\x7f]")

#: Characters that are not "accented letters" and are legitimate in English
#: source: typography (em dash, ellipsis, guillemets), symbols (arrows, boxes)
#: and emoji. The rule is about *language*, not about ASCII purity.
_ALLOWED_NON_ASCII_CATEGORIES = frozenset({"Pd", "Po", "Ps", "Pe", "Pi", "Pf", "Sm", "So", "Sk"})

#: Emoji variation selectors (U+FE0E/U+FE0F) are ``Mn`` (nonspacing mark) like
#: a combining accent, but they carry no language: they pick a glyph style for
#: the emoji they follow (``✏️``).
_VARIATION_SELECTORS = frozenset({"\ufe0e", "\ufe0f"})


def _offending_characters(text: str) -> list[tuple[int, str]]:
    """``(line_number, character)`` for every accented LETTER in *text*."""
    offenders: list[tuple[int, str]] = []
    for number, line in enumerate(text.split("\n"), start=1):
        for character in _ACCENTED.findall(line):
            if character.isspace() or character in _VARIATION_SELECTORS:
                continue
            category = unicodedata.category(character)
            if category in _ALLOWED_NON_ASCII_CATEGORIES:
                continue
            # Anything left is a letter with a diacritic (Ll/Lu/Lt/Lm/Lo).
            offenders.append((number, character))
    return offenders


def _source_files() -> list[Path]:
    """Every top-level launcher module — the set the project rule greps."""
    return sorted(path for path in LAUNCHER_DIR.glob("*.py"))


def test_no_accented_letters_outside_the_locale_tables() -> None:
    """``grep -rE "[éèêàçùûôîï]" launcher/*.py`` returns only the locales."""
    failures: list[str] = []
    for path in _source_files():
        for number, character in _offending_characters(
            path.read_text(encoding="utf-8")
        ):
            failures.append(
                f"{path.relative_to(REPO_ROOT)}:{number}: {character!r} "
                f"({unicodedata.name(character, '?')})"
            )
    assert not failures, (
        "accented literals outside launcher/locales/ — move the string into "
        "launcher/locales/fr.py and call t() on it:\n  " + "\n  ".join(failures)
    )


def test_the_locale_tables_are_where_accents_are_allowed() -> None:
    """The French table must still be accented — otherwise the rule is empty.

    A guard that passes because the translations were deleted is not a guard.
    """
    french = (LOCALES_DIR / "fr.py").read_text(encoding="utf-8")
    assert _offending_characters(french), "launcher/locales/fr.py lost its accents"
    assert "MSG" in french


@pytest.mark.parametrize("language", i18n.SUPPORTED_LANGUAGES)
def test_every_supported_language_has_a_locale_module(language: str) -> None:
    """``locales/<code>.py`` exists for every supported language.

    English included: symmetry is what lets the loader, the tests and any
    tooling treat the default language like any other.
    """
    module = LOCALES_DIR / f"{language}.py"
    assert module.is_file(), f"missing locale module for {language!r}"


def test_the_default_language_is_english() -> None:
    """The default is ``en_US``, and ``MINIMAX_LANG`` selects the language."""
    assert i18n.DEFAULT_LANGUAGE == "en_US"
    assert i18n.LANGUAGE_ENV == "MINIMAX_LANG"
    assert i18n.normalize_language(None) == "en_US"
    assert i18n.normalize_language("") == "en_US"
    assert i18n.normalize_language("en") == "en_US"


def test_english_is_the_identity_mapping() -> None:
    """The English table must not translate anything: the source IS English."""
    from launcher.locales import en_US

    assert en_US.MSG == {}
    assert i18n.t("Start") == "Start"

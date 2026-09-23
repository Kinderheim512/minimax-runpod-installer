"""Language layer for the MiniMax H3 Launcher.

The launcher ships in English (``en_US``) and can run in French (``fr``).
Source strings are English; :func:`t` returns the active language's rendering
of the string it is given, so a call site reads::

    self._label(row, text=t("Start"))

which is the gettext convention: the English text is the key, and the French
table maps it back. That keeps the code English-readable *and* the French
translation complete, without inventing an artificial key namespace that no
one can grep.

Resolution order for :func:`t`:

1. the active language's table;
2. the default language's table (so a missing French entry falls back to
   English rather than to a raw key);
3. the input string itself.

The last rule is what makes a missing translation harmless: the UI shows the
English sentence, never ``launcher.tab.dashboard``.

The language is chosen by, in order: :func:`set_language`, the
``LAUNCHER_LANG`` environment variable, the persisted GUI setting, then
``en_US``. An unrecognised value falls back to ``en_US`` instead of failing —
a typo must never stop the launcher from starting.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import os
from typing import Callable, Mapping, Optional

from . import locales

#: Canonical language codes, in the order the selector shows them.
DEFAULT_LANGUAGE = "en_US"
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en_US", "fr")

#: How each language names itself in the selector (owned by the locales
#: package: the native French name carries an accent, which no other module
#: outside ``launcher/locales`` is allowed to).
LANGUAGE_LABELS: Mapping[str, str] = locales.LANGUAGE_LABELS

#: Environment variable that picks the language.
LANGUAGE_ENV = "LAUNCHER_LANG"

#: Accepts the loose spellings a user may type.
_ALIASES: Mapping[str, str] = {
    "en": "en_US",
    "en-us": "en_US",
    "en_us": "en_US",
    "us": "en_US",
    "english": "en_US",
    "fr": "fr",
    "fr-fr": "fr",
    "fr_fr": "fr",
    "francais": "fr",
    "french": "fr",
}
_ALIASES = {**_ALIASES, **locales.LANGUAGE_ALIASES}

_active: Optional[str] = None
_tables: dict[str, Mapping[str, str]] = {}


def normalize_language(value: Optional[str]) -> str:
    """Map a user-provided language name to a supported code.

    Anything unrecognised (including ``None`` and an empty string) becomes
    :data:`DEFAULT_LANGUAGE`: a typo must never keep the launcher from
    starting.
    """
    if not isinstance(value, str):
        return DEFAULT_LANGUAGE
    key = value.strip().lower().replace(" ", "")
    if not key:
        return DEFAULT_LANGUAGE
    resolved = _ALIASES.get(key)
    if resolved in SUPPORTED_LANGUAGES:
        return resolved
    if key in SUPPORTED_LANGUAGES:
        return key
    return DEFAULT_LANGUAGE


def _load_table(language: str) -> Mapping[str, str]:
    """Import the locale module for *language* (cached)."""
    if language in _tables:
        return _tables[language]
    try:
        module = __import__(
            f"launcher.locales.{language}", fromlist=["MSG"]
        )
        table = getattr(module, "MSG", {})
    except Exception:  # noqa: BLE001 - a broken locale must not stop the app
        table = {}
    if not isinstance(table, Mapping):
        table = {}
    _tables[language] = table
    return table


def get_language() -> str:
    """The active language code (``en_US`` unless something says otherwise)."""
    global _active
    if _active is None:
        _active = normalize_language(os.environ.get(LANGUAGE_ENV))
    return _active


def set_language(value: Optional[str]) -> str:
    """Force the active language; returns the code actually applied.

    ``None`` clears the override so the next :func:`get_language` re-reads the
    environment — which is what a test (or a settings reload) wants.
    """
    global _active
    if value is None:
        _active = None
        return get_language()
    _active = normalize_language(value)
    return _active


def t(text: str, *args: object) -> str:
    """Render *text* in the active language.

    ``args`` are interpolated with ``str.format`` semantics when the
    translation contains placeholders, so a call site stays:
    ``t("Stop {name}?", name)``. A translation whose placeholders do not match
    the arguments degrades to the untranslated English text rather than
    raising inside a UI callback.
    """
    if not isinstance(text, str) or not text:
        return text
    language = get_language()
    if language != DEFAULT_LANGUAGE:
        translated = _load_table(language).get(text)
        if translated is not None:
            return _format(translated, args, fallback=text)
    return _format(text, args, fallback=text)


def _format(template: str, args: tuple, *, fallback: str) -> str:
    if not args:
        return template
    try:
        return template.format(*args)
    except (IndexError, KeyError, ValueError):
        return fallback


def translator() -> Callable[..., str]:
    """Return :func:`t` — the seam tests and the GUI bind to."""
    return t

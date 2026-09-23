"""Locale tables for the launcher UI.

One module per language (``fr.py``, …), each exposing ``MSG``: an English
source string mapped to that language's rendering. English itself needs no
table — the source string is already English — so ``en_US`` has no module and
``launcher.i18n`` resolves it to the identity.

See :mod:`launcher.i18n` for the lookup rules and
:mod:`launcher.locales.fr` for the shape of an entry.
"""

#: How each language names itself in the selector. The native name is
#: deliberately kept in its own language ("Français", not "French"), which is
#: why these two entries — and only these — carry accented letters outside a
#: locale table.
LANGUAGE_LABELS = {
    "en_US": "English (US)",
    "fr": "Français",
}

#: Loose spellings a user may type, including the native one.
LANGUAGE_ALIASES = {
    "français": "fr",
}

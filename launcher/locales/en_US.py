"""English locale table for the launcher UI.

English needs no translations: the source strings in the code *are* English,
so :func:`launcher.i18n.t` resolves ``en_US`` to the identity. This module
exists anyway, for two reasons:

* the locale set is symmetric — ``launcher/locales/en_US.py`` and
  ``launcher/locales/fr.py`` both define ``MSG``, so the loader, the tests and
  any tooling treat the two the same way instead of special-casing the default;
* it gives the language a place to hold the few strings that are *not*
  identity even in English (none today).

``MSG`` is intentionally empty. Adding an entry here would override the source
string for English speakers, which is never what a translation should do —
that is what the French table is for.
"""

from __future__ import annotations

MSG: dict[str, str] = {}

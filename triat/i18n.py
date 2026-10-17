"""Internationalisation (gettext) de Triat.

La langue source est le français : les `msgid` sont les chaînes françaises
telles qu'affichées. Sans catalogue (`.mo`) pour la langue de l'utilisateur,
`_()` renvoie donc la chaîne telle quelle et `ngettext` applique la règle du
pluriel français (« 0 titre », « 1 titre », « 2 titres »).

Les catalogues sont compilés par `tools/i18n.sh` dans `triat/locale/` (dépôt)
ou installés dans `<préfixe>/share/locale` (paquet, Flatpak).
"""

from __future__ import annotations

import gettext
import locale
import sys
from pathlib import Path

DOMAIN = "triat"


class _SourceTranslations(gettext.NullTranslations):
    """Repli sans catalogue : identité, pluriel français (n > 1)."""

    def ngettext(self, msgid1: str, msgid2: str, n: int) -> str:
        return msgid1 if n <= 1 else msgid2


def locale_dir() -> str:
    """`triat/locale` s'il existe (dépôt), sinon `<préfixe>/share/locale`."""
    local = Path(__file__).parent / "locale"
    if local.is_dir():
        return str(local)
    return str(Path(sys.prefix) / "share" / "locale")


_translations: gettext.NullTranslations = _SourceTranslations()


def setup(localedir: str | None = None, languages: list[str] | None = None) -> None:
    """Branche le domaine `triat`. Sans .mo, repli transparent sur le français."""
    global _translations
    localedir = localedir or locale_dir()
    # Pour GTK / libadwaita (chaînes de Gtk.Builder, le cas échéant).
    for bind in (getattr(locale, "bindtextdomain", None), gettext.bindtextdomain):
        if bind is not None:
            try:
                bind(DOMAIN, localedir)
            except OSError:
                pass
    gettext.textdomain(DOMAIN)
    found = gettext.translation(DOMAIN, localedir, languages=languages, fallback=True)
    _translations = (_SourceTranslations()
                     if type(found) is gettext.NullTranslations else found)


def _(message: str) -> str:
    """Traduit `message` (msgid français)."""
    return _translations.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translations.ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    return _translations.pgettext(context, message)


def nformat(singular: str, plural: str, n: int, **fields) -> str:
    """`ngettext` puis substitution de `{n}` (et des `fields` éventuels) :
    « {n} titre » / « {n} titres »."""
    return ngettext(singular, plural, n).format(n=n, **fields)


setup()

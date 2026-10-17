"""Rail compact : l'icône de navigation est centrée dans le cercle actif."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402

from triat.ui.window import Sidebar  # noqa: E402


def test_compact_rail_centres_nav_icons():
    sidebar = Sidebar(on_navigate=lambda _key: None)
    assert sidebar._nav_contents, "aucun bouton de navigation construit"

    sidebar.set_compact(True)
    assert all(c.get_halign() == Gtk.Align.CENTER for c in sidebar._nav_contents)

    sidebar.set_compact(False)
    assert all(c.get_halign() == Gtk.Align.FILL for c in sidebar._nav_contents)

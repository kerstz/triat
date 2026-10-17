"""Page Stats : état vide, chiffres, onglet de la bibliothèque."""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw

from triat.core.models import TrackRef
from triat.ui.library_page import LibraryPage
from triat.ui.stats_page import StatsPage
from tests.test_window import engine  # noqa: F401  (fixture)

Adw.init()


def test_empty_state(engine):                            # noqa: F811
    page = StatsPage(engine)
    page.show_stats(engine.library.stats_period(None))
    assert page.empty.get_visible()
    assert not page.content.get_visible()


def test_figures_and_rankings(engine):                   # noqa: F811
    ref = TrackRef("a" * 11, "Titre", "Artiste", 100.0)
    engine.library.add_history_entry(ref, time.time() - 10, 90.0)
    engine.library.add_history_entry(ref, time.time() - 20, 90.0)
    page = StatsPage(engine)
    page.show_stats(engine.library.stats_period(None))
    assert not page.empty.get_visible() and page.content.get_visible()
    assert page.plays.value.get_text() == "2"
    assert page.hours.value.get_text() == "3 min"
    assert page.top_tracks.list.get_row_at_index(0) is not None
    assert page.top_tracks.list.get_row_at_index(1) is None
    assert page.top_artists.list.get_row_at_index(0) is not None


def test_stale_result_is_ignored(engine):                # noqa: F811
    page = StatsPage(engine)
    page._token = 5
    page._apply(4, {"plays": 0})
    assert page.last_stats is None


def test_library_has_a_stats_tab(engine):                # noqa: F811
    page = LibraryPage(engine, lambda refs, i: None)
    page._tab_buttons["stats"].set_active(True)
    assert page.views.get_visible_child_name() == "stats"
    page._tab_buttons["playlists"].set_active(True)
    assert page.views.get_visible_child_name() == "playlists"

"""Bibliothèque et bienvenue : états vides, comptages, layout étroit."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from triat.core import desktop
from triat.core.models import TrackRef
from triat.ui.library_page import LibraryPage
from triat.ui.onboarding import OnboardingView
from tests.test_window import engine  # noqa: F401  (fixture)

Adw.init()


# ---- Bibliothèque -----------------------------------------------------------

def _page(engine):                                       # noqa: F811
    return LibraryPage(engine, lambda refs, i: None)


def test_empty_tabs_show_an_empty_state(engine):         # noqa: F811
    page = _page(engine)
    for key, title in (("favorites", "Aucun favori"),
                       ("history", "Aucune écoute")):
        page._tab_buttons[key].set_active(True)
        assert page.plain_empty.get_visible()
        assert page.plain_empty._title.get_text() == title
        assert not page.plain.get_visible()
    page._tab_buttons["artists"].set_active(True)
    assert page.artists_empty.get_visible()


def test_tab_with_content_hides_the_empty_state(engine):  # noqa: F811
    engine.library.set_favorite(TrackRef("a" * 11, "Titre", "Artiste", 100.0), True)
    page = _page(engine)
    page._tab_buttons["favorites"].set_active(True)
    assert not page.plain_empty.get_visible()
    assert page.plain.get_visible()
    assert page.status.get_text() == "1 favori"


def test_smart_counts_do_not_load_rows(engine, monkeypatch):  # noqa: F811
    engine.library.upsert_track(TrackRef("b" * 11, "Neuf", "Artiste", 100.0))
    engine.library.set_favorite(TrackRef("c" * 11, "Aimé", "Artiste", 100.0), True)
    page = _page(engine)
    monkeypatch.setattr(engine.library, "unplayed",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("lignes")))
    monkeypatch.setattr(engine.library, "added_since",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("lignes")))
    page._load_playlists()
    counts = {tile.smart.key: tile.count.get_text() for tile in page.smart_tiles}
    assert counts["unplayed"] == "1"            # « Aimé » : 1 favori, jamais lu
    assert counts["recent"] == "1"


def test_library_counts_match_the_row_queries(engine):    # noqa: F811
    library = engine.library
    library.set_favorite(TrackRef("d" * 11, "A", "X", 10.0), True)
    library.upsert_track(TrackRef("e" * 11, "Cache", "Y", 10.0))   # non possédée
    assert library.count_unplayed(owned=True) == len(library.unplayed(owned=True))
    assert library.count_unplayed() == len(library.unplayed())
    assert library.count_added_since(0.0, owned=True) == len(
        library.added_since(0.0, owned=True))
    assert library.count_top_tracks(0.0) == len(library.top_tracks(since=0.0))


def test_section_title_is_french(engine):                # noqa: F811
    page = _page(engine)
    texts = []

    def walk(widget):
        while widget is not None:
            if isinstance(widget, Gtk.Label):
                texts.append(widget.get_text())
            walk(widget.get_first_child())
            widget = widget.get_next_sibling()

    walk(page)
    assert "Listes intelligentes" in texts
    assert "Smart playlists" not in texts


# ---- Bienvenue --------------------------------------------------------------

def test_onboarding_fits_a_narrow_window(engine):        # noqa: F811
    view = OnboardingView(engine, lambda: None)
    minimum, _natural, _a, _b = view.measure(Gtk.Orientation.HORIZONTAL, -1)
    assert minimum <= 360
    assert isinstance(view.scroller, Gtk.ScrolledWindow)


def test_onboarding_has_a_previous_button(engine):       # noqa: F811
    view = OnboardingView(engine, lambda: None)
    assert not view._previous.get_visible()
    view._next()
    assert view._previous.get_visible()
    view._back()
    assert view._step == 0


def test_account_step_offers_one_way_forward(engine):    # noqa: F811
    view = OnboardingView(engine, lambda: None)
    assert view._skip.get_visible() and not view._next_button.get_visible()
    view._signed_in = True
    view._update_buttons()
    assert view._next_button.get_visible() and not view._skip.get_visible()


# ---- Navigateurs ------------------------------------------------------------

def test_browser_labels_keep_their_casing():
    assert desktop.browser_label("librewolf") == "LibreWolf"
    assert desktop.browser_label("firefox") == "Firefox"
    assert desktop.browser_label("inconnu") == "Inconnu"


def test_flatpak_and_snap_profiles_are_detected(tmp_path):
    (tmp_path / ".var/app/org.mozilla.firefox/.mozilla/firefox").mkdir(parents=True)
    (tmp_path / "snap/chromium/common/chromium").mkdir(parents=True)
    (tmp_path / ".config/vivaldi").mkdir(parents=True)
    found = desktop.detect_browser_profiles(home=tmp_path)
    assert found["firefox"] == str(
        tmp_path / ".var/app/org.mozilla.firefox/.mozilla/firefox")
    assert found["chromium"] == str(tmp_path / "snap/chromium/common/chromium")
    assert found["vivaldi"] is None            # emplacement natif
    assert "brave" not in found


def test_native_profile_wins_over_flatpak(tmp_path):
    (tmp_path / ".mozilla/firefox").mkdir(parents=True)
    (tmp_path / ".var/app/org.mozilla.firefox/.mozilla/firefox").mkdir(parents=True)
    assert desktop.detect_browser_profiles(["firefox"], home=tmp_path) == {
        "firefox": None}

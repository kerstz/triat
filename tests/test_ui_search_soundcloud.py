"""Recherche, badge, lignes et menu SoundCloud (SC-T14)."""

from __future__ import annotations

from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw

from tests.fakes import FakeSoundCloud
from tests.test_ui_lists import spin
from tests.test_window import engine  # noqa: F401  (fixture)
from triat.core import search_results
from triat.core.models import TrackRef
from triat.ui.search_page import SearchPage
from triat.ui.track_actions import TrackActions
from triat.ui.track_list import TrackList
from triat.ui.widgets.cover import Cover

Adw.init()


class Deferred:
    """Moteur de recherche factice, YouTube livré à la main."""

    def __init__(self, engine, soundcloud=None) -> None:
        self.settings = engine.settings
        self.soundcloud = soundcloud
        self.callbacks: list = []
        self.played: list = []

    def search_all_async(self, query, callback) -> None:
        self.callbacks.append((query, callback))


def yt_ref(n: int = 0) -> TrackRef:
    return TrackRef(f"{n:011d}", f"Titre {n}", "Artiste", 120.0)


def yt_results(query="q"):
    return search_results.build(query, [yt_ref(1)], [], [])


def enable(engine, source="both"):
    engine.settings.set("soundcloud_enabled", True)
    engine.settings.set("content_source", source)


def sc_page(engine, sc, source="both"):
    enable(engine, source)
    fake = Deferred(engine, sc)
    plays = []
    page = SearchPage(fake, on_play=lambda refs, pos: plays.append((refs, pos)))
    return page, fake, plays


# ---- Puces de source -----------------------------------------------------------

def test_source_chips_hidden_when_soundcloud_disabled(engine):
    page = SearchPage(Deferred(engine, FakeSoundCloud()), on_play=lambda *_: None)
    page.refresh_sources()
    assert not page.source_row.get_visible()
    assert page.current_source() == "youtube"


def test_source_chips_visible_when_enabled(engine):
    page, _fake, _plays = sc_page(engine, FakeSoundCloud())
    page.refresh_sources()
    assert page.source_row.get_visible()
    assert page.current_source() == "both"


def test_source_choice_is_not_persisted(engine):
    page, _fake, _plays = sc_page(engine, FakeSoundCloud(), source="youtube")
    page._source_buttons["soundcloud"].set_active(True)
    assert page.current_source() == "soundcloud"
    assert engine.settings.get("content_source") == "youtube"


# ---- Recherche ---------------------------------------------------------------------

def test_soundcloud_failure_keeps_youtube_results(engine):
    page, fake, _plays = sc_page(engine, FakeSoundCloud(fail=True))
    page.run_search("daft")
    fake.callbacks[0][1](yt_results("daft"), None)
    assert spin(lambda: not page._sc_pending)
    assert page.results_data.tracks
    assert page.sc_status.get_text() == "SoundCloud injoignable"
    assert page.sc_section.get_visible()
    assert len(page.results.tracks) == 1


def test_youtube_only_search_does_not_call_soundcloud(engine):
    sc = FakeSoundCloud([FakeSoundCloud.make_track(1)])
    page, fake, _plays = sc_page(engine, sc, source="youtube")
    page.run_search("daft")
    fake.callbacks[0][1](yt_results("daft"), None)
    assert sc.calls == []
    assert not page.sc_section.get_visible()


def test_soundcloud_only_skips_youtube(engine):
    sc = FakeSoundCloud([FakeSoundCloud.make_track(1)])
    page, fake, _plays = sc_page(engine, sc, source="soundcloud")
    page.run_search("daft")
    assert spin(lambda: page.results_data is not None)
    assert fake.callbacks == []
    assert [r.video_id for r in page.sc_results.tracks] == ["soundcloud:tracks:1"]


def test_late_soundcloud_answer_is_dropped(engine):
    page, fake, _plays = sc_page(engine, FakeSoundCloud())
    page.run_search("a")
    stale = page._sc_seq
    page.run_search("bb")
    page._sc_done(stale, [FakeSoundCloud.make_track(9)], None)
    assert page._sc_all == []


def test_soundcloud_click_plays_only_playable_tracks(engine):
    tracks = [FakeSoundCloud.make_track(n) for n in (1, 2, 3)]
    tracks[1] = replace(tracks[1], playable=False, reason="snip")
    page, fake, plays = sc_page(engine, FakeSoundCloud(tracks), source="soundcloud")
    page.run_search("daft")
    assert spin(lambda: page.results_data is not None)
    page._on_sc_activated(None, 2)
    refs, start = plays[0]
    assert [r.video_id for r in refs] == ["soundcloud:tracks:1", "soundcloud:tracks:3"]
    assert start == 1
    assert page.sc_status.get_text() == "1 titre ignoré"
    page._on_sc_activated(None, 1)          # non lisible : rien
    assert len(plays) == 1


def test_hide_unplayable_setting_filters_the_section(engine):
    tracks = [FakeSoundCloud.make_track(1),
              replace(FakeSoundCloud.make_track(2), playable=False, reason="drm")]
    engine.settings.set("soundcloud_hide_unplayable", True)
    page, _fake, _plays = sc_page(engine, FakeSoundCloud(tracks), source="soundcloud")
    page.run_search("daft")
    assert spin(lambda: page.results_data is not None)
    assert len(page.sc_results.tracks) == 1


# ---- Badge -----------------------------------------------------------------------------

def test_badge_shown_for_soundcloud_track_only():
    cover = Cover(size=40)
    cover.set_track(FakeSoundCloud.make_track(1).ref, badge=True)
    assert cover.badge_visible
    cover.set_track(yt_ref(), badge=True)
    assert not cover.badge_visible


def test_badge_follows_the_setting():
    cover = Cover(size=40)
    cover.set_track(FakeSoundCloud.make_track(1).ref, badge=False)
    assert not cover.badge_visible


# ---- Lignes et menu --------------------------------------------------------------------

def test_soundcloud_row_subtitle_has_attribution():
    from tests.test_ui_lists import FakeListItem  # noqa: F401
    view = TrackList()
    ref = FakeSoundCloud.make_track(1).ref

    class Item:
        def __init__(self):
            self.ref, self.index = ref, 0

    class Slot:
        def __init__(self):
            self.child = None

        def set_child(self, child):
            self.child = child

        def get_item(self):
            return Item()

    slot = Slot()
    view._on_setup(None, slot)
    view._on_bind(None, slot)
    assert slot._widgets[3].get_text().endswith("· SoundCloud")


def test_unplayable_row_is_greyed_with_tooltip_and_not_activable():
    view = TrackList()
    ref = FakeSoundCloud.make_track(1).ref
    view.set_tracks([ref])
    view.set_unplayable({ref.video_id: "drm"})
    seen = []
    view.connect("track-activated", lambda *_: seen.append(1))
    view._on_activate(None, 0)
    assert seen == []

    class Slot:
        activatable = True

        def set_child(self, child):
            self.child = child

        def get_item(self):
            return view._store.get_item(0)

        def set_activatable(self, value):
            self.activatable = value

    slot = Slot()
    view._on_setup(None, slot)
    view._on_bind(None, slot)
    assert not slot.activatable
    assert slot.child.has_css_class("unplayable")
    assert slot.child.get_tooltip_text() == "Protégé"


def _menu_labels(view) -> list[str]:
    labels = []
    for section in (view._lecture, view._organiser, view._divers):
        for i in range(section.get_n_items()):
            labels.append(section.get_item_attribute_value(
                i, "label", None).get_string())
    return labels


def test_soundcloud_menu_has_no_download():
    view = TrackList()
    view.set_tracks([FakeSoundCloud.make_track(1).ref, yt_ref()])
    view._open_menu(0, None, 0, 0)
    labels = _menu_labels(view)
    assert "Télécharger" not in labels
    assert "Ouvrir sur SoundCloud" in labels and "Copier le lien" in labels
    view._open_menu(1, None, 0, 0)
    assert "Télécharger" in _menu_labels(view)


def test_unplayable_menu_offers_no_queueing():
    view = TrackList()
    ref = FakeSoundCloud.make_track(1).ref
    view.set_tracks([ref])
    view.set_unplayable({ref.video_id: "snip"})
    view._open_menu(0, None, 0, 0)
    labels = _menu_labels(view)
    assert "Lire ensuite" not in labels and "Ajouter à la file" not in labels
    assert "Ouvrir sur SoundCloud" in labels


def test_soundcloud_download_is_refused(engine):
    class Downloads:
        def add(self, refs):
            raise AssertionError("pas de téléchargement SoundCloud")

    engine.downloads = Downloads()
    TrackActions(engine).handle("download", FakeSoundCloud.make_track(1).ref)

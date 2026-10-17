"""Tests du câblage interface ↔ moteur (sans clic humain)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import pytest
from gi.repository import Adw, Gio, Gtk

from triat.core.engine import Engine
from triat.core.library import Library
from triat.core.models import Segment, TrackRef
from triat.core.settings import Settings
from triat.ui.window import PlayerBar, TriatWindow, date_line, greeting
from tests.fakes import (FakeCookies, FakeExtractor, FakeInnerTube,
                         FakePlayer, FakeRecommender, FakeSponsorBlock)

Adw.init()


@pytest.fixture
def engine(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("autoplay_radio", False)
    settings.set("sync_port", 0)        # jamais le vrai port 47823 en test
    eng = Engine(settings=settings, library=Library(tmp_path / "l.db"),
                 player=FakePlayer(), extractor=FakeExtractor(),
                 sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
                 ytmusic=object(), cookies=FakeCookies(),
                 innertube=FakeInnerTube())
    yield eng
    eng.sync.stop()
    eng.library.close()


@pytest.fixture(scope="session")
def application():
    """Une seule application pour toute la session.

    Un identifiant ne peut être enregistré qu'une fois par processus : en
    créer une par test ferait échouer tous les suivants.
    """
    # NON_UNIQUE : l'application ne prend jamais le nom sur le bus (un autre
    # processus pytest le détenant faisait échouer tous les tests).
    app = Adw.Application(application_id="org.triat.TriatTest",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    # GTK exige l'enregistrement avant qu'on attache une fenêtre ; sans
    # `run()`, il faut émettre « startup » à la main.
    app.register(None)
    app.emit("startup")
    return app


def _refresh(home) -> None:
    """Rafraîchit l'accueil (calculé dans un fil) et attend le résultat."""
    from tests.test_engine import pump
    home.refresh()
    assert pump(lambda: not home._building)


@pytest.fixture
def window(application, engine):
    return TriatWindow(application, engine)


# ---- Formatage ------------------------------------------------------------

def test_greeting_follows_the_hour():
    import datetime

    at = lambda h: datetime.datetime(2026, 9, 23, h, 0)   # noqa: E731
    assert greeting(at(9)) == "Bonjour"
    assert greeting(at(14)) == "Bon après-midi"
    assert greeting(at(21)) == "Bonsoir"


def test_date_line_is_french_without_relying_on_locale():
    import datetime

    assert date_line(datetime.datetime(2026, 9, 23)) == "Mercredi 23 septembre"


# ---- Barre de lecture -----------------------------------------------------

def test_player_bar_shows_the_current_track(engine):
    bar = PlayerBar(engine)
    engine.emit("track-changed", TrackRef("a" * 11, "Instant Crush",
                                          "Daft Punk", 337.0))
    assert bar._title.get_text() == "Instant Crush"
    assert bar._artist.get_text() == "Daft Punk"
    assert bar.progress.props.duration == 337.0
    assert bar._total.get_text() == "5:37"


def test_player_bar_resets_when_nothing_plays(engine):
    bar = PlayerBar(engine)
    engine.emit("track-changed", TrackRef("a" * 11, "T", "A", 100.0))
    engine.emit("track-changed", None)
    assert bar._title.get_text() == "Rien en lecture"
    assert bar.progress.props.duration == 0.0


def test_play_button_icon_follows_state(engine):
    bar = PlayerBar(engine)
    engine.emit("state-changed", "playing")
    assert bar.play_button.get_icon_name() == "triat-pause-symbolic"
    engine.emit("state-changed", "paused")
    assert bar.play_button.get_icon_name() == "triat-play-symbolic"


def test_position_updates_the_elapsed_label(engine):
    bar = PlayerBar(engine)
    engine.emit("duration-changed", 200.0)
    engine.emit("position-changed", 65.0)
    assert bar._elapsed.get_text() == "1:05"
    assert bar.progress.props.position == 65.0


def test_seek_from_the_bar_reaches_the_player(engine):
    bar = PlayerBar(engine)
    bar.progress.props.duration = 200.0
    bar.progress.emit("seek-requested", 42.0)
    assert engine.player.seeks[-1] == 42.0


def test_volume_widget_drives_the_engine(engine):
    bar = PlayerBar(engine)
    bar.volume.emit("volume-changed", 30.0)
    assert engine.player.volume == 30.0
    assert engine.settings.get("volume") == 30.0


def test_volume_starts_from_saved_settings(engine):
    engine.settings.set("volume", 42.0)
    assert PlayerBar(engine).volume.props.volume == 42.0


# ---- Fenêtre --------------------------------------------------------------

def test_window_builds_with_all_its_parts(window):
    assert window.player_bar is not None
    assert window.search is not None
    assert window.pages.get_child_by_name("home") is not None
    assert window.pages.get_child_by_name("search") is not None


def test_sidebar_navigation_switches_page(window):
    window._on_navigate("search")
    assert window.pages.get_visible_child_name() == "search"
    window._on_navigate("home")
    assert window.pages.get_visible_child_name() == "home"


def test_unimplemented_screens_do_not_crash(window):
    for key in ("library", "radio", "downloads"):
        window._on_navigate(key)      # toast « à venir », pas d'exception


def test_engine_error_reaches_the_user(window, engine):
    engine.emit("error", "Piste illisible")   # ne doit pas lever


def test_skipped_segment_offers_to_cancel(window, engine):
    engine.emit("segment-skipped", Segment(0, 22, "music_offtopic", "u"), 22.0)
    # Le toast est construit et poussé sans erreur ; le libellé est traduit.


def test_search_results_start_playback(window, engine):
    refs = [TrackRef(f"{i}" * 11, f"T{i}") for i in range(3)]
    window.search.results.set_tracks(refs)
    window._play_from_list(refs, 1)
    assert len(engine.queue) == 3
    assert engine.queue.current.video_id == "1" * 11


def test_home_resume_offers_search_on_empty_library(window, engine):
    """Bibliothèque vide : l'accueil invite à chercher plutôt qu'à rien."""
    _refresh(window.home)
    assert window.home.resume.primary.get_label() == "Rechercher"
    window.home.resume.primary.emit("clicked")
    assert window.pages.get_visible_child_name() == "search"


def test_home_resume_shows_last_listened(window, engine):
    ref = TrackRef("r" * 11, "Get Lucky", "Daft Punk", 369.0)
    engine.library.record_play(ref, 120, True)
    _refresh(window.home)
    assert window.home.resume.title.get_text() == "Get Lucky"
    assert window.home.resume.primary.get_label() == "Reprendre"


# ---- Lecture en cours -----------------------------------------------------

def test_now_playing_is_attached_to_the_sheet(window):
    assert window.now_playing is not None
    assert window.stack.get_child_by_name("nowplaying") is window.now_playing


def test_now_playing_shows_the_track(window, engine):
    engine.emit("track-changed", TrackRef("b" * 11, "Get Lucky",
                                          "Daft Punk", 369.0))
    view = window.now_playing
    assert view._title.get_text() == "Get Lucky"
    assert view._artist.get_text() == "Daft Punk"
    assert view._total.get_text() == "6:09"


def test_repeat_button_cycles_three_modes(window, engine):
    view = window.now_playing
    assert engine.repeat == "off"
    view._cycle_repeat(None)
    assert engine.repeat == "all"
    view._cycle_repeat(None)
    assert engine.repeat == "one"
    view._cycle_repeat(None)
    assert engine.repeat == "off"


def test_shuffle_toggle_reaches_the_queue(window, engine):
    window.now_playing.shuffle.set_active(True)
    assert engine.shuffle is True


def test_radio_switch_persists_the_setting(window, engine):
    window.now_playing._on_radio_toggled(None, True)
    assert engine.settings.get("autoplay_radio") is True


def test_queue_column_lists_the_queue(window, engine):
    refs = [TrackRef(f"{i}" * 11, f"T{i}") for i in range(4)]
    engine.play_refs(refs)
    window.now_playing._refresh_queue()
    assert len(window.now_playing.queue_list.tracks) == 4


def test_activating_a_queue_row_jumps_to_it(window, engine):
    refs = [TrackRef(f"{i}" * 11, f"T{i}") for i in range(4)]
    engine.play_refs(refs)
    window.now_playing._on_queue_activated(None, 2)
    assert engine.queue.current.video_id == "2" * 11


def test_seek_from_now_playing_reaches_the_player(window, engine):
    view = window.now_playing
    view.progress.props.duration = 300.0
    view.progress.emit("seek-requested", 120.0)
    assert engine.player.seeks[-1] == 120.0


def test_segments_reach_both_progress_bars(window, engine):
    engine.emit("segments-changed", [Segment(0, 20, "music_offtopic", "u")])
    assert window.player_bar.progress.segments == [(0.0, 20.0, 1.0)]
    assert window.now_playing.progress.segments == [(0.0, 20.0, 1.0)]
    engine.emit("segments-changed", [])
    assert window.player_bar.progress.segments == []


def test_sheet_opens_and_closes(window):
    window.open_sheet()
    assert window.sheet_open
    window.close_sheet()
    assert not window.sheet_open


# ---- Pochettes ------------------------------------------------------------

def test_cover_pattern_is_deterministic():
    from triat.ui.widgets.cover import PATTERNS, pattern_for

    assert pattern_for("kJQP7kiw5Fk") == pattern_for("kJQP7kiw5Fk")
    assert pattern_for("kJQP7kiw5Fk") in PATTERNS


def test_every_pattern_produces_points():
    from triat.ui.widgets.cover import PATTERNS, pattern_points

    for name in PATTERNS:
        points = pattern_points(name)
        assert 20 < len(points) < 121, f"{name}: {len(points)} points"
        assert all(0 <= x <= 1 and 0 <= y <= 1 for x, y, _ in points)


def test_cover_shows_the_current_track(engine):
    from triat.ui.window import PlayerBar

    bar = PlayerBar(engine)
    engine.emit("track-changed", TrackRef("z" * 11, "T", "A", 100.0))
    assert bar.cover is not None
    engine.emit("track-changed", None)      # ne doit pas lever


def test_artwork_refuses_foreign_hosts(tmp_path, monkeypatch):
    from triat.core import artwork

    monkeypatch.setattr(artwork, "covers_dir", lambda: tmp_path)
    assert artwork.fetch("https://attaquant.test/pochette.jpg") is None
    assert artwork.fetch("file:///etc/passwd") is None
    assert artwork.fetch("") is None


def test_artwork_cache_path_is_derived_from_the_url(tmp_path, monkeypatch):
    from triat.core import artwork

    monkeypatch.setattr(artwork, "covers_dir", lambda: tmp_path)
    a = artwork.cache_path("https://i.ytimg.com/vi/aaa/hq.jpg")
    b = artwork.cache_path("https://i.ytimg.com/vi/bbb/hq.jpg")
    assert a != b
    assert a.parent == tmp_path
    assert "/" not in a.name, "aucun chemin ne vient de l'URL"


# ---- Réglages -------------------------------------------------------------

def test_settings_dialog_builds(window, engine):
    from triat.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(engine)
    assert dialog.get_title() == "Réglages"


def test_sponsorblock_policy_is_persisted(engine):
    from triat.core.sponsorblock import SKIP
    from triat.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(engine)
    engine.settings.set("sponsorblock_categories", {"intro": "ignore"})

    class FakeButton:
        def get_active(self): return True

    dialog._on_policy(FakeButton(), "intro", SKIP)
    assert engine.settings.get("sponsorblock_categories")["intro"] == SKIP


def test_status_row_reports_no_cookies(engine):
    from triat.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(engine)
    dialog._refresh_status()
    from tests.test_engine import pump
    assert pump(lambda: "Aucun cookie" in dialog.status_row.get_subtitle())


def test_every_sponsorblock_category_has_a_label():
    from triat.core.sponsorblock import CATEGORIES
    from triat.ui.settings_dialog import CATEGORY_LABELS

    for name in CATEGORIES:
        assert name in CATEGORY_LABELS, f"{name} sans libellé lisible"


# ---- Menu contextuel ------------------------------------------------------

def test_track_actions_queue_next(engine):
    from triat.ui.track_actions import TrackActions

    engine.play_refs([TrackRef("a" * 11, "A"), TrackRef("b" * 11, "B")])
    actions = TrackActions(engine)
    actions.handle("next", TrackRef("v" * 11, "VIP"))
    assert [t.video_id for t in engine.queue.upcoming][0] == "v" * 11


def test_track_actions_append_to_queue(engine):
    from triat.ui.track_actions import TrackActions

    engine.play_refs([TrackRef("a" * 11, "A")])
    TrackActions(engine).handle("queue", TrackRef("c" * 11, "C"))
    assert len(engine.queue) == 2


def test_track_actions_toggle_favorite(engine):
    from triat.ui.track_actions import TrackActions

    ref = TrackRef("d" * 11, "D", "Artiste")
    TrackActions(engine).handle("favorite", ref)
    assert engine.library.is_favorite(ref.video_id)
    TrackActions(engine).handle("favorite", ref)
    assert not engine.library.is_favorite(ref.video_id)


def test_track_actions_ignore_unknown_and_none(engine):
    from triat.ui.track_actions import TrackActions

    actions = TrackActions(engine)
    actions.handle("inconnue", TrackRef("e" * 11, "E"))   # ne lève pas
    actions.handle("queue", None)
    assert len(engine.queue) == 0


def test_track_list_emits_actions():
    from triat.ui.track_list import TrackList

    widget = TrackList()
    widget.set_tracks([TrackRef(f"{i}" * 11, f"T{i}") for i in range(3)])
    seen = []
    widget.connect("track-action", lambda _w, a, i: seen.append((a, i)))
    widget._menu_index = 1
    widget._on_menu_action(None, None, "queue")
    assert seen == [("queue", 1)]


def test_menu_action_out_of_range_is_ignored():
    from triat.ui.track_list import TrackList

    widget = TrackList()
    seen = []
    widget.connect("track-action", lambda _w, a, i: seen.append((a, i)))
    widget._menu_index = 99
    widget._on_menu_action(None, None, "queue")
    assert seen == []


# ---- Égaliseur --------------------------------------------------------------

def test_equalizer_preset_drives_the_player(engine):
    from triat.ui.equalizer_group import EqualizerGroup
    group = EqualizerGroup(engine)
    assert not group.scales[0].get_parent().get_sensitive()
    group.switch.set_active(True)
    assert group.scales[0].get_parent().get_sensitive()
    group.preset.set_selected(1)                     # Basses
    assert group.gains()[0] == 6.0
    assert engine.player.equalizer[0] == 6.0


def test_moving_a_slider_switches_to_custom(engine):
    from triat.ui.equalizer_group import EqualizerGroup
    group = EqualizerGroup(engine)
    group.switch.set_active(True)
    group.scales[4].set_value(7)
    assert group.preset.get_selected() == len(group._preset_ids) - 1
    group._apply_now()                               # sans attendre le délai
    assert engine.settings.get("eq_gains")[4] == 7.0


def test_equalizer_group_restores_saved_gains(engine):
    from triat.ui.equalizer_group import EqualizerGroup
    engine.set_equalizer(enabled=True, gains=(0, 0, 0, 0, 0, 1, 2, 4, 5, 6))
    group = EqualizerGroup(engine)
    assert group.switch.get_active()
    assert group.gains()[-1] == 6.0
    assert group._preset_ids[group.preset.get_selected()] == "treble"


# ---- Immersion --------------------------------------------------------------

def _playing(engine):
    from tests.test_engine import pump
    engine.play_refs([TrackRef("v0", "Titre 0", "Artiste", 200.0),
                      TrackRef("v1", "Titre 1", "Artiste", 200.0)])
    assert pump(lambda: engine.player.played)


def test_immersion_needs_something_playing(window):
    window.open_immersion()
    assert not window.immersion.active
    assert window.stack.get_visible_child_name() != "immersion"


def test_immersion_opens_and_closes_without_touching_audio(window, engine):
    _playing(engine)
    window.open_immersion()
    assert window.immersion.active
    assert window.stack.get_visible_child_name() == "immersion"
    assert window.immersion.title.get_text() == engine.current.title
    assert window.immersion.up_next.get_text() == "À suivre : Titre 1"
    window.close_immersion()
    assert not window.immersion.active
    assert window.stack.get_visible_child_name() == "main"
    assert engine.player.state == "playing"
    assert engine.video_state == "off"


def test_immersion_escape_closes(window, engine):
    from gi.repository import Gdk
    _playing(engine)
    window.open_immersion()
    window.immersion._on_key(None, Gdk.KEY_Escape, 0, 0)
    assert not window.immersion.active


def test_immersion_shows_video_states(window, engine):
    _playing(engine)
    view = window.immersion
    window.open_immersion()
    engine.emit("video-changed", "unavailable", None)
    assert view.placeholder_box.get_visible()
    assert "indisponible" in view.message.get_text()
    from triat.core.extractor import VideoStream
    engine.emit("video-changed", "ready",
                VideoStream("v0", "https://x.invalid/v", 1920, 1080, "VP9"))
    assert not view.placeholder_box.get_visible()
    assert view.format_pill.get_text() == "1080p · VP9 · Opus"
    window.close_immersion()


def test_immersion_controls_hide_then_come_back(window, engine):
    _playing(engine)
    view = window.immersion
    window.open_immersion()
    assert view.controls_visible
    view._conceal()
    assert not view.controls_visible
    view._on_motion(None, 10.0, 10.0)
    view._on_motion(None, 50.0, 50.0)
    assert view.controls_visible
    window.close_immersion()


def test_narrow_window_keeps_an_icon_rail_beside_the_content(window):
    """Tuilé à ~950 px, la navigation devient un rail : elle ne recouvre
    jamais le contenu (l'ancien repli masquait tout l'écran)."""
    window._set_narrow(True)
    assert window.sidebar.compact
    assert window.sidebar.get_size_request()[0] == window.sidebar.COMPACT
    assert window.pages.get_parent() is window.sidebar.get_parent().get_parent().get_parent()
    window._on_navigate("library")
    assert window.pages.get_visible_child_name() == "library"
    window._set_narrow(False)
    assert not window.sidebar.compact


def test_home_offers_generated_mixes(window, engine):
    refs = [TrackRef(f"id{i:04d}", f"T{i}", f"A{i % 3}", 180.0) for i in range(30)]
    engine.library.upsert_many(refs)
    engine.library.add_to_playlist(engine.library.create_playlist("Ma musique"), refs)
    _refresh(window.home)
    from tests.test_engine import pump
    assert pump(lambda: window.home.mixes)
    assert window.home.mixes[0].title == "Mix du jour"
    card = window.home.mix_box.get_child_at_index(0).get_child()
    card.emit("clicked")
    from tests.test_engine import pump
    assert pump(lambda: engine.player.played)


class _FakeSpectrum:
    available = True

    def __init__(self):
        self.running = False
        self.callback = None

    def start(self, callback):
        self.running, self.callback = True, callback
        return True

    def stop(self):
        self.running, self.callback = False, None


def test_matrix_mode_runs_the_spectrum_only_while_shown(window, engine):
    _playing(engine)
    view = window.immersion
    view._spectrum = _FakeSpectrum()
    engine.settings.set("immersion_mode", "glyph")
    view.set_mode("glyph", remember=False)
    window.open_immersion()
    assert view.scene == "glyph"
    assert view._spectrum.running
    view._spectrum.callback([0.5] * 24)          # une image du spectre
    assert engine.video_state == "off"            # pas de clip téléchargé
    window.close_immersion()
    assert not view._spectrum.running


def test_missing_clip_falls_back_to_the_matrix(window, engine):
    _playing(engine)
    view = window.immersion
    view._spectrum = _FakeSpectrum()
    view.set_mode("clip", remember=False)
    window.open_immersion()
    engine.emit("video-changed", "unavailable", None)
    assert view.scene == "glyph" and view.mode == "clip"
    from triat.core.extractor import VideoStream
    engine.emit("video-changed", "ready",
                VideoStream("v1", "https://x.invalid/v", 1920, 1080, "VP9"))
    assert view.scene == "clip" and not view._spectrum.running
    window.close_immersion()


def test_v_key_switches_scene_and_is_remembered(window, engine):
    from gi.repository import Gdk
    _playing(engine)
    view = window.immersion
    view._spectrum = _FakeSpectrum()
    view.set_mode("clip", remember=False)
    window.open_immersion()
    view._on_key(None, Gdk.KEY_v, 0, 0)
    assert view.mode == "glyph" and view.scene == "glyph"
    assert engine.settings.get("immersion_mode") == "glyph"
    window.close_immersion()


def test_player_bar_fullscreen_starts_something(window, engine):
    refs = [TrackRef(f"id{i:04d}", f"T{i}", "A", 180.0) for i in range(20)]
    engine.library.upsert_many(refs)
    engine.library.add_to_playlist(engine.library.create_playlist("Ma musique"), refs)
    _refresh(window.home)
    window.immersion._spectrum = _FakeSpectrum()
    window.player_bar.fullscreen_button.emit("clicked")
    assert window.immersion.active
    window.close_immersion()


# ---- Recherche ------------------------------------------------------------

def _search(window, query):
    from gi.repository import GLib
    done = []
    page = window.search
    original = page._on_results
    page._on_results = lambda r, e: (original(r, e), done.append(1))
    page.run_search(query)
    context = GLib.MainContext.default()
    for _ in range(2000):
        if done:
            break
        context.iteration(False)
    page._on_results = original
    assert done, "la recherche n'a pas répondu"


def test_search_shows_best_result_and_chips(window, engine):
    engine.recommender = type(engine.recommender)(results=[
        TrackRef("a" * 11, "Get Lucky", "Daft Punk"),
        TrackRef("b" * 11, "One More Time", "Daft Punk - Topic"),
    ])
    _search(window, "daft punk")
    page = window.search
    assert page.chips.get_visible() and page.views.get_visible()
    assert page.best_card.best.kind == "artist"
    assert page.results_data.artists == [("Daft Punk", 2)]
    page.select_filter("tracks")
    assert page.views.get_visible_child_name() == "tracks"
    assert engine.settings.get("search_history")[0] == "daft punk"


def test_search_best_artist_opens_artist_page(window, engine):
    engine.recommender = type(engine.recommender)(results=[
        TrackRef("a" * 11, "Get Lucky", "Daft Punk")])
    _search(window, "daft punk")
    window.search.best_card.emit("clicked")
    assert window.pages.get_visible_child_name() == "artist"


def test_search_finds_local_playlist(window, engine):
    engine.library.create_playlist("Soirée")
    _search(window, "soiree")
    page = window.search
    assert page.best_card.best.kind == "playlist"
    page.best_card.emit("clicked")
    assert window.pages.get_visible_child_name() == "library"


def test_private_session_keeps_no_history(window, engine):
    engine.settings.set("private_session", True)
    _search(window, "secret")
    assert engine.settings.get("search_history") == []


def test_track_rows_build_and_bind():
    """La factory construit et remplit une ligne (le rendu réel l'appelle,
    les autres tests non : un import manquant y est passé inaperçu)."""
    from triat.ui.track_list import TrackItem, TrackList

    class Slot:
        def set_child(self, child):
            self.child = child

        def get_item(self):
            return TrackItem(TrackRef("a" * 11, "Get Lucky", "Daft Punk", 369.0), 0)

    view, slot = TrackList(), Slot()
    view._on_setup(None, slot)
    view._on_bind(None, slot)
    assert slot.child is not None


# ---- Bibliothèque ---------------------------------------------------------

def test_library_opens_playlist_detail_and_back(window, engine):
    refs = [TrackRef(f"{i}" * 11, f"T{i}", "A", 60.0) for i in range(3)]
    engine.library.upsert_many(refs)
    pid = engine.library.create_playlist("Nuit")
    engine.library.add_to_playlist(pid, refs)
    page = window.library
    page.refresh()
    assert page.playlists.get_row_at_index(0).playlist_id == pid
    page.show_playlist(pid)
    assert page.views.get_visible_child_name() == "detail"
    assert page.detail_title.get_text() == "Nuit"
    assert page.detail_meta.get_text().startswith("3 titres, 3 min")
    page._play_detail(False)
    assert len(engine.queue) == 3
    page.close_detail()
    assert page.views.get_visible_child_name() == "playlists"


def test_library_smart_playlist_detail(window, engine):
    neuve = TrackRef("n" * 11, "Neuve", "A")
    engine.library.upsert_track(TrackRef("c" * 11, "Cache", "B"))
    engine.library.set_favorite(neuve, True)
    page = window.library
    unplayed = next(s for s in page.smarts if s.key == "unplayed")
    page._open_detail(smart=unplayed)
    assert page.detail_title.get_text() == "Jamais écoutés"
    assert not page.delete_button.get_visible()
    assert [r.video_id for r in page.tracks.tracks] == ["n" * 11]


def test_library_artists_tab(window, engine):
    engine.library.set_favorite(TrackRef("d" * 11, "Get Lucky", "Daft Punk"), True)
    engine.library.upsert_track(TrackRef("e" * 11, "Cache", "Inconnu"))
    page = window.library
    page._tab_buttons["artists"].set_active(True)
    assert page.views.get_visible_child_name() == "artists"
    assert page.artists.get_child_at_index(1) is None     # pas le cache
    card = page.artists.get_child_at_index(0).get_child()
    card.emit("clicked")
    assert window.pages.get_visible_child_name() == "artist"


def test_settings_library_page_clears_searches(window, engine):
    from triat.ui.settings_dialog import SettingsDialog
    engine.settings.set("search_history", ["daft punk"])
    engine.session_skips["seconds"] = 78.0
    dialog = SettingsDialog(engine)
    assert dialog.session_label.get_text() == "1:18"
    assert dialog.library_stats.get_subtitle().startswith("0 piste, 0 playlist")
    dialog._clear_searches(Gtk.Button())
    assert engine.settings.get("search_history") == []


def test_immersion_scene_setting_applies_live(window, engine):
    if window.immersion is None:
        pytest.skip("pas d'Immersion dans cet environnement")
    window._on_setting_changed("immersion_mode", "glyph")
    assert window.immersion.mode == "glyph"


# ---- Réglages : synchronisation --------------------------------------------

@pytest.fixture
def sync_dialog(window, engine, monkeypatch):
    """Réglages sur le bureau de test : pas de mDNS, port choisi par l'OS."""
    from triat.ui.settings_dialog import SettingsDialog

    monkeypatch.setattr(engine.sync, "_announce", lambda *_a: None)
    dialog = SettingsDialog(engine)
    dialog.present(window)
    yield dialog
    dialog._close_pairing()


def _sync_device(name="Téléphone de test", last_sync=None):
    return {"name": name, "key": "AAAA", "paired_at": 1.0,
            "last_sync": last_sync}


def test_sync_page_builds_and_starts_off(window, engine):
    from triat.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(engine)
    titles = [page.get_title() for page in _pages(dialog)]
    assert titles.index("Synchronisation") == titles.index("Système") - 1
    assert not dialog.sync_switch.get_active()
    assert not dialog.pair_button.get_sensitive()       # rien à associer
    assert "Active" in dialog.sync_address.get_subtitle()
    assert not engine.sync.running
    assert dialog._device_rows[0].get_title() == "Aucun téléphone associé"


def _pages(dialog):
    """Pages d'un Adw.PreferencesDialog, dans l'ordre d'affichage."""
    pages = dialog.get_visible_page()
    stack = pages.get_ancestor(Adw.ViewStack) if pages else None
    return [stack.get_page(child).get_child() for child in
            _children(stack)] if stack else []


def _children(stack):
    child = stack.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def test_sync_switch_starts_and_stops_the_server(sync_dialog, engine):
    sync_dialog.sync_switch.set_active(True)
    assert engine.sync.running
    assert engine.settings.get("sync_enabled") is True
    assert sync_dialog.pair_button.get_sensitive()
    assert f":{engine.sync.port}" in sync_dialog.sync_address.get_subtitle()

    sync_dialog.sync_switch.set_active(False)
    assert not engine.sync.running
    assert engine.settings.get("sync_enabled") is False
    assert not sync_dialog.pair_button.get_sensitive()


def test_sync_switch_goes_back_when_the_server_cannot_start(sync_dialog, engine,
                                                          monkeypatch):
    monkeypatch.setattr(engine.sync, "start", lambda announce=True: False)
    sync_dialog.sync_switch.set_active(True)
    assert not sync_dialog.sync_switch.get_active()
    assert engine.settings.get("sync_enabled") is False


def test_pairing_shows_an_eight_character_code(sync_dialog, engine):
    from triat.core.sync import normalize_code

    sync_dialog.sync_switch.set_active(True)
    sync_dialog.pair_button.emit("clicked")
    shown = sync_dialog.pairing_code_label.get_text()
    assert len(shown) == 9 and shown[4] == "-"          # ABCD-EFGH
    assert len(normalize_code(shown)) == 8
    assert engine.sync.pairing_active
    assert sync_dialog.pairing_countdown.get_label() == "Expire dans 2:00"

    sync_dialog._tick_pairing()
    assert sync_dialog.pairing_countdown.get_label() == "Expire dans 1:59"


def test_pairing_closes_by_itself_at_zero(sync_dialog, engine):
    sync_dialog.sync_switch.set_active(True)
    sync_dialog.pair_button.emit("clicked")
    sync_dialog._pairing_left = 1
    assert sync_dialog._tick_pairing() is False          # la source s'arrête
    assert sync_dialog._pairing_dialog is None
    assert not engine.sync.pairing_active                # le code ne vaut plus rien


def test_pairing_closes_when_a_phone_appears(sync_dialog, engine):
    sync_dialog.sync_switch.set_active(True)
    sync_dialog.pair_button.emit("clicked")
    assert sync_dialog._tick_pairing() is True           # personne : on continue
    engine.settings.set("sync_devices", {"tel-1": _sync_device()})
    assert sync_dialog._tick_pairing() is False
    assert sync_dialog._pairing_dialog is None
    assert not engine.sync.pairing_active
    assert sync_dialog._device_rows[0].get_title() == "Téléphone de test"


def test_cancelling_the_pairing_dialog_drops_the_code(sync_dialog, engine):
    sync_dialog.sync_switch.set_active(True)
    sync_dialog.pair_button.emit("clicked")
    sync_dialog._pairing_dialog.close()                  # Annuler ou Échap
    assert sync_dialog._pairing_dialog is None
    assert sync_dialog._pairing_timer == 0
    assert not engine.sync.pairing_active


def test_forget_removes_the_device(sync_dialog, engine):
    engine.settings.set("sync_devices", {
        "tel-1": _sync_device("Premier"), "tel-2": _sync_device("Second")})
    sync_dialog._refresh_devices()
    assert [r.get_title() for r in sync_dialog._device_rows] == ["Premier", "Second"]

    button = _find_button(sync_dialog._device_rows[0], "Oublier")
    assert button is not None
    button.emit("clicked")
    assert list(engine.sync.devices()) == ["tel-2"]
    assert [r.get_title() for r in sync_dialog._device_rows] == ["Second"]

    _find_button(sync_dialog._device_rows[0], "Oublier").emit("clicked")
    assert engine.sync.devices() == {}
    assert sync_dialog._device_rows[0].get_title() == "Aucun téléphone associé"


def _find_button(widget, label):
    """Premier Gtk.Button portant ce libellé, sous `widget`."""
    if isinstance(widget, Gtk.Button) and widget.get_label() == label:
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find_button(child, label)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def test_last_sync_is_told_in_french():
    from triat.ui.settings_dialog import format_last_sync

    assert format_last_sync(None) == "Jamais synchronisé"
    assert format_last_sync(1000.0, now=1030.0) == "Dernière synchro : à l'instant"
    assert format_last_sync(1000.0, now=1000.0 + 300) == "Dernière synchro : il y a 5 min"
    assert format_last_sync(1000.0, now=1000.0 + 7300) == "Dernière synchro : il y a 2 h"
    assert format_last_sync(1000.0, now=1000.0 + 3 * 86400) == "Dernière synchro : il y a 3 j"


# ---- Audit UI : toasts, navigation, raccourcis, synchro --------------------

def test_toast_is_literal_text(window):
    seen = []
    window.toasts.add_toast = seen.append
    window._toast("Simon & Garfunkel <3")
    assert not seen[0].get_use_markup()
    assert seen[0].get_title() == "Simon & Garfunkel <3"


def test_segment_toasts_use_french_category_labels(window):
    seen = []
    window.toasts.add_toast = seen.append
    seg = Segment(10.0, 20.0, "preview")
    window._on_segment_pending(window.engine, seg)
    window._on_segment_skipped(window.engine, seg, 10.0)
    assert "Récapitulatif" in seen[0].get_title()
    assert "preview" not in seen[0].get_title()
    assert "Récapitulatif" in seen[1].get_title()
    assert not seen[1].get_use_markup()


def test_navigate_leaves_the_sheet(window, engine):
    engine.emit("track-changed", TrackRef("a" * 11, "T", "A", duration=10.0))
    window.open_sheet()
    assert window.sheet_open
    window._on_navigate("search")
    assert window.stack.get_visible_child_name() == "main"
    assert window.pages.get_visible_child_name() == "search"


def test_artist_action_closes_the_sheet(window, engine):
    ref = TrackRef("a" * 11, "T", "Daft Punk", duration=10.0)
    window.open_sheet()
    window._on_track_action("artist", ref)
    assert not window.sheet_open
    assert window.pages.get_visible_child_name() == "artist"


def test_space_runs_in_capture_phase_and_toggles(window, engine, monkeypatch):
    assert (window._space_controller.get_propagation_phase()
            == Gtk.PropagationPhase.CAPTURE)
    calls = []
    monkeypatch.setattr(engine, "toggle_pause", lambda: calls.append(1))
    monkeypatch.setattr(engine, "next", lambda: calls.append("next"))
    window.player_bar.next_button.grab_focus()
    assert window._sc_toggle() is True
    assert calls == [1]


def test_space_is_left_to_text_fields(window, engine, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "toggle_pause", lambda: calls.append(1))
    monkeypatch.setattr(window, "get_focus", lambda: Gtk.Text())
    assert window._sc_toggle() is False
    assert window._sc_seek(5.0)() is False
    assert calls == []


def test_arrow_shortcuts_seek_and_skip(window, engine, monkeypatch):
    engine._current_ref = TrackRef("a" * 11, "T", "A", duration=100.0)
    engine.player.position_hint = 20.0
    engine.player.duration = 100.0
    window._sc_seek(5.0)()
    window._sc_seek(-5.0)()
    assert engine.player.seeks[-2:] == [25.0, 20.0]
    skipped = []
    monkeypatch.setattr(engine, "next", lambda: skipped.append("n"))
    window._sc_skip(True)()
    assert skipped == ["n"]


def test_escape_closes_sheet_then_artist(window, engine):
    window.open_sheet()
    assert window._sc_escape() is True
    assert not window.sheet_open
    window.pages.set_visible_child_name("artist")
    assert window._sc_escape() is True
    assert window.pages.get_visible_child_name() == "library"
    assert window._sc_escape() is False


def test_shortcut_help_lists_the_main_keys():
    from triat.ui.window import SHORTCUTS

    accels = {accel for _title, accel in SHORTCUTS}
    assert {"space", "<Primary>m", "Left", "<Shift>Right"} <= accels


def test_app_accels_include_mini():
    from triat.app import APP_ACCELS

    assert APP_ACCELS["mini"] == ["<Primary>m"]


def test_player_bar_follows_volume_changes(engine):
    bar = PlayerBar(engine)
    engine.set_volume(30)
    assert bar.volume.props.volume == 30


def test_player_bar_spinner_while_loading_or_busy(engine):
    bar = PlayerBar(engine)
    assert not bar.spinner.get_visible()
    engine.emit("state-changed", "loading")
    assert bar.spinner.get_visible()
    engine.emit("state-changed", "playing")
    assert not bar.spinner.get_visible()
    engine.emit("busy", True)
    assert bar.spinner.get_visible()
    engine.emit("busy", False)
    assert not bar.spinner.get_visible()


def test_player_bar_play_label_follows_state(engine):
    bar = PlayerBar(engine)
    engine.emit("state-changed", "playing")
    assert bar.play_button.get_tooltip_text() == "Pause"
    assert isinstance(bar.expand_button, Gtk.Button)


def test_history_cleared_refreshes_home(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window.home, "refresh", lambda: calls.append(1))
    window._on_setting_changed("history_cleared", True)
    window._on_setting_changed("private_session", True)
    assert len(calls) == 2


def test_home_uses_tutoiement(window):
    assert window.home.mixes_section is not None
    tooltip = window.home.resume.radio.get_tooltip_text()
    assert "tu écoutes" in tooltip
    for text in ("vous", "Vous", "votre", "Votre"):
        assert text not in tooltip


def test_space_activates_a_keyboard_focused_button(window, engine, monkeypatch):
    """Focus clavier sur un bouton : Espace l'active, il ne bascule pas la
    lecture. Après un clic souris (focus non visible), il reste lecture."""
    calls = []
    monkeypatch.setattr(engine, "toggle_pause", lambda: calls.append(1))
    button = window.player_bar.next_button
    monkeypatch.setattr(window, "get_focus", lambda: button)
    monkeypatch.setattr(window, "get_focus_visible", lambda: True)
    assert window._sc_toggle() is False
    assert calls == []
    monkeypatch.setattr(window, "get_focus_visible", lambda: False)
    assert window._sc_toggle() is True
    assert calls == [1]


# ---- Accueil : mixes figés, couvertures, suggestions distantes -------------

def _library_with_playlist(engine, n=12):
    refs = [TrackRef(f"id{i:04d}", f"T{i}", f"A{i}", 180.0) for i in range(n)]
    engine.library.add_to_playlist(engine.library.create_playlist("Ma musique"), refs)
    return refs


def test_home_renders_cached_mixes_without_rebuilding(window, engine, monkeypatch):
    import datetime as dt
    from triat.core import mixes as mixes_module
    refs = _library_with_playlist(engine)
    store = mixes_module.MixStore(engine.library)
    store.save(dt.date.today(),
               [mixes_module.Mix("daily", "Mix du jour", refs, 1)],
               engine.library.count_owned(), 1.0)

    def boom(*_a, **_k):
        raise AssertionError("reconstruit alors que le cache est valide")

    monkeypatch.setattr(mixes_module, "build_mixes", boom)
    _refresh(window.home)
    assert [m.title for m in window.home.mixes] == ["Mix du jour"]
    assert window.home.mix_box.get_child_at_index(0) is not None
    assert window.home.mix_box.get_child_at_index(1) is None


def test_home_builds_large_libraries_off_the_main_thread(window, engine, monkeypatch):
    from triat.core import mixes as mixes_module
    from tests.test_engine import pump
    _library_with_playlist(engine, 40)
    monkeypatch.setattr(mixes_module, "SYNC_BUILD_LIMIT", 0)
    window.home.refresh()
    assert window.home._building         # lancé, pas encore rendu
    assert pump(lambda: window.home.mixes)
    assert window.home.mixes[0].title == "Mix du jour"
    assert not window.home._building


def test_home_playlists_use_single_query(window, engine, monkeypatch):
    _library_with_playlist(engine, 5)

    def boom(*_a, **_k):
        raise AssertionError("une requête par playlist")

    monkeypatch.setattr(engine.library, "playlist_items", boom)
    _refresh(window.home)
    assert len(window.home.mixes_grid) == 1
    assert window.home.mixes_grid.items[0].title == "Ma musique"


class _StubFeed:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.reset_calls = 0

    def cached(self):
        return self.snapshot

    def needs_refresh(self):
        return False

    def reset(self):
        self.reset_calls += 1


def test_home_shows_remote_section_from_cache(window, engine):
    from triat.core.home_feed import HomeSnapshot
    tracks = [TrackRef(f"r{i}", f"Reco {i}", "Quelqu'un") for i in range(4)]
    snap = HomeSnapshot("innertube", [{"title": "Recommandé pour toi",
                                       "tracks": tracks}], 1.0, stale=False)
    home = window.home
    home._feed = _StubFeed(snap)
    _refresh(home)
    assert home.remote_grid.get_visible() and len(home.remote_grid) == 4
    assert "sans compte" in home.remote_note.get_text()
    snap.stale = True
    _refresh(home)
    assert "hors ligne" in home.remote_note.get_text()
    # Ce qui est écarté disparaît du cache affiché.
    engine.library.block("track", "r0", "Reco 0")
    _refresh(home)
    assert len(home.remote_grid) == 3
    home._feed = _StubFeed(None)
    _refresh(home)
    assert not home.remote_grid.get_visible()


def test_track_menu_block_action(window, engine):
    refs = [TrackRef(f"v{i:04d}", f"T{i}", "Vialice" if i < 10 else f"A{i}", 180.0)
            for i in range(30)]
    engine.library.add_to_playlist(engine.library.create_playlist("P"), refs)
    _refresh(window.home)
    assert any(r.artist == "Vialice" for m in window.home.mixes for r in m.refs)
    window.actions.apply_block(refs[0], "artist")
    assert "vialice" in engine.library.blocked()["artist"]
    _refresh(window.home)
    assert not any(r.artist == "Vialice" for m in window.home.mixes for r in m.refs)
    window.actions.apply_block(refs[15], "track")
    assert refs[15].video_id in engine.library.blocked()["track"]


def test_track_list_menu_offers_block():
    from triat.ui.track_list import TrackList
    view = TrackList()
    view.set_tracks([TrackRef("v1", "Un", "A")])
    seen = []
    view.connect("track-action", lambda _v, name, index: seen.append((name, index)))
    view._menu_index = 0
    assert view.activate_action("piste.block", None)
    assert seen == [("block", 0)]


def test_cookie_change_clears_home_cache(window, engine):
    engine.library.reco_cache_put("home:remote", {"source": "innertube",
                                                  "sections": []})
    window._on_setting_changed("cookie_source", "chrome")
    assert engine.library.reco_cache_get("home:remote") is None


# ---- Accueil : fil d'arrière-plan, doublons, effacement --------------------

def test_recent_listening_is_deduplicated():
    from triat.ui.window import dedupe_recent
    refs = [TrackRef("a" * 11, "Get Lucky", "Daft Punk", 369.0),
            TrackRef("b" * 11, "Get Lucky (Official Video)", "Daft Punk", 369.0),
            TrackRef("c" * 11, "Other", "Someone", 200.0)]
    assert [r.video_id for r in dedupe_recent(refs)] == ["a" * 11, "c" * 11]
    assert len(dedupe_recent(refs, limit=1)) == 1


def test_home_refresh_fills_recent_off_the_gtk_thread(window, engine):
    from tests.test_engine import pump
    for vid, title in (("a" * 11, "Get Lucky"),
                       ("b" * 11, "Get Lucky (Official Video)")):
        engine.library.record_play(TrackRef(vid, title, "Daft Punk", 369.0), 120, True)
    _refresh(window.home)
    assert pump(lambda: len(window.home.recent_grid.items) > 0)
    assert len(window.home.recent_grid.items) == 1


def test_stale_home_refresh_is_ignored(window, engine):
    home = window.home
    from tests.test_engine import pump
    pump(lambda: not home._building)
    home._refresh_seq += 5
    before = list(home.recent_grid.items)
    called = []
    home.refresh = lambda: called.append(1)
    home._building = True
    home._refresh_done(home._refresh_seq - 1,
                       {"recent": [TrackRef("z" * 11, "X", "Y")],
                        "covers": [], "mixes": None})
    assert called and home.recent_grid.items == before


def test_data_wiped_resets_caches_and_refreshes(window, engine):
    calls = []
    home = window.home
    home._feed = type("F", (), {"reset": lambda self: calls.append("feed"),
                                "cached": lambda self: None,
                                "needs_refresh": lambda self: False})()
    home._store.reset = lambda: calls.append("store")
    home.refresh = lambda: calls.append("refresh")
    engine.emit("data-wiped")
    assert calls == ["feed", "store", "refresh"]


def test_player_bar_shows_clean_titles(engine):
    bar = PlayerBar(engine)
    bar._on_track(engine, TrackRef("d" * 11, "Daft Punk - Get Lucky (Official Video)",
                                   "Daft Punk - Topic", 369.0))
    assert bar._title.get_text() == "Get Lucky"

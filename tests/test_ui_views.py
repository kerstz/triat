"""Tests des vues : recherche, mini-lecteur, lecture en cours, paroles, téléchargements."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import pytest
from gi.repository import Adw, Gio, GLib

from triat.core.downloads import DONE, FAILED, PENDING, Task
from triat.core.engine import Engine
from triat.core.library import Library
from triat.core.models import TrackRef
from triat.core.settings import Settings
from triat.ui.downloads_page import DownloadsPage
from triat.ui.lyrics_view import LyricsView
from triat.ui.mini_player import MiniPlayer
from triat.ui.now_playing import NowPlayingView
from triat.ui.search_page import SearchPage, friendly_error
from tests.fakes import (FakeCookies, FakeExtractor, FakeInnerTube,
                         FakePlayer, FakeRecommender, FakeSponsorBlock)

Adw.init()


@pytest.fixture(scope="module")
def application():
    app = Adw.Application(application_id="org.triat.TriatViewsTest",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    app.emit("startup")
    return app


@pytest.fixture
def engine(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("autoplay_radio", False)
    eng = Engine(settings=settings, library=Library(tmp_path / "l.db"),
                 player=FakePlayer(), extractor=FakeExtractor(),
                 sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
                 ytmusic=object(), cookies=FakeCookies(),
                 innertube=FakeInnerTube())
    yield eng
    eng.library.close()


def spin(predicate, timeout: float = 2.0) -> bool:
    context = GLib.MainContext.default()
    end = GLib.get_monotonic_time() + timeout * 1_000_000
    while GLib.get_monotonic_time() < end:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
    return predicate()


def ref(n: int = 0) -> TrackRef:
    return TrackRef(f"{n:011d}", f"Titre {n}", "Artiste", 120.0)


# ---- Recherche ---------------------------------------------------------------

class DeferredSearch:
    """Moteur de recherche factice : garde les rappels pour les livrer à la main."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.settings = engine.settings
        self.callbacks: list = []

    def search_all_async(self, query, callback) -> None:
        self.callbacks.append((query, callback))


def results_for(query: str):
    from triat.core import search_results
    return search_results.build(query, [ref(len(query))], [], [])


def test_late_search_result_is_dropped(engine):
    fake = DeferredSearch(engine)
    page = SearchPage(fake, on_play=lambda *_: None)
    page.run_search("a")
    page.run_search("bb")
    (_q1, first), (_q2, second) = fake.callbacks
    second(results_for("bb"), None)
    first(results_for("a"), None)           # la plus lente arrive en dernier
    assert page.results_data.query == "bb"


def test_search_shows_busy_state(engine):
    fake = DeferredSearch(engine)
    page = SearchPage(fake, on_play=lambda *_: None)
    page.run_search("daft")
    assert page.empty.spinner.get_visible()


def test_search_error_is_friendly_with_retry(engine):
    fake = DeferredSearch(engine)
    page = SearchPage(fake, on_play=lambda *_: None)
    page.run_search("daft")
    fake.callbacks[0][1](None, "<urlopen error [Errno -3] Temporary failure>")
    assert page.empty._title.get_text() == "Pas de connexion"
    assert "urlopen" not in page.empty._detail.get_text()
    assert page.empty.action_button.get_visible()
    page.empty.action_button.emit("clicked")
    assert len(fake.callbacks) == 2 and fake.callbacks[1][0] == "daft"


def test_friendly_error_fallback():
    title, _detail = friendly_error("ERROR: unexpected token")
    assert title == "Recherche impossible"


# ---- Mini-lecteur --------------------------------------------------------------

def test_mini_disconnects_engine_handlers_on_close(application, engine):
    mini = MiniPlayer(application, engine)
    handlers = list(mini._handlers)
    assert handlers and all(engine.handler_is_connected(h) for h in handlers)
    mini._on_close_request(mini)
    assert not any(engine.handler_is_connected(h) for h in handlers)


def test_mini_primes_state_when_music_is_playing(application, engine):
    engine.state = "playing"
    mini = MiniPlayer(application, engine)
    assert mini.play_button.get_icon_name() == "triat-pause-symbolic"
    assert mini.play_button.get_sensitive()


def test_mini_play_label_follows_state(application, engine):
    mini = MiniPlayer(application, engine)
    engine.emit("state-changed", "playing")
    assert mini.play_button.get_tooltip_text() == "Pause"
    engine.emit("state-changed", "paused")
    assert mini.play_button.get_tooltip_text() == "Lecture"


# ---- Lecture en cours -----------------------------------------------------------

def test_now_playing_options_are_resynchronised(engine):
    view = NowPlayingView(engine)
    engine.set_volume(30)
    assert view.volume.props.volume == 30
    engine.shuffle = True
    assert view.shuffle.get_active()
    engine.repeat = "all"
    assert "la file" in view.repeat.get_tooltip_text()
    # Changement venu d'ailleurs : le moteur n'est pas re-sollicité en retour.
    engine.queue.shuffle = False
    engine.emit("options-changed")
    assert not view.shuffle.get_active()
    assert engine.queue.shuffle is False


def test_now_playing_menu_uses_the_callback(engine):
    seen = []
    view = NowPlayingView(engine, on_action=lambda a, r: seen.append((a, r)))
    engine.enqueue([ref(1), ref(2)])
    view.queue_list.emit("track-action", "artist", 1)
    assert [(a, r.video_id) for a, r in seen] == [("artist", ref(2).video_id)]


def test_now_playing_heart_follows_library(engine):
    view = NowPlayingView(engine)
    track = ref(3)
    engine._current_ref = track
    engine.library.toggle_favorite(track)
    engine.emit("library-changed")
    assert view.favorite.get_active()


def test_now_playing_play_label_follows_state(engine):
    view = NowPlayingView(engine)
    engine.emit("state-changed", "playing")
    assert view.play_button.get_tooltip_text() == "Pause"
    engine.emit("state-changed", "paused")
    assert view.play_button.get_tooltip_text() == "Lecture"


# ---- Paroles ----------------------------------------------------------------------

def test_lyrics_error_shows_a_message_and_retry(engine):
    class Broken:
        calls = 0

        def fetch(self, _ref):
            Broken.calls += 1
            raise OSError("hors ligne")

    engine.lyrics = Broken()
    view = LyricsView(engine)
    engine.emit("track-changed", ref(4))
    assert spin(lambda: view._message.get_text()
                == "Paroles indisponibles hors ligne")
    assert view._retry.get_visible()
    view._retry.emit("clicked")
    assert spin(lambda: Broken.calls == 2)


def test_unreachable_lyrics_show_the_retry_button(engine):
    from triat.core.lyrics import Lyrics

    class Offline:
        calls = 0

        def fetch(self, ref):
            Offline.calls += 1
            return Lyrics(ref.video_id, False, [], unreachable=True)

    engine.lyrics = Offline()
    view = LyricsView(engine)
    engine.emit("track-changed", ref(5))
    assert spin(lambda: view._retry.get_visible())
    assert view._message.get_text() == "Paroles indisponibles hors ligne"


# ---- Téléchargements ------------------------------------------------------------

def test_downloads_rows_are_updated_in_place(engine):
    manager = engine.downloads
    task = Task(ref=ref(5))
    manager._queue.append(task)
    page = DownloadsPage(engine)
    row = page.list_box.get_first_child()
    assert row is not None
    task.state = "running"
    task.progress = 0.5
    page.refresh()
    assert page.list_box.get_first_child() is row
    assert "50 %" in page._rows[ref(5).video_id][1].get_text()


def test_downloads_failed_and_finished_actions(engine):
    manager = engine.downloads
    failed = Task(ref=ref(6), state=FAILED, error="boom")
    finished = Task(ref=ref(7), state=DONE)
    manager._queue.extend([failed, finished])
    page = DownloadsPage(engine)
    assert page.clear_button.get_visible()
    actions = page._rows[ref(6).video_id][3]
    assert actions.get_first_child().get_tooltip_text() == "Réessayer"
    page._clear_finished(None)
    assert page._rows == {}


def test_downloads_cancel_uses_close_icon(engine):
    engine.downloads._queue.append(Task(ref=ref(8), state=PENDING))
    page = DownloadsPage(engine)
    actions = page._rows[ref(8).video_id][3]
    assert actions.get_first_child().get_icon_name() == "triat-close-symbolic"

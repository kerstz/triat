"""Lecteur : piste SoundCloud (attribution, pas de clip, pas de paroles)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from triat.core.models import TrackRef
from triat.ui.immersion import ImmersionView
from triat.ui.lyrics_view import LyricsView
from triat.ui.now_playing import NowPlayingView
from tests.test_window import engine  # noqa: F401  (fixture)

SC = TrackRef(video_id="soundcloud:tracks:5", title="t", artist="a",
              web_url="https://soundcloud.com/a/t")
YT = TrackRef("v0", "Titre 0", "Artiste", 200.0)


class SpyLyrics:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, ref):
        self.calls += 1
        raise RuntimeError("ne doit pas être appelé")


def test_now_playing_soundcloud_source_and_no_immersion(engine):  # noqa: F811
    view = NowPlayingView(engine)
    view._on_track(engine, SC)
    assert not view.immersion_button.get_sensitive()
    assert view.immersion_button.get_tooltip_text() == "Pas de clip pour SoundCloud"
    assert view._source_button.get_visible()
    assert view._source_button.get_label() == "SoundCloud"
    assert view._artist.get_text() == "a · SoundCloud"


def test_now_playing_youtube_unchanged(engine):  # noqa: F811
    view = NowPlayingView(engine)
    view._on_track(engine, SC)
    view._on_track(engine, YT)
    assert view.immersion_button.get_sensitive()
    assert "Ctrl+I" in view.immersion_button.get_tooltip_text()
    assert not view._source_button.get_visible()
    assert "SoundCloud" not in view._artist.get_text()


def test_lyrics_view_skips_the_service_for_soundcloud(engine):  # noqa: F811
    spy = SpyLyrics()
    engine.lyrics = spy
    view = LyricsView(engine)
    view._on_track(engine, SC)
    assert view._message.get_text() == "Paroles non proposées pour SoundCloud"
    assert spy.calls == 0


def test_immersion_requests_no_lyrics_for_soundcloud(engine):  # noqa: F811
    spy = SpyLyrics()
    engine.lyrics = spy
    view = ImmersionView(engine, lambda: None)
    view._on_track(engine, SC)
    assert spy.calls == 0

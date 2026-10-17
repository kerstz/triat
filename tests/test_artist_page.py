"""Page Artiste : un résultat en ligne d'un ancien artiste est ignoré."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw

from triat.core.models import TrackRef
from triat.ui.artist_page import ArtistPage

Adw.init()


class _Library:
    def tracks_by_artist(self, artist):
        return []


class _Engine:
    current = None

    def __init__(self):
        self.library = _Library()
        self.callbacks = []

    def search_async(self, query, callback):
        self.callbacks.append((query, callback))


def test_stale_search_result_is_ignored():
    engine = _Engine()
    page = ArtistPage(engine, lambda refs, i: None)
    page.show_artist("Alpha")
    page.show_artist("Beta")
    (_, old), (_, new) = engine.callbacks
    old([TrackRef("a" * 11, "Old", "Alpha")], None)
    assert page.tracks.tracks == []
    new([TrackRef("b" * 11, "New", "Beta")], None)
    assert [t.video_id for t in page.tracks.tracks] == ["b" * 11]

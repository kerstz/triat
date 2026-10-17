"""Accessibilité des widgets : rôles, libellés, animations réduites."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import types

from gi.repository import Adw, Gtk

from triat.ui.widgets import common
from triat.ui.widgets.cover import Cover
from triat.ui.widgets.glyph import GlyphArt

Adw.init()


def test_glyph_art_is_presentational():
    assert GlyphArt().get_accessible_role() == Gtk.AccessibleRole.PRESENTATION


def test_cover_is_an_image_labelled_by_its_track():
    cover = Cover()
    assert cover.get_accessible_role() == Gtk.AccessibleRole.IMG
    assert cover.accessible_text() is None
    cover.set_track(types.SimpleNamespace(video_id="v", title="Aurore",
                                          thumbnail=None, source="youtube"),
                    badge=False)
    assert cover.accessible_text() == "Pochette de Aurore"
    cover.clear()
    assert cover.accessible_text() is None


def test_cover_label_includes_the_source_badge():
    cover = Cover()
    cover.set_track(types.SimpleNamespace(video_id="v", title="Aurore",
                                          thumbnail=None, source="soundcloud"),
                    badge=True)
    text = cover.accessible_text()
    assert text.startswith("Pochette de Aurore") and "SoundCloud" in text


def test_glyph_ignores_the_spectrum_when_animations_are_off(monkeypatch):
    glyph = GlyphArt()
    glyph.set_levels([0.5, 0.2])
    assert glyph._levels == [0.5, 0.2]
    monkeypatch.setattr(common, "animations_enabled", lambda *_: False)
    import triat.ui.widgets.glyph as glyph_mod
    monkeypatch.setattr(glyph_mod, "animations_enabled", lambda *_: False)
    glyph.set_levels([0.9])
    assert glyph._levels == []


def test_app_preloads_cookies_off_the_gtk_thread():
    import threading

    from triat.app import TriatApp

    seen = []
    done = threading.Event()

    def preload():
        seen.append(threading.current_thread())
        done.set()

    app = TriatApp.__new__(TriatApp)
    app._engine = types.SimpleNamespace(
        cookies=types.SimpleNamespace(preload=preload))
    app._preload_cookies()
    assert done.wait(2)
    assert seen[0] is not threading.current_thread()

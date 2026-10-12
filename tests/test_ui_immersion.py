"""Immersion : calque matrice, volume synchronisé, animations, clavier."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import pytest

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

from triat.core.models import TrackRef
from triat.ui import immersion as immersion_module
from triat.ui.immersion import ImmersionView
from tests.test_window import engine  # noqa: F401  (fixture)


@pytest.fixture
def view(engine):                                       # noqa: F811
    view = ImmersionView(engine, lambda: None)
    engine.play_refs([TrackRef("v0", "Titre 0", "Artiste", 200.0)])
    view.activate()
    yield view
    view.deactivate()


def test_clip_scene_hides_the_matrix_layer_by_opacity(view):
    view._show_scene("clip")
    assert view.glyph_layer.get_opacity() == 0.0
    assert view.glyph_layer.get_visible()        # jamais `visible` : GLArea
    view._show_scene("glyph")
    assert view.glyph_layer.get_opacity() == 1.0
    assert view.scene == "glyph"


def test_volume_follows_the_engine(view, engine):
    engine.set_volume(33.0)
    assert view.volume.props.volume == 33.0


def test_options_handler_is_disconnected_on_deactivate(engine):  # noqa: F811
    view = ImmersionView(engine, lambda: None)
    view.activate()
    view.deactivate()
    view.volume.props.volume = 10.0
    engine.set_volume(77.0)
    assert view.volume.props.volume == 10.0


def test_no_spectrum_when_animations_are_disabled(view, monkeypatch):
    monkeypatch.setattr(immersion_module, "_animations_enabled", lambda: False)
    view._show_scene("clip")
    view._show_scene("glyph")
    assert not view._spectrum.running


def test_any_key_brings_the_controls_back(view):
    view._conceal()
    assert not view.controls_visible
    view._on_key(None, Gdk.KEY_Left, 0, 0)
    assert view.controls_visible


def test_controls_stay_while_a_control_has_keyboard_focus(view, monkeypatch):
    view._keyboard_nav = True
    monkeypatch.setattr(view, "_focus_in_controls", lambda: True)
    view._conceal()
    assert view.controls_visible, "le focus clavier doit garder les contrôles"
    monkeypatch.setattr(view, "_focus_in_controls", lambda: False)
    view._conceal()
    assert not view.controls_visible


def test_pointer_use_conceals_even_with_focus(view, monkeypatch):
    monkeypatch.setattr(view, "_focus_in_controls", lambda: True)
    view._keyboard_nav = False                  # la souris a repris la main
    view._conceal()
    assert not view.controls_visible


def test_cover_radius_follows_the_art_scale(view):
    assert view.cover._radius == 6.0

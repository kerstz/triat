"""Tests des listes de pistes et des widgets partagés (audit UI)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import pytest
from gi.repository import Adw, GLib, Gtk

from triat.core.models import TrackRef
from triat.core.smart import DAY, smart_playlists
from triat.ui.track_list import TrackList
from triat.ui.widgets import cover as cover_module
from triat.ui.widgets.common import EmptyState, plural
from triat.ui.widgets.dot_progress import DotProgressBar
from triat.ui.widgets.dot_volume import DotVolume

Adw.init()


def refs(count: int = 3) -> list[TrackRef]:
    return [TrackRef(f"{i:011d}", f"Titre {i}", "Artiste", 100.0)
            for i in range(count)]


class FakeListItem:
    """Ligne de liste factice : seule la position compte."""

    def __init__(self, position: int) -> None:
        self._position = position

    def get_position(self) -> int:
        return self._position

    def get_child(self):
        return None


def spin(predicate, timeout: float = 2.0) -> bool:
    context = GLib.MainContext.default()
    end = GLib.get_monotonic_time() + timeout * 1_000_000
    while GLib.get_monotonic_time() < end:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
    return predicate()


# ---- TrackList -------------------------------------------------------------

def test_context_menu_uses_the_row_position():
    view = TrackList()
    view.set_tracks(refs(3))
    seen = []
    view.connect("track-action", lambda _v, action, index: seen.append(
        (action, index)))
    # Clic droit sur la deuxième ligne, quelles que soient les coordonnées.
    view._on_row_right_click(None, 1, 5.0, 900.0, FakeListItem(1))
    view._on_menu_action(None, None, "copy")
    assert seen == [("copy", 1)]


def test_context_menu_ignores_an_unbound_row():
    view = TrackList()
    view.set_tracks(refs(2))
    seen = []
    view.connect("track-action", lambda *_a: seen.append(1))
    view._on_row_right_click(None, 1, 0.0, 0.0,
                             FakeListItem(Gtk.INVALID_LIST_POSITION))
    view._on_menu_action(None, None, "copy")
    assert seen == []


def test_menu_label_follows_the_favourite_state():
    view = TrackList()
    items = refs(2)
    view.set_tracks(items)
    favourites = {items[1].video_id}
    view.set_favorite_checker(lambda ref: ref.video_id in favourites)

    def label() -> str:
        return view._organiser.get_item_attribute_value(
            0, "label", None).get_string()

    view._open_menu(0, None, 0, 0)
    assert label() == "Ajouter aux favoris"
    view._open_menu(1, None, 0, 0)
    assert label() == "Retirer des favoris"


def test_rows_activate_on_single_click():
    assert TrackList().get_single_click_activate()


# ---- Widgets partagés -------------------------------------------------------

@pytest.mark.parametrize("count,expected", [
    (0, "0 titre"), (1, "1 titre"), (2, "2 titres")])
def test_plural(count, expected):
    assert plural(count, "titre") == expected


def test_empty_state_busy_and_action():
    state = EmptyState("A", "b")
    calls = []
    state.set_message("Recherche", "", busy=True)
    assert state.spinner.get_visible() and state.spinner.get_spinning()
    assert not state.action_button.get_visible()
    state.set_message("Erreur", "x", action="Réessayer",
                      on_action=lambda: calls.append(1))
    assert not state.spinner.get_visible()
    assert state.action_button.get_visible()
    state.action_button.emit("clicked")
    assert calls == [1]
    state.set_message("Autre", "y")
    assert not state.action_button.get_visible()


# ---- DotProgressBar / DotVolume ---------------------------------------------

class FakeGesture:
    def __init__(self, start_x: float) -> None:
        self._start = start_x

    def get_start_point(self):
        return True, self._start, 0.0


@pytest.fixture
def bar() -> DotProgressBar:
    widget = DotProgressBar()
    widget.props.duration = 100.0
    return widget


def test_engine_position_is_ignored_while_dragging(bar):
    bar.props.position = 10.0
    bar._on_drag_begin(None, 0, 0)
    bar._on_drag_update(FakeGesture(0.0), 20.0, 0.0)
    previewed = bar.props.position
    bar.props.position = 11.0               # position venue du moteur
    assert bar.props.position == previewed
    bar._on_drag_end(FakeGesture(0.0), 20.0, 0.0)
    bar.props.position = 11.0
    assert bar.props.position == 11.0


def test_drag_seeks_once(bar):
    seen = []
    bar.connect("seek-requested", lambda _w, value: seen.append(value))
    bar._on_drag_begin(None, 0, 0)
    bar._on_drag_update(FakeGesture(0.0), 20.0, 0.0)
    bar._on_released(None, 1, 20.0, 0.0)    # relâche issue du glissement
    bar._on_drag_end(FakeGesture(0.0), 20.0, 0.0)
    assert len(seen) == 1


def test_simple_click_seeks_once(bar):
    seen = []
    bar.connect("seek-requested", lambda _w, value: seen.append(value))
    bar._on_drag_begin(None, 0, 0)
    bar._on_released(None, 1, 0.0, 0.0)
    bar._on_drag_end(FakeGesture(0.0), 0.0, 0.0)
    assert len(seen) == 1


def test_progress_accessible_value_follows_the_position(bar):
    bar.props.position = 65.0
    assert bar._a11y_key == (65, 100)


def test_volume_updates_without_error():
    volume = DotVolume()
    volume.props.volume = 60.0
    assert volume.props.volume == 60.0


# ---- Smart playlists --------------------------------------------------------

class FakeLibrary:
    def __init__(self) -> None:
        self.since = []

    def top_tracks(self, limit, since):
        self.since.append(since)
        return []

    def unplayed(self, **_kw):
        return []

    def added_since(self, since, **_kw):
        self.since.append(since)
        return []


def test_smart_moment_is_computed_when_the_rule_runs(monkeypatch):
    library = FakeLibrary()
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr("triat.core.smart.time.time", lambda: clock["now"])
    playlists = {p.key: p for p in smart_playlists(library)}
    clock["now"] += 10 * DAY                 # l'application reste ouverte
    playlists["repeat"].fetch()
    playlists["recent"].fetch()
    assert library.since == [clock["now"] - 30 * DAY, clock["now"] - 14 * DAY]


def test_smart_fixed_now_is_respected():
    library = FakeLibrary()
    playlists = {p.key: p for p in smart_playlists(library, now=5_000_000.0)}
    playlists["repeat"].fetch()
    assert library.since == [5_000_000.0 - 30 * DAY]


# ---- Cache de pochettes -----------------------------------------------------

def test_failed_cover_is_not_requested_again(monkeypatch):
    calls = []
    monkeypatch.setattr(cover_module.artwork, "fetch",
                        lambda url: calls.append(url))
    cache = cover_module._TextureCache()
    answers = []
    cache.request("http://x/a.jpg", 64, answers.append)
    assert spin(lambda: answers == [None])
    cache.request("http://x/a.jpg", 64, answers.append)
    assert spin(lambda: len(answers) == 2)
    assert calls == ["http://x/a.jpg"], "l'échec doit être mémorisé"


def test_failure_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(cover_module.artwork, "fetch",
                        lambda url: calls.append(url))
    cache = cover_module._TextureCache()
    cache.FAILURE_TTL = 0.0
    answers = []
    cache.request("http://x/b.jpg", 64, answers.append)
    assert spin(lambda: len(answers) == 1)
    cache.request("http://x/b.jpg", 64, answers.append)
    assert spin(lambda: len(answers) == 2)
    assert len(calls) == 2


def test_texture_is_built_without_the_deprecated_api():
    from gi.repository import Gdk, GdkPixbuf

    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 4, 4)
    pixbuf.fill(0x336699FF)
    texture = cover_module._TextureCache._texture_from(pixbuf)
    assert isinstance(texture, Gdk.MemoryTexture)
    assert (texture.get_width(), texture.get_height()) == (4, 4)

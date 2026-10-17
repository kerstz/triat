"""Minuterie de sommeil : bouton, menu, compte à rebours (Lecture en cours)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from triat.ui.now_playing import NowPlayingView
from tests.test_window import engine  # noqa: F401  (fixture)


def test_sleep_choice_minutes(engine, monkeypatch):      # noqa: F811
    calls = []
    monkeypatch.setattr(engine, "set_sleep_timer",
                        lambda *a, **k: calls.append((a, k)))
    view = NowPlayingView(engine)
    view.sleep_items[30].emit("clicked")
    assert calls == [((30,), {})]


def test_sleep_choice_end_of_track_and_queue(engine, monkeypatch):   # noqa: F811
    calls = []
    monkeypatch.setattr(engine, "set_sleep_timer",
                        lambda *a, **k: calls.append((a, k)))
    view = NowPlayingView(engine)
    view.sleep_items["end_of_track"].emit("clicked")
    view.sleep_items["end_of_queue"].emit("clicked")
    assert calls == [((), {"end_of_track": True}), ((), {"end_of_queue": True})]


def test_sleep_label_follows_signal_and_cancel(engine):   # noqa: F811
    view = NowPlayingView(engine)
    assert not view._sleep_label.get_visible()
    assert not view.sleep_items["cancel"].get_visible()
    engine.set_sleep_timer(end_of_track=True)
    assert view._sleep_label.get_text() == "fin du titre"
    assert view.sleep_items["cancel"].get_visible()
    engine.set_sleep_timer(end_of_queue=True)
    assert view._sleep_label.get_text() == "fin de la file"
    engine.set_sleep_timer(30)
    assert view._sleep_source
    assert view._sleep_label.get_text() in ("30:00", "29:59")
    view.sleep_items["cancel"].emit("clicked")
    assert engine.sleep_timer_remaining() is None
    assert not view._sleep_label.get_visible()
    assert view._sleep_source == 0


def test_sleep_fading_label(engine):                      # noqa: F811
    view = NowPlayingView(engine)
    engine.emit("sleep-timer-changed", {
        "mode": "minutes", "minutes": 15, "deadline": 0.0, "fading": True})
    assert view._sleep_label.get_text() == "fondu…"
    engine.emit("sleep-timer-changed", None)
    assert view._sleep_source == 0

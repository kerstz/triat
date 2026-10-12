"""MPRIS : Seeked, SetPosition, options synchronisées."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")


from triat.core.models import TrackRef
from triat.services.mpris import MprisService, track_object_path
from tests.test_window import engine  # noqa: F401 - fixture


class Bus:
    def __init__(self) -> None:
        self.signals: list[tuple[str, str, object]] = []

    def emit_signal(self, _dest, _path, iface, name, payload) -> None:
        self.signals.append((iface, name, payload.unpack()))


def make(engine):
    service = MprisService(engine, None)
    service._connection = Bus()
    service._connect_engine()
    return service


def test_seeked_emitted_on_position_jump(engine):
    service = make(engine)
    engine.emit("track-changed", TrackRef("a" * 11, "T", "A", duration=200.0))
    bus = service._connection
    bus.signals.clear()
    for pos in (1.0, 1.4, 1.8):
        engine.emit("position-changed", pos)
    assert not [s for s in bus.signals if s[1] == "Seeked"]
    engine.emit("position-changed", 90.0)
    seeked = [s for s in bus.signals if s[1] == "Seeked"]
    assert seeked and seeked[0][2] == (90_000_000,)


def test_options_changed_relays_properties(engine):
    service = make(engine)
    bus = service._connection
    engine.set_volume(40)
    names = set()
    for _i, name, (_iface, changed, _inv) in (s for s in bus.signals
                                              if s[1] == "PropertiesChanged"):
        names |= set(changed)
    assert {"Volume", "Shuffle", "LoopStatus"} <= names


def test_set_position_validates_track_and_range(engine):
    service = make(engine)
    ref = TrackRef("b" * 11, "T", "A", duration=100.0)
    engine.current_ref = None
    engine._current_ref = ref
    engine.player.duration = 100.0
    engine.player.seeks.clear()
    service._set_position("/org/triat/Triat/track/other", 5_000_000)
    service._set_position(track_object_path(ref.video_id), 500_000_000)
    assert engine.player.seeks == []
    service._set_position(track_object_path(ref.video_id), 5_000_000)
    assert engine.player.seeks == [5.0]

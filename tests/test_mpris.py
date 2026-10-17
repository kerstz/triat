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


def test_unpublish_disconnects_engine_handlers(engine):
    service = make(engine)
    bus = service._connection
    assert service._handler_ids
    service.unpublish()
    assert service._handler_ids == []
    bus.signals.clear()
    engine.set_volume(30)
    engine.emit("position-changed", 10.0)
    assert bus.signals == [], "plus aucun signal après unpublish"


def test_connect_engine_twice_does_not_double_handlers(engine):
    service = make(engine)
    service._connect_engine()
    bus = service._connection
    bus.signals.clear()
    engine.set_volume(20)
    assert len([s for s in bus.signals if s[1] == "PropertiesChanged"]) == 1


def test_track_object_path_valide_pour_soundcloud():
    path = track_object_path("soundcloud:tracks:123")
    assert path == "/org/triat/Triat/track/soundcloud_tracks_123" or \
        path.endswith("soundcloud_tracks_123")
    assert ":" not in path


def test_metadata_xesam_url_est_le_permalink(engine):
    service = make(engine)
    engine._current_ref = TrackRef("soundcloud:tracks:5", "T", "A", 10.0,
                                   web_url="https://soundcloud.com/a/t")
    assert service._metadata()["xesam:url"].unpack() == "https://soundcloud.com/a/t"
    engine._current_ref = TrackRef("b" * 11, "T", "A", 10.0)
    assert service._metadata()["xesam:url"].unpack().endswith("b" * 11)


def _piste(ref):
    return type("T", (), {"to_ref": lambda self: ref})()


def test_openuri_soundcloud_joue_via_open_uri(engine, monkeypatch):
    service = make(engine)
    ref = TrackRef("soundcloud:tracks:7", "T", "A", 10.0)
    monkeypatch.setattr("triat.services.mpris.GLib.idle_add",
                        lambda fn, *a: fn(*a))
    played = []
    monkeypatch.setattr(engine, "play_refs",
                        lambda refs, start=0: played.append(list(refs)))
    service._play_soundcloud(lambda uri: _piste(ref), "https://soundcloud.com/a/t")
    assert played == [[ref]]


def test_openuri_soundcloud_desactive_ne_joue_rien(engine, monkeypatch, caplog):
    from triat.core.extractor import ExtractionError
    service = make(engine)
    played = []
    monkeypatch.setattr(engine, "play_refs", lambda *a, **k: played.append(a))

    def refuse(uri):
        raise ExtractionError("SoundCloud désactivé")

    with caplog.at_level("WARNING"):
        service._play_soundcloud(refuse, "https://soundcloud.com/a/t")
    assert played == []
    assert "refusé" in caplog.text


def test_openuri_reconnait_les_permalinks_soundcloud(engine):
    service = make(engine)
    assert service._is_sc_permalink("https://soundcloud.com/a/t")
    assert not service._is_sc_permalink("https://evil.example/soundcloud.com")
    assert not service._is_sc_permalink("https://www.youtube.com/watch?v=x")

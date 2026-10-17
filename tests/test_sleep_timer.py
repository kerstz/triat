"""Minuterie de sommeil (F02) : horloge injectée, aucun vrai délai."""

from __future__ import annotations

import types

import pytest

from triat.core import engine as engine_mod
from triat.core.engine import Engine
from triat.core.library import Library
from triat.core.models import TrackRef
from triat.core.settings import Settings
from tests.fakes import (FakeExtractor, FakePlayer, FakeRecommender,
                         FakeSponsorBlock)
from tests.test_engine import pump


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class VolumePlayer(FakePlayer):
    """FakePlayer qui garde l'historique des volumes."""

    def __init__(self) -> None:
        super().__init__()
        self.volumes: list[float] = []

    def set_volume(self, value: float) -> None:
        self.volumes.append(value)
        super().set_volume(value)


@pytest.fixture
def eng(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("autoplay_radio", False)
    settings.set("sync_port", 0)
    settings.set("volume", 80.0)
    e = Engine(settings=settings, library=Library(tmp_path / "l.db"),
               player=VolumePlayer(), extractor=FakeExtractor(),
               sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
               ytmusic=object(),
               cookies=types.SimpleNamespace(clear=lambda: None,
                                             release=lambda: None))
    e._clock = Clock()
    yield e
    e.sync.stop()
    e.library.close()


def refs(n):
    return [TrackRef(f"v{i}", f"T{i}", "A", 200.0) for i in range(n)]


def test_remaining_and_signal(eng):
    seen = []
    eng.connect("sleep-timer-changed", lambda _e, st: seen.append(st))
    assert eng.sleep_timer_remaining() is None
    eng.set_sleep_timer(10)
    assert eng.sleep_timer_remaining() == 600.0
    eng._clock.now += 100
    assert eng.sleep_timer_remaining() == 500.0
    assert seen[0]["mode"] == "minutes" and seen[0]["minutes"] == 10
    eng.cancel_sleep_timer()
    assert seen[-1] is None and eng.sleep_timer_remaining() is None
    assert eng._sleep_id == 0


def test_exactly_one_mode(eng):
    with pytest.raises(ValueError):
        eng.set_sleep_timer()
    with pytest.raises(ValueError):
        eng.set_sleep_timer(5, end_of_track=True)
    with pytest.raises(ValueError):
        eng.set_sleep_timer(0)


def test_fade_then_pause_then_volume_restored(eng):
    eng.play_refs(refs(2))
    assert pump(lambda: eng.player.played)
    eng.set_sleep_timer(1)
    eng._clock.now += 60                     # échéance : début du fondu
    eng._sleep_tick()
    assert eng._sleep["fading"]
    eng._clock.now += engine_mod.SLEEP_FADE_SECONDS / 2
    eng._sleep_tick()
    assert eng.player.volume == pytest.approx(40.0)
    assert eng.player.state == "playing"
    eng._clock.now += engine_mod.SLEEP_FADE_SECONDS
    seen = []
    eng.connect("sleep-timer-changed", lambda _e, st: seen.append(st))
    assert eng._sleep_tick() is False
    assert eng.player.state == "paused"
    assert eng.player.volume == 80.0         # restauré après la pause
    assert eng._sleep is None and seen == [None]


def test_cancel_during_fade_restores_volume(eng):
    eng.set_sleep_timer(1)
    eng._clock.now += 60
    eng._sleep_tick()
    eng._clock.now += 10
    eng._sleep_tick()
    assert eng.player.volume < 80.0
    eng.cancel_sleep_timer()
    assert eng.player.volume == 80.0


def test_end_of_track_stops_the_chain(eng):
    eng.play_refs(refs(3))
    assert pump(lambda: eng.player.played)
    eng.set_sleep_timer(end_of_track=True)
    eng.player.emit("eof")
    pump(timeout=0.2)
    assert len(eng.player.played) == 1       # pas d'enchaînement
    assert eng.sleep_timer_remaining() is None and eng._sleep is None


def test_end_of_queue_lets_the_queue_run_then_stops_without_radio(eng):
    eng.settings.set("autoplay_radio", True)
    eng.play_refs(refs(2))
    assert pump(lambda: eng.player.played)
    eng.set_sleep_timer(end_of_queue=True)
    eng.player.emit("eof")
    assert pump(lambda: len(eng.player.played) == 2)
    assert eng._sleep is not None            # il reste une piste
    eng.player.emit("eof")
    pump(timeout=0.2)
    assert eng._sleep is None
    assert eng.recommender.calls == []       # pas de radio


def test_wipe_and_shutdown_remove_the_timer(eng):
    eng.set_sleep_timer(5)
    assert eng._sleep_id
    eng.wipe_user_data()
    assert eng._sleep is None and eng._sleep_id == 0
    eng.set_sleep_timer(5)
    eng.shutdown()
    assert eng._sleep is None and eng._sleep_id == 0

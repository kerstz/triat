"""Player : filtres audio (gain par piste) et fondu de pause, sans libmpv."""

from __future__ import annotations

import types

import pytest

from triat.core import player as player_mod
from triat.core.player import Player
from tests.test_engine import pump


@pytest.fixture
def pl(monkeypatch):
    monkeypatch.setattr(player_mod, "PAUSE_FADE_SECONDS", 0.06)
    p = Player.__new__(Player)
    p._mpv = types.SimpleNamespace(af="", volume=100.0, pause=False)
    p._state = "playing"
    p._normalise = False
    p._eq_filter = ""
    p._gain_filter = ""
    p._volume = 100.0
    p.pause_fade = True
    p._fade_id = 0
    p._fade_dir = ""
    return p


def test_gain_filter_sits_next_to_the_equalizer(pl):
    pl.set_equalizer([1.0] * 10)
    eq = pl._mpv.af
    assert eq
    pl.set_gain(-3.5)
    assert pl._mpv.af == f"{eq},volume=-3.50dB"
    pl.set_normalisation(True)
    assert pl._mpv.af.endswith(f"volume=-3.50dB,{player_mod.NORMALISE_FILTER}")
    pl.set_gain(0.0)
    assert "-3.50dB" not in pl._mpv.af and eq in pl._mpv.af


def test_pause_fades_out_then_pauses_and_restores_volume(pl):
    volumes = []
    pl.toggle_pause()
    assert pl._mpv.pause is False            # pas encore : fondu en cours
    assert pump(lambda: pl._mpv.pause is True)
    assert pl._mpv.volume == 100.0 and pl._fade_id == 0
    del volumes


def test_resume_fades_in_from_silence(pl):
    pl._mpv.pause = True
    pl.toggle_pause()
    assert pl._mpv.pause is False and pl._mpv.volume == 0.0
    assert pump(lambda: pl._mpv.volume == 100.0)
    assert pl._mpv.pause is False


def test_pause_fade_disabled_is_immediate(pl):
    pl.pause_fade = False
    pl.toggle_pause()
    assert pl._mpv.pause is True and pl._fade_id == 0


def test_second_press_during_fade_out_cancels_the_pause(pl):
    pl.toggle_pause()
    pl.toggle_pause()
    pump(timeout=0.15)
    assert pl._mpv.pause is False and pl._mpv.volume == 100.0


def test_no_fade_when_volume_is_already_zero(pl):
    pl.set_volume(0.0)
    pl.toggle_pause()
    assert pl._mpv.pause is True


# ---- end-file attribué à la bonne piste (P3-1) ------------------------------

@pytest.fixture
def lp(monkeypatch):
    """Player sans libmpv, avec le seul état utile aux événements."""
    p = Player.__new__(Player)
    p._mpv = types.SimpleNamespace()
    p._gen = 0
    p._entry_id = None
    p._state = "playing"
    p._track = None
    p._duration = 0.0
    p._position = 0.0
    p._fade_id = 0
    p._fade_dir = ""
    return p


def _end_file(entry_id):
    data = types.SimpleNamespace(reason=0, playlist_entry_id=entry_id)
    return types.SimpleNamespace(data=data)


def test_end_file_of_a_replaced_entry_is_ignored(lp):
    lp._entry_id = 7
    assert lp._entry_matches(_end_file(7))
    assert not lp._entry_matches(_end_file(6))


def test_end_file_before_start_file_belongs_to_the_old_file(lp):
    lp._entry_id = None                 # play_track vient d'être appelé
    assert not lp._entry_matches(_end_file(7))


def test_end_file_without_entry_id_is_accepted(lp):
    lp._entry_id = None
    event = types.SimpleNamespace(data=types.SimpleNamespace(reason=0))
    assert lp._entry_matches(event)


def test_set_state_from_a_stale_generation_is_dropped(lp):
    lp._gen = 3
    states = []
    lp.emit = lambda name, *a: states.append((name, a))
    lp._set_state("paused", 2)
    assert lp._state == "playing" and not states
    lp._set_state("paused", 3)
    assert lp._state == "paused"
    lp._set_state("idle")               # sans génération : toujours appliqué
    assert lp._state == "idle"

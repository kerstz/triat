"""Tests d'orchestration : enchaînement, SponsorBlock, historique, reprise.

L'Engine fait son travail dans des threads et republie via GLib.idle_add :
les tests font donc tourner une vraie boucle d'événements GLib.
"""

from __future__ import annotations

import time

import pytest
from gi.repository import GLib

from triat.core.engine import Engine
from triat.core.library import Library
from triat.core.models import Segment, Track, TrackRef
from triat.core.settings import Settings
from triat.core.sponsorblock import ASK, SKIP
from tests.fakes import (FakeCookies, FakeExtractor, FakePlayer, FakeRecommender,
                         FakeSponsorBlock)


def pump(condition=None, timeout: float = 2.0) -> bool:
    """Fait tourner la boucle GLib jusqu'à `condition` ou expiration."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if condition is None:
            return True
        if condition():
            return True
        time.sleep(0.005)
    return condition() if condition else True


def refs(n: int) -> list[TrackRef]:
    return [TrackRef(f"v{i}", f"Titre {i}", "Artiste", 200.0) for i in range(n)]


@pytest.fixture
def engine(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    settings.set("autoplay_radio", False)
    settings.set("sync_port", 0)        # jamais le vrai port 47823 en test
    eng = Engine(
        settings=settings,
        library=Library(tmp_path / "lib.db"),
        player=FakePlayer(),
        extractor=FakeExtractor(),
        sponsorblock=FakeSponsorBlock(),
        recommender=FakeRecommender(),
        ytmusic=object(),
        cookies=object(),
    )
    yield eng
    eng.sync.stop()
    eng.library.close()


# ---- Enchaînement -------------------------------------------------------

def test_playing_a_queue_resolves_and_starts_the_first_track(engine):
    engine.play_refs(refs(3))
    assert pump(lambda: engine.player.played)
    assert engine.player.played[0].video_id == "v0"
    assert engine.current.video_id == "v0"


def test_end_of_track_advances_to_the_next(engine):
    engine.play_refs(refs(3))
    assert pump(lambda: engine.player.played)
    engine.player.finish()
    assert pump(lambda: len(engine.player.played) == 2)
    assert engine.player.played[1].video_id == "v1"


def test_end_of_last_track_stops_when_autoplay_is_off(engine):
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    engine.player.finish()
    pump(timeout=0.3)
    assert len(engine.player.played) == 1


def test_a_dead_track_does_not_block_the_queue(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("autoplay_radio", False)
    engine = Engine(
        settings=settings, library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(fail_ids={"v0"}),
        sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
        ytmusic=object(), cookies=object(),
    )
    errors = []
    engine.connect("error", lambda _e, msg: errors.append(msg))
    engine.play_refs(refs(2))
    assert pump(lambda: engine.player.played, timeout=3.0)
    assert engine.player.played[0].video_id == "v1", "on saute la piste illisible"
    assert errors and "v0" in errors[0]
    engine.library.close()


def test_previous_rewinds_mid_track_instead_of_changing_track(engine):
    engine.play_refs(refs(3))
    assert pump(lambda: engine.player.played)
    engine.jump_to(1)
    assert pump(lambda: len(engine.player.played) == 2)
    engine.player.advance(60.0)
    engine.previous()
    assert engine.player.seeks[-1] == 0.0
    assert engine.current.video_id == "v1", "on reste sur la même piste"


def test_previous_at_the_very_start_goes_back_one_track(engine):
    engine.play_refs(refs(3))
    assert pump(lambda: engine.player.played)
    engine.jump_to(1)
    assert pump(lambda: len(engine.player.played) == 2)
    engine.player.advance(1.0)
    engine.previous()
    assert pump(lambda: engine.current.video_id == "v0")


# ---- SponsorBlock -------------------------------------------------------

def test_segment_is_skipped_automatically(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("autoplay_radio", False)
    settings.set("sponsorblock_categories", {"music_offtopic": SKIP})
    engine = Engine(
        settings=settings, library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(),
        sponsorblock=FakeSponsorBlock([Segment(0, 22.0, "music_offtopic", "u1")]),
        recommender=FakeRecommender(), ytmusic=object(), cookies=object(),
    )
    skips = []
    engine.connect("segment-skipped", lambda _e, seg, gained: skips.append((seg, gained)))

    engine.play_refs(refs(1))
    assert pump(lambda: engine._planner is not None and engine.player.played)
    engine.player.advance(1.0)
    pump(timeout=0.2)

    assert engine.player.seeks == [22.0]
    assert skips and skips[0][0].category == "music_offtopic"
    assert skips[0][1] == pytest.approx(21.0)
    assert engine.session_skips["segments"] == 1
    assert engine.session_skips["seconds"] == pytest.approx(21.0)
    assert len(engine.session_skips["tracks"]) == 1
    engine.library.close()


def test_same_segment_is_never_skipped_twice(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("sponsorblock_categories", {"sponsor": SKIP})
    engine = Engine(
        settings=settings, library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(),
        sponsorblock=FakeSponsorBlock([Segment(10, 30, "sponsor", "u1")]),
        recommender=FakeRecommender(), ytmusic=object(), cookies=object(),
    )
    engine.play_refs(refs(1))
    assert pump(lambda: engine._planner is not None and engine.player.played)
    engine.player.advance(11.0)
    pump(timeout=0.1)
    # mpv renvoie souvent des positions antérieures juste après un seek.
    engine.player.advance(12.0)
    engine.player.advance(15.0)
    pump(timeout=0.2)
    assert engine.player.seeks == [30.0], "un seul saut malgré le retour en arrière"
    engine.library.close()


def test_ask_policy_notifies_instead_of_skipping(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("sponsorblock_categories", {"intro": ASK})
    engine = Engine(
        settings=settings, library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(),
        sponsorblock=FakeSponsorBlock([Segment(0, 10, "intro", "u1")]),
        recommender=FakeRecommender(), ytmusic=object(), cookies=object(),
    )
    pending = []
    engine.connect("segment-pending", lambda _e, seg: pending.append(seg))
    engine.play_refs(refs(1))
    assert pump(lambda: engine._planner is not None and engine.player.played)
    engine.player.advance(2.0)
    engine.player.advance(3.0)
    pump(timeout=0.2)
    assert engine.player.seeks == [], "rien n'est sauté sans accord"
    assert len(pending) == 1, "on ne redemande pas à chaque tick"
    engine.library.close()


def test_cancelling_a_skip_rewinds_and_disables_it(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("sponsorblock_categories", {"sponsor": SKIP})
    segment = Segment(10, 30, "sponsor", "u1")
    engine = Engine(
        settings=settings, library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(),
        sponsorblock=FakeSponsorBlock([segment]),
        recommender=FakeRecommender(), ytmusic=object(), cookies=object(),
    )
    engine.play_refs(refs(1))
    assert pump(lambda: engine._planner is not None and engine.player.played)
    engine.player.advance(11.0)
    pump(timeout=0.1)

    engine.cancel_segment(segment)
    assert engine.player.seeks[-1] == 10.0
    engine.player.advance(11.0)
    pump(timeout=0.2)
    assert engine.player.seeks[-1] == 10.0, "après annulation, plus aucun saut"
    engine.library.close()


def test_sponsorblock_disabled_means_no_network_call(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("sponsorblock_enabled", False)
    sb = FakeSponsorBlock([Segment(0, 20, "sponsor", "u")])
    engine = Engine(
        settings=settings, library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(), sponsorblock=sb,
        recommender=FakeRecommender(), ytmusic=object(), cookies=object(),
    )
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    pump(timeout=0.3)
    assert sb.calls == []
    engine.library.close()


# ---- Historique ---------------------------------------------------------

def test_history_records_a_track_listened_long_enough(engine):
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    engine.player.advance(150.0)          # 200 s au total, ratio 0.5
    engine.player.finish()
    pump(timeout=0.3)
    assert engine.library.stats()["plays"] == 1


def test_history_ignores_a_track_barely_started(engine):
    engine.play_refs(refs(2))
    assert pump(lambda: engine.player.played)
    engine.player.advance(4.0)
    engine.next()
    pump(timeout=0.3)
    assert engine.library.stats()["plays"] == 0


def test_private_session_writes_no_history(engine):
    engine.settings.set("private_session", True)
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    engine.player.advance(180.0)
    engine.player.finish()
    pump(timeout=0.3)
    assert engine.library.stats()["plays"] == 0


def test_a_track_is_recorded_only_once(engine):
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    engine.player.advance(180.0)
    engine.player.finish()
    engine._flush_history(completed=True)
    pump(timeout=0.3)
    assert engine.library.stats()["plays"] == 1


# ---- Radio automatique --------------------------------------------------

def test_autoplay_refills_the_queue_at_the_end(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("autoplay_radio", True)
    engine = Engine(
        settings=settings, library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(),
        sponsorblock=FakeSponsorBlock(),
        recommender=FakeRecommender(radio=[TrackRef("r1", "Radio 1"),
                                           TrackRef("r2", "Radio 2")]),
        ytmusic=object(), cookies=object(),
    )
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    engine.player.finish()
    assert pump(lambda: len(engine.queue) == 3, timeout=3.0)
    assert pump(lambda: engine.current.video_id == "r1", timeout=3.0)
    engine.library.close()


def test_autoplay_does_not_repeat_tracks_already_queued(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("autoplay_radio", True)
    engine = Engine(
        settings=settings, library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(),
        sponsorblock=FakeSponsorBlock(),
        recommender=FakeRecommender(radio=[TrackRef("v0", "Déjà là"),
                                           TrackRef("neuf", "Neuf")]),
        ytmusic=object(), cookies=object(),
    )
    engine.play_refs(refs(1))          # contient déjà v0
    assert pump(lambda: engine.player.played)
    engine.player.finish()
    assert pump(lambda: len(engine.queue) == 2, timeout=3.0)
    assert [t.video_id for t in engine.queue.items] == ["v0", "neuf"]
    engine.library.close()


# ---- Persistance --------------------------------------------------------

def test_state_survives_a_restart(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("autoplay_radio", False)
    engine = Engine(
        settings=settings, library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(),
        sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
        ytmusic=object(), cookies=object(),
    )
    engine.play_refs(refs(4))
    assert pump(lambda: engine.player.played)
    engine.jump_to(2)
    assert pump(lambda: len(engine.player.played) == 2)
    engine.player.advance(75.0)
    engine.save_state()
    engine.library.close()

    revived = Engine(
        settings=Settings(tmp_path / "s.json"), library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(),
        sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
        ytmusic=object(), cookies=object(),
    )
    assert revived.restore_state() is True
    assert len(revived.queue) == 4
    assert revived.current.video_id == "v2"
    assert revived.player.played == [], "restaurer ne lance pas la lecture"
    revived.library.close()


def test_restore_on_a_fresh_profile_returns_false(tmp_path):
    engine = Engine(
        settings=Settings(tmp_path / "s.json"), library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(),
        sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
        ytmusic=object(), cookies=object(),
    )
    assert engine.restore_state() is False
    engine.library.close()


# ---- Préchargement --------------------------------------------------------

def test_next_track_is_resolved_in_advance(engine):
    engine.play_refs(refs(3))
    assert pump(lambda: engine.player.played)
    # Le préchargement part 3 s après le démarrage : on le déclenche.
    engine._prefetch_next()
    assert pump(lambda: engine._prefetched is not None, timeout=3.0)
    assert engine._prefetched[0] == "v1"


def test_prefetched_track_is_used_without_a_second_resolution(engine):
    engine.play_refs(refs(3))
    assert pump(lambda: engine.player.played)
    engine._prefetch_next()
    assert pump(lambda: engine._prefetched is not None, timeout=3.0)

    before = len(engine.extractor.resolved)
    engine.player.finish()
    assert pump(lambda: len(engine.player.played) == 2, timeout=3.0)
    assert len(engine.extractor.resolved) == before, \
        "la piste anticipée ne doit pas être résolue une seconde fois"


def test_prefetch_is_dropped_when_the_queue_changes(engine):
    engine.play_refs(refs(3))
    assert pump(lambda: engine.player.played)
    engine._prefetch_next()
    assert pump(lambda: engine._prefetched is not None, timeout=3.0)

    engine.queue.insert_next([TrackRef("neuf", "Neuf")])
    pump(timeout=0.3)
    assert engine._prefetched is None, "l'anticipation ne vaut plus"


def test_prefetch_does_nothing_on_an_empty_queue(engine):
    engine._prefetch_next()
    pump(timeout=0.3)
    assert engine._prefetched is None


def test_normalisation_setting_reaches_the_player(engine):
    engine.set_normalisation(False)
    assert engine.player.normalisation is False
    assert engine.settings.get("normalise_volume") is False


def test_equalizer_is_off_by_default(engine):
    assert engine.equalizer == (False, (0.0,) * 10)
    assert engine.player.equalizer is None


def test_equalizer_setting_reaches_the_player(engine):
    gains = (6, 5, 4, 2, 0, 0, 0, 0, 0, 0)
    engine.set_equalizer(enabled=True, gains=gains)
    assert engine.player.equalizer == tuple(float(g) for g in gains)
    assert engine.settings.get("eq_enabled") is True
    engine.set_equalizer(enabled=False)
    assert engine.player.equalizer is None
    # Les gains survivent à la désactivation.
    assert engine.equalizer[1][0] == 6.0


# ---- Clip vidéo (Immersion) ---------------------------------------------

def test_video_mode_grafts_the_clip_on_the_current_track(engine):
    engine.play_refs(refs(2))
    assert pump(lambda: engine.player.played)
    states = []
    engine.connect("video-changed", lambda _e, s, _v: states.append(s))
    engine.set_video_mode(True)
    assert pump(lambda: engine.player.video is not None)
    assert engine.player.video[0] == "v0"
    assert states == ["loading", "ready"]
    # Qualité « haute » par défaut → 1080p au plus.
    assert engine.extractor.video_heights == [1080]


def test_video_follows_the_next_track(engine):
    engine.play_refs(refs(2))
    assert pump(lambda: engine.player.played)
    engine.set_video_mode(True)
    assert pump(lambda: engine.player.video is not None)
    engine.next()
    assert pump(lambda: engine.player.video and engine.player.video[0] == "v1")


def test_leaving_video_mode_drops_the_clip(engine):
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    engine.set_video_mode(True)
    assert pump(lambda: engine.player.video is not None)
    engine.set_video_mode(False)
    assert engine.player.video is None
    assert engine.video_state == "off"


def test_missing_clip_is_reported_but_audio_goes_on(engine):
    engine.extractor.no_video_ids.add("v0")
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    engine.set_video_mode(True)
    assert pump(lambda: engine.video_state == "unavailable")
    assert engine.player.video is None
    assert engine.player.state == "playing"


def test_late_clip_of_a_skipped_track_is_ignored(engine):
    engine.play_refs(refs(2))
    assert pump(lambda: engine.player.played)
    engine.set_video_mode(True)
    engine.next()                   # avant que le clip de v0 n'arrive
    assert pump(lambda: engine.player.video is not None)
    pump(timeout=0.2)
    assert engine.player.video[0] == "v1"


def test_backfill_fills_missing_durations(tmp_path):
    import time as _time
    from gi.repository import GLib
    from tests.fakes import (FakeCookies, FakeExtractor, FakeInnerTube,
                             FakePlayer, FakeRecommender, FakeSponsorBlock)
    from triat.core.engine import Engine
    from triat.core.library import Library
    from triat.core.models import TrackRef
    from triat.core.settings import Settings

    inner = FakeInnerTube()
    inner.lengths = {"a" * 11: 200.0}
    eng = Engine(settings=Settings(tmp_path / "s.json"),
                 library=Library(tmp_path / "l.db"), player=FakePlayer(),
                 extractor=FakeExtractor(), sponsorblock=FakeSponsorBlock(),
                 recommender=FakeRecommender(), ytmusic=object(),
                 cookies=FakeCookies(), innertube=inner)
    eng.library.upsert_many([TrackRef("a" * 11, "A"), TrackRef("b" * 11, "B")])
    changed = []
    eng.connect("library-changed", lambda *_: changed.append(1))
    eng.backfill_durations_async()
    deadline = _time.time() + 5
    while not changed and _time.time() < deadline:
        GLib.MainContext.default().iteration(False)
    assert changed
    assert eng.library.get_track("a" * 11).duration == 200.0
    assert eng.library.missing_duration() == ["b" * 11]
    # L'échec n'est pas réessayé dans la session.
    eng.backfill_durations_async()
    assert not eng._backfilling
    eng.library.close()


def test_play_after_restart_opens_the_restored_track(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("autoplay_radio", False)
    engine = Engine(settings=settings, library=Library(tmp_path / "l.db"),
                    player=FakePlayer(), extractor=FakeExtractor(),
                    sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
                    ytmusic=object(), cookies=object())
    engine.play_refs(refs(3), start=1)
    pump(lambda: engine.player.played)
    engine.save_state()
    engine.library.close()

    revived = Engine(settings=settings, library=Library(tmp_path / "l.db"),
                     player=FakePlayer(), extractor=FakeExtractor(),
                     sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
                     ytmusic=object(), cookies=object())
    assert revived.restore_state()
    assert not revived.player.played
    revived.toggle_pause()                       # « lecture » après redémarrage
    assert pump(lambda: revived.player.played)
    revived.library.close()


# ---- Synchro Wi-Fi ---------------------------------------------------------

def _listens(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def test_sync_is_off_until_enabled(engine):
    assert engine.sync is not None
    assert not engine.sync.running          # la construction ne démarre rien


def test_sync_can_be_enabled_then_disabled_and_frees_the_port(engine, monkeypatch):
    import socket
    monkeypatch.setattr(engine.sync, "_announce", lambda *_a: None)   # pas de mDNS
    assert engine.set_sync_enabled(True)
    assert engine.sync.running
    assert engine.settings.get("sync_enabled") is True
    assert Settings(engine.settings._path).get("sync_enabled") is True   # sauvé
    port = engine.sync.port
    assert port and _listens(port)
    assert engine.set_sync_enabled(True)    # deux fois de suite : sans effet

    assert not engine.set_sync_enabled(False)
    assert not engine.sync.running
    assert engine.settings.get("sync_enabled") is False
    assert not _listens(port)
    engine.set_sync_enabled(False)          # couper ce qui est coupé : rien
    # Le port est libre : on peut s'y relier.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("0.0.0.0", port))


def test_shutdown_stops_the_sync_server(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "s.json")
    settings.set("sync_port", 0)
    eng = Engine(settings=settings, library=Library(tmp_path / "l.db"),
                 player=FakePlayer(), extractor=FakeExtractor(),
                 sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
                 ytmusic=object(), cookies=object())
    monkeypatch.setattr(eng.sync, "_announce", lambda *_a: None)
    eng.set_sync_enabled(True)
    port = eng.sync.port
    assert _listens(port)
    eng.shutdown()
    assert not eng.sync.running
    assert not _listens(port)


def test_a_sync_that_changes_the_library_emits_library_changed(engine):
    changed = []
    engine.connect("library-changed", lambda *_: changed.append(1))
    engine.sync._on_changed({"favorites": 1})   # appelé depuis le fil du serveur
    assert pump(lambda: changed)


# ---- Audit 2026-10-12 : événements de l'ancienne piste, précédent, URL ---

def test_stale_events_are_not_credited_to_the_next_track(engine):
    engine.play_refs(refs(4))
    assert pump(lambda: engine.player.played)
    engine.player.advance(100.0)
    engine.next()                              # v1 en cours de résolution
    engine.player.advance(101.0)               # reliquat de v0
    assert engine._max_position < 1.0
    assert pump(lambda: len(engine.player.played) == 2)
    assert engine.current.video_id == "v1"


def test_old_eof_does_not_skip_a_second_track(engine):
    engine.play_refs(refs(4))
    assert pump(lambda: engine.player.played)
    engine.next()
    engine.player.finish()                     # fin de v0 avant le chargement de v1
    pump(timeout=0.3)
    assert engine.current.video_id == "v1"


def test_previous_leaves_the_track_after_a_rewind(engine):
    engine.play_refs(refs(3), start=1)
    assert pump(lambda: engine.player.played)
    engine.player.advance(60.0)
    engine.previous()                          # rembobine
    assert engine.player.seeks == [0.0]
    engine.previous()                          # position_hint = 0 : change de piste
    assert pump(lambda: engine.current.video_id == "v0")


def test_expired_prefetch_is_discarded(engine):
    ref = refs(1)[0]
    track = engine.extractor.resolve(ref.url)
    engine._prefetched = (ref.video_id, track, time.monotonic() - 7200)
    assert engine._take_prefetched(ref) is None
    engine._prefetched = (ref.video_id, track, time.monotonic())
    assert engine._take_prefetched(ref) is track


def test_a_playback_error_retries_once_then_skips(engine):
    engine.play_refs(refs(3))
    assert pump(lambda: engine.player.played)
    engine.player.advance(42.0)
    engine.player.emit("error", "403")
    assert pump(lambda: len(engine.player.played) == 2)
    assert engine.player.played[1].video_id == "v0"      # même piste, ré-résolue
    assert engine.player.starts[-1] == 42.0
    engine.player.emit("error", "403")
    assert pump(lambda: len(engine.player.played) == 3)
    assert engine.player.played[2].video_id == "v1"      # seconde panne : suivante


def test_private_session_does_not_announce_now_playing(engine, monkeypatch):
    engine.settings.set("private_session", True)
    called = []
    monkeypatch.setattr(engine.scrobbler, "now_playing", lambda ref: called.append(ref))
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    assert called == []


def test_autoplay_does_not_hammer_radio_when_it_returns_nothing(engine):
    engine.settings.set("autoplay_radio", True)
    calls = []
    engine.recommender.radio_for = lambda *a, **k: calls.append(1) or []
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    for pos in range(171, 190):
        engine.player.advance(float(pos))
        pump(timeout=0.02)
    assert len(calls) == 1


def test_wipe_user_data_clears_everything(engine, monkeypatch, tmp_path):
    import types
    from triat.core import artwork
    cleared = []
    monkeypatch.setattr(artwork, "clear_cache", lambda: cleared.append(1))
    # Journal et file de scrobbles : jamais ceux de l'utilisateur.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    engine.cookies = types.SimpleNamespace(clear=lambda: cleared.append("cookies"))
    engine.play_refs(refs(2))
    assert pump(lambda: engine.player.played)
    engine.settings.set("search_history", ["daft punk"])
    engine.save_state()
    engine.library.set_favorite(refs(1)[0], True)
    engine.wipe_user_data()
    assert engine.queue.items == [] and engine.current is None
    assert engine.settings.get("last_queue") == []
    assert engine.settings.get("search_history") == []
    assert engine.library.list_favorites() == []
    assert cleared == ["cookies", 1]


def test_option_setters_emit_a_signal(engine):
    seen = []
    engine.connect("options-changed", lambda _e: seen.append(1))
    engine.set_volume(40.0)
    engine.shuffle = True
    engine.repeat = "all"
    assert len(seen) == 3


# ---- Signaux de recommandation -----------------------------------------

def _skips(engine, video_id=None):
    sql = "SELECT video_id, context FROM reco_skips"
    rows = engine.library._db.execute(sql).fetchall()
    return [r for r in rows if video_id is None or r["video_id"] == video_id]


def test_manual_next_records_a_skip(engine):
    engine.play_refs(refs(2))
    assert pump(lambda: engine.player.played)
    engine.player.advance(10.0)
    engine.next()
    assert [r["video_id"] for r in _skips(engine)] == ["v0"]


def test_eof_stop_and_jump_are_not_skips(engine):
    engine.play_refs(refs(4))
    assert pump(lambda: len(engine.player.played) == 1)
    engine.player.advance(5.0)
    engine.player.finish()
    assert pump(lambda: len(engine.player.played) == 2)
    engine.player.advance(5.0)
    engine.jump_to(3)
    assert pump(lambda: len(engine.player.played) == 3)
    engine.player.advance(5.0)
    engine.stop()
    assert _skips(engine) == []


def test_private_session_records_no_skip(engine):
    engine.settings.set("private_session", True)
    engine.play_refs(refs(2))
    assert pump(lambda: engine.player.played)
    engine.player.advance(10.0)
    engine.next()
    assert _skips(engine) == []


def test_late_next_is_not_a_skip(engine):
    engine.play_refs(refs(2))
    assert pump(lambda: engine.player.played)
    engine.player.advance(150.0)          # 75 % d'une piste de 200 s
    engine.next()
    assert _skips(engine) == []


def test_skip_after_35s_is_both_a_play_and_a_skip(engine):
    engine.play_refs(refs(2))
    assert pump(lambda: engine.player.played)
    engine.player.advance(35.0)
    engine.next()
    assert engine.library.stats()["plays"] == 1
    assert len(_skips(engine, "v0")) == 1


def _autoplay_engine(tmp_path, radio):
    settings = Settings(tmp_path / "s.json")
    settings.set("autoplay_radio", True)
    settings.set("sync_port", 0)
    return Engine(
        settings=settings, library=Library(tmp_path / "l.db"),
        player=FakePlayer(), extractor=FakeExtractor(),
        sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(radio=radio),
        ytmusic=object(), cookies=object())


def test_autoplay_passes_context_and_private_flag(tmp_path):
    eng = _autoplay_engine(tmp_path, [TrackRef("r1", "Radio 1")])
    eng.play_refs(refs(14))
    assert pump(lambda: eng.player.played)
    eng.queue.jump_to(13)
    assert pump(lambda: len(eng.player.played) == 2)
    eng.player.advance(175.0)
    assert pump(lambda: eng.recommender.calls)
    call = eng.recommender.calls[-1]
    assert 0 < len(call["context"]) <= 10 and call["private"] is False
    assert "r1" in eng._radio_ids or pump(lambda: "r1" in eng._radio_ids)
    eng.sync.stop()
    eng.library.close()

    eng = _autoplay_engine(tmp_path, [])
    eng.settings.set("private_session", True)
    eng.play_refs(refs(1))
    assert pump(lambda: eng.player.played)
    eng.player.advance(175.0)
    assert pump(lambda: eng.recommender.calls)
    assert eng.recommender.calls[-1]["private"] is True
    eng.sync.stop()
    eng.library.close()


def test_autoplay_retries_after_backoff(engine):
    engine.settings.set("autoplay_radio", True)
    now = [1000.0]
    engine._clock = lambda: now[0]
    calls = []
    engine.recommender.radio_for = lambda *a, **k: calls.append(1) or []
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    engine.player.advance(175.0)
    assert pump(lambda: len(calls) == 1)
    now[0] += 30
    engine.player.advance(176.0)
    pump(timeout=0.1)
    assert len(calls) == 1
    now[0] += 31
    engine.player.advance(177.0)
    assert pump(lambda: len(calls) == 2)


def test_private_session_does_not_cache_played_track(engine):
    engine.settings.set("private_session", True)
    engine.play_refs(refs(2))
    assert pump(lambda: engine.player.played)
    pump(timeout=0.1)
    assert engine.library.get_track("v0") is None
    engine.settings.set("private_session", False)
    engine.play_refs(refs(1))
    assert engine.library.get_track("v0") is not None


def test_block_drops_upcoming_radio_items_only(engine):
    queued = [TrackRef("mine", "À moi", "Banni"), TrackRef("keep", "Reste", "Ok")]
    radio = [TrackRef("rad1", "Radio 1", "Banni"), TrackRef("rad2", "Radio 2", "Ok")]
    engine.play_refs(refs(1))
    assert pump(lambda: engine.player.played)
    engine.enqueue(queued)
    engine._radio_ids.update(r.video_id for r in radio)
    engine.enqueue(radio)
    engine.library.block("artist", "Banni", "Banni")
    assert engine.drop_blocked_upcoming() == 1
    assert [t.video_id for t in engine.queue.items] == ["v0", "mine", "keep", "rad2"]


# ---- Lot W3-A : moteur --------------------------------------------------

def _state_home(monkeypatch, tmp_path):
    """Isole journal et file de scrobbles du profil réel."""
    state = tmp_path / "xdg-state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return state


def _real_services_engine(tmp_path, **kw):
    settings = Settings(tmp_path / "settings.json")
    settings.set("autoplay_radio", False)
    settings.set("sync_port", 0)
    return Engine(settings=settings, library=Library(tmp_path / "lib.db"),
                  player=FakePlayer(), extractor=FakeExtractor(),
                  recommender=FakeRecommender(), ytmusic=object(),
                  cookies=object(), **kw)


def test_private_session_writes_no_lyrics_nor_segments(tmp_path):
    eng = _real_services_engine(tmp_path)
    try:
        eng.settings.set("private_session", True)
        vid = "dQw4w9WgXcQ"
        eng.sponsorblock.fetch = lambda video_id, cats: [
            Segment(10.0, 20.0, "sponsor", "u1")]
        entry = {"plainLyrics": "\n".join(f"ligne {i}" for i in range(10))}
        eng.lyrics._call = lambda *a, **k: entry
        got = eng.sponsorblock.segments_for(vid, ["sponsor"], 200.0)
        assert got
        eng.lyrics.fetch(TrackRef(vid, "Titre", "Artiste", duration=200.0))
        db = eng.library._db
        assert db.execute("SELECT COUNT(*) FROM lyrics").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM segment_fetches").fetchone()[0] == 0
    finally:
        eng.sync.stop()
        eng.library.close()


def test_wipe_user_data_contract_and_extras(engine, monkeypatch, tmp_path):
    import types
    state = _state_home(monkeypatch, tmp_path)
    calls = []
    engine.downloads = types.SimpleNamespace(
        cancel_all=lambda: calls.append("cancel"),
        delete_all_files=lambda: calls.append("delete_files"))
    engine.cookies = types.SimpleNamespace(clear=lambda: None)
    engine.scrobbler._queue_path = state / "scrobble-queue.json"
    engine.scrobbler._enqueue({"listened_at": 1, "track_metadata": {"a": 1}})
    assert engine.scrobbler.pending()
    engine.settings.set("visitor_data", "xyz")
    engine.innertube = types.SimpleNamespace(
        reset_visitor=lambda: calls.append("visitor"))
    wiped = []
    engine.connect("data-wiped", lambda _e: wiped.append(1))
    monkeypatch.setattr("triat.core.logs.truncate",
                        lambda: calls.append("logs"))
    monkeypatch.setattr(engine.library, "delete_corrupt_copies",
                        lambda: calls.append("corrupt") or 0)
    engine.wipe_user_data()
    assert engine.scrobbler.pending() == []
    assert engine.settings.get("visitor_data") in (None, "")
    assert {"cancel", "visitor", "logs", "corrupt"} <= set(calls)
    assert "delete_files" not in calls
    assert wiped == [1]
    calls.clear()
    engine.wipe_user_data(delete_downloads=True)
    assert "delete_files" in calls and "cancel" in calls


def test_shutdown_closes_library_under_drain_and_removes_autosave(
        tmp_path, monkeypatch):
    eng = _real_services_engine(tmp_path)
    order = []
    import contextlib

    @contextlib.contextmanager
    def fake_drain():
        order.append("enter")
        yield
        order.append("exit")

    eng.sync.drain = fake_drain
    real_close = eng.library.close
    eng.library.close = lambda: (order.append("close"), real_close())
    removed = []
    real_remove = GLib.source_remove
    monkeypatch.setattr(GLib, "source_remove",
                        lambda i: (removed.append(i), real_remove(i))[1])
    timer = eng._autosave_id
    assert timer
    eng.shutdown()
    assert order == ["enter", "close", "exit"]
    assert timer in removed


def test_segments_changed_signal_and_accessor(tmp_path):
    seg = Segment(10.0, 20.0, "sponsor", "u")
    eng = Engine(settings=_seg_settings(tmp_path),
                 library=Library(tmp_path / "lib.db"), player=FakePlayer(),
                 extractor=FakeExtractor(),
                 sponsorblock=FakeSponsorBlock([seg]),
                 recommender=FakeRecommender(), ytmusic=object(),
                 cookies=object())
    try:
        seen = []
        eng.connect("segments-changed", lambda _e, segs: seen.append(segs))
        assert eng.current_segments() == []
        eng.play_refs(refs(2))
        assert pump(lambda: seen)
        assert seen[0] == [seg] and eng.current_segments() == [seg]
        eng.stop()
        assert seen[-1] == [] and eng.current_segments() == []
    finally:
        eng.sync.stop()
        eng.library.close()


def _seg_settings(tmp_path):
    s = Settings(tmp_path / "settings.json")
    s.set("autoplay_radio", False)
    s.set("sync_port", 0)
    s.set("sponsorblock_enabled", True)
    s.set("sponsorblock_categories", {"sponsor": SKIP})
    return s


def test_stale_radio_result_is_ignored(tmp_path):
    eng = _real_services_engine(tmp_path)
    try:
        eng.settings.set("autoplay_radio", True)
        eng.play_refs(refs(1))
        assert pump(lambda: eng.player.played)
        pump(timeout=0.2)
        eng._refilling = True
        eng._radio_token += 1
        stale = eng._radio_token
        eng._radio_token += 1               # une autre radio a pris le relais
        before = list(eng.queue.items)
        eng._autoplay_ready(refs(5)[2:], stale, eng._current_ref.video_id, "x")
        assert eng.queue.items == before
        # La piste de départ a changé : résultat jeté aussi.
        eng._autoplay_ready(refs(5)[2:], eng._radio_token, "autre", "x")
        assert eng.queue.items == before
    finally:
        eng.sync.stop()
        eng.library.close()


def test_radio_inputs_are_prepared_on_main_thread_with_reason(tmp_path):
    reco = FakeRecommender(radio=refs(8)[3:])
    reco.last_reason = "Parce que"
    eng = Engine(settings=_seg_settings(tmp_path),
                 library=Library(tmp_path / "lib.db"), player=FakePlayer(),
                 extractor=FakeExtractor(), sponsorblock=FakeSponsorBlock(),
                 recommender=reco, ytmusic=object(), cookies=object())
    try:
        pump(timeout=0.2)               # laisse passer les rappels du démarrage
        eng.settings.set("autoplay_radio", True)
        eng.settings.set("private_session", True)
        eng.queue.set_items(refs(2))
        # set_items lance la résolution de la piste 0, qui réécrit
        # `_current_ref` : on attend qu'elle ait fini avant de poser le
        # point de départ, sinon la radio est jugée périmée (origine changée).
        assert pump(lambda: eng._busy_count == 0 and eng.player.played)
        pump(timeout=0.1)
        eng._current_ref = refs(2)[1]
        eng._refilling = False
        eng._try_autoplay()
        assert pump(lambda: reco.calls)
        call = reco.calls[0]
        assert call["private"] is True
        assert call["exclude"] == [r.video_id for r in refs(2)]
        assert pump(lambda: eng.radio_reason == "Parce que")
    finally:
        eng.sync.stop()
        eng.library.close()


def test_busy_stays_on_until_all_requests_finish(engine):
    seen = []
    engine.connect("busy", lambda _e, b: seen.append(b))
    engine._set_busy(True)
    engine._set_busy(True)
    engine._set_busy(False)
    assert engine._busy is True and seen == [True]
    engine._set_busy(False)
    assert engine._busy is False and seen == [True, False]
    engine._set_busy(False)             # jamais négatif
    assert engine._busy_count == 0


def test_failure_cap_is_ten_even_for_a_long_queue(tmp_path):
    ids = {f"v{i}" for i in range(40)}
    eng = Engine(settings=_seg_settings(tmp_path),
                 library=Library(tmp_path / "lib.db"), player=FakePlayer(),
                 extractor=FakeExtractor(ids), sponsorblock=FakeSponsorBlock(),
                 recommender=FakeRecommender(), ytmusic=object(),
                 cookies=object())
    try:
        errors = []
        eng.connect("error", lambda _e, m: errors.append(m))
        eng.play_refs(refs(40))
        assert pump(lambda: any("interrompue" in m for m in errors), 15.0)
        assert len(eng.extractor.resolved) <= 11
    finally:
        eng.sync.stop()
        eng.library.close()


# ---- SoundCloud (SC-T8) -------------------------------------------------

def _sc_ref(n: int = 1, duration: float = 180.0) -> TrackRef:
    return TrackRef(f"soundcloud:tracks:{n}", f"SC {n}", "Auteur", duration)


def _sc_engine(tmp_path, *, unplayable=None, radio=None, sponsorblock=None):
    from tests.fakes import FakeSoundCloud
    from triat.core.sources import SourceRouter
    settings = _seg_settings(tmp_path)
    settings.set("soundcloud_enabled", True)
    fake = FakeSoundCloud([FakeSoundCloud.make_track(1), FakeSoundCloud.make_track(2),
                           FakeSoundCloud.make_track(3)],
                          unplayable=unplayable)
    router = SourceRouter(FakeExtractor(), fake, settings)
    reco = FakeRecommender(radio=radio or [])
    sb = sponsorblock or FakeSponsorBlock([Segment(1.0, 5.0, "sponsor", "u")])
    eng = Engine(settings=settings, library=Library(tmp_path / "lib.db"),
                 player=FakePlayer(), extractor=router, sponsorblock=sb,
                 recommender=reco, ytmusic=object(), cookies=object())
    return eng, reco, sb


def test_expires_soon_reads_expire_or_expires():
    from triat.core.engine import _expires_soon
    soon = int(time.time()) + 30
    assert _expires_soon(f"https://x.invalide/a?expire={soon}")
    assert _expires_soon(f"https://x.invalide/a?expires={soon}")
    far = int(time.time()) + 36000
    assert not _expires_soon(f"https://x.invalide/a?expires={far}")


def test_soundcloud_track_triggers_no_sponsorblock_call(tmp_path):
    eng, _reco, sb = _sc_engine(tmp_path)
    try:
        eng.play_refs([_sc_ref(1)])
        assert pump(lambda: eng.player.played)
        pump(timeout=0.2)
        assert sb.calls == []
        assert eng.current_segments() == []
    finally:
        eng.sync.stop()
        eng.library.close()


def test_soundcloud_track_has_no_clip(tmp_path):
    eng, _reco, _sb = _sc_engine(tmp_path)
    try:
        eng._load_video(_sc_ref(1), eng._load_token)
        assert eng._video_state == "unavailable"
        assert eng.extractor._youtube.video_heights == []
    finally:
        eng.sync.stop()
        eng.library.close()


def test_backfill_ignores_soundcloud_ids(tmp_path):
    from tests.fakes import FakeCookies, FakeInnerTube
    inner = FakeInnerTube()
    inner.lengths = {}
    eng = Engine(settings=Settings(tmp_path / "s.json"),
                 library=Library(tmp_path / "l.db"), player=FakePlayer(),
                 extractor=FakeExtractor(), sponsorblock=FakeSponsorBlock(),
                 recommender=FakeRecommender(), ytmusic=object(),
                 cookies=FakeCookies(), innertube=inner)
    try:
        eng.library.upsert_many([_sc_ref(7, 0.0)])
        asked = []
        inner.video_length = lambda vid: asked.append(vid)
        eng.backfill_durations_async()
        pump(timeout=0.3)
        assert asked == []
    finally:
        eng.library.close()


def test_no_radio_after_a_soundcloud_track(tmp_path):
    eng, reco, _sb = _sc_engine(tmp_path, radio=refs(5))
    try:
        eng.settings.set("autoplay_radio", True)
        eng.play_refs([_sc_ref(1)])
        assert pump(lambda: eng.player.played)
        eng._try_autoplay()
        eng.start_radio()
        pump(timeout=0.3)
        assert reco.calls == []
    finally:
        eng.sync.stop()
        eng.library.close()


def test_unplayable_soundcloud_track_moves_on_with_a_precise_label(tmp_path):
    eng, _reco, _sb = _sc_engine(tmp_path, unplayable={1: "snip"})
    try:
        errors = []
        eng.connect("error", lambda _e, m: errors.append(m))
        eng.play_refs([_sc_ref(1), _sc_ref(2)])
        assert pump(lambda: eng.player.played)
        assert any("Extrait Go+" in m for m in errors)
        assert eng.current.video_id == "soundcloud:tracks:2"
    finally:
        eng.sync.stop()
        eng.library.close()


def test_default_router_has_soundcloud_off_and_can_be_toggled(tmp_path):
    from triat.core.sources import SourceRouter
    settings = Settings(tmp_path / "s.json")
    settings.set("sync_port", 0)
    eng = Engine(settings=settings, library=Library(tmp_path / "l.db"),
                 player=FakePlayer(), sponsorblock=FakeSponsorBlock(),
                 recommender=FakeRecommender(), ytmusic=object(),
                 cookies=FakeCookies())
    try:
        assert isinstance(eng.extractor, SourceRouter)
        assert eng.soundcloud is None
        eng.set_soundcloud_enabled(True)
        client = eng.soundcloud
        assert client is not None
        eng.set_soundcloud_enabled(True)
        assert eng.soundcloud is client
        assert settings.get("soundcloud_enabled") is True
        eng.set_soundcloud_enabled(False)
        assert eng.soundcloud is None and eng.extractor.soundcloud is None
    finally:
        eng.sync.stop()
        eng.library.close()


# ---- Normalisation par piste et fondu de pause (F03, F04) ---------------

class _LoudTrack(Track):
    """Track avec un loudness annoncé (sous-classe sans slots)."""


class _GainPlayer(FakePlayer):
    def __init__(self) -> None:
        super().__init__()
        self.gains: list[float] = []

    def set_gain(self, db: float) -> None:
        self.gains.append(db)


def _loud(video_id: str, loudness) -> Track:
    track = _LoudTrack(video_id, "T", "A", 200.0, "https://x.invalide/a.webm",
                       "https://x.invalide/a")
    track.loudness_db = loudness
    return track


def test_normalisation_gain_is_clamped_and_defensive():
    from triat.core.engine import normalisation_gain
    assert normalisation_gain(None) == 0.0
    assert normalisation_gain("abc") == 0.0
    assert normalisation_gain(float("nan")) == 0.0
    assert normalisation_gain(0.0) == 0.0
    assert normalisation_gain(3.0) == -3.0       # piste trop forte : on baisse
    assert normalisation_gain(-4.0) == 4.0
    assert normalisation_gain(30.0) == -12.0     # bornes
    assert normalisation_gain(-30.0) == 6.0


def test_track_gain_applied_per_source_and_setting(tmp_path):
    eng = Engine(settings=_seg_settings(tmp_path),
                 library=Library(tmp_path / "lib.db"), player=_GainPlayer(),
                 extractor=FakeExtractor(), sponsorblock=FakeSponsorBlock(),
                 recommender=FakeRecommender(), ytmusic=object(),
                 cookies=object())
    try:
        assert eng.settings.get("normalise_volume") is True
        eng._apply_track_gain(_loud("aaaaaaaaaaa", 5.0))
        eng._apply_track_gain(_loud("soundcloud:tracks:1", 5.0))
        eng._apply_track_gain(Track("bbbbbbbbbbb", "T", "A", 1.0, "u", "w"))
        assert eng.player.gains == [-5.0, 0.0, 0.0]
        eng.player.gains.clear()
        eng.set_normalisation(False)
        eng._apply_track_gain(_loud("aaaaaaaaaaa", 5.0))
        assert eng.player.gains == [0.0]
        assert eng.settings.get("normalise_volume") is False
    finally:
        eng.sync.stop()
        eng.library.close()


def test_gain_is_set_before_the_track_starts(tmp_path):
    class LoudExtractor(FakeExtractor):
        def resolve(self, url):
            base = super().resolve(url)
            track = _loud(base.video_id, 2.0)
            track.title = base.title
            return track

    eng = Engine(settings=_seg_settings(tmp_path),
                 library=Library(tmp_path / "lib.db"), player=_GainPlayer(),
                 extractor=LoudExtractor(), sponsorblock=FakeSponsorBlock(),
                 recommender=FakeRecommender(), ytmusic=object(),
                 cookies=object())
    try:
        eng.play_refs([TrackRef("aaaaaaaaaaa", "T", "A", 200.0)])
        assert pump(lambda: eng.player.played)
        assert eng.player.gains == [-2.0]
    finally:
        eng.sync.stop()
        eng.library.close()


def test_pause_fade_setting_reaches_the_player(engine):
    assert engine.settings.get("pause_fade") is True
    assert engine.player.pause_fade is True
    engine.set_pause_fade(False)
    assert engine.player.pause_fade is False
    assert engine.settings.get("pause_fade") is False

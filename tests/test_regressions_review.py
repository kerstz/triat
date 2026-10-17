"""Régressions relevées par la revue du lot de corrections : chaque test
reproduit un scénario décrit dans la revue (P1-1 … P2-12)."""

from __future__ import annotations

import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from gi.repository import GLib

from triat.core import artwork, sync, sync_state
from triat.core.engine import Engine
from triat.core.library import Library
from triat.core.models import Track, TrackRef
from triat.core.settings import Settings
from tests.fakes import (FakeExtractor, FakePlayer, FakeRecommender,
                         FakeSponsorBlock)


def pump(condition=None, timeout: float = 2.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if condition is not None and condition():
            return True
        time.sleep(0.005)
    return condition() if condition else True


def make_engine(tmp_path, fail=()):
    settings = Settings(tmp_path / "s.json")
    settings.set("autoplay_radio", False)
    settings.set("sync_port", 0)
    eng = Engine(settings=settings, library=Library(tmp_path / "l.db"),
                 player=FakePlayer(), extractor=FakeExtractor(set(fail)),
                 sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
                 ytmusic=object(),
                 cookies=types.SimpleNamespace(clear=lambda: None))
    return eng


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "clear_cache", lambda: None)
    eng = make_engine(tmp_path)
    yield eng
    eng.sync.stop()
    eng.library.close()


A = "aaaaaaaaaaa"
B = "bbbbbbbbbbb"


def ref(video_id: str, duration: float = 200.0) -> TrackRef:
    return TrackRef(video_id, f"T {video_id}", "Artiste", duration)


# ---- P1-1 : Tout effacer et synchro en vol -------------------------------------

def _pair(engine) -> tuple[bytes, bytes]:
    import os
    key = os.urandom(32)
    engine.settings.set("sync_devices", {"dev": {
        "name": "P", "key": sync.b64(key), "paired_at": 0, "last_sync": None}})
    blob = sync.seal(key, {"v": 1, "playlists": [], "likes": [], "history": []},
                     "dev")
    return key, blob


def test_wipe_waits_for_a_running_sync_and_nothing_comes_back(engine, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    engine.library.set_favorite(ref(A), True)
    _key, blob = _pair(engine)
    real_apply = sync_state.apply

    def slow_apply(library, merged):
        time.sleep(0.4)
        return real_apply(library, merged)

    monkeypatch.setattr(sync_state, "apply", slow_apply)
    result = []
    thread = threading.Thread(
        target=lambda: result.append(engine.sync._sync("dev", blob)[0]))
    thread.start()
    time.sleep(0.1)
    engine.wipe_user_data()
    thread.join()
    assert engine.library.list_favorites() == []
    assert engine.sync.devices() == {}
    assert result


def test_sync_rechecks_the_device_inside_the_lock(engine):
    engine.library.set_favorite(ref(A), True)
    _key, blob = _pair(engine)
    result = []
    with engine.sync._sync_lock:
        thread = threading.Thread(
            target=lambda: result.append(engine.sync._sync("dev", blob)[0]))
        thread.start()
        time.sleep(0.1)
        engine.sync.forget_all_devices()
    thread.join()
    assert result == [401]


# ---- P1-2 : pas de boucle infinie sur une piste illisible ----------------------

def test_stream_error_under_repeat_one_stops(engine):
    engine.player.connect("state-changed", lambda p, s: s == "playing" and
                          GLib.idle_add(lambda: (p.emit("error", "boom"), False)[1]))
    errors = []
    engine.connect("error", lambda _e, m: errors.append(m))
    engine.repeat = "one"
    engine.play_refs([ref(A)])
    pump(timeout=1.5)
    assert len(engine.extractor.resolved) <= 6
    assert errors


def test_unresolvable_queue_under_repeat_all_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "clear_cache", lambda: None)
    eng = make_engine(tmp_path, fail={A, B})
    try:
        errors = []
        eng.connect("error", lambda _e, m: errors.append(m))
        eng.repeat = "all"
        eng.play_refs([ref(A), ref(B)])
        pump(timeout=2.6)
        count = len(eng.extractor.resolved)
        assert count == 3
        assert any("interrompue" in m for m in errors)
        pump(timeout=1.0)
        assert len(eng.extractor.resolved) == count
    finally:
        eng.sync.stop()
        eng.library.close()


def test_real_playback_resets_the_failure_counter(engine):
    engine.play_refs([ref(A), ref(B)])
    assert pump(lambda: engine.player.played)
    engine._failures = 2
    engine.player.advance(10.0)
    assert engine._failures == 0


# ---- P2-1 : générations du lecteur ---------------------------------------------

def test_player_drops_events_of_a_replaced_file(monkeypatch):
    from triat.core import player as player_mod
    monkeypatch.setattr(player_mod.mpv, "MPV", MagicMock())
    p = player_mod.Player()
    seen = []
    p.connect("position-changed", lambda _p, v: seen.append(v))
    p.connect("eof", lambda _p: seen.append("eof"))
    p.connect("error", lambda _p, m: seen.append("error"))

    def track(vid):
        return Track(video_id=vid, title="t", artist="a", duration=100.0,
                     stream_url="https://x.invalide/" + vid, webpage_url="")

    p.play_track(track(A))
    stale = p._gen
    p.play_track(track(B))
    assert p._gen == stale + 1
    p._emit_position(150.0, stale)
    p._emit_eof(stale)
    p._emit_error("boum", stale)
    assert seen == []
    assert p.position_hint == 0.0
    p._emit_position(3.0, p._gen)
    assert seen == [3.0] and p.position_hint == 3.0
    p.stop()
    p._emit_eof(p._gen - 1)
    assert "eof" not in seen


# ---- P2-2 / P2-6 : position sauvegardée et reprise ------------------------------

def test_save_state_ignores_the_old_track_position(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "clear_cache", lambda: None)
    eng = make_engine(tmp_path, fail={B})
    try:
        eng.play_refs([ref(A, 300.0), ref(B, 300.0)])
        assert pump(lambda: eng.player.played)
        eng.player.advance(200.0)
        eng.next()                       # B échoue : A joue encore dans mpv
        pump(timeout=0.5)
        eng.save_state()
        assert eng.settings.get("last_index") == 1
        assert eng.settings.get("last_position") == 0.0
    finally:
        eng.sync.stop()
        eng.library.close()


def test_play_after_the_end_does_not_jump_to_the_autosaved_position(engine):
    engine.play_refs([ref(A)])
    assert pump(lambda: engine.player.played)
    engine.player.advance(190.0)
    engine.save_state()
    engine.player.finish()
    pump(timeout=0.1)
    engine.toggle_pause()
    pump(timeout=0.6)
    assert engine.player.seeks == []
    assert len(engine.player.played) == 2


def test_resume_still_seeks_to_the_restored_position(engine):
    engine.settings.update(last_queue=[ref(A).as_dict()], last_index=0,
                           last_position=42.0)
    assert engine.restore_state()
    engine.resume()
    assert pump(lambda: engine.player.played)
    assert pump(lambda: engine.player.seeks == [42.0], timeout=2.0)


# ---- P2-3 / P2-11 : annonce mDNS ------------------------------------------------

def test_restart_during_registration_does_not_leak_zeroconf(tmp_path, monkeypatch):
    import zeroconf
    live = []

    class FakeZC:
        def register_service(self, _service):
            time.sleep(0.4)
            live.append(self)

        def unregister_service(self, _service):
            if self in live:
                live.remove(self)

        def update_service(self, _service):
            pass

        def close(self):
            pass

    monkeypatch.setattr(zeroconf, "Zeroconf", FakeZC)
    monkeypatch.setattr(sync, "_local_ipv4", lambda: ["192.168.1.10"])
    settings = Settings(tmp_path / "s.json")
    settings.set("sync_port", 0)
    server = sync.SyncServer(library=None, settings=settings)
    assert server.start()
    time.sleep(0.05)
    server.stop()
    assert server.start()
    time.sleep(1.2)
    assert len(live) == 1
    server.stop()
    assert pump(lambda: not live, timeout=2.0)


def test_refresh_announcement_updates_outside_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "_local_ipv4", lambda: ["2.2.2.2"])
    settings = Settings(tmp_path / "s.json")
    settings.set("sync_port", 0)
    server = sync.SyncServer(library=None, settings=settings)
    server.port = 4242
    held = []

    class FakeZC:
        def update_service(self, _service):
            held.append(server._lock.locked())

    zc = FakeZC()
    server._zeroconf, server._announced = zc, ["1.1.1.1"]
    assert server.refresh_announcement() is True
    assert held == [False]
    assert server._announced == ["2.2.2.2"]


# ---- P2-4 : instantané reçu mal formé -------------------------------------------

BAD_PAYLOADS = {
    "like track str": {"v": 1, "likes": [
        {"id": A, "at": 1, "liked": True, "track": "x"}]},
    "playlist track int": {"v": 1, "playlists": [
        {"uid": "u", "name": "n", "updated": 5, "tracks": [1]}]},
    "history seconds str": {"v": 1, "history": [
        {"id": A, "at": 1000, "seconds": "abc", "track": {"id": A}}]},
    "duration str": {"v": 1, "history": [
        {"id": A, "at": 1000, "seconds": 1,
         "track": {"id": A, "duration": "abc"}}]},
    "duration nan": {"v": 1, "history": [
        {"id": A, "at": 1000, "seconds": 1,
         "track": {"id": A, "duration": "nan"}}]},
    "name int": {"v": 1, "playlists": [
        {"uid": "u2", "name": 5, "updated": 5, "tracks": []}]},
}


@pytest.mark.parametrize("name", sorted(BAD_PAYLOADS))
def test_malformed_snapshot_never_raises(tmp_path, name):
    lib = Library(tmp_path / "l.db")
    try:
        clean = sync_state.sanitize(BAD_PAYLOADS[name])
        merged = sync_state.merge(sync_state.snapshot(lib), clean)
        sync_state.apply(lib, merged)          # ne lève pas
        assert lib.list_favorites() == []
    finally:
        lib.close()


def test_batch_rolls_back_everything_on_error(tmp_path):
    lib = Library(tmp_path / "l.db")
    try:
        with pytest.raises(RuntimeError):
            with lib.batch():
                lib.set_favorite(ref(A), True)
                lib.create_playlist("P")
                raise RuntimeError("boum")
        assert lib.list_favorites() == []
        assert lib.list_playlists() == []
        with lib.batch():
            lib.set_favorite(ref(A), True)
        assert len(lib.list_favorites()) == 1
    finally:
        lib.close()


# ---- P2-5 : paroles hors ligne --------------------------------------------------

def test_unreachable_service_is_flagged_on_the_lyrics(tmp_path):
    from triat.core import lyrics as lyrics_mod
    lib = Library(tmp_path / "l.db")
    try:
        service = lyrics_mod.LyricsService(lib)
        service._call = lambda path, params: lyrics_mod._UNREACHABLE
        result = service.fetch(TrackRef("abcdefghijk", "Titre", "Artiste", 200.0))
        assert result.unreachable is True
        assert not result.available
    finally:
        lib.close()


# ---- P2-7 : moteur JS -----------------------------------------------------------

def test_node_is_enabled_for_yt_dlp(tmp_path, monkeypatch):
    import yt_dlp
    from triat.core.extractor import BASE_OPTS, Extractor
    assert "node" in BASE_OPTS["js_runtimes"] and "deno" in BASE_OPTS["js_runtimes"]
    options = Extractor()._opts
    assert options["js_runtimes"] is not BASE_OPTS["js_runtimes"]
    # yt-dlp accepte cette forme (ValueError sinon).
    with yt_dlp.YoutubeDL({**options, "quiet": True}) as ydl:
        assert "node" in ydl.params["js_runtimes"]


def test_download_options_enable_node(tmp_path, monkeypatch):
    from triat.core import downloads as dl
    monkeypatch.setattr(dl, "downloads_dir", lambda: tmp_path)
    manager = dl.DownloadManager(settings=Settings(tmp_path / "s.json"))
    task = types.SimpleNamespace(ref=TrackRef(A, "Titre", "Art"))
    assert "node" in manager._options(task)["js_runtimes"]


# ---- P2-8 : collision de titre dans l'index des téléchargements ------------------

def test_same_title_other_video_does_not_steal_the_file(tmp_path, monkeypatch):
    from triat.core import downloads as dl
    monkeypatch.setattr(dl, "downloads_dir", lambda: tmp_path)
    lib = Library(tmp_path / "l.db")
    try:
        manager = dl.DownloadManager(settings=Settings(tmp_path / "s.json"),
                                     library=lib)
        first = TrackRef(A, "Intro", "Artiste")
        second = TrackRef(B, "Intro", "Artiste")
        folder = manager.target_dir(first)
        folder.mkdir(parents=True)
        (folder / "Intro.opus").write_bytes(b"x" * 10)
        assert manager.local_path(first) == folder / "Intro.opus"
        assert lib.download_path(A)
        assert manager.local_path(second) is None
        assert lib.download_path(B) is None
    finally:
        lib.close()


def test_offline_uses_the_index_without_scanning(tmp_path):
    from triat.core.smart import smart_playlists
    lib = Library(tmp_path / "l.db")
    try:
        lib.upsert_many([ref(A), ref(B)])
        lib.record_download(A, "/x/a.opus")
        checked = []

        class Downloads:
            def local_path(self, r):
                checked.append(r.video_id)
                return Path("/x/a.opus")

        def forbidden(*_a, **_k):
            raise AssertionError("balayage de toute la bibliothèque")

        lib.all_tracks = forbidden
        offline = {s.key: s for s in smart_playlists(lib, Downloads())}["offline"]
        assert [r.video_id for r in offline.fetch()] == [A]
        assert checked == [A]
    finally:
        lib.close()


# ---- P2-12 : dossier Musique ----------------------------------------------------

def test_downloads_dir_follows_the_user_music_directory(tmp_path, monkeypatch):
    from gi.repository import GLib as G
    from triat.core import paths
    monkeypatch.delenv("XDG_MUSIC_DIR", raising=False)
    monkeypatch.setattr(G, "get_user_special_dir", lambda _d: str(tmp_path / "Musik"))
    assert paths.downloads_dir() == tmp_path / "Musik" / "Triat"
    monkeypatch.setenv("XDG_MUSIC_DIR", str(tmp_path / "env"))
    assert paths.downloads_dir() == tmp_path / "env" / "Triat"


def test_downloads_dir_falls_back_to_home_music(tmp_path, monkeypatch):
    from gi.repository import GLib as G
    from triat.core import paths
    monkeypatch.delenv("XDG_MUSIC_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(G, "get_user_special_dir", lambda _d: None)
    assert paths.downloads_dir() == tmp_path / "Music" / "Triat"
    monkeypatch.setattr(G, "get_user_special_dir", lambda _d: str(tmp_path))
    assert paths.downloads_dir() == tmp_path / "Music" / "Triat"


def test_flatpak_launcher_has_no_shell_lookup():
    launcher = Path(__file__).resolve().parents[1] / "packaging/flatpak/triat.sh"
    assert "xdg-user-dir" not in launcher.read_text().replace(
        "aucun xdg-user-dir ici", "")

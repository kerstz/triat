"""Durcissements du cœur : cache de pochettes, constantes partagées,
version unique, élagage du cache, file de scrobbles, etc."""

from __future__ import annotations

import os
import time

import pytest


# ---- Pochettes ---------------------------------------------------------

class _Resp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, n=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_artwork_failed_write_leaves_no_part_file(tmp_path, monkeypatch):
    from triat.core import artwork
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Resp(b"\x89PNG\r\n\x1a\n" + b"0" * 20))
    monkeypatch.setattr(artwork, "image_is_sane", lambda _p: True)

    def boom(*_a):
        raise OSError("disque plein")
    monkeypatch.setattr(artwork.os, "replace", boom)
    assert artwork.fetch("https://i.ytimg.com/vi/abc/hq.jpg") is None
    assert list(artwork.covers_dir().glob("*.part")) == []


def test_artwork_cache_hit_refreshes_mtime(tmp_path, monkeypatch):
    from triat.core import artwork
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    url = "https://i.ytimg.com/vi/abc/hq.jpg"
    path = artwork.cache_path(url)
    path.write_bytes(b"x" * 10)
    os.utime(path, (1000, 1000))
    assert artwork.fetch(url) == path
    assert path.stat().st_mtime > 1_000_000


def test_artwork_prune_collects_stale_parts_only(tmp_path, monkeypatch):
    from triat.core import artwork
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    folder = artwork.covers_dir()
    old, fresh = folder / "old.part", folder / "fresh.part"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    stale = time.time() - 2 * artwork.PART_MAX_AGE
    os.utime(old, (stale, stale))
    assert artwork.prune(10**9) == 1
    assert not old.exists() and fresh.exists()


def test_artwork_has_no_cache_size_helper():
    from triat.core import artwork
    assert not hasattr(artwork, "cache_size")


# ---- Trousseau en mémoire ---------------------------------------------

COOKIES = "\n".join([
    "# Netscape HTTP Cookie File",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvaleur",
])


def test_cookie_text_is_read_from_keyring_once(tmp_path, monkeypatch):
    from triat.core import auth
    calls = []
    store = {}
    monkeypatch.setattr(auth.keyring, "get_password",
                        lambda s, k: (calls.append(1), store.get((s, k)))[1])
    monkeypatch.setattr(auth.keyring, "set_password",
                        lambda s, k, v: store.__setitem__((s, k), v))
    monkeypatch.setattr(auth.keyring, "delete_password",
                        lambda s, k: store.pop((s, k), None))
    mgr = auth.CookieManager()
    mgr._runtime_path = tmp_path / "cookies.txt"
    src = tmp_path / "c.txt"
    src.write_text(COOKIES, encoding="utf-8")

    assert not mgr.status().present
    mgr.status(); mgr.status()
    assert len(calls) == 1, "le trousseau n'est lu qu'une fois"
    mgr.import_file(src)                     # _store invalide et réamorce
    n = len(calls)
    assert mgr.status().present
    mgr.cookie_header()
    assert len(calls) == n, "la lecture suivante vient de la mémoire"
    mgr.clear()
    assert not mgr.status().present
    assert len(calls) == n, "clear() remplace le cache sans relire"


def test_scrobbler_enabled_never_touches_keyring_on_caller_thread(tmp_path, monkeypatch):
    import threading
    from triat.core import scrobble
    from triat.core.settings import Settings
    threads = []
    ready = threading.Event()

    def get(_s, _k):
        threads.append(threading.current_thread())
        ready.set()
        return "jeton"
    monkeypatch.setattr(scrobble.keyring, "get_password", get)
    settings = Settings(tmp_path / "s.json")
    settings.set("scrobble_enabled", True)
    sc = scrobble.Scrobbler(settings, queue_path=tmp_path / "q.json")
    assert sc.enabled is False, "pas de jeton en mémoire : False, sans bloquer"
    assert ready.wait(2)
    for _ in range(100):
        if sc.enabled:
            break
        threading.Event().wait(0.01)
    assert sc.enabled
    assert threads and all(t is not threading.main_thread() for t in threads)


# ---- Scrobble : date de début et file de reprise ---------------------

@pytest.fixture
def scrobbler(tmp_path, monkeypatch):
    from triat.core import scrobble
    from triat.core.settings import Settings
    monkeypatch.setattr(scrobble.keyring, "get_password", lambda s, k: "jeton")
    settings = Settings(tmp_path / "s.json")
    settings.set("scrobble_enabled", True)
    sc = scrobble.Scrobbler(settings, queue_path=tmp_path / "queue.json")
    assert sc.token == "jeton"
    return sc


def _ref():
    from triat.core.models import TrackRef
    return TrackRef("a" * 11, "Titre", "Artiste", 200.0)


def test_listened_at_is_the_start_time(scrobbler, monkeypatch):
    sent = []
    monkeypatch.setattr(scrobbler, "_post",
                        lambda body: (sent.append(body), "ok")[1])
    monkeypatch.setattr("threading.Thread", _InlineThread)
    assert scrobbler.submit(_ref(), 150.0, 200.0, started_at=1_700_000_000.0)
    assert sent[0]["payload"][0]["listened_at"] == 1_700_000_000


class _InlineThread:
    def __init__(self, target=None, args=(), daemon=None, **_):
        self._t, self._a = target, args

    def start(self):
        self._t(*self._a)


def test_failed_submit_is_queued_then_flushed_on_success(scrobbler, monkeypatch):
    monkeypatch.setattr("threading.Thread", _InlineThread)
    results = iter(["retry", "ok", "ok"])
    bodies = []

    def post(body):
        bodies.append(body)
        return next(results)
    monkeypatch.setattr(scrobbler, "_post", post)

    scrobbler.submit(_ref(), 150.0, 200.0, started_at=1000.0)
    assert [d["listened_at"] for d in scrobbler.pending()] == [1000]
    mode = scrobbler._path().stat().st_mode & 0o777
    assert mode == 0o600

    scrobbler.submit(_ref(), 150.0, 200.0, started_at=2000.0)
    assert scrobbler.pending() == []
    assert bodies[-1]["listen_type"] == "import"
    assert [d["listened_at"] for d in bodies[-1]["payload"]] == [1000]


def test_permanent_refusal_is_not_queued(scrobbler, monkeypatch):
    monkeypatch.setattr("threading.Thread", _InlineThread)
    monkeypatch.setattr(scrobbler, "_post", lambda body: "drop")
    scrobbler.submit(_ref(), 150.0, 200.0, started_at=1.0)
    assert scrobbler.pending() == []


def test_scrobble_queue_is_bounded(scrobbler):
    from triat.core import scrobble
    for i in range(scrobble.QUEUE_MAX + 25):
        scrobbler._enqueue({"listened_at": i, "track_metadata": {"a": 1}})
    items = scrobbler.pending()
    assert len(items) == scrobble.QUEUE_MAX
    assert items[0]["listened_at"] == 25, "la plus ancienne part la première"


def test_http_error_classification(scrobbler, monkeypatch):
    import urllib.error
    from triat.core import scrobble

    def make(code):
        def fake(*_a, **_k):
            raise urllib.error.HTTPError("u", code, "x", {}, None)
        return fake
    for code, expected in ((503, "retry"), (429, "retry"), (400, "drop"),
                           (401, "drop")):
        monkeypatch.setattr(scrobble, "urlopen_checked", make(code))
        assert scrobbler._post({"payload": []}) == expected


# ---- Bibliothèque : élagage et fermeture ------------------------------

@pytest.fixture
def lib(tmp_path):
    from triat.core.library import Library
    library = Library(tmp_path / "l.db")
    yield library
    library.close()


def test_prune_cache_only_drops_stale_unkept_rows(lib):
    from triat.core.models import Segment, TrackRef
    day = 86400.0
    now = 1_800_000_000.0
    old = now - 100 * day

    def add(vid: str, at: float) -> TrackRef:
        r = TrackRef(vid, f"T {vid}", "A", 100.0)
        lib.upsert_track(r)
        lib._db.execute("UPDATE tracks SET added_at = ? WHERE video_id = ?",
                        (at, vid))
        lib._db.commit()
        return r

    stale = add("stale0000aa", old)
    fresh = add("fresh0000aa", now - day)
    fav = add("fav00000aa", old)
    played = add("played0000a", old)
    in_pl = add("inpl0000aaa", old)
    lib.set_favorite(fav, True)
    lib.record_play(played, 120.0, True)
    lib._db.execute("UPDATE tracks SET added_at = ? WHERE video_id = ?",
                    (old, played.video_id))
    pid = lib.create_playlist("P")
    lib.add_to_playlist(pid, [in_pl])
    lib.record_download(add("dl000000aaa", old).video_id, "/x/y.opus")

    lib.cache_segments(stale.video_id, [Segment(1.0, 5.0, "sponsor", "u1")])
    lib._db.execute("UPDATE segment_fetches SET fetched_at = ?", (old,))
    lib.cache_segments(fresh.video_id, [Segment(1.0, 5.0, "sponsor", "u2")])
    lib._db.execute("UPDATE segment_fetches SET fetched_at = ? "
                    "WHERE video_id = ?", (now - day, fresh.video_id))
    lib.reco_cache_put("home:remote", {"a": 1})
    lib._db.execute("UPDATE reco_cache SET created_at = ?", (old,))
    lib._db.commit()

    removed = lib.prune_cache(older_than=90 * day, now=now)
    assert removed["tracks"] == 1
    assert lib.get_track(stale.video_id) is None
    for kept in (fresh, fav, played, in_pl):
        assert lib.get_track(kept.video_id) is not None
    assert lib.get_track("dl000000aaa") is not None
    assert removed["segment_fetches"] == 1 and removed["segments"] == 1
    assert lib.cached_segments(fresh.video_id) is not None
    assert removed["reco_cache"] == 1
    assert lib.search("stale") == [], "l'index plein texte suit"


def test_prune_cache_drops_old_lyrics(lib):
    from triat.core.lyrics import LyricsService
    LyricsService(lib)._store("v" * 11, "", "texte", False, False)
    lib._db.execute("UPDATE lyrics SET fetched_at = 1")
    lib._db.commit()
    assert lib.prune_cache(older_than=86400)["lyrics"] == 1


def test_closed_library_is_inert_not_raising(lib):
    from triat.core.models import TrackRef
    r = TrackRef("c" * 11, "T", "A", 10.0)
    lib.upsert_track(r)
    lib.close()
    lib.close()                                  # idempotent
    assert lib.closed
    # Appels typiques de fils en retard : aucune exception.
    lib.upsert_track(r)
    lib.upsert_many([r])
    lib.set_duration(r.video_id, 5.0)
    lib.record_play(r, 60.0, True)
    assert lib.get_track(r.video_id) is None
    assert lib.search("T") == []
    assert lib.missing_duration() == []
    assert lib.count_owned() == 0
    assert lib.cached_segments(r.video_id) is None
    lib.cache_segments(r.video_id, [])
    lib.prune_cache()


def test_late_worker_thread_after_close_raises_nothing(lib):
    import threading
    errors = []

    def worker():
        try:
            for _ in range(50):
                lib.missing_duration()
                lib.stats()
        except Exception as exc:                 # pragma: no cover
            errors.append(exc)
    lib.close()
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert errors == []

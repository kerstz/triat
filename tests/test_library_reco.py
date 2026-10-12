"""SQL des recommandations : schéma v4, signaux, blocklist, caches."""

from __future__ import annotations

import sqlite3

import pytest

from triat.core import library as library_module
from triat.core.library import SCHEMA, Library, _decay
from triat.core.models import Segment, TrackRef
from triat.core.reco import DAY, HALF_LIFE_PLAYS, SKIP_HALF_LIFE, artist_key

NOW = 1_790_000_000.0


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path / "l.db")
    yield library
    library.close()


def ref(vid: str, artist: str = "Art", duration: float = 200.0) -> TrackRef:
    return TrackRef(vid, f"Titre {vid}", artist, duration)


def hist(lib: Library, r: TrackRef, at: float, seconds: float) -> None:
    lib.add_history_entry(r, at, seconds)


def signals(lib: Library) -> dict:
    return {s.ref.video_id: s for s in
            lib.track_signals(NOW, HALF_LIFE_PLAYS, SKIP_HALF_LIFE)}


# ---- Schéma ----------------------------------------------------------------

def test_schema_v4_tables_and_indexes(lib):
    indexes = {r["name"] for r in lib._db.execute("PRAGMA index_list(playlist_items)")}
    assert "idx_playlist_items_video" in indexes
    tables = {r["name"] for r in lib._db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"reco_skips", "reco_blocklist", "reco_cache"} <= tables
    assert lib._db.execute("SELECT version FROM schema_version").fetchone()[0] == 4
    assert library_module.SCHEMA_VERSION == 4


@pytest.mark.parametrize("version", [2, 3])
def test_existing_db_is_upgraded_in_place(tmp_path, version):
    """Une base écrite par le schéma précédent s'ouvre, garde ses données
    et gagne les index et les tables v4."""
    old_schema = SCHEMA.split("-- v4 :")[0]
    if version == 2:                       # avant les téléchargements par id
        start = old_schema.index("-- Fichiers téléchargés")
        end = old_schema.index("CREATE TABLE IF NOT EXISTS history")
        old_schema = old_schema[:start] + old_schema[end:]
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(old_schema)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.execute("INSERT INTO schema_version VALUES (?)", (version,))
    db.execute("INSERT INTO tracks (video_id, title, artist, added_at, play_count)"
               " VALUES ('old1', 'Ancien', 'Vieux', 1.0, 3)")
    db.execute("INSERT INTO playlists (name, created_at, updated_at)"
               " VALUES ('P', 1, 1)")
    db.execute("INSERT INTO playlist_items VALUES (1, 0, 'old1')")
    db.commit()
    db.close()

    lib = Library(path)
    try:
        assert lib._db.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        assert lib.get_track("old1").title == "Ancien"
        assert lib.playlist_covers()[0]["first_id"] == "old1"
        names = {r["name"] for r in lib._db.execute(
            "SELECT name FROM sqlite_master")}
        assert {"idx_playlist_items_video", "reco_skips", "reco_blocklist",
                "reco_cache", "idx_tracks_artist_key"} <= names
        assert [s.ref.video_id for s in lib.track_signals(NOW, 1, 1)] == ["old1"]
    finally:
        lib.close()


def test_owned_queries_use_the_video_index(lib):
    sql = "SELECT * FROM tracks WHERE " + library_module._OWNED.format(t="tracks")
    plan = " ".join(r["detail"] for r in
                    lib._db.execute("EXPLAIN QUERY PLAN " + sql))
    assert "idx_playlist_items_video" in plan


def test_decay_halves_every_half_life():
    assert _decay(0, 10) == 1.0
    assert abs(_decay(10, 10) - 0.5) < 1e-12
    assert _decay(-5, 10) == 1.0           # un événement « futur » ne dépasse pas 1


# ---- Fuite du cache, couvertures, co-écoute ------------------------------------

def test_tracks_by_artist_owned_excludes_cache(lib):
    owned = [ref(f"own{i}", "Vialice") for i in range(5)]
    lib.add_to_playlist(lib.create_playlist("P"), owned)
    lib.upsert_many([ref(f"cac{i}", "Vialice") for i in range(20)])
    assert len(lib.tracks_by_artist("Vialice")) == 25
    got = lib.tracks_by_artist("Vialice", owned=True)
    assert len(got) == 5 and all(r.video_id.startswith("own") for r in got)


def test_playlist_covers_single_query(lib):
    first = lib.create_playlist("Pleine")
    lib.create_playlist("Vide")
    lib.add_to_playlist(first, [TrackRef("a", "A", "X", 10, "http://c/a.jpg"),
                                ref("b")])
    covers = {c["name"]: c for c in lib.playlist_covers()}
    assert covers["Pleine"]["first_id"] == "a"
    assert covers["Pleine"]["item_count"] == 2
    assert covers["Pleine"]["first_thumbnail"] == "http://c/a.jpg"
    assert covers["Vide"]["first_id"] is None and covers["Vide"]["item_count"] == 0


def test_co_played_range_query_matches_and_normalises(lib):
    seed, near, far = ref("seed"), ref("near"), ref("far")
    common, specific = ref("common"), ref("specific")
    base = NOW - 4 * DAY
    for k in range(3):
        at = base + k * 10 * 3600
        hist(lib, seed, at, 200)
        hist(lib, near, at + 600, 200)             # +10 min : dans la fenêtre
        hist(lib, far, at + 3 * 3600, 200)         # +3 h : hors fenêtre
        hist(lib, common, at + 120, 200)
        hist(lib, specific, at + 240, 200)
    # `common` est un titre en boucle : 47 autres écoutes, loin de `seed`.
    for k in range(47):
        hist(lib, common, base - 30 * DAY - k * 7200, 200)
    ids = [r.video_id for r in lib.co_played("seed", now=NOW)]
    assert "far" not in ids and {"near", "common", "specific"} <= set(ids)
    assert ids.index("specific") < ids.index("common")


# ---- Signaux --------------------------------------------------------------------

def test_track_signals_decay_and_completion(lib):
    recent, old = ref("recent"), ref("old")
    hist(lib, recent, NOW - DAY, 200)
    hist(lib, old, NOW - 90 * DAY, 200)
    lib.upsert_track(ref("cacheonly"))
    s = signals(lib)
    assert "cacheonly" not in s
    assert s["recent"].affinity > s["old"].affinity > 0
    # Une écoute de 20 s sur 200 s pèse 0,2 d'une écoute complète.
    brief, full = ref("brief"), ref("full")
    hist(lib, brief, NOW - DAY, 20)
    hist(lib, full, NOW - DAY, 200)
    s = signals(lib)
    assert abs(s["brief"].affinity / s["full"].affinity - 0.2) < 1e-6


def test_track_signals_ignores_completed_flag_for_synced_entries(lib):
    r = ref("synced")
    hist(lib, r, NOW - 10, 190)      # synchronisée : completed = 0, 95 %
    got = signals(lib)["synced"]
    assert abs(got.affinity - _decay(10, HALF_LIFE_PLAYS)) < 1e-9
    assert got.plays == 1 and got.last_at == NOW - 10


def test_track_signals_flags_and_future_plays(lib):
    fav, listed = ref("fav"), ref("listed")
    lib.set_favorite(fav, True)
    lib.add_to_playlist(lib.create_playlist("P"), [listed])
    hist(lib, ref("future"), NOW + DAY, 200)      # après `now` : ignorée
    s = signals(lib)
    assert s["fav"].favorite and not s["fav"].listed
    assert s["listed"].listed and s["listed"].plays == 0
    assert s["future"].plays == 0 and s["future"].affinity == 0


def test_record_skip_and_skip_weights(lib):
    r = ref("sk")
    lib.record_skip(r, 4.0, "radio", at=NOW)
    lib.record_skip(r, 6.0, "", at=NOW - 14 * DAY)
    weights = lib.skip_weights(["sk", "autre"], NOW, 14 * DAY)
    assert abs(weights["sk"] - 1.5) < 1e-9 and "autre" not in weights
    assert signals(lib) == {}            # un saut seul ne rend pas « à soi »
    lib.set_favorite(r, True)
    assert abs(signals(lib)["sk"].skip_weight - 1.5) < 1e-6


def test_recent_ids_and_offtopic_seconds(lib):
    hist(lib, ref("r1"), NOW - 100, 200)
    hist(lib, ref("r2"), NOW - 10 * DAY, 200)
    assert lib.recent_ids(NOW - DAY) == {"r1"}
    lib.cache_segments("r1", [Segment(0, 150, "music_offtopic"),
                              Segment(160, 170, "sponsor")])
    assert lib.offtopic_seconds(["r1", "r2"]) == {"r1": 150.0}


# ---- Blocklist, caches, effacement ---------------------------------------------

def test_blocklist_roundtrip_and_artist_key(lib):
    lib.block("artist", artist_key("Daft Punk - Topic"), "Daft Punk")
    lib.block("track", "abc", "Un titre")
    blocked = lib.blocked()
    assert blocked["artist"] == {"daft punk": "Daft Punk"}
    assert blocked["track"] == {"abc": "Un titre"}
    lib.unblock("artist", "Daft Punk")
    lib.unblock("track", "abc")
    assert lib.blocked() == {"track": {}, "artist": {}}
    with pytest.raises(ValueError):
        lib.block("album", "x")


def test_blocked_tracks_and_artists_leave_the_signals(lib):
    lib.add_to_playlist(lib.create_playlist("P"),
                        [ref("a", "Daft Punk - Topic"), ref("b", "Autre"),
                         ref("c", "Autre")])
    lib.block("artist", "Daft Punk")
    lib.block("track", "c")
    assert set(signals(lib)) == {"b"}


def test_clear_history_and_wipe_clear_reco_tables(lib):
    r = ref("a")
    hist(lib, r, NOW - 5, 200)
    lib.record_skip(r, 3.0)
    lib.reco_cache_put("home:remote", {"x": 1})
    lib.block("track", "a", "A")
    lib.clear_history()
    count = lambda t: lib._db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
    assert count("reco_skips") == 0 and count("reco_cache") == 0
    assert count("reco_blocklist") == 1
    lib.record_skip(r, 3.0)
    lib.reco_cache_put("k", {})
    lib.wipe()
    assert count("reco_skips") == count("reco_cache") == count("reco_blocklist") == 0


def test_reco_cache_put_get_clear_prefix(lib):
    lib.reco_cache_put("mixes:2026-01-01", {"a": 1}, at=10.0)
    lib.reco_cache_put("mixes:2026-01-09", {"a": 2}, at=20.0)
    lib.reco_cache_put("home:remote", {"b": 3}, at=30.0)
    assert lib.reco_cache_get("home:remote") == ({"b": 3}, 30.0)
    assert lib.reco_cache_get("absent") is None
    lib.reco_cache_clear("mixes:", before="mixes:2026-01-05")
    assert lib.reco_cache_get("mixes:2026-01-01") is None
    assert lib.reco_cache_get("mixes:2026-01-09") is not None
    lib.reco_cache_clear("mixes:")
    assert lib.reco_cache_get("mixes:2026-01-09") is None
    assert lib.reco_cache_get("home:remote") is not None
    lib.reco_cache_clear()
    assert lib.reco_cache_get("home:remote") is None


def test_count_owned_matches_owned_tracks(lib):
    lib.upsert_many([ref("cache1"), ref("cache2")])
    lib.add_to_playlist(lib.create_playlist("P"), [ref("p1")])
    lib.set_favorite(ref("f1"), True)
    hist(lib, ref("h1"), NOW - 5, 100)
    assert lib.count_owned() == 3

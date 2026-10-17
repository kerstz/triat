"""Mixes de l'accueil : locaux, stables sur la journée, variés."""

from __future__ import annotations

import datetime as dt
import threading

import pytest

from triat.core.library import Library
from triat.core.mixes import MIX_SIZE, MixStore, build_mixes
from triat.core.models import TrackRef


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "l.db")
    refs = []
    for artist, n in (("Vialice", 10), ("LEDOUBLE", 6), ("Solo", 1)):
        refs += [TrackRef(f"{artist[:3]}{i:03d}", f"{artist} {i}", artist, 180.0)
                 for i in range(n)]
    refs += [TrackRef(f"misc{i:03d}", f"Divers {i}", f"Autre {i}", 200.0)
             for i in range(40)]
    lib.upsert_many(refs)
    # Une playlist qui intercale Vialice et des inconnus.
    pid = lib.create_playlist("Mix 217")
    lib.add_to_playlist(pid, [r for pair in zip(refs[:10], refs[20:30])
                              for r in pair])
    # Les « Divers » sont dans la bibliothèque (playlist), pas seulement
    # dans le cache de recherche.
    divers = lib.create_playlist("Divers")
    lib.add_to_playlist(divers, [r for r in refs if r.video_id.startswith("misc")])
    yield lib
    lib.close()


def test_empty_library_has_no_mix(tmp_path):
    lib = Library(tmp_path / "vide.db")
    assert build_mixes(lib) == []
    lib.close()


def test_daily_mix_is_stable_within_a_day(library):
    day = dt.date(2026, 9, 23)
    a = build_mixes(library, day)[0]
    b = build_mixes(library, day)[0]
    other = build_mixes(library, dt.date(2026, 9, 24))[0]
    assert a.title == "Mix du jour"
    assert [r.video_id for r in a.refs] == [r.video_id for r in b.refs]
    assert [r.video_id for r in a.refs] != [r.video_id for r in other.refs]
    assert len(a.refs) == MIX_SIZE
    assert len({r.video_id for r in a.refs}) == MIX_SIZE


def test_artist_mix_mixes_artist_and_playlist_neighbours(library):
    mixes = {m.title: m for m in build_mixes(library, dt.date(2026, 9, 23))}
    vialice = mixes["Mix Vialice"]
    artists = [r.artist for r in vialice.refs]
    assert artists[0] == "Vialice"
    assert "Vialice" in artists and any(a.startswith("Autre") for a in artists)
    assert "Mix Solo" not in mixes          # une seule piste : pas de mix


def test_subtitle_names_the_first_artists():
    from triat.core.mixes import Mix
    mix = Mix("k", "t", [TrackRef("a", "x", "A"), TrackRef("b", "y", "B"),
                         TrackRef("c", "z", "A"), TrackRef("d", "w", "C")])
    assert mix.subtitle == "4 titres, avec A, B et C"


def test_playlist_neighbours(library):
    near = library.playlist_neighbours(["Via000"], radius=1)
    assert [r.video_id for r in near] == ["misc003"]   # refs[20]


def test_daily_mix_ignores_the_search_cache(tmp_path):
    lib = Library(tmp_path / "c.db")
    lib.upsert_many([TrackRef(f"cache{i:03d}", f"Cache {i}", "X", 100.0)
                     for i in range(30)])           # résultats de recherche
    lib.set_favorite(TrackRef("fav0000000a", "Aimé", "Y", 100.0), True)
    mix = build_mixes(lib, dt.date(2026, 9, 23))[0]
    assert [r.video_id for r in mix.refs] == ["fav0000000a"]
    lib.close()


# ---- Mixes v2 : signaux, diversité, cache figé ---------------------------

NOW = 1_790_000_000.0
DAY_S = 86400.0
TODAY = dt.date(2026, 9, 23)


def _owned_lib(tmp_path, refs, name="m.db"):
    lib = Library(tmp_path / name)
    lib.add_to_playlist(lib.create_playlist("Tout"), refs)
    return lib


def _tracks(prefix: str, n: int, artist=None) -> list[TrackRef]:
    return [TrackRef(f"{prefix}{i:04d}", f"{prefix} {i}",
                     artist or f"{prefix.upper()}{i}", 200.0) for i in range(n)]


def _daily(lib, **kw):
    return build_mixes(lib, TODAY, now=NOW, **kw)[0]


def test_artist_mix_ignores_search_cache(tmp_path):
    lib = Library(tmp_path / "c.db")
    owned = _tracks("own", 5, "Vialice")
    others = _tracks("oth", 5)
    lib.add_to_playlist(lib.create_playlist("P"),
                        [r for pair in zip(owned, others, strict=True)
                         for r in pair])
    lib.upsert_many([TrackRef(f"cac{i:03d}", f"Cache {i}", "Vialice", 100.0)
                     for i in range(20)])
    mixes = {m.title: m for m in build_mixes(lib, TODAY, now=NOW)}
    ids = [r.video_id for r in mixes["Mix Vialice"].refs]
    assert len(ids) >= 8 and not any(i.startswith("cac") for i in ids)
    lib.close()


def test_daily_mix_skips_tracks_played_in_the_last_hours(tmp_path):
    refs = _tracks("t", 60)
    lib = _owned_lib(tmp_path, refs)
    for r in refs[:10]:
        lib.add_history_entry(r, NOW - 3600, 200)
    for r in refs[10:20]:
        lib.add_history_entry(r, NOW - 10 * DAY_S, 200)
    ids = {r.video_id for r in _daily(lib).refs}
    assert len(ids & {r.video_id for r in refs[:10]}) <= 3
    lib.close()


def test_daily_mix_penalises_skipped_tracks(tmp_path):
    skipped, loved = _tracks("s", 10), _tracks("l", 10)
    lib = _owned_lib(tmp_path, _tracks("f", 40))
    for r in skipped + loved:
        lib.set_favorite(r, True, at=NOW - 20 * DAY_S)
    for r in skipped:
        for _ in range(3):
            lib.record_skip(r, 5.0, at=NOW)
    ids = {r.video_id for r in _daily(lib).refs}
    assert len(ids & {r.video_id for r in skipped}) <= 1
    assert len(ids & {r.video_id for r in loved}) >= 4
    lib.close()


def test_daily_mix_artist_cap_and_spacing(tmp_path):
    prolific = _tracks("p", 40, "Prolifique")
    lib = _owned_lib(tmp_path, prolific + _tracks("o", 30))
    refs = _daily(lib).refs
    names = [r.artist for r in refs]
    assert names.count("Prolifique") <= 3
    assert all(not (a == b == "Prolifique")
               for a, b in zip(names, names[1:], strict=False))
    lib.close()


def test_daily_mix_overlap_with_yesterday_is_limited(tmp_path):
    refs = _tracks("t", 300)
    lib = _owned_lib(tmp_path, refs)
    for r in refs[:50]:
        lib.set_favorite(r, True, at=NOW - 20 * DAY_S)
    d1 = _daily(lib)
    d2 = build_mixes(
        lib, TODAY + dt.timedelta(days=1), now=NOW + DAY_S,
        previous=[{"daily": [r.video_id for r in d1.refs], "artists": []}])[0]
    overlap = {r.video_id for r in d1.refs} & {r.video_id for r in d2.refs}
    assert len(overlap) <= 12
    lib.close()


def test_daily_pool_covers_whole_library(tmp_path):
    refs = _tracks("t", 3000)
    lib = _owned_lib(tmp_path, refs)
    # added_at croissant avec l'indice : les 1000 premières sont hors des
    # 2000 plus récentes, inatteignables avec l'ancien plafond.
    lib._db.execute("UPDATE tracks SET added_at = CAST(substr(video_id, 2) AS INTEGER)")
    lib._db.commit()
    seen = set()
    for offset in range(10):
        seen |= {r.video_id for r in build_mixes(
            lib, TODAY + dt.timedelta(days=offset), now=NOW)[0].refs}
    assert any(int(v[1:]) < 1000 for v in seen)
    lib.close()


def test_mixes_do_not_duplicate_each_other(tmp_path):
    lib = _owned_lib(tmp_path, _tracks("t", 80))
    mixes = {m.key: m for m in build_mixes(lib, TODAY, now=NOW)}
    daily = {r.video_id for r in mixes["daily"].refs}
    fresh = {r.video_id for r in mixes["fresh"].refs}
    assert fresh and not daily & fresh
    lib.close()


def test_artist_groups_merge_topic_suffix(tmp_path):
    plain = _tracks("a", 4, "Daft Punk")
    topic = _tracks("b", 4, "Daft Punk - Topic")
    lib = _owned_lib(tmp_path, plain + topic + _tracks("o", 10))
    keys = [m.key for m in build_mixes(lib, TODAY, now=NOW)
            if m.key.startswith("artist:")]
    assert keys == ["artist:daft punk"]
    lib.close()


def test_blocked_track_and_artist_absent_from_all_mixes(tmp_path):
    refs = _tracks("a", 12, "Vialice") + _tracks("t", 40)
    lib = _owned_lib(tmp_path, refs)
    before = build_mixes(lib, TODAY, now=NOW)
    victim = next(r for m in before if m.key == "fresh" for r in m.refs
                  if r.artist != "Vialice")
    lib.block("track", victim.video_id, victim.title)
    lib.block("artist", "Vialice", "Vialice")
    after = build_mixes(lib, TODAY, now=NOW)
    for mix in after:
        assert all(r.video_id != victim.video_id and r.artist != "Vialice"
                   for r in mix.refs)
    assert "Mix Vialice" not in {m.title for m in after}
    # Les mixes figés sont filtrés à l'affichage, sans reconstruction.
    from triat.core.mixes import filter_blocked
    shown = filter_blocked(before, lib.blocked())
    assert all(r.artist != "Vialice" for m in shown for r in m.refs)
    lib.close()


def test_mix_store_frozen_for_the_day_and_invalidated_by_import(tmp_path):
    from triat.core.mixes import MixStore
    refs = _tracks("t", 60)
    lib = _owned_lib(tmp_path, refs)
    store = MixStore(lib)
    first = store.get_or_build(TODAY, NOW)
    ids = [r.video_id for r in first[0].refs]
    lib.add_history_entry(refs[0], NOW - 10, 200)
    lib.set_favorite(refs[1], True)
    assert [r.video_id for r in store.get_or_build(TODAY, NOW)[0].refs] == ids
    # Gros import : le cache du jour est périmé.
    lib.add_to_playlist(lib.create_playlist("Import"), _tracks("n", 100))
    assert store.load(TODAY) is None
    rebuilt = store.get_or_build(TODAY, NOW)
    assert {r.video_id for m in rebuilt for r in m.refs} - set(ids)
    # Un autre jour reconstruit, et retient hier.
    tomorrow = TODAY + dt.timedelta(days=1)
    assert store.load(tomorrow) is None
    store.get_or_build(tomorrow, NOW + DAY_S)
    prev = store.previous(tomorrow)
    assert len(prev) == 1 and prev[0]["daily"]
    # Les entrées trop anciennes sont élaguées.
    far = TODAY + dt.timedelta(days=20)
    store.get_or_build(far, NOW + 20 * DAY_S)
    assert lib.reco_cache_get(f"mixes:{TODAY.isoformat()}") is None
    lib.close()


def test_mix_store_rejects_empty_and_survives_roundtrip(tmp_path):
    from triat.core.mixes import MixStore
    lib = Library(tmp_path / "e.db")
    store = MixStore(lib)
    assert store.get_or_build(TODAY, NOW) == []
    assert store.load(TODAY) is None
    lib.close()


def test_mix_reason_is_set(tmp_path):
    lib = _owned_lib(tmp_path, _tracks("a", 10, "Vialice") + _tracks("t", 60))
    mixes = {m.key: m for m in build_mixes(lib, TODAY, now=NOW)}
    assert mixes["daily"].reason and mixes["fresh"].reason
    assert "Vialice" in mixes["artist:vialice"].reason
    lib.close()


class _GatedLibrary:
    """Délègue à la vraie bibliothèque ; `blocked()` attend un signal pour
    simuler une construction longue, et expose `epoch`."""

    def __init__(self, lib):
        self._lib = lib
        self.epoch = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def blocked(self):
        self.started.set()
        assert self.release.wait(5)
        return self._lib.blocked()

    def __getattr__(self, name):
        return getattr(self._lib, name)


def _build_in_thread(store, gated, interfere, day):
    out = []
    thread = threading.Thread(target=lambda: out.append(store.get_or_build(day)))
    thread.start()
    assert gated.started.wait(5)
    interfere()
    gated.release.set()
    thread.join(5)
    return out[0]


def test_get_or_build_in_flight_during_reset_does_not_write(library):
    gated = _GatedLibrary(library)
    store = MixStore(gated)
    day = dt.date(2026, 9, 23)
    result = _build_in_thread(store, gated, store.reset, day)
    assert result == []
    assert library.reco_cache_get("mixes:" + day.isoformat()) is None


def test_get_or_build_in_flight_during_wipe_does_not_write(library):
    gated = _GatedLibrary(library)
    store = MixStore(gated)
    day = dt.date(2026, 9, 23)

    def wipe():
        gated.epoch += 1

    assert _build_in_thread(store, gated, wipe, day) == []
    assert library.reco_cache_get("mixes:" + day.isoformat()) is None


def test_get_or_build_without_interference_writes(library):
    gated = _GatedLibrary(library)
    store = MixStore(gated)
    day = dt.date(2026, 9, 23)
    result = _build_in_thread(store, gated, lambda: None, day)
    assert result and library.reco_cache_get("mixes:" + day.isoformat())

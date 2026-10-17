"""Fusion des instantanés de synchro (core/sync_state.py)."""

from __future__ import annotations

import time

import pytest

from triat.core import sync_state as ss
from triat.core.library import Library
from triat.core.models import TrackRef


def ref(i: int) -> TrackRef:
    return TrackRef(f"{i:011d}", f"T{i}", f"A{i}", 100.0 + i)


def sync(a: Library, b: Library) -> dict:
    merged = ss.merge(ss.snapshot(a), ss.snapshot(b))
    ss.apply(a, merged)
    ss.apply(b, merged)
    return merged


def view(lib: Library) -> dict:
    playlists = {p["uid"]: (p["name"], [r.video_id for r in lib.playlist_items(p["id"])])
                 for p in lib.list_playlists()}
    likes = sorted(r.video_id for r in lib.list_favorites())
    plays = sorted((r.video_id, round(at)) for r, at, _s in lib.history_entries())
    return {"playlists": playlists, "likes": likes, "plays": plays}


def test_two_libraries_converge(tmp_path):
    a, b = Library(tmp_path / "a.db"), Library(tmp_path / "b.db")
    pid = a.create_playlist("Nuit")
    a.add_to_playlist(pid, [ref(1), ref(2)])
    a.set_favorite(ref(1), True)
    a.record_play(ref(1), 90, True)
    b.create_playlist("Route")
    b.set_favorite(ref(3), True)
    b.record_play(ref(3), 60, True)

    sync(a, b)
    assert view(a) == view(b)
    assert {name for name, _ in view(a)["playlists"].values()} == {"Nuit", "Route"}
    assert view(a)["likes"] == [ref(1).video_id, ref(3).video_id]
    assert len(view(a)["plays"]) == 2
    a.close(); b.close()


def test_deletion_and_unlike_propagate(tmp_path):
    a, b = Library(tmp_path / "a.db"), Library(tmp_path / "b.db")
    pid = a.create_playlist("Temporaire")
    a.add_to_playlist(pid, [ref(1)])
    a.set_favorite(ref(1), True)
    sync(a, b)
    assert len(b.list_playlists()) == 1 and b.list_favorites()

    time.sleep(0.01)
    b.delete_playlist(b.list_playlists()[0]["id"])
    b.set_favorite(ref(1), False)
    sync(a, b)
    # Sans trace de suppression, la playlist serait revenue depuis `a`.
    assert a.list_playlists() == [] and b.list_playlists() == []
    assert a.list_favorites() == [] and b.list_favorites() == []
    a.close(); b.close()


def test_newest_edit_wins(tmp_path):
    a, b = Library(tmp_path / "a.db"), Library(tmp_path / "b.db")
    pid = a.create_playlist("Mix")
    a.add_to_playlist(pid, [ref(1), ref(2)])
    sync(a, b)
    time.sleep(0.01)
    other = b.list_playlists()[0]["id"]
    b.set_playlist_order(other, [ref(2).video_id, ref(1).video_id])
    b.rename_playlist(other, "Mix réordonné")
    sync(a, b)
    name, order = next(iter(view(a)["playlists"].values()))
    assert name == "Mix réordonné" and order == [ref(2).video_id, ref(1).video_id]
    a.close(); b.close()


def test_merge_is_commutative_and_idempotent(tmp_path):
    a, b = Library(tmp_path / "a.db"), Library(tmp_path / "b.db")
    a.add_to_playlist(a.create_playlist("X"), [ref(1)])
    b.set_favorite(ref(2), True)
    sa, sb = ss.snapshot(a), ss.snapshot(b)
    assert ss.merge(sa, sb) == ss.merge(sb, sa)
    sync(a, b)
    before = view(a)
    changes = ss.apply(a, ss.merge(ss.snapshot(a), ss.snapshot(b)))
    assert changes == {"playlists": 0, "likes": 0, "history": 0}
    assert view(a) == before
    a.close(); b.close()


def test_invalid_tracks_are_ignored():
    assert ss.track_from_wire({"id": "local:/sdcard/x.mp3"}) is None
    assert ss.track_from_wire({"id": 12}) is None
    assert ss.track_from_wire({"id": "a" * 11}).title == ""
    for bad in ("soundcloud:t", "sc:12345678", "abc def ghi", "abcdefghij!"):
        assert ss.track_from_wire({"id": bad}) is None
    assert ss.track_from_wire({"id": "A-b_c9XyZ01"}) is not None


def test_tombstone_keeps_author_date(tmp_path):
    lib = Library(tmp_path / "a.db")
    pid = lib.create_playlist("Nuit", uid="u1")
    now = time.time()
    lib.set_playlist_updated(pid, now - 7200)
    ss.apply(lib, {"playlists": [{"uid": "u1", "name": "", "updated": int((now - 3600) * 1000),
                                  "deleted": True, "tracks": []}]})
    assert lib.tombstones("playlist")["u1"] == pytest.approx(now - 3600, abs=0.01)
    lib.close()


def test_newer_edit_beats_older_delete_across_three_devices(tmp_path):
    a, b, c = (Library(tmp_path / f"{n}.db") for n in "abc")
    a.create_playlist("Nuit", uid="u1")
    sync(a, b); sync(a, c)
    t = time.time()
    b.delete_playlist(b.list_playlists()[0]["id"], at=t + 10)       # t1
    sync(a, b)                                                     # a apprend t1
    cid = c.list_playlists()[0]["id"]
    c.rename_playlist(cid, "Nuit 2")
    c.set_playlist_updated(cid, t + 20)                             # t2 > t1
    sync(a, c)
    assert [p["name"] for p in a.list_playlists()] == ["Nuit 2"]
    for lib in (a, b, c):
        lib.close()


def test_empty_title_does_not_overwrite_known_title(tmp_path):
    lib = Library(tmp_path / "a.db")
    lib.upsert_track(ref(1))
    ss.apply(lib, {"history": [{"id": ref(1).video_id, "at": 1_700_000_000_000,
                                "seconds": 5, "track": {"id": ref(1).video_id,
                                                        "title": "", "artist": ""}}]})
    assert lib.get_track(ref(1).video_id).title == "T1"
    lib.close()


def test_sanitize_rejects_bad_shape_and_drops_bad_items():
    assert ss.sanitize({"v": 1, "playlists": "oops"}) is None
    clean = ss.sanitize({"v": 1, "playlists": [1, {"uid": "x", "updated": "zz"}, {"uid": 3}],
                         "likes": [{"id": "a", "at": "7"}], "history": None or []})
    assert [p["uid"] for p in clean["playlists"]] == ["x"]
    assert clean["playlists"][0]["updated"] == 0 and clean["likes"][0]["at"] == 7


# ---- P2-4 : horodatages bornés ----------------------------------------------

NOW = 1_800_000_000_000
YEAR_2100 = 4_102_444_800_000


def test_sanitize_caps_future_stamps_and_truncates_names():
    clean = ss.sanitize(
        {"v": 1,
         "playlists": [{"uid": "u", "name": "n" * 500, "updated": YEAR_2100,
                        "deleted": True}],
         "likes": [{"id": "a", "at": YEAR_2100, "liked": False}],
         "history": [{"id": "b", "at": NOW - 5}]}, now_ms=NOW)
    ceiling = NOW + ss.FUTURE_SLACK_MS
    assert clean["playlists"][0]["updated"] == ceiling
    assert clean["likes"][0]["at"] == ceiling
    assert clean["history"][0]["at"] == NOW - 5          # le passé ne bouge pas
    assert len(clean["playlists"][0]["name"]) == 200


def test_future_playlist_no_longer_wins_forever(tmp_path):
    lib = Library(tmp_path / "a.db")
    lib.create_playlist("Local", uid="u1")
    pid = lib.list_playlists()[0]["id"]
    now_ms = int(time.time() * 1000)
    remote = ss.sanitize({"v": 1, "likes": [], "history": [],
                          "playlists": [{"uid": "u1", "name": "Pirate",
                                         "updated": YEAR_2100, "deleted": False,
                                         "tracks": []}]})
    ss.apply(lib, ss.merge(ss.snapshot(lib), remote))
    assert lib.list_playlists()[0]["name"] == "Pirate"
    # Une édition locale ultérieure (après la fenêtre tolérée) reprend la main.
    lib.rename_playlist(pid, "Légitime")
    lib.set_playlist_updated(pid, (now_ms + ss.FUTURE_SLACK_MS) / 1000 + 60)
    remote2 = ss.sanitize({"v": 1, "likes": [], "history": [],
                           "playlists": [{"uid": "u1", "name": "Pirate 2",
                                          "updated": YEAR_2100, "deleted": False,
                                          "tracks": []}]})
    ss.apply(lib, ss.merge(ss.snapshot(lib), remote2))
    assert lib.list_playlists()[0]["name"] == "Légitime"
    lib.close()


def test_future_tombstone_is_brought_back_and_prunable(tmp_path):
    lib = Library(tmp_path / "a.db")
    remote = ss.sanitize({"v": 1, "likes": [], "history": [],
                          "playlists": [{"uid": "u9", "name": "", "updated": YEAR_2100,
                                         "deleted": True, "tracks": []}]})
    ss.apply(lib, ss.merge(ss.snapshot(lib), remote))
    at = lib.tombstones("playlist")["u9"]
    assert at < time.time() + ss.FUTURE_SLACK_MS / 1000 + 5
    # Élaguable : une fois la limite de conservation passée, elle disparaît.
    assert lib.prune_tombstones(-3600) >= 1
    assert lib.tombstones("playlist") == {}
    lib.close()


# ---- SoundCloud : jamais envoyé, toujours préservé ---------------------------

SC = "soundcloud:tracks:123"


def sc_ref() -> TrackRef:
    return TrackRef(SC, "Titre SC", "Artiste SC", 90.0)


def test_snapshot_never_contains_soundcloud(tmp_path):
    lib = Library(tmp_path / "a.db")
    pid = lib.create_playlist("Mixte")
    lib.add_to_playlist(pid, [ref(1), sc_ref()])
    lib.set_favorite(sc_ref(), True)
    lib.set_favorite(ref(2), True)
    lib.record_play(sc_ref(), 30, True)
    lib.record_play(ref(1), 30, True)
    lib.record_tombstone("like", "soundcloud:tracks:1", 1_700_000_000.0)
    lib.record_tombstone("like", ref(3).video_id, 1_700_000_000.0)
    snap = ss.snapshot(lib)
    assert "soundcloud" not in repr(snap)
    assert [t["id"] for t in snap["playlists"][0]["tracks"]] == [ref(1).video_id]
    assert {x["id"] for x in snap["likes"]} == {ref(2).video_id, ref(3).video_id}
    assert [h["id"] for h in snap["history"]] == [ref(1).video_id]
    lib.close()


def test_spec_loss_scenario_soundcloud_track_stays_at_its_index(tmp_path):
    """Spec 4.9 : le bureau ajoute un titre SC, le téléphone (qui ne le voit
    pas) réordonne la playlist plus tard et gagne : la piste SC reste."""
    desktop, phone = Library(tmp_path / "d.db"), Library(tmp_path / "p.db")
    pid = desktop.create_playlist("P", uid="u1")
    desktop.add_to_playlist(pid, [ref(1), ref(2), ref(3)])
    sync(desktop, phone)
    desktop.upsert_track(sc_ref())
    desktop.add_to_playlist(pid, [sc_ref()])
    desktop.set_playlist_order(pid, [ref(1).video_id, SC, ref(2).video_id, ref(3).video_id])
    time.sleep(0.01)
    ppid = phone.list_playlists()[0]["id"]
    phone.set_playlist_order(ppid, [ref(3).video_id, ref(1).video_id, ref(2).video_id])
    merged = ss.merge(ss.snapshot(desktop), ss.snapshot(phone))
    assert merged == ss.merge(ss.snapshot(phone), ss.snapshot(desktop))   # commutatif
    ss.apply(desktop, merged)
    ss.apply(phone, merged)
    assert [r.video_id for r in desktop.playlist_items(pid)] == [
        ref(3).video_id, SC, ref(1).video_id, ref(2).video_id]
    # Le téléphone n'a jamais reçu la piste SC.
    assert SC not in [r.video_id for r in phone.playlist_items(ppid)]
    # Idempotent : une seconde application ne change plus rien.
    assert ss.apply(desktop, merged) == {"playlists": 0, "likes": 0, "history": 0}
    desktop.close(); phone.close()


def test_soundcloud_index_clamped_when_remote_is_shorter(tmp_path):
    lib = Library(tmp_path / "a.db")
    pid = lib.create_playlist("P", uid="u1")
    lib.add_to_playlist(pid, [ref(1), ref(2), ref(3), sc_ref()])
    ss.apply(lib, {"playlists": [{"uid": "u1", "name": "P",
                                  "updated": int((time.time() + 100) * 1000),
                                  "deleted": False,
                                  "tracks": [ss.track_to_wire(ref(1))]}]})
    assert [r.video_id for r in lib.playlist_items(pid)] == [ref(1).video_id, SC]
    lib.close()


def test_remote_cannot_unlike_a_soundcloud_favorite(tmp_path):
    lib = Library(tmp_path / "a.db")
    lib.set_favorite(sc_ref(), True)
    ss.apply(lib, {"likes": [{"id": SC, "at": int((time.time() + 100) * 1000),
                              "liked": False, "track": None}]})
    assert [r.video_id for r in lib.list_favorites()] == [SC]
    lib.close()

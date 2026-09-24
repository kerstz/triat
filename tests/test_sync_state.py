"""Fusion des instantanés de synchro (core/sync_state.py)."""

from __future__ import annotations

import time

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
    assert ss.track_from_wire({"id": "a" * 11}).title == "Sans titre"

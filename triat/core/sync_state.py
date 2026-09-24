"""Synchro bureau ↔ téléphone : l'état échangé et sa fusion.

Chaque appareil envoie un instantané complet de ce qui se synchronise ;
`merge` en fait l'union, de façon déterministe et commutative (fusionner A
avec B ou B avec A donne la même chose), puis chacun applique le résultat.
Pour une bibliothèque de quelques milliers de titres, un instantané pèse
quelques centaines de kilo-octets compressés : plus simple et plus sûr
qu'un journal de modifications.

Format (version 1), horodatages en millisecondes :

    {"v": 1,
     "playlists": [{"uid", "name", "updated", "deleted", "tracks": [TRACK]}],
     "likes":     [{"id", "at", "liked", "track": TRACK}],
     "history":   [{"id", "at", "seconds", "track": TRACK}]}

    TRACK = {"id", "title", "artist", "duration", "thumb"}

Règles :
- playlist : la plus récente (`updated`) gagne, suppression comprise ;
- favori : le plus récent (`at`) gagne, qu'il ajoute ou retire ;
- historique : union, dédoublonnée à la seconde, bornée aux plus récentes.
"""

from __future__ import annotations

from .models import TrackRef

VERSION = 1
HISTORY_LIMIT = 3000


# ---- Pistes ----------------------------------------------------------------

def track_to_wire(ref: TrackRef) -> dict:
    return {"id": ref.video_id, "title": ref.title, "artist": ref.artist,
            "duration": float(ref.duration or 0), "thumb": ref.thumbnail}


def track_from_wire(data: dict) -> TrackRef | None:
    video_id = data.get("id")
    if not isinstance(video_id, str) or len(video_id) != 11:
        return None                     # pas une vidéo YouTube : ignorée
    return TrackRef(video_id=video_id,
                    title=str(data.get("title") or "Sans titre")[:300],
                    artist=str(data.get("artist") or "")[:200],
                    duration=float(data.get("duration") or 0),
                    thumbnail=data.get("thumb") if isinstance(data.get("thumb"), str) else None)


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


# ---- Fusion ----------------------------------------------------------------

def _newest(items, key: str, stamp: str) -> dict:
    """Garde, par clé, l'élément le plus récent. Égalité : ordre stable
    et indépendant du sens de la fusion (on compare le contenu)."""
    best: dict = {}
    for item in items:
        k = item.get(key)
        if not isinstance(k, str):
            continue
        current = best.get(k)
        if current is None:
            best[k] = item
            continue
        a, b = (item.get(stamp) or 0), (current.get(stamp) or 0)
        if a > b or (a == b and repr(sorted(item.items())) > repr(sorted(current.items()))):
            best[k] = item
    return best


def merge(a: dict, b: dict) -> dict:
    """Union de deux instantanés. Pure : ne modifie ni `a` ni `b`."""
    playlists = _newest([*a.get("playlists", []), *b.get("playlists", [])],
                        "uid", "updated")
    likes = _newest([*a.get("likes", []), *b.get("likes", [])], "id", "at")

    history: dict = {}
    for entry in [*a.get("history", []), *b.get("history", [])]:
        if not isinstance(entry.get("id"), str):
            continue
        history.setdefault((entry["id"], int(entry.get("at", 0)) // 1000), entry)
    recent = sorted(history.values(), key=lambda e: (e.get("at", 0), e["id"]),
                    reverse=True)[:HISTORY_LIMIT]

    return {
        "v": VERSION,
        "playlists": sorted(playlists.values(), key=lambda p: p["uid"]),
        "likes": sorted(likes.values(), key=lambda l: l["id"]),
        "history": recent,
    }


# ---- Bibliothèque du bureau ------------------------------------------------

def snapshot(library) -> dict:
    """Ce que le bureau sait, au format d'échange."""
    playlists = []
    for item in library.list_playlists():
        refs = library.playlist_items(item["id"])
        playlists.append({"uid": item["uid"], "name": item["name"],
                          "updated": _ms(item["updated_at"]), "deleted": False,
                          "tracks": [track_to_wire(r) for r in refs]})
    for uid, at in library.tombstones("playlist").items():
        playlists.append({"uid": uid, "name": "", "updated": _ms(at),
                          "deleted": True, "tracks": []})

    likes = [{"id": ref.video_id, "at": _ms(at), "liked": True,
              "track": track_to_wire(ref)}
             for ref, at in library.favorites_with_dates()]
    likes += [{"id": vid, "at": _ms(at), "liked": False, "track": None}
              for vid, at in library.tombstones("like").items()]

    history = [{"id": ref.video_id, "at": _ms(at), "seconds": seconds,
                "track": track_to_wire(ref)}
               for ref, at, seconds in library.history_entries(HISTORY_LIMIT)]
    return {"v": VERSION, "playlists": playlists, "likes": likes,
            "history": history}


def apply(library, merged: dict) -> dict:
    """Applique un instantané fusionné à la bibliothèque du bureau.

    Idempotent : l'appliquer deux fois ne change rien la seconde fois.
    Renvoie le nombre de changements par catégorie."""
    changes = {"playlists": 0, "likes": 0, "history": 0}
    local = {p["uid"]: p for p in library.list_playlists()}

    for playlist in merged.get("playlists", []):
        uid = playlist["uid"]
        mine = local.get(uid)
        updated = playlist.get("updated", 0) / 1000
        if playlist.get("deleted"):
            if mine is not None and mine["updated_at"] <= updated:
                library.delete_playlist(mine["id"])
                changes["playlists"] += 1
            library.record_tombstone("playlist", uid, updated)
            continue
        if mine is not None and _ms(mine["updated_at"]) >= playlist.get("updated", 0):
            continue                    # la nôtre est au moins aussi récente
        refs = [r for r in (track_from_wire(t) for t in playlist.get("tracks", [])) if r]
        library.upsert_many(refs)
        if mine is None:
            playlist_id = library.create_playlist(playlist.get("name") or "Playlist",
                                                  uid=uid)
        else:
            playlist_id = mine["id"]
            if mine["name"] != playlist.get("name"):
                library.rename_playlist(playlist_id, playlist.get("name") or mine["name"])
        library.set_playlist_order(playlist_id, [r.video_id for r in refs])
        library.set_playlist_updated(playlist_id, updated)
        changes["playlists"] += 1

    favorites = {ref.video_id: at for ref, at in library.favorites_with_dates()}
    for like in merged.get("likes", []):
        vid = like["id"]
        liked_here = vid in favorites
        if like.get("liked") and not liked_here:
            ref = track_from_wire(like.get("track") or {})
            if ref is not None:
                library.set_favorite(ref, True, at=like.get("at", 0) / 1000)
                changes["likes"] += 1
        elif not like.get("liked") and liked_here:
            if _ms(favorites[vid]) <= like.get("at", 0):
                ref = library.get_track(vid)
                if ref is not None:
                    library.set_favorite(ref, False, at=like.get("at", 0) / 1000)
                    changes["likes"] += 1

    for entry in merged.get("history", []):
        ref = track_from_wire(entry.get("track") or {})
        if ref is not None and library.add_history_entry(
                ref, entry.get("at", 0) / 1000, float(entry.get("seconds") or 0)):
            changes["history"] += 1
    return changes

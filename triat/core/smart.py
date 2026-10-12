"""Smart playlists : playlists calculées par règles (screens.md §8).

Rien n'est stocké : chaque ouverture relit la bibliothèque. Les règles
restent volontairement simples et lisibles, affichées telles quelles.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .models import TrackRef

DAY = 86400.0


@dataclass(frozen=True)
class SmartPlaylist:
    key: str
    name: str
    rule: str                      # affichée en mono sous le nom
    fetch: Callable[[], list[TrackRef]]


def smart_playlists(library, downloads=None, now: float | None = None,
                    limit: int = 500) -> list[SmartPlaylist]:
    """Les quatre smart playlists de la maquette, dans l'ordre."""
    def moment() -> float:
        # Calculé à chaque appel : l'application reste ouverte des jours,
        # « 30 derniers jours » ne doit pas rester figé à son lancement.
        return time.time() if now is None else now

    def on_repeat() -> list[TrackRef]:
        return library.top_tracks(limit=limit, since=moment() - 30 * DAY)

    def never_played() -> list[TrackRef]:
        return library.unplayed(limit=limit, owned=True)

    def recently_added() -> list[TrackRef]:
        return library.added_since(moment() - 14 * DAY, limit=limit, owned=True)

    scanned = [False]

    def offline() -> list[TrackRef]:
        if downloads is None:
            return []
        # L'index des téléchargements fait foi : on ne vérifie que ces
        # fichiers-là, au lieu de sonder le disque pour chaque piste.
        found: list[TrackRef] = []
        for video_id in library.downloaded_ids():
            ref = library.get_track(video_id)
            if ref is not None and downloads.local_path(ref) is not None:
                found.append(ref)
                if len(found) >= limit:
                    break
        if found or library.downloaded_ids() or scanned[0]:
            return found
        # Index vide (fichiers antérieurs à l'index) : un seul balayage
        # borné, dont `local_path` alimente l'index pour la suite.
        scanned[0] = True
        return [ref for ref in library.all_tracks(limit=5000)
                if downloads.local_path(ref) is not None][:limit]

    return [
        SmartPlaylist("repeat", "En boucle", "écoutes · 30 j", on_repeat),
        SmartPlaylist("unplayed", "Jamais écoutés", "ajoutés · lectures = 0", never_played),
        SmartPlaylist("recent", "Récemment ajoutés", "ajout < 14 j", recently_added),
        SmartPlaylist("offline", "Hors-ligne", "téléchargé = vrai", offline),
    ]

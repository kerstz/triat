"""Mise en forme des résultats de recherche (screens.md §2).

YouTube ne renvoie que des pistes (les recherches filtrées de YouTube Music
sont fermées). On en tire le reste : les artistes présents dans les
résultats, les playlists locales dont le nom correspond, et un « meilleur
résultat ». Module pur, sans GTK, pour être testé directement.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from ..i18n import nformat
from .models import TrackRef
from .soundcloud import ScPlaylist, ScTrack, ScUser

# Suffixes des chaînes générées par YouTube (« Artiste - Topic »).
_CHANNEL_SUFFIXES = (" - topic", " - sujet", "vevo")


def normalize(text: str) -> str:
    """Minuscules, sans accents ni espaces superflus."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(bare.casefold().split())


def clean_artist(name: str) -> str:
    """Nom d'artiste sans le suffixe de chaîne automatique."""
    name = (name or "").strip()
    lowered = name.lower()
    for suffix in _CHANNEL_SUFFIXES:
        if lowered.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)].rstrip(" -")
            break
    return name


@dataclass
class BestResult:
    kind: str                 # "artist" | "playlist" | "track"
    title: str
    detail: str = ""
    ref: TrackRef | None = None
    playlist_id: int | None = None


@dataclass
class SearchResults:
    query: str
    tracks: list[TrackRef] = field(default_factory=list)
    artists: list[tuple[str, int]] = field(default_factory=list)
    playlists: list[dict] = field(default_factory=list)
    best: BestResult | None = None
    sc_tracks: list[ScTrack] = field(default_factory=list)
    sc_artists: list[ScUser] = field(default_factory=list)
    sc_playlists: list[ScPlaylist] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.tracks or self.artists or self.playlists
                    or self.sc_tracks or self.sc_artists or self.sc_playlists)


def _matches(query: str, text: str) -> bool:
    """Tous les mots de la requête figurent dans le texte."""
    haystack = normalize(text)
    return all(term in haystack for term in normalize(query).split())


def _artists(query: str, tracks: list[TrackRef], limit: int) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}
    for ref in tracks:
        name = clean_artist(ref.artist)
        if not name:
            continue
        key = normalize(name)
        counts[key] += 1
        names.setdefault(key, name)
    # Ceux dont le nom répond à la requête passent devant.
    ranked = sorted(counts, key=lambda k: (not _matches(query, k), -counts[k], k))
    return [(names[k], counts[k]) for k in ranked[:limit]]


def _best(query: str, tracks, artists, playlists,
          sc_artists=(), source: str = "youtube") -> BestResult | None:
    wanted = normalize(query)
    # Un artiste SoundCloud ne devient meilleur résultat que si la source
    # active est SoundCloud ; le comportement par défaut reste inchangé.
    if source == "soundcloud":
        for user in sc_artists:
            key = normalize(user.username)
            if key == wanted or (key.startswith(wanted) and len(wanted) >= 3):
                count = user.track_count
                detail = nformat("{n} titre", "{n} titres", count)
                return BestResult("artist", user.username, detail)
    for name, count in artists:
        key = normalize(name)
        if key == wanted or (key.startswith(wanted) and len(wanted) >= 3):
            detail = nformat("{n} titre", "{n} titres", count)
            return BestResult("artist", name, detail)
    for playlist in playlists:
        if normalize(playlist.get("name", "")) == wanted:
            count = playlist.get("item_count", 0)
            return BestResult("playlist", playlist["name"],
                              nformat("Playlist · {n} titre", "Playlist · {n} titres", count),
                              playlist_id=playlist.get("id"))
    if tracks:
        ref = tracks[0]
        return BestResult("track", ref.title, clean_artist(ref.artist), ref=ref)
    return None


def build(query: str, remote: list[TrackRef], local: list[TrackRef] = (),
          playlists: list[dict] = (), artist_limit: int = 8, *,
          sc_tracks=(), sc_artists=(), sc_playlists=(),
          source: str = "youtube", hide_unplayable: bool = False
          ) -> SearchResults:
    """Assemble les résultats : pistes distantes puis locales, sans doublon.

    Les listes SoundCloud sont facultatives. `hide_unplayable` retire les
    pistes SoundCloud non lisibles ; `source="soundcloud"` autorise un
    artiste SoundCloud comme meilleur résultat.
    """
    seen: set[str] = set()
    tracks: list[TrackRef] = []
    for ref in [*remote, *local]:
        if ref.video_id in seen:
            continue
        seen.add(ref.video_id)
        tracks.append(ref)
    matched = [p for p in playlists if _matches(query, p.get("name", ""))]
    artists = _artists(query, tracks, artist_limit)
    sc_tracks = [t for t in sc_tracks if t.playable or not hide_unplayable]
    sc_artists = list(sc_artists)
    return SearchResults(query=query, tracks=tracks, artists=artists,
                         playlists=matched,
                         best=_best(query, tracks, artists, matched,
                                    sc_artists, source),
                         sc_tracks=sc_tracks, sc_artists=sc_artists,
                         sc_playlists=list(sc_playlists))


HISTORY_SIZE = 8


def clean_history(value) -> list[str]:
    """Historique lu des réglages : chaînes non vides seulement."""
    if not isinstance(value, list):
        return []
    return [q for q in value if isinstance(q, str) and q.strip()][:HISTORY_SIZE]


def remember_query(history, query: str) -> list[str]:
    """Place `query` en tête, sans doublon (casse et accents ignorés).

    Les URL ne sont pas retenues : elles encombrent et peuvent être privées.
    """
    query = " ".join((query or "").split())
    kept = clean_history(history)
    if not query or query.startswith(("http://", "https://")):
        return kept
    key = normalize(query)
    return [query, *[q for q in kept if normalize(q) != key]][:HISTORY_SIZE]

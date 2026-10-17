"""Paroles synchronisées via LRCLIB.

Les paroles ne sont jamais stockées dans le code : elles sont demandées à
l'exécution et gardées en cache local, comme les pochettes. L'API est
publique et ne demande pas de clé.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from .net import USER_AGENT_FALLBACK, read_capped, urlopen_checked

log = logging.getLogger(__name__)

API = "https://lrclib.net/api"
TIMEOUT = 10.0
CACHE_MAX_AGE = 90 * 86400
# Écart de durée toléré pour considérer qu'un résultat est la bonne piste.
DURATION_TOLERANCE = 6.0
# En dessous, des paroles d'un morceau de plus d'une minute sont suspectes.
MIN_LINES = 4

# Taille du cache mémoire (session privée : rien n'est écrit en base).
MEMORY_CACHE_SIZE = 64

_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def is_youtube_id(video_id: str) -> bool:
    """Un identifiant YouTube fait exactement 11 caractères [A-Za-z0-9_-]."""
    return bool(_YOUTUBE_ID.fullmatch(video_id or ""))


# Une ligne LRC : [mm:ss.xx] suivi du texte.
_LRC_LINE = re.compile(r"^\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]\s*(.*)$")


@dataclass(slots=True)
class Lyrics:
    """Paroles d'une piste. `lines` est vide pour une piste instrumentale."""

    video_id: str
    synced: bool
    lines: list[tuple[float, str]]     # (seconde, texte)
    plain: str = ""
    instrumental: bool = False
    source: str = "LRCLIB"
    # Service injoignable (hors ligne, panne) : l'interface propose de réessayer.
    unreachable: bool = False

    @property
    def available(self) -> bool:
        return bool(self.lines or self.plain or self.instrumental)

    def index_at(self, position: float) -> int:
        """Index de la ligne en cours, ou -1 avant la première."""
        if not self.lines:
            return -1
        found = -1
        for index, (stamp, _text) in enumerate(self.lines):
            if stamp <= position:
                found = index
            else:
                break
        return found


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Transforme un bloc LRC en lignes horodatées, triées."""
    lines: list[tuple[float, str]] = []
    for raw in (text or "").splitlines():
        match = _LRC_LINE.match(raw.strip())
        if not match:
            continue
        minutes, seconds, fraction, content = match.groups()
        stamp = int(minutes) * 60 + int(seconds)
        if fraction:
            stamp += int(fraction) / (10 ** len(fraction))
        lines.append((stamp, content.strip()))
    lines.sort(key=lambda item: item[0])
    return lines


_UNREACHABLE = object()      # réponse de `_call` : réseau ou serveur en panne


class LyricsService:
    """Recherche, met en cache et sert les paroles."""

    def __init__(self, library=None, timeout: float = TIMEOUT, *,
                 is_private: Callable[[], bool] | None = None) -> None:
        self._library = library
        self._timeout = timeout
        self._is_private = is_private
        # Cache mémoire borné (LRU), utilisé surtout en session privée.
        self._memory: OrderedDict[str, Lyrics] = OrderedDict()
        self._memory_lock = threading.Lock()
        # Récupérations en vol : video_id -> [événement, résultat].
        self._inflight: dict[str, list] = {}
        self._inflight_lock = threading.Lock()
        if library is not None:
            self._ensure_table()

    # ---- Cache -----------------------------------------------------------

    def _ensure_table(self) -> None:
        with self._library._lock:
            self._library._db.execute("""
                CREATE TABLE IF NOT EXISTS lyrics (
                    video_id     TEXT PRIMARY KEY,
                    synced       INTEGER NOT NULL DEFAULT 0,
                    instrumental INTEGER NOT NULL DEFAULT 0,
                    body         TEXT NOT NULL DEFAULT '',
                    plain        TEXT NOT NULL DEFAULT '',
                    fetched_at   REAL NOT NULL
                )
            """)
            self._library._db.commit()

    def _private(self) -> bool:
        try:
            return bool(self._is_private and self._is_private())
        except Exception:
            return True        # dans le doute, on n'écrit rien

    def _memory_get(self, video_id: str) -> Lyrics | None:
        with self._memory_lock:
            found = self._memory.get(video_id)
            if found is not None:
                self._memory.move_to_end(video_id)
            return found

    def _memory_put(self, video_id: str, lyrics: Lyrics) -> None:
        with self._memory_lock:
            self._memory[video_id] = lyrics
            self._memory.move_to_end(video_id)
            while len(self._memory) > MEMORY_CACHE_SIZE:
                self._memory.popitem(last=False)

    def _cached(self, video_id: str) -> Lyrics | None:
        found = self._memory_get(video_id)
        if found is not None:
            return found
        if self._library is None:
            return None
        with self._library._lock:
            row = self._library._db.execute(
                "SELECT * FROM lyrics WHERE video_id = ?", (video_id,)).fetchone()
        if row is None or (time.time() - row["fetched_at"]) > CACHE_MAX_AGE:
            return None
        # Entrée poubelle mise en cache avant le filtre : on la redemande.
        if (row["body"] or row["plain"]) and not self._plausible(
                {"syncedLyrics": row["body"], "plainLyrics": row["plain"],
                 "instrumental": row["instrumental"]}, 0.0):
            return None
        return Lyrics(
            video_id=video_id,
            synced=bool(row["synced"]),
            lines=parse_lrc(row["body"]) if row["synced"] else [],
            plain=row["plain"],
            instrumental=bool(row["instrumental"]),
        )

    def _store(self, video_id: str, body: str, plain: str,
               synced: bool, instrumental: bool) -> None:
        if self._library is None or self._private():
            return
        try:
            with self._library._lock:
                self._library._db.execute(
                    "INSERT OR REPLACE INTO lyrics "
                    "(video_id, synced, instrumental, body, plain, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (video_id, int(synced), int(instrumental), body, plain,
                     time.time()))
                self._library._db.commit()
        except sqlite3.Error as exc:
            log.debug("Cache des paroles impossible : %s", exc)

    # ---- Réseau ----------------------------------------------------------

    def _call(self, path: str, params: dict) -> object | None:
        url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT_FALLBACK})
        try:
            with urlopen_checked(request, self._timeout, ("lrclib.net",)) as response:
                return json.loads(read_capped(response).decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                log.info("LRCLIB HTTP %s", exc.code)
                return _UNREACHABLE        # panne serveur : pas une absence
        except Exception as exc:
            log.debug("LRCLIB injoignable : %s", exc)
            return _UNREACHABLE
        return None

    @staticmethod
    def _score(entry: dict, duration: float) -> float:
        """Écart de durée : le plus petit gagne."""
        try:
            return abs(float(entry.get("duration") or 0) - duration)
        except (TypeError, ValueError):
            return 1e9

    @staticmethod
    def _plausible(entry, duration: float) -> bool:
        """Écarte les entrées vides ou vandalisées.

        LRCLIB est ouvert en écriture : on y trouve par exemple une seule
        ligne « probe » pour « Never Gonna Give You Up ». Un morceau de plus
        d'une minute a forcément plusieurs lignes de paroles.
        """
        if not isinstance(entry, dict):
            return False
        if entry.get("instrumental"):
            return True
        text = entry.get("plainLyrics") or entry.get("syncedLyrics") or ""
        lines = [line for line in str(text).splitlines() if line.strip()]
        if duration and duration < 60:
            return bool(lines)
        return len(lines) >= MIN_LINES

    def fetch(self, ref) -> Lyrics:
        """Paroles d'une piste. Bloquant ; ne lève jamais.

        Deux appels concurrents pour le même id ne font qu'une requête.
        """
        video_id = getattr(ref, "video_id", "") or ""
        # LRCLIB ne connaît que les pistes YouTube ici : ni réseau ni cache.
        if video_id and not is_youtube_id(video_id):
            return Lyrics(video_id, False, [])
        cached = self._cached(video_id)
        if cached is not None:
            return cached
        with self._inflight_lock:
            slot = self._inflight.get(video_id)
            leader = slot is None
            if leader:
                slot = [threading.Event(), None]
                self._inflight[video_id] = slot
        if not leader:
            slot[0].wait()
            if slot[1] is not None:
                return slot[1]
            return Lyrics(video_id, False, [])
        try:
            result = self._fetch_remote(video_id, ref)
            slot[1] = result
            return result
        except Exception as exc:        # ne lève jamais
            log.debug("Paroles : échec inattendu : %s", exc)
            return Lyrics(video_id, False, [])
        finally:
            with self._inflight_lock:
                self._inflight.pop(video_id, None)
            slot[0].set()

    def _fetch_remote(self, video_id: str, ref) -> Lyrics:

        title = (getattr(ref, "title", "") or "").strip()
        artist = (getattr(ref, "artist", "") or "").strip()
        duration = float(getattr(ref, "duration", 0) or 0)
        if not title:
            return Lyrics(video_id, False, [])

        entry = None
        unreachable = False
        # Correspondance exacte d'abord : artiste + titre + durée.
        if artist and duration > 0:
            entry = self._call("get", {
                "artist_name": artist, "track_name": title,
                "duration": int(duration)})
            if entry is _UNREACHABLE:
                unreachable, entry = True, None
        if not self._plausible(entry, duration):
            entry = None

        if entry is None:
            results = self._call("search", {"track_name": title,
                                            **({"artist_name": artist} if artist else {})})
            if results is _UNREACHABLE:
                unreachable, results = True, None
            if isinstance(results, list):
                results = [e for e in results if self._plausible(e, duration)]
            if isinstance(results, list) and results:
                if duration > 0:
                    best = min(results, key=lambda e: self._score(e, duration))
                    # Une durée trop éloignée signale une autre version.
                    entry = best if self._score(best, duration) <= DURATION_TOLERANCE \
                        else results[0]
                else:
                    entry = results[0]

        if not isinstance(entry, dict):
            # Hors ligne ou serveur en panne : ne pas mémoriser « pas de
            # paroles » pour 90 jours.
            result = Lyrics(video_id, False, [], unreachable=unreachable)
            if not unreachable:
                self._store(video_id, "", "", False, False)
                if self._private():
                    self._memory_put(video_id, result)
            return result

        body = entry.get("syncedLyrics") or ""
        plain = entry.get("plainLyrics") or ""
        instrumental = bool(entry.get("instrumental"))
        self._store(video_id, body, plain, bool(body), instrumental)
        result = Lyrics(
            video_id=video_id,
            synced=bool(body),
            lines=parse_lrc(body),
            plain=plain,
            instrumental=instrumental,
        )
        if self._private():
            self._memory_put(video_id, result)
        return result

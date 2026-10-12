"""Bibliothèque locale : SQLite + recherche plein texte FTS5.

Toutes les méthodes sont utilisables depuis n'importe quel thread : une
seule connexion protégée par un verrou, en mode WAL.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterable, Sequence

from .models import Segment, TrackRef
from .paths import db_path
from .reco import TrackSignal, artist_key

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4   # 2 : uid des playlists et traces de suppression (synchro)
#                      3 : index des téléchargements par video_id
#                      4 : index playlist_items(video_id), tables reco_*

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    video_id   TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    artist     TEXT NOT NULL DEFAULT '',
    album      TEXT,
    duration   REAL NOT NULL DEFAULT 0,
    thumbnail  TEXT,
    added_at   REAL NOT NULL,
    play_count INTEGER NOT NULL DEFAULT 0,
    last_played REAL
);

CREATE TABLE IF NOT EXISTS favorites (
    video_id TEXT PRIMARY KEY REFERENCES tracks(video_id) ON DELETE CASCADE,
    added_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    remote_id  TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    video_id    TEXT NOT NULL REFERENCES tracks(video_id) ON DELETE CASCADE,
    PRIMARY KEY (playlist_id, position)
);

-- Traces de suppression pour la synchro : sans elles, une playlist effacée
-- ou un favori retiré reviendrait depuis l'autre appareil.
CREATE TABLE IF NOT EXISTS sync_tombstones (
    kind TEXT NOT NULL,          -- 'playlist' (clé = uid) ou 'like' (clé = video_id)
    key  TEXT NOT NULL,
    at   REAL NOT NULL,
    PRIMARY KEY (kind, key)
);

-- Fichiers téléchargés, retrouvés par video_id : le titre ou l'album d'une
-- piste peuvent être corrigés après coup, pas son identifiant.
CREATE TABLE IF NOT EXISTS downloads (
    video_id TEXT PRIMARY KEY,
    path     TEXT NOT NULL,
    at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id  TEXT NOT NULL,
    played_at REAL NOT NULL,
    seconds   REAL NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_history_played_at ON history(played_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_video ON history(video_id);

CREATE TABLE IF NOT EXISTS segments (
    video_id   TEXT NOT NULL,
    uuid       TEXT NOT NULL,
    category   TEXT NOT NULL,
    start_s    REAL NOT NULL,
    end_s      REAL NOT NULL,
    PRIMARY KEY (video_id, uuid)
);

-- Une ligne par vidéo interrogée, y compris celles sans aucun segment :
-- sinon on réinterroge l'API à chaque lecture d'une piste sans segment.
CREATE TABLE IF NOT EXISTS segment_fetches (
    video_id   TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
    title, artist, album,
    content='tracks', content_rowid='rowid', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS tracks_ai AFTER INSERT ON tracks BEGIN
    INSERT INTO tracks_fts(rowid, title, artist, album)
    VALUES (new.rowid, new.title, new.artist, coalesce(new.album, ''));
END;
CREATE TRIGGER IF NOT EXISTS tracks_ad AFTER DELETE ON tracks BEGIN
    INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
    VALUES ('delete', old.rowid, old.title, old.artist, coalesce(old.album, ''));
END;
CREATE TRIGGER IF NOT EXISTS tracks_au AFTER UPDATE ON tracks BEGIN
    INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
    VALUES ('delete', old.rowid, old.title, old.artist, coalesce(old.album, ''));
    INSERT INTO tracks_fts(rowid, title, artist, album)
    VALUES (new.rowid, new.title, new.artist, coalesce(new.album, ''));
END;

-- v4 : sans cet index, chaque filtre `_OWNED` est quadratique (74 s à 50k pistes).
CREATE INDEX IF NOT EXISTS idx_playlist_items_video ON playlist_items(video_id);
CREATE INDEX IF NOT EXISTS idx_tracks_artist_key ON tracks(lower(trim(artist)));

-- Sauts manuels : le seul signal négatif. Jamais écrit en session privée.
CREATE TABLE IF NOT EXISTS reco_skips (
    video_id TEXT NOT NULL,
    at       REAL NOT NULL,
    position REAL NOT NULL DEFAULT 0,
    context  TEXT NOT NULL DEFAULT ''      -- '' | 'radio' | 'mix:<clé>'
);
CREATE INDEX IF NOT EXISTS idx_reco_skips_video ON reco_skips(video_id, at);

-- « Ne plus recommander » : 'track' (clé = video_id) ou 'artist' (clé =
-- clé d'artiste normalisée). Local : jamais synchronisé.
CREATE TABLE IF NOT EXISTS reco_blocklist (
    kind  TEXT NOT NULL CHECK (kind IN ('track', 'artist')),
    key   TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    at    REAL NOT NULL,
    PRIMARY KEY (kind, key)
);

-- Résultats dérivés mis en cache : 'mixes:AAAA-MM-JJ', 'home:remote'.
CREATE TABLE IF NOT EXISTS reco_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,              -- JSON
    created_at REAL NOT NULL
);
"""


# Piste « à soi » : rangée dans une playlist, en favori, ou déjà écoutée.
# Le reste de `tracks` n'est que le cache des recherches et des radios.
_OWNED = """(EXISTS (SELECT 1 FROM playlist_items o WHERE o.video_id = {t}.video_id)
    OR EXISTS (SELECT 1 FROM favorites o WHERE o.video_id = {t}.video_id)
    OR {t}.play_count > 0)"""


def _decay(age, half_life):
    """Poids d'un événement vieux de `age` secondes (fonction SQL
    `triat_decay`, disponible même sans les fonctions maths de SQLite)."""
    return 0.5 ** (max(float(age or 0.0), 0.0) / float(half_life))


def _row_to_ref(row: sqlite3.Row) -> TrackRef:
    return TrackRef(
        video_id=row["video_id"],
        title=row["title"],
        artist=row["artist"],
        duration=row["duration"],
        thumbnail=row["thumbnail"],
        album=row["album"],
    )


class Library:
    def __init__(self, path=None) -> None:
        self._path = str(path or db_path())
        self._lock = threading.RLock()
        self._batch_depth = 0
        try:
            self._open()
        except sqlite3.DatabaseError as exc:
            # Fichier abîmé (coupure pendant une écriture, disque plein) :
            # on le met de côté, récupérable, plutôt que de ne plus démarrer.
            self._quarantine(exc)
            self._open()

    def _open(self) -> None:
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.create_function("triat_decay", 2, _decay, deterministic=True)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._migrate()
        except sqlite3.DatabaseError:
            self._db.close()
            raise

    def _quarantine(self, exc: Exception) -> None:
        if self._path == ":memory:":
            raise exc
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            source = Path(self._path + suffix)
            if source.exists():
                source.rename(f"{self._path}.corrompue-{stamp}{suffix}")
        log.error("Bibliothèque illisible (%s) : mise de côté en "
                  "%s.corrompue-%s, nouvelle base créée", exc, self._path, stamp)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _commit(self) -> None:
        # Dans un `batch()`, la validation est repoussée à la sortie.
        if self._batch_depth == 0:
            self._db.commit()

    @contextmanager
    def batch(self):
        """Regroupe plusieurs écritures en une seule transaction : tout est
        validé à la sortie, ou tout est annulé si une exception passe.
        Le verrou est tenu pendant toute la durée."""
        with self._lock:
            self._batch_depth += 1
            try:
                yield self
            except BaseException:
                self._batch_depth -= 1
                if self._batch_depth == 0:
                    self._db.rollback()
                raise
            else:
                self._batch_depth -= 1
                if self._batch_depth == 0:
                    self._db.commit()

    # ---- Schéma --------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._db.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] < SCHEMA_VERSION:
                self._db.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            elif row["version"] > SCHEMA_VERSION:
                log.warning(
                    "Base écrite par une version plus récente (%s > %s)",
                    row["version"], SCHEMA_VERSION,
                )
            # v2 : identifiant stable des playlists, partagé entre appareils.
            columns = {r["name"] for r in self._db.execute("PRAGMA table_info(playlists)")}
            if "uid" not in columns:
                self._db.execute("ALTER TABLE playlists ADD COLUMN uid TEXT")
            for row in self._db.execute(
                    "SELECT id FROM playlists WHERE uid IS NULL").fetchall():
                self._db.execute("UPDATE playlists SET uid = ? WHERE id = ?",
                                 (uuid.uuid4().hex, row["id"]))
            self._commit()

    # ---- Pistes --------------------------------------------------------

    def upsert_track(self, ref: TrackRef) -> None:
        """Enregistre ou rafraîchit une piste sans toucher aux compteurs."""
        if not ref.video_id:
            return
        with self._lock:
            self._db.execute(
                """
                INSERT INTO tracks (video_id, title, artist, album, duration,
                                    thumbnail, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    title     = coalesce(NULLIF(excluded.title, ''), tracks.title),
                    artist    = coalesce(NULLIF(excluded.artist, ''), tracks.artist),
                    album     = coalesce(excluded.album, tracks.album),
                    duration  = CASE WHEN excluded.duration > 0
                                     THEN excluded.duration ELSE tracks.duration END,
                    thumbnail = coalesce(excluded.thumbnail, tracks.thumbnail)
                """,
                (ref.video_id, ref.title, ref.artist, ref.album,
                 ref.duration, ref.thumbnail, time.time()),
            )
            self._db.execute("UPDATE tracks SET title = 'Sans titre'"
                             " WHERE video_id = ? AND title = ''", (ref.video_id,))
            self._commit()

    def upsert_many(self, refs: Iterable[TrackRef]) -> None:
        """Une seule transaction pour tout le lot (une validation, pas N)."""
        with self.batch():
            for ref in refs:
                self.upsert_track(ref)

    def get_track(self, video_id: str) -> TrackRef | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tracks WHERE video_id = ?", (video_id,)
            ).fetchone()
        return _row_to_ref(row) if row else None

    def search(self, query: str, limit: int = 50) -> list[TrackRef]:
        """Recherche plein texte locale. Chaque mot devient un préfixe."""
        terms = [t for t in query.replace('"', " ").split() if t]
        if not terms:
            return []
        match = " ".join(f'"{t}"*' for t in terms)
        with self._lock:
            try:
                rows = self._db.execute(
                    """
                    SELECT t.* FROM tracks_fts f
                    JOIN tracks t ON t.rowid = f.rowid
                    WHERE tracks_fts MATCH ?
                    ORDER BY rank LIMIT ?
                    """,
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                # Une saisie exotique peut produire une requête FTS invalide :
                # aucun résultat vaut mieux qu'une fenêtre qui tombe.
                log.info("Requête de recherche rejetée par FTS5: %s", exc)
                return []
        return [_row_to_ref(r) for r in rows]

    def tracks_by_artist(self, artist: str, limit: int = 200,
                         owned: bool = False) -> list[TrackRef]:
        """Pistes d'un artiste déjà connues localement.

        Comparaison insensible à la casse et aux espaces : les métadonnées
        YouTube ne sont pas normalisées. `owned` écarte le cache des
        recherches (voir `_OWNED`) : les mixes ne doivent pas en tirer.
        """
        needle = (artist or "").strip().lower()
        if not needle:
            return []
        mine = " AND " + _OWNED.format(t="tracks") if owned else ""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM tracks
                WHERE lower(trim(artist)) = ?""" + mine + """
                ORDER BY play_count DESC, title
                LIMIT ?
                """,
                (needle, limit),
            ).fetchall()
        return [_row_to_ref(r) for r in rows]

    def all_tracks(self, limit: int = 2000, owned: bool = False) -> list[TrackRef]:
        """Pistes connues, récentes d'abord. `owned` écarte le cache de
        recherche (voir `_OWNED`)."""
        where = " WHERE " + _OWNED.format(t="tracks") if owned else ""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tracks" + where + " ORDER BY added_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [_row_to_ref(r) for r in rows]

    # ---- Téléchargements -----------------------------------------------

    def record_download(self, video_id: str, path: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO downloads (video_id, path, at)"
                " VALUES (?, ?, ?)", (video_id, str(path), time.time()))
            self._commit()

    def download_path(self, video_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT path FROM downloads WHERE video_id = ?",
                                   (video_id,)).fetchone()
        return row["path"] if row else None

    def download_owner(self, path: str) -> str | None:
        """`video_id` auquel ce fichier est déjà attribué, s'il l'est."""
        with self._lock:
            row = self._db.execute("SELECT video_id FROM downloads WHERE path = ?",
                                   (str(path),)).fetchone()
        return row["video_id"] if row else None

    def forget_download(self, video_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM downloads WHERE video_id = ?", (video_id,))
            self._commit()

    def downloaded_ids(self) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT video_id FROM downloads ORDER BY at DESC").fetchall()
        return [r["video_id"] for r in rows]

    def added_since(self, since: float, limit: int = 500,
                    owned: bool = False) -> list[TrackRef]:
        """Pistes entrées dans la bibliothèque après `since`, récentes d'abord."""
        mine = " AND " + _OWNED.format(t="tracks") if owned else ""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tracks WHERE added_at >= ?" + mine +
                " ORDER BY added_at DESC LIMIT ?", (since, limit)
            ).fetchall()
        return [_row_to_ref(r) for r in rows]

    def missing_duration(self, limit: int = 500) -> list[str]:
        """Identifiants des pistes sans durée connue."""
        with self._lock:
            rows = self._db.execute(
                "SELECT video_id FROM tracks WHERE duration <= 0"
                " ORDER BY added_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r["video_id"] for r in rows]

    def set_duration(self, video_id: str, seconds: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tracks SET duration = ? WHERE video_id = ? AND duration <= 0",
                (float(seconds), video_id))
            self._commit()

    def unplayed(self, limit: int = 500, owned: bool = False) -> list[TrackRef]:
        mine = " AND " + _OWNED.format(t="tracks") if owned else ""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tracks WHERE play_count = 0" + mine +
                " ORDER BY added_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_ref(r) for r in rows]

    def playlist_neighbours(self, video_ids: Sequence[str], radius: int = 3,
                            limit: int = 100) -> list[TrackRef]:
        """Pistes rangées à moins de `radius` places de celles données, dans
        n'importe quelle playlist. L'ordre choisi par l'utilisateur est un
        bon indice de parenté : on l'exploite pour les mixes."""
        ids = list(dict.fromkeys(video_ids))
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._db.execute(
                f"""
                SELECT t.*, MIN(ABS(n.position - p.position)) AS gap
                FROM playlist_items p
                JOIN playlist_items n ON n.playlist_id = p.playlist_id
                     AND ABS(n.position - p.position) BETWEEN 1 AND ?
                JOIN tracks t ON t.video_id = n.video_id
                WHERE p.video_id IN ({marks}) AND n.video_id NOT IN ({marks})
                GROUP BY t.video_id ORDER BY gap, t.title LIMIT ?
                """,
                (radius, *ids, *ids, limit),
            ).fetchall()
        return [_row_to_ref(r) for r in rows]

    def artists(self, limit: int = 200, owned: bool = False) -> list[tuple[str, int]]:
        """Artistes de la bibliothèque, avec leur nombre de pistes."""
        mine = " AND " + _OWNED.format(t="tracks") if owned else ""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT artist, COUNT(*) AS n FROM tracks
                WHERE trim(artist) != ''""" + mine + """
                GROUP BY lower(trim(artist))
                ORDER BY n DESC, artist
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [(r["artist"], r["n"]) for r in rows]

    # ---- Favoris -------------------------------------------------------

    def is_favorite(self, video_id: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM favorites WHERE video_id = ?", (video_id,)
            ).fetchone() is not None

    def set_favorite(self, ref: TrackRef, favorite: bool,
                     at: float | None = None) -> None:
        """`at` : date de l'action, fournie par la synchro ; sinon maintenant."""
        self.upsert_track(ref)
        when = time.time() if at is None else at
        with self._lock:
            if favorite:
                self._db.execute(
                    "INSERT OR IGNORE INTO favorites VALUES (?, ?)",
                    (ref.video_id, when),
                )
                self._db.execute("DELETE FROM sync_tombstones WHERE kind = 'like'"
                                 " AND key = ?", (ref.video_id,))
            else:
                self._db.execute(
                    "DELETE FROM favorites WHERE video_id = ?", (ref.video_id,)
                )
                self._tombstone("like", ref.video_id, when)
            self._commit()

    def toggle_favorite(self, ref: TrackRef) -> bool:
        new_state = not self.is_favorite(ref.video_id)
        self.set_favorite(ref, new_state)
        return new_state

    def favorites_with_dates(self) -> list[tuple[TrackRef, float]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT t.*, f.added_at AS liked_at FROM favorites f"
                " JOIN tracks t ON t.video_id = f.video_id").fetchall()
        return [(_row_to_ref(r), r["liked_at"]) for r in rows]

    def list_favorites(self, limit: int = 500) -> list[TrackRef]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT t.* FROM favorites f
                JOIN tracks t ON t.video_id = f.video_id
                ORDER BY f.added_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_ref(r) for r in rows]

    # ---- Playlists -----------------------------------------------------

    def create_playlist(self, name: str, remote_id: str | None = None,
                        uid: str | None = None) -> int:
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO playlists (name, remote_id, created_at, updated_at, uid)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, remote_id, now, now, uid or uuid.uuid4().hex),
            )
            self._commit()
            return int(cur.lastrowid)

    def rename_playlist(self, playlist_id: int, name: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?",
                (name, time.time(), playlist_id),
            )
            self._commit()

    def delete_playlist(self, playlist_id: int, at: float | None = None) -> None:
        """`at` : date de la suppression chez son auteur (synchro) ;
        par défaut, maintenant."""
        with self._lock:
            row = self._db.execute("SELECT uid FROM playlists WHERE id = ?",
                                   (playlist_id,)).fetchone()
            if row is not None and row["uid"]:
                self._tombstone("playlist", row["uid"], at)
            self._db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
            self._commit()

    def _tombstone(self, kind: str, key: str, at: float | None = None) -> None:
        """Trace une suppression (verrou déjà tenu par l'appelant)."""
        self._db.execute(
            "INSERT OR REPLACE INTO sync_tombstones (kind, key, at) VALUES (?, ?, ?)",
            (kind, key, time.time() if at is None else at))

    def record_tombstone(self, kind: str, key: str, at: float) -> None:
        """Trace reçue d'un autre appareil : on garde la plus récente."""
        with self._lock:
            row = self._db.execute(
                "SELECT at FROM sync_tombstones WHERE kind = ? AND key = ?",
                (kind, key)).fetchone()
            if row is None or row["at"] < at:
                self._tombstone(kind, key, at)
                self._commit()

    def set_playlist_updated(self, playlist_id: int, at: float) -> None:
        """Date de modification imposée par la synchro (celle de l'auteur)."""
        with self._lock:
            self._db.execute("UPDATE playlists SET updated_at = ? WHERE id = ?",
                             (at, playlist_id))
            self._commit()

    def tombstones(self, kind: str) -> dict[str, float]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key, at FROM sync_tombstones WHERE kind = ?", (kind,)).fetchall()
        return {r["key"]: r["at"] for r in rows}

    def list_playlists(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT p.*, (SELECT COUNT(*) FROM playlist_items i
                             WHERE i.playlist_id = p.id) AS item_count,
                       (SELECT COALESCE(SUM(t.duration), 0)
                        FROM playlist_items i JOIN tracks t
                             ON t.video_id = i.video_id
                        WHERE i.playlist_id = p.id) AS total_duration,
                       (SELECT COUNT(*) FROM playlist_items i JOIN tracks t
                             ON t.video_id = i.video_id
                        WHERE i.playlist_id = p.id AND t.duration > 0)
                        AS known_durations
                FROM playlists p ORDER BY p.updated_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def playlist_covers(self) -> list[dict]:
        """Une ligne par playlist avec la première piste : `id, name,
        item_count, first_id, first_thumbnail`. Une seule requête (l'accueil
        chargeait toutes les pistes de toutes les playlists)."""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT p.id, p.name, p.updated_at,
                       (SELECT COUNT(*) FROM playlist_items i
                        WHERE i.playlist_id = p.id) AS item_count,
                       f.video_id AS first_id, t.thumbnail AS first_thumbnail
                FROM playlists p
                LEFT JOIN playlist_items f ON f.playlist_id = p.id
                     AND f.position = (SELECT MIN(position) FROM playlist_items
                                       WHERE playlist_id = p.id)
                LEFT JOIN tracks t ON t.video_id = f.video_id
                ORDER BY p.updated_at DESC, p.id
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def add_to_playlist(self, playlist_id: int, refs: Sequence[TrackRef]) -> int:
        """Ajoute à la fin. Renvoie le nombre de pistes réellement ajoutées."""
        if not refs:
            return 0
        self.upsert_many(refs)
        with self._lock:
            row = self._db.execute(
                "SELECT coalesce(MAX(position), -1) AS m FROM playlist_items"
                " WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()
            position = int(row["m"]) + 1
            added = 0
            for ref in refs:
                self._db.execute(
                    "INSERT INTO playlist_items (playlist_id, position, video_id)"
                    " VALUES (?, ?, ?)",
                    (playlist_id, position, ref.video_id),
                )
                position += 1
                added += 1
            self._db.execute(
                "UPDATE playlists SET updated_at = ? WHERE id = ?",
                (time.time(), playlist_id),
            )
            self._commit()
        return added

    def playlist_items(self, playlist_id: int) -> list[TrackRef]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT t.* FROM playlist_items i
                JOIN tracks t ON t.video_id = i.video_id
                WHERE i.playlist_id = ? ORDER BY i.position
                """,
                (playlist_id,),
            ).fetchall()
        return [_row_to_ref(r) for r in rows]

    def set_playlist_order(self, playlist_id: int, video_ids: Sequence[str]) -> None:
        """Réécrit l'ordre complet d'une playlist (drag & drop)."""
        with self._lock:
            self._db.execute(
                "DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,)
            )
            self._db.executemany(
                "INSERT INTO playlist_items (playlist_id, position, video_id)"
                " VALUES (?, ?, ?)",
                [(playlist_id, i, vid) for i, vid in enumerate(video_ids)],
            )
            self._db.execute(
                "UPDATE playlists SET updated_at = ? WHERE id = ?",
                (time.time(), playlist_id),
            )
            self._commit()

    # ---- Historique ----------------------------------------------------

    def record_play(self, ref: TrackRef, seconds: float, completed: bool) -> None:
        self.upsert_track(ref)
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO history (video_id, played_at, seconds, completed)"
                " VALUES (?, ?, ?, ?)",
                (ref.video_id, now, seconds, int(completed)),
            )
            self._db.execute(
                "UPDATE tracks SET play_count = play_count + 1, last_played = ?"
                " WHERE video_id = ?",
                (now, ref.video_id),
            )
            self._commit()

    def history_entries(self, limit: int = 3000) -> list[tuple[TrackRef, float, float]]:
        """Écoutes récentes : (piste, date, secondes écoutées)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT t.*, h.played_at AS at, h.seconds AS listened FROM history h"
                " JOIN tracks t ON t.video_id = h.video_id"
                " ORDER BY h.played_at DESC LIMIT ?", (limit,)).fetchall()
        return [(_row_to_ref(r), r["at"], r["listened"]) for r in rows]

    def add_history_entry(self, ref: TrackRef, at: float, seconds: float) -> bool:
        """Écoute venue d'un autre appareil. False si déjà connue (à la seconde)."""
        with self._lock:
            known = self._db.execute(
                "SELECT 1 FROM history WHERE video_id = ? AND ABS(played_at - ?) < 1",
                (ref.video_id, at)).fetchone()
        if known is not None:
            return False
        self.upsert_track(ref)
        with self._lock:
            self._db.execute(
                "INSERT INTO history (video_id, played_at, seconds, completed)"
                " VALUES (?, ?, ?, 0)", (ref.video_id, at, seconds))
            self._db.execute(
                "UPDATE tracks SET play_count = play_count + 1,"
                " last_played = MAX(COALESCE(last_played, 0), ?) WHERE video_id = ?",
                (at, ref.video_id))
            self._commit()
        return True

    def recent(self, limit: int = 50) -> list[TrackRef]:
        """Pistes récemment jouées, dédoublonnées."""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT t.*, MAX(h.played_at) AS last
                FROM history h JOIN tracks t ON t.video_id = h.video_id
                GROUP BY h.video_id ORDER BY last DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_ref(r) for r in rows]

    def top_tracks(self, limit: int = 50, since: float | None = None) -> list[TrackRef]:
        with self._lock:
            if since is None:
                rows = self._db.execute(
                    "SELECT * FROM tracks WHERE play_count > 0"
                    " ORDER BY play_count DESC, last_played DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._db.execute(
                    """
                    SELECT t.*, COUNT(*) AS plays
                    FROM history h JOIN tracks t ON t.video_id = h.video_id
                    WHERE h.played_at >= ?
                    GROUP BY h.video_id ORDER BY plays DESC LIMIT ?
                    """,
                    (since, limit),
                ).fetchall()
        return [_row_to_ref(r) for r in rows]

    def co_played(self, video_id: str, window: float = 1800.0,
                  limit: int = 25, now: float | None = None) -> list[TrackRef]:
        """Pistes souvent écoutées autour de celle-ci.

        Base de la recommandation locale : pas de modèle, juste de la
        co-occurrence dans une fenêtre temporelle. Marche hors ligne et
        sans compte Google. Les co-écoutes récentes pèsent plus (décroissance)
        et le score est divisé par la racine du nombre d'écoutes de la piste
        voisine : un titre écouté en boucle ne co-occurre pas avec tout.
        """
        from .reco import HALF_LIFE_PLAYS
        at = time.time() if now is None else float(now)
        with self._lock:
            rows = self._db.execute(
                """
                SELECT t.*, SUM(triat_decay(:now - b.played_at, :hl)) AS score
                FROM history a
                JOIN history b
                  ON b.played_at BETWEEN a.played_at - :w AND a.played_at + :w
                 AND b.video_id != a.video_id
                JOIN tracks t ON t.video_id = b.video_id
                WHERE a.video_id = :vid AND a.played_at <= :now
                GROUP BY b.video_id
                ORDER BY score DESC, t.video_id
                LIMIT :n
                """,
                {"now": at, "hl": HALF_LIFE_PLAYS, "w": float(window),
                 "vid": video_id, "n": max(int(limit), 1) * 3},
            ).fetchall()
        ranked = sorted(
            rows, key=lambda r: (-r["score"] / math.sqrt(max(r["play_count"], 1)),
                                 r["video_id"]))
        return [_row_to_ref(r) for r in ranked[:limit]]

    def clear_history(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM history")
            # Sauts et résultats en cache dérivent de l'historique ; la liste
            # « ne plus recommander » est un choix explicite : elle reste.
            self._db.execute("DELETE FROM reco_skips")
            self._db.execute("DELETE FROM reco_cache")
            self._db.execute("UPDATE tracks SET play_count = 0, last_played = NULL")
            self._commit()

    # ---- Signaux de recommandation -------------------------------------

    def count_owned(self) -> int:
        """Nombre de pistes « à soi » (voir `_OWNED`), sans charger de ligne."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM tracks WHERE " + _OWNED.format(t="tracks")
            ).fetchone()
        return int(row[0])

    def record_skip(self, ref: TrackRef, position: float, context: str = "",
                    at: float | None = None) -> None:
        """Note un saut manuel : le seul signal négatif des recommandations."""
        if not ref.video_id:
            return
        self.upsert_track(ref)
        with self._lock:
            self._db.execute(
                "INSERT INTO reco_skips (video_id, at, position, context)"
                " VALUES (?, ?, ?, ?)",
                (ref.video_id, time.time() if at is None else float(at),
                 float(position), context))
            self._commit()

    def recent_ids(self, since: float) -> set[str]:
        """Identifiants écoutés depuis `since` (secondes epoch)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT video_id FROM history WHERE played_at >= ?",
                (since,)).fetchall()
        return {r[0] for r in rows}

    def _by_chunks(self, sql: str, ids: Sequence[str], params: tuple = ()) -> list:
        """Exécute `sql` (contenant `{marks}`) par lots de 500 identifiants."""
        ids = list(dict.fromkeys(ids))
        rows: list = []
        with self._lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                rows.extend(self._db.execute(
                    sql.format(marks=marks), (*params, *chunk)).fetchall())
        return rows

    def skip_weights(self, video_ids: Sequence[str], now: float,
                     half_life: float) -> dict[str, float]:
        """Poids (avec décroissance) des sauts de chaque piste ; les pistes
        jamais sautées sont absentes."""
        rows = self._by_chunks(
            "SELECT video_id, SUM(triat_decay(? - at, ?)) AS w FROM reco_skips"
            " WHERE at <= ? AND video_id IN ({marks}) GROUP BY video_id",
            video_ids, (now, half_life, now))
        return {r["video_id"]: float(r["w"]) for r in rows}

    def offtopic_seconds(self, video_ids: Sequence[str]) -> dict[str, float]:
        """Secondes « hors musique » connues par le cache SponsorBlock.
        Local uniquement : aucune requête réseau."""
        rows = self._by_chunks(
            "SELECT video_id, SUM(end_s - start_s) AS s FROM segments"
            " WHERE category = 'music_offtopic' AND video_id IN ({marks})"
            " GROUP BY video_id", video_ids)
        return {r["video_id"]: float(r["s"]) for r in rows}

    def track_signals(self, now: float, half_life: float,
                      skip_half_life: float) -> list[TrackSignal]:
        """Signaux de toutes les pistes « à soi » en une requête.

        L'affinité cumule les écoutes avec une décroissance, pondérées par la
        part réellement écoutée : on ne se fie pas au drapeau `completed`
        (les écoutes synchronisées l'ont toujours à 0). Les pistes écartées
        (« ne plus recommander ») sont absentes.
        """
        with self._lock:
            rows = self._db.execute(
                """
                WITH owned AS (
                  SELECT video_id FROM playlist_items
                  UNION SELECT video_id FROM favorites
                  UNION SELECT video_id FROM tracks WHERE play_count > 0),
                plays AS (
                  SELECT h.video_id,
                    SUM(triat_decay(:now - h.played_at, :hl) *
                      CASE WHEN h.completed = 1
                                OR (t.duration > 0 AND h.seconds >= 0.8 * t.duration)
                                THEN 1.0
                           WHEN t.duration <= 0 THEN 0.6
                           WHEN h.seconds >= 0.3 * t.duration THEN 0.5
                           ELSE 0.2 END) AS affinity,
                    COUNT(*) AS plays, MAX(h.played_at) AS last_at
                  FROM history h JOIN tracks t ON t.video_id = h.video_id
                  WHERE h.played_at <= :now GROUP BY h.video_id),
                sk AS (SELECT video_id,
                              SUM(triat_decay(:now - at, :shl)) AS skip_weight
                       FROM reco_skips WHERE at <= :now GROUP BY video_id)
                SELECT t.*, coalesce(p.affinity, 0) AS affinity,
                       coalesce(p.plays, 0) AS plays, p.last_at,
                       coalesce(s.skip_weight, 0) AS skip_weight,
                       EXISTS (SELECT 1 FROM favorites f
                               WHERE f.video_id = t.video_id) AS favorite,
                       EXISTS (SELECT 1 FROM playlist_items i
                               WHERE i.video_id = t.video_id) AS listed
                FROM owned o JOIN tracks t ON t.video_id = o.video_id
                LEFT JOIN plays p ON p.video_id = t.video_id
                LEFT JOIN sk s ON s.video_id = t.video_id
                WHERE NOT EXISTS (SELECT 1 FROM reco_blocklist b
                                  WHERE b.kind = 'track' AND b.key = t.video_id)
                ORDER BY t.video_id
                """,
                {"now": float(now), "hl": float(half_life),
                 "shl": float(skip_half_life)},
            ).fetchall()
            banned = {r["key"] for r in self._db.execute(
                "SELECT key FROM reco_blocklist WHERE kind = 'artist'")}
        out: list[TrackSignal] = []
        for r in rows:
            ref = _row_to_ref(r)
            if banned and artist_key(ref.artist) in banned:
                continue
            out.append(TrackSignal(
                ref=ref, affinity=float(r["affinity"]), plays=int(r["plays"]),
                last_at=r["last_at"], skip_weight=float(r["skip_weight"]),
                favorite=bool(r["favorite"]), listed=bool(r["listed"]),
                added_at=float(r["added_at"])))
        return out

    # ---- Liste « ne plus recommander » ---------------------------------

    def block(self, kind: str, key: str, label: str = "",
              at: float | None = None) -> None:
        """Écarte une piste (`kind='track'`, clé = video_id) ou un artiste
        (`kind='artist'`, clé normalisée par `artist_key`) des recommandations.
        Local : jamais synchronisé."""
        if kind not in ("track", "artist"):
            raise ValueError(f"Type inconnu : {kind!r}")
        key = artist_key(key) if kind == "artist" else (key or "").strip()
        if not key:
            return
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO reco_blocklist (kind, key, label, at)"
                " VALUES (?, ?, ?, ?)",
                (kind, key, label or "", time.time() if at is None else at))
            self._commit()

    def unblock(self, kind: str, key: str) -> None:
        if kind == "artist":
            key = artist_key(key)
        with self._lock:
            self._db.execute(
                "DELETE FROM reco_blocklist WHERE kind = ? AND key = ?",
                (kind, key))
            self._commit()

    def blocked(self) -> dict[str, dict[str, str]]:
        """`{'track': {video_id: libellé}, 'artist': {clé: libellé}}`."""
        out: dict[str, dict[str, str]] = {"track": {}, "artist": {}}
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, key, label FROM reco_blocklist ORDER BY at, key"
            ).fetchall()
        for r in rows:
            out[r["kind"]][r["key"]] = r["label"]
        return out

    # ---- Cache des résultats dérivés -----------------------------------

    def reco_cache_get(self, key: str) -> tuple[dict, float] | None:
        """`(charge utile, date de création)` ou None (absent, illisible)."""
        with self._lock:
            row = self._db.execute(
                "SELECT payload, created_at FROM reco_cache WHERE key = ?",
                (key,)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            return None
        return (payload, float(row["created_at"])) if isinstance(payload, dict) \
            else None

    def reco_cache_put(self, key: str, payload: dict,
                       at: float | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO reco_cache (key, payload, created_at)"
                " VALUES (?, ?, ?)",
                (key, json.dumps(payload, ensure_ascii=False),
                 time.time() if at is None else float(at)))
            self._commit()

    def reco_cache_clear(self, prefix: str = "", before: str | None = None) -> None:
        """Supprime les entrées dont la clé commence par `prefix` (toutes si
        vide) ; avec `before`, seulement celles dont la clé lui est
        strictement inférieure (clés datées ISO : les plus anciennes)."""
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%") \
            .replace("_", "\\_")
        sql = "DELETE FROM reco_cache WHERE key LIKE ? ESCAPE '\\'"
        params: list = [escaped + "%"]
        if before is not None:
            sql += " AND key < ?"
            params.append(before)
        with self._lock:
            self._db.execute(sql, params)
            self._commit()

    # ---- Cache SponsorBlock -------------------------------------------

    def cache_segments(self, video_id: str, segments: Sequence[Segment]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
            self._db.executemany(
                "INSERT OR REPLACE INTO segments"
                " (video_id, uuid, category, start_s, end_s) VALUES (?, ?, ?, ?, ?)",
                [(video_id, s.uuid or f"{s.category}:{s.start}", s.category,
                  s.start, s.end) for s in segments],
            )
            self._db.execute(
                "INSERT OR REPLACE INTO segment_fetches VALUES (?, ?)",
                (video_id, time.time()),
            )
            self._commit()

    def cached_segments(self, video_id: str, max_age: float = 7 * 86400
                        ) -> list[Segment] | None:
        """None = jamais interrogé ou cache périmé. Liste vide = pas de segment."""
        with self._lock:
            row = self._db.execute(
                "SELECT fetched_at FROM segment_fetches WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            if row is None or (time.time() - row["fetched_at"]) > max_age:
                return None
            rows = self._db.execute(
                "SELECT * FROM segments WHERE video_id = ? ORDER BY start_s",
                (video_id,),
            ).fetchall()
        return [Segment(start=r["start_s"], end=r["end_s"],
                        category=r["category"], uuid=r["uuid"]) for r in rows]

    # ---- Maintenance ---------------------------------------------------

    def wipe(self) -> None:
        """Efface toutes les données utilisateur (bouton « effacer mes données »)."""
        with self._lock:
            existing = {r["name"] for r in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table in ("history", "playlist_items", "playlists", "favorites",
                          "segments", "segment_fetches", "tracks", "sync_tombstones",
                          "lyrics", "downloads", "reco_skips", "reco_blocklist",
                          "reco_cache"):
                if table in existing:
                    self._db.execute(f"DELETE FROM {table}")
            self._db.execute("INSERT INTO tracks_fts(tracks_fts) VALUES ('rebuild')")
            self._commit()
            self._db.execute("VACUUM")
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def stats(self) -> dict:
        with self._lock:
            row = self._db.execute(
                """
                SELECT (SELECT COUNT(*) FROM tracks)     AS tracks,
                       (SELECT COUNT(*) FROM favorites)  AS favorites,
                       (SELECT COUNT(*) FROM playlists)  AS playlists,
                       (SELECT COUNT(*) FROM history)    AS plays,
                       (SELECT coalesce(SUM(seconds), 0) FROM history) AS seconds
                """
            ).fetchone()
        return dict(row)

    # ---- Comptages (sans charger les lignes) --------------------------

    def count_top_tracks(self, since: float) -> int:
        """Nombre de pistes écoutées depuis `since` (voir `top_tracks`)."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(DISTINCT h.video_id) FROM history h"
                " JOIN tracks t ON t.video_id = h.video_id"
                " WHERE h.played_at >= ?", (since,)
            ).fetchone()
        return int(row[0])

    def count_unplayed(self, owned: bool = False) -> int:
        """Nombre de pistes jamais lues (voir `unplayed`)."""
        mine = " AND " + _OWNED.format(t="tracks") if owned else ""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM tracks WHERE play_count = 0" + mine
            ).fetchone()
        return int(row[0])

    def count_added_since(self, since: float, owned: bool = False) -> int:
        """Nombre de pistes entrées après `since` (voir `added_since`)."""
        mine = " AND " + _OWNED.format(t="tracks") if owned else ""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM tracks WHERE added_at >= ?" + mine,
                (since,)
            ).fetchone()
        return int(row[0])

"""Sauvegarde et restauration de la bibliothèque (library.db).

Contenu : uniquement library.db (pistes, favoris, playlists, historique...).
Aucun cookie ni secret : les cookies YouTube vivent dans le trousseau du
système et le schéma de library.db n'a aucune table pour eux. Les
sauvegardes peuvent donc être copiées sans exposer de session.

La copie passe par `sqlite3.Connection.backup`, cohérente même si la base
est ouverte en WAL par l'application.

`restore_backup` ne doit être appelé que l'application FERMÉE : remplacer
le fichier sous les pieds d'une connexion ouverte corromprait la base. Un
fichier `library.db-wal` non vide est pris comme signe d'une base ouverte
et la restauration est alors refusée (sauf `force=True`).
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import library, paths

_NAME = re.compile(r"^library-(\d{8}-\d{6})\.db$")
_FMT = "%Y%m%d-%H%M%S"


class BackupError(Exception):
    """Sauvegarde ou restauration impossible (message en français)."""


def backup_dir() -> Path:
    path = paths.state_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _integrity(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        return bool(row) and row[0] == "ok"
    except sqlite3.Error:
        return False


def create_backup(library_path: Path | None = None, dest_dir: Path | None = None,
                  now: datetime | None = None) -> Path:
    src = Path(library_path) if library_path else paths.db_path()
    if not src.is_file():
        raise BackupError(f"Bibliothèque introuvable : {src}")
    dest = Path(dest_dir) if dest_dir else backup_dir()
    dest.mkdir(parents=True, exist_ok=True)
    dest.chmod(0o700)
    stamp = (now or datetime.now()).strftime(_FMT)
    final = dest / f"library-{stamp}.db"
    tmp = dest / f"library-{stamp}.db.tmp"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
            # La copie hérite du mode WAL de la source : on la repasse en
            # journal classique pour qu'elle tienne en un seul fichier (sinon
            # des `-wal`/`-shm` orphelins restent à côté après le renommage).
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
            source.close()
        tmp.chmod(0o600)
        if not _integrity(tmp):
            raise BackupError("La copie ne passe pas le contrôle d'intégrité.")
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        for side in ("-wal", "-shm", "-journal"):
            Path(f"{tmp}{side}").unlink(missing_ok=True)
    return final


def list_backups(dest_dir: Path | None = None) -> list[Path]:
    dest = Path(dest_dir) if dest_dir else backup_dir()
    if not dest.is_dir():
        return []
    found = [p for p in dest.iterdir() if _NAME.match(p.name) and p.is_file()]
    return sorted(found, key=lambda p: p.name, reverse=True)


def _stamp(path: Path) -> datetime:
    return datetime.strptime(_NAME.match(path.name).group(1), _FMT)


def rotate(dest_dir: Path | None = None, keep_days: int = 7, keep_min: int = 3,
           now: datetime | None = None) -> list[Path]:
    """Supprime les sauvegardes de plus de `keep_days` jours en gardant
    toujours les `keep_min` plus récentes. Renvoie les fichiers supprimés."""
    limit = (now or datetime.now()) - timedelta(days=keep_days)
    removed = []
    for path in list_backups(dest_dir)[keep_min:]:
        if _stamp(path) < limit:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def maybe_auto_backup(now: datetime | None = None,
                      dest_dir: Path | None = None) -> Path | None:
    """Au plus une sauvegarde par 24 h, puis rotation. None si rien à faire."""
    current = now or datetime.now()
    existing = list_backups(dest_dir)
    if existing and current - _stamp(existing[0]) < timedelta(hours=24):
        return None
    created = create_backup(dest_dir=dest_dir, now=current)
    rotate(dest_dir, now=current)
    return created


def database_looks_open(library_path: Path) -> bool:
    wal = Path(str(library_path) + "-wal")
    try:
        return wal.stat().st_size > 0
    except OSError:
        return False


def restore_backup(path: Path, library_path: Path | None = None,
                   now: datetime | None = None, force: bool = False) -> Path:
    """Restaure `path` sur la bibliothèque (application fermée !).

    L'ancienne base est conservée en `library.db.avant-restauration-<date>`.
    Renvoie le chemin de cette copie de côté (ou de la base restaurée si
    aucune base n'existait).
    """
    src = Path(path)
    if src.is_symlink() or not src.is_file():
        raise BackupError("La source doit être un fichier ordinaire (pas un lien).")
    if not _integrity(src):
        raise BackupError("Ce fichier n'est pas une base SQLite saine.")
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        raise BackupError("Ce fichier n'a pas de table schema_version : "
                          "ce n'est pas une sauvegarde de Triat.") from None
    version = row[0] if row else None
    if version is None:
        raise BackupError("Version de schéma illisible dans la sauvegarde.")
    if version > library.SCHEMA_VERSION:
        raise BackupError(
            f"Cette sauvegarde (schéma {version}) est plus récente que ta "
            f"version de Triat (schéma {library.SCHEMA_VERSION}) : mets Triat à jour.")
    target = Path(library_path) if library_path else paths.db_path()
    if not force and database_looks_open(target):
        raise BackupError("La bibliothèque semble ouverte (library.db-wal non "
                          "vide) : ferme Triat avant de restaurer.")
    stamp = (now or datetime.now()).strftime(_FMT)
    aside = target.with_name(f"{target.name}.avant-restauration-{stamp}")
    tmp = target.with_name(target.name + ".restore.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        tmp.chmod(0o600)
        had_db = target.exists()
        if had_db:
            os.replace(target, aside)
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return aside if had_db else target

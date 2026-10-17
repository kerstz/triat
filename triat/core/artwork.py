"""Récupération et cache des pochettes.

Les images viennent d'URL fournies par les API YouTube : chacune passe par
`safe_media_url()` avant d'être demandée (docs/security/ §6). Le
téléchargement est bloquant — à appeler depuis un thread.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

from .net import MEDIA_HOST_SUFFIXES, USER_AGENT_FALLBACK, UnsafeURL, safe_media_url, urlopen_checked
from .paths import cache_dir

log = logging.getLogger(__name__)

MAX_BYTES = 4 * 1024 * 1024      # une pochette raisonnable
TIMEOUT = 12.0


def covers_dir() -> Path:
    path = cache_dir() / "covers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_path(url: str) -> Path:
    """Chemin de cache déterminé par l'URL, jamais par un nom fourni."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return covers_dir() / f"{digest}.img"


def fetch(url: str) -> Path | None:
    """Télécharge une pochette si besoin et renvoie son chemin local.

    Renvoie None si l'URL est refusée ou le téléchargement échoue : une
    pochette manquante n'est jamais une erreur bloquante.
    """
    if not url:
        return None
    try:
        safe_media_url(url)
    except UnsafeURL as exc:
        log.info("Pochette refusée : %s", exc)
        return None

    # Pochettes SoundCloud : jamais dans `covers/` (voir plus bas).
    if is_soundcloud_host(url):
        return _fetch_soundcloud(url)

    path = cache_path(url)
    try:
        if path.stat().st_size > 0:
            # `relatime` rend `atime` peu fiable : l'ancienneté d'usage du
            # cache est portée par `mtime`, rafraîchi à chaque accès.
            os.utime(path)
            return path
    except OSError:
        pass

    data = _download(url)
    if data is None:
        return None

    # Écriture atomique dans un fichier temporaire propre à cet appel : la
    # pochette et la matrice de points demandent souvent la même image en
    # même temps, un nom partagé mélangerait les deux écritures.
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".part")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if not image_is_sane(temporary):
            return None
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        log.debug("Cache de pochette impossible : %s", exc)
        return None
    finally:
        # Jamais de `.part` orphelin (disque plein, image refusée).
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    _maybe_prune()
    return path


def _download(url: str) -> bytes | None:
    """Octets d'une image valide (taille et signature), ou None."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT_FALLBACK})
    try:
        with urlopen_checked(request, TIMEOUT, MEDIA_HOST_SUFFIXES) as response:
            data = response.read(MAX_BYTES + 1)
    except Exception as exc:
        log.debug("Pochette indisponible (%s) : %s", url[:60], exc)
        return None
    if not data or len(data) > MAX_BYTES or not looks_like_image(data):
        return None
    return data


# ---- Pochettes SoundCloud ----------------------------------------------
#
# Les images `sndcdn.com` ne sont jamais écrites dans `covers/` : c'est une
# rétention persistante de contenu tiers, proscrite (spec §7.3, point 5).
# Le chargeur d'images attend un chemin : le fichier vit donc dans le
# dossier temporaire du système, indexé par un LRU borné en mémoire. Il est
# supprimé à l'éviction, à la fermeture et au vidage du cache.

SC_MEMORY_LIMIT = 128
_sc_files: OrderedDict[str, Path] = OrderedDict()
_sc_lock = threading.Lock()


def is_soundcloud_host(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host == "sndcdn.com" or host.endswith(".sndcdn.com")


def _fetch_soundcloud(url: str) -> Path | None:
    with _sc_lock:
        cached = _sc_files.get(url)
        if cached is not None and cached.exists():
            _sc_files.move_to_end(url)
            return cached
        _sc_files.pop(url, None)

    data = _download(url)
    if data is None:
        return None
    try:
        fd, name = tempfile.mkstemp(prefix="triat-sc-", suffix=".img")
    except OSError as exc:
        log.debug("Pochette SoundCloud impossible : %s", exc)
        return None
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        sane = image_is_sane(path)
    except OSError as exc:
        log.debug("Pochette SoundCloud illisible : %s", exc)
        sane = False
    if not sane:
        path.unlink(missing_ok=True)
        return None

    with _sc_lock:
        _sc_files[url] = path
        _sc_files.move_to_end(url)
        while len(_sc_files) > SC_MEMORY_LIMIT:
            _old_url, old = _sc_files.popitem(last=False)
            old.unlink(missing_ok=True)
    return path


def _sc_clear() -> None:
    with _sc_lock:
        for path in _sc_files.values():
            path.unlink(missing_ok=True)
        _sc_files.clear()


atexit.register(_sc_clear)


# Signatures des formats servis par YouTube (JPEG, PNG, WebP).
_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")


def looks_like_image(data: bytes) -> bool:
    return data.startswith(_MAGIC) or (data[:4] == b"RIFF"
                                       and data[8:12] == b"WEBP")


# Au-delà, une image de 4 Mo peut se décompresser en plusieurs Go de
# pixels (« bombe de décompression ») : on lit l'en-tête avant de décoder.
MAX_SIDE = 4096


def image_is_sane(path) -> bool:
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        info = GdkPixbuf.Pixbuf.get_file_info(str(path))
    except Exception as exc:
        log.debug("En-tête d'image illisible : %s", exc)
        return False
    if info is None or info[0] is None:
        return False
    _format, width, height = info
    return 0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE


# Plafond du cache ; au-delà on retire les pochettes les moins récentes.
CACHE_LIMIT = 200 * 1024 * 1024
_PRUNE_EVERY = 50
_writes = 0


def _maybe_prune() -> None:
    global _writes
    _writes += 1
    if _writes % _PRUNE_EVERY:
        return
    prune(CACHE_LIMIT)


# Un `.part` plus vieux que cela est l'orphelin d'une écriture interrompue.
PART_MAX_AGE = 3600.0


def prune(limit: int) -> int:
    """Ramène le cache sous `limit` octets (les moins récemment utilisées
    d'abord) et retire les `.part` orphelins. Renvoie le nombre de fichiers
    retirés."""
    removed = 0
    try:
        folder = covers_dir()
        now = time.time()
        for part in folder.glob("*.part"):
            try:
                if now - part.stat().st_mtime > PART_MAX_AGE:
                    part.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        files = []
        for f in folder.glob("*.img"):
            try:
                st = f.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, f))
    except OSError:
        return removed
    total = sum(size for _t, size, _f in files)
    for _mtime, size, f in sorted(files):
        if total <= limit:
            break
        f.unlink(missing_ok=True)
        total -= size
        removed += 1
    return removed


def clear_cache() -> None:
    for f in covers_dir().glob("*.img"):
        f.unlink(missing_ok=True)
    _sc_clear()

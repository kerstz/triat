"""Téléchargements hors-ligne (TODO §4.9).

yt-dlp extrait l'audio, ffmpeg pose les tags et la pochette. Les segments
SponsorBlock peuvent être retirés du fichier produit, pour obtenir une
piste propre plutôt qu'une vidéo avec son intro parlée.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp

from .extractor import JS_RUNTIMES
from .models import TrackRef
from .paths import downloads_dir

log = logging.getLogger(__name__)

FORMATS = {
    "opus": ("bestaudio[acodec=opus]/bestaudio/best", "opus"),
    "m4a": ("bestaudio[ext=m4a]/bestaudio/best", "m4a"),
    "mp3": ("bestaudio/best", "mp3"),
}
QUALITY_BITRATE = {"low": "96", "normal": "128", "high": "192", "max": "0"}

# Caractères interdits ou pénibles dans un nom de fichier.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

PENDING, RUNNING, DONE, FAILED, CANCELLED = (
    "pending", "running", "done", "failed", "cancelled")


def safe_name(value: str, fallback: str = "Inconnu") -> str:
    """Nom de fichier sûr : ni séparateur, ni caractère de contrôle."""
    cleaned = _UNSAFE.sub("", (value or "").strip()).strip(". ")
    # Pas de nom caché ni de point final ; le repli est nettoyé de même
    # (un identifiant venu du réseau ne doit jamais porter un `../`).
    if not cleaned and fallback:
        cleaned = _UNSAFE.sub("", fallback).strip(". ") or "Inconnu"
    return cleaned[:80]


@dataclass
class Task:
    """Une piste à télécharger."""

    ref: TrackRef
    state: str = PENDING
    progress: float = 0.0          # 0..1
    path: Path | None = None
    error: str = ""
    started_at: float = field(default_factory=time.time)

    @property
    def label(self) -> str:
        return self.ref.label


class DownloadManager:
    """File de téléchargement, un élément à la fois.

    Séquentiel volontairement : plusieurs extractions simultanées font
    réagir YouTube par du throttling.
    """

    def __init__(self, engine=None, settings=None, library=None,
                 sponsorblock=None) -> None:
        self._engine = engine
        self._settings = settings
        self._library = library
        self._sponsorblock = sponsorblock
        self._queue: list[Task] = []
        self._cookie_manager = None
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._paused = threading.Event()
        self.on_change = None          # callback(task) depuis un thread

    # ---- File ------------------------------------------------------------

    @property
    def tasks(self) -> list[Task]:
        with self._lock:
            return list(self._queue)

    @property
    def active(self) -> Task | None:
        return next((t for t in self.tasks if t.state == RUNNING), None)

    def add(self, refs) -> int:
        """Ajoute des pistes, en ignorant celles déjà présentes ou faites."""
        added = 0
        with self._lock:
            known = {t.ref.video_id for t in self._queue
                     if t.state in (PENDING, RUNNING, DONE)}
            for ref in refs:
                if ref.video_id in known:
                    continue
                if self.local_path(ref) is not None:
                    continue
                self._queue.append(Task(ref=ref))
                known.add(ref.video_id)
                added += 1
        if added:
            self._ensure_worker()
        return added

    def cancel(self, video_id: str) -> None:
        with self._lock:
            for task in self._queue:
                if task.ref.video_id == video_id and task.state == PENDING:
                    task.state = CANCELLED
                elif task.ref.video_id == video_id and task.state == RUNNING:
                    self._cancel.set()

    def clear_finished(self) -> None:
        with self._lock:
            self._queue = [t for t in self._queue
                           if t.state in (PENDING, RUNNING)]

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()
        self._ensure_worker()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    # ---- Fichiers --------------------------------------------------------

    def target_dir(self, ref: TrackRef) -> Path:
        """Dossier de destination : Artiste/Album, ou Artiste seul."""
        root = downloads_dir()
        artist = safe_name(ref.artist, "Artiste inconnu")
        album = safe_name(ref.album or "", "")
        return root / artist / album if album else root / artist

    @staticmethod
    def _stems(ref: TrackRef) -> list[str]:
        """Noms de fichier possibles pour cette piste."""
        return [safe_name(ref.title, ref.video_id)]

    def local_path(self, ref: TrackRef) -> Path | None:
        """Fichier déjà téléchargé pour cette piste, s'il existe.

        L'index par `video_id` fait foi : le titre et l'album d'une piste
        sont corrigés après sa première lecture, pas son identifiant."""
        library = self._library
        if library is not None:
            recorded = library.download_path(ref.video_id)
            if recorded:
                path = Path(recorded)
                try:
                    if path.is_file() and path.stat().st_size > 0:
                        return path
                except OSError:
                    pass
                library.forget_download(ref.video_id)
        found = self._probe(ref)
        if found is not None and library is not None:
            library.record_download(ref.video_id, str(found))
        return found

    def _probe(self, ref: TrackRef) -> Path | None:
        folder = self.target_dir(ref)
        if not folder.is_dir():
            return None
        for stem in self._stems(ref):
            for extension in ("opus", "m4a", "mp3", "webm", "ogg"):
                candidate = folder / f"{stem}.{extension}"
                try:
                    if not (candidate.is_file() and candidate.stat().st_size > 0):
                        continue
                except OSError:
                    continue
                # Même artiste et même titre, autre vidéo : le fichier est
                # déjà celui d'une autre piste, pas de l'index à corrompre.
                if self._library is not None:
                    owner = self._library.download_owner(str(candidate))
                    if owner is not None and owner != ref.video_id:
                        continue
                return candidate
        return None

    # ---- Exécution -------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _notify(self, task: Task) -> None:
        if self.on_change is not None:
            try:
                self.on_change(task)
            except Exception as exc:
                log.debug("Notification de téléchargement : %s", exc)

    def _next_task(self) -> Task | None:
        with self._lock:
            return next((t for t in self._queue if t.state == PENDING), None)

    def _run(self) -> None:
        while not self._paused.is_set():
            task = self._next_task()
            if task is None:
                return
            task.state = RUNNING
            task.progress = 0.0
            self._cancel.clear()
            self._notify(task)
            try:
                path = self._download(task)
            except Exception as exc:
                task.state = CANCELLED if self._cancel.is_set() else FAILED
                task.error = str(exc).strip().splitlines()[0] if str(exc) else ""
                log.info("Téléchargement échoué (%s) : %s",
                         task.ref.video_id, task.error)
            else:
                task.state = DONE
                task.path = path
                task.progress = 1.0
            self._notify(task)

    def _options(self, task: Task) -> dict:
        settings = self._settings
        fmt = (settings.get("download_format") if settings else None) or "opus"
        selector, extension = FORMATS.get(fmt, FORMATS["opus"])
        quality = (settings.get("audio_quality") if settings else None) or "high"

        folder = self.target_dir(task.ref)
        folder.mkdir(parents=True, exist_ok=True)
        stem = self._stems(task.ref)[0]

        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": extension,
            "preferredquality": QUALITY_BITRATE.get(quality, "192"),
        }, {"key": "FFmpegMetadata", "add_metadata": True}]
        # La pochette ne s'embarque proprement que dans certains conteneurs.
        if extension in ("m4a", "mp3"):
            postprocessors.append({"key": "EmbedThumbnail"})

        options = {
            "format": selector,
            # `outtmpl` est un gabarit : un « % » du titre serait interprété
            # (« 50%(id)s off » → « 50abc off ») et le fichier introuvable.
            "outtmpl": str(folder / f"{stem}".replace("%", "%%")) + ".%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # `quiet` ne suffit pas : la barre de progression du
            # téléchargeur écrit malgré tout sur la sortie standard.
            "noprogress": True,
            "consoletitle": False,
            "writethumbnail": extension in ("m4a", "mp3"),
            "postprocessors": postprocessors,
            "progress_hooks": [lambda d: self._on_progress(task, d)],
            # Jamais de téléchargeur externe : une CVE 2026 y fuite les
            # cookies vers des hôtes tiers (docs/security/).
            "external_downloader": None,
            "js_runtimes": {k: dict(v) for k, v in JS_RUNTIMES.items()},
        }
        if settings is not None and settings.get("download_cut_sponsorblock", True):
            options["postprocessors"] = [{
                "key": "SponsorBlock",
                "categories": ["music_offtopic", "sponsor", "selfpromo", "intro",
                               "outro", "interaction", "preview"],
            }, {
                "key": "ModifyChapters",
                "remove_sponsor_segments": ["music_offtopic", "sponsor",
                                            "selfpromo", "intro", "outro",
                                            "interaction", "preview"],
            }] + options["postprocessors"]
        return options

    def _on_progress(self, task: Task, data: dict) -> None:
        if self._cancel.is_set():
            raise yt_dlp.utils.DownloadError("Annulé")
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            done = data.get("downloaded_bytes") or 0
            if total:
                task.progress = min(0.99, done / total)
                self._notify(task)
        elif data.get("status") == "finished":
            task.progress = 0.99
            self._notify(task)

    def _download(self, task: Task) -> Path:
        options = self._options(task)
        extractor = getattr(self._engine, "extractor", None)
        if extractor is not None and hasattr(extractor, "download"):
            # Même repli entre clients YouTube que la lecture.
            extractor.download(task.ref.url, options)
        else:
            with yt_dlp.YoutubeDL(options) as ydl:
                if self._cookie_manager is not None:
                    self._cookie_manager.load_into(ydl)
                ydl.extract_info(task.ref.url, download=True)
        path = self._probe(task.ref)
        if path is None:
            raise RuntimeError("Fichier introuvable après téléchargement")
        if self._library is not None:
            self._library.upsert_track(task.ref)
            self._library.record_download(task.ref.video_id, str(path))
        return path

    def attach_cookies(self, cookie_manager) -> None:
        self._cookie_manager = cookie_manager

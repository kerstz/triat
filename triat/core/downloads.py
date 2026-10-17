"""Téléchargements hors-ligne (TODO §4.9).

yt-dlp extrait l'audio, ffmpeg pose les tags et la pochette. Les segments
SponsorBlock peuvent être retirés du fichier produit, pour obtenir une
piste propre plutôt qu'une vidéo avec son intro parlée.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp

from .extractor import JS_RUNTIMES
from .models import TrackRef
from .paths import downloads_dir
from .sponsorblock import CATEGORIES, SKIP

log = logging.getLogger(__name__)

FORMATS = {
    "opus": ("bestaudio[acodec=opus]/bestaudio/best", "opus"),
    "m4a": ("bestaudio[ext=m4a]/bestaudio/best", "m4a"),
    "mp3": ("bestaudio/best", "mp3"),
}
QUALITY_BITRATE = {"low": "96", "normal": "128", "high": "192", "max": "0"}

# Caractères interdits ou pénibles dans un nom de fichier.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Extensions des fichiers que Triat produit (audio + vignettes laissées par
# yt-dlp) : seuls ceux-là sont supprimés par `delete_all_files`.
_MANAGED_EXTENSIONS = frozenset({
    ".opus", ".m4a", ".mp3", ".webm", ".ogg",
    ".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl"})
_TEMP_EXTENSIONS = frozenset({".part", ".ytdl"})
# Nommage de Triat : « titre [id YouTube de 11 caractères] ».
_TRIAT_STEM = re.compile(r" \[[A-Za-z0-9_-]{11}\]$")


def is_downloadable_id(video_id: str) -> bool:
    """Seuls les identifiants YouTube se téléchargent : un id d'une autre
    source (« soundcloud:… », tout id contenant « : ») est refusé."""
    return bool(video_id) and ":" not in video_id \
        and not video_id.startswith("soundcloud")


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
                if not is_downloadable_id(ref.video_id):
                    continue            # SoundCloud : jamais téléchargé
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

    def cancel_all(self) -> None:
        """Annule le téléchargement en cours et toute la file en attente.

        Le fil de travail existant est respecté : il constate l'annulation
        à la prochaine progression et reprend la file (désormais vide)."""
        with self._lock:
            for task in self._queue:
                if task.state == PENDING:
                    task.state = CANCELLED
                elif task.state == RUNNING:
                    self._cancel.set()
            snapshot = list(self._queue)
        for task in snapshot:
            if task.state == CANCELLED:
                self._notify(task)

    def delete_all_files(self) -> int:
        """Supprime les fichiers téléchargés par Triat ; renvoie leur nombre.

        Le dossier est configurable et peut contenir la musique personnelle
        de l'utilisateur : n'est supprimé que (a) ce que l'index `downloads`
        connaît, avec les vignettes sœurs de même nom, (b) les fichiers dont
        le nom se termine par « [id YouTube] » (nommage de Triat), (c) leurs
        `.part`/`.ytdl`. Rien hors du dossier, aucun lien symbolique suivi,
        aucun dossier supprimé (impossible de savoir lesquels Triat a créés)."""
        root = downloads_dir()
        if not root.is_dir() or root.is_symlink():
            return 0
        library = self._library
        indexed: set[tuple[str, str]] = set()      # (dossier, stem) connus
        if library is not None:
            for video_id in library.downloaded_ids():
                recorded = library.download_path(video_id)
                if recorded:
                    path = Path(recorded)
                    indexed.add((str(path.parent), path.stem))
        removed = 0
        for folder, _dirs, names in os.walk(root, followlinks=False):
            for name in names:
                path = Path(folder) / name
                suffix = path.suffix.lower()
                if suffix not in _MANAGED_EXTENSIONS:
                    continue
                stem = path.stem
                if suffix in _TEMP_EXTENSIONS:        # « x.opus.part »
                    stem = Path(stem).stem
                if (str(path.parent), stem) not in indexed \
                        and not _TRIAT_STEM.search(stem):
                    continue
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    log.debug("Suppression impossible (%s) : %s", path, exc)
        if library is not None:
            for video_id in library.downloaded_ids():
                library.forget_download(video_id)
        return removed

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
        """Noms de fichier possibles, le plus récent d'abord.

        « titre [video_id] » évite qu'un même titre écrase une autre vidéo ;
        l'ancien nom (titre seul) reste en second pour les fichiers déjà
        téléchargés."""
        title = safe_name(ref.title, ref.video_id)
        return [f"{title} [{safe_name(ref.video_id)}]", title]

    def local_path(self, ref: TrackRef) -> Path | None:
        """Fichier déjà téléchargé pour cette piste, s'il existe.

        L'index par `video_id` fait foi : le titre et l'album d'une piste
        sont corrigés après sa première lecture, pas son identifiant.

        Un fichier trouvé sur disque est réindexé : c'est voulu (fichier
        présent = piste lisible hors ligne, même après un effacement de
        l'index qui aurait gardé les fichiers). Les ids non YouTube
        (SoundCloud) n'ont jamais de fichier local : aucune requête."""
        if not is_downloadable_id(ref.video_id):
            return None
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
        with self._lock:
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

    def _claim_next(self) -> Task | None:
        """Prend la prochaine tâche (passée à RUNNING), ou None. Quand le
        worker doit s'arrêter (file vide, pause), la décision de sortie et
        `self._worker = None` se font dans le même verrou que le test de la
        file : un `add()` concurrent voit alors soit la tâche prise ici, soit
        un worker absent à relancer, jamais un worker mourant."""
        with self._lock:
            task = None
            if not self._paused.is_set():
                task = next((t for t in self._queue if t.state == PENDING), None)
            if task is None:
                if self._worker is threading.current_thread():
                    self._worker = None
                return None
            task.state = RUNNING
            task.progress = 0.0
            self._cancel.clear()
            return task

    def _run(self) -> None:
        while True:
            task = self._claim_next()
            if task is None:
                return
            self._notify(task)
            try:
                path = self._download(task)
            except Exception as exc:
                error = str(exc).strip().splitlines()[0] if str(exc) else ""
                with self._lock:
                    task.state = CANCELLED if self._cancel.is_set() else FAILED
                    task.error = error
                log.debug("Téléchargement échoué (%s) : %s",
                         task.ref.video_id, error)
            else:
                with self._lock:
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
            categories = self._cut_categories()
            if categories:
                options["postprocessors"] = [{
                    "key": "SponsorBlock",
                    "categories": categories,
                }, {
                    "key": "ModifyChapters",
                    "remove_sponsor_segments": categories,
                }] + options["postprocessors"]
        return options

    def _cut_categories(self) -> list[str]:
        """Catégories SponsorBlock à couper du fichier : celles dont la
        politique est « sauter ». « Demander » et « ignorer » ne coupent
        rien : un fichier ne peut pas poser la question."""
        policies = (self._settings.get("sponsorblock_categories")
                    if self._settings else None) or {}
        return [cat for cat in CATEGORIES if policies.get(cat) == SKIP]

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

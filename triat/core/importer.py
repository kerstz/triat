"""Import de playlists depuis un fichier.

Formats acceptés : liste d'URL YouTube (une par ligne), M3U/M3U8, ou
identifiants bruts. Les commentaires et lignes vides sont ignorés.

Les métadonnées sont récupérées par **oEmbed** plutôt que par yt-dlp :
l'endpoint est public, sans clé ni quota, et répond en ~0,2 s — contre
plusieurs secondes pour une extraction complète. Sur 200 pistes, c'est la
différence entre cinq secondes et sept minutes. L'URL de flux, elle, reste
résolue au dernier moment par le moteur.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .constants import APP_USER_AGENT
from .models import TrackRef
from .soundcloud import ScPlaylist, ScTrack
from .net import check_url, urlopen_checked

log = logging.getLogger(__name__)

OEMBED_URL = "https://www.youtube.com/oembed"
USER_AGENT = APP_USER_AGENT
MAX_WORKERS = 8
TIMEOUT = 10.0

# Un identifiant de vidéo YouTube : 11 caractères de l'alphabet base64url.
_ID = r"[A-Za-z0-9_-]{11}"
# L'id est ancré : un identifiant de 12 caractères ou plus n'est pas tronqué
# en 11 (il serait faux), il est refusé.
_END = r"(?![A-Za-z0-9_-])"
_ID_PATTERNS = (
    re.compile(rf"[?&]v=({_ID}){_END}"),
    re.compile(rf"youtu\.be/({_ID}){_END}"),
    re.compile(rf"/shorts/({_ID}){_END}"),
    re.compile(rf"/embed/({_ID}){_END}"),
    re.compile(rf"/live/({_ID}){_END}"),
)
_BARE_ID = re.compile(rf"^({_ID})$")

# Liens SoundCloud. Délimiteurs exclus : espace, virgule, guillemets (les
# exports csv/json contiennent l'URL au milieu d'une ligne).
_SEG = r"[\w%.~-]+"
_SC_SET = re.compile(
    rf"https?://(?:www\.|m\.)?soundcloud\.com/({_SEG})/sets/({_SEG})", re.I)
_SC_TRACK = re.compile(
    rf"https?://(?:www\.|m\.)?soundcloud\.com/({_SEG})/({_SEG})", re.I)
_SC_SHORT = re.compile(r"https?://on\.soundcloud\.com/([A-Za-z0-9]+)", re.I)
# Pages d'un profil qui ne sont pas des pistes.
_SC_NOT_TRACK = frozenset({
    "sets", "tracks", "albums", "likes", "reposts", "following", "followers",
    "popular-tracks", "comments", "playlists", "spotlight", "you", "discover",
    "search", "stream", "upload", "pages", "charts", "mobile", "terms-of-use",
})
# Plafond de liens SoundCloud résolus par import (séquentiel : on reste poli).
MAX_SC_LINKS = 200

# Mentions que les chaînes collent aux titres et qui n'apportent rien.
_NOISE = re.compile(
    r"""\s*[\(\[]\s*(?:
        official\s*(?:music\s*)?(?:video|audio|visualizer|lyric\s*video)?
      | clip\s*officiel | video\s*officielle? | audio\s*officiel
      | lyrics? (?:\s*video)? | paroles
      | hd | hq | 4k | full\s*hd | remaster(?:ed)?(?:\s*\d{4})?
      | visuali[sz]er | mv | m/v
    )\s*[\)\]]""",
    re.IGNORECASE | re.VERBOSE,
)
_TRAILING = re.compile(r"\s*[-–—]\s*(?:official\s*video|clip\s*officiel)\s*$",
                       re.IGNORECASE)


class ImportError_(ValueError):
    """Erreur d'import lisible par l'utilisateur."""


def extract_soundcloud_url(line: str) -> str | None:
    """URL SoundCloud normalisée (https, sans « m. », sans requête), ou None.

    Les liens courts on.soundcloud.com sont renvoyés tels quels : on ne suit
    jamais la redirection nous-mêmes, `resolve_permalink` s'en charge.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    found = _SC_SHORT.search(line)
    if found:
        return f"https://on.soundcloud.com/{found.group(1)}"
    found = _SC_SET.search(line)
    if found:
        return f"https://soundcloud.com/{found.group(1)}/sets/{found.group(2)}"
    found = _SC_TRACK.search(line)
    if found and found.group(2).lower() not in _SC_NOT_TRACK \
            and found.group(1).lower() not in _SC_NOT_TRACK:
        return f"https://soundcloud.com/{found.group(1)}/{found.group(2)}"
    return None


def extract_video_id(line: str) -> str | None:
    """Tire un identifiant de vidéo d'une ligne, ou None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    for pattern in _ID_PATTERNS:
        found = pattern.search(line)
        if found:
            return found.group(1)
    bare = _BARE_ID.match(line)
    return bare.group(1) if bare else None


def clean_title(raw: str) -> str:
    """Retire les mentions parasites d'un titre YouTube."""
    cleaned = _NOISE.sub("", raw or "")
    cleaned = _TRAILING.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" -–—|") or (raw or "").strip()


def split_artist_title(title: str, channel: str) -> tuple[str, str]:
    """Sépare « Artiste - Titre », en repli sur le nom de la chaîne.

    Les chaînes « Topic » de YouTube portent le nom de l'artiste suivi de
    « - Topic » : on le nettoie aussi.
    """
    channel = re.sub(r"\s*-\s*Topic\s*$", "", channel or "").strip()
    cleaned = clean_title(title)

    for separator in (" - ", " – ", " — ", " | "):
        if separator in cleaned:
            left, right = cleaned.split(separator, 1)
            left, right = left.strip(), right.strip()
            if not (left and right):
                continue
            # « Titre - Artiste » existe aussi : si le côté droit est le nom
            # de la chaîne, c'est lui l'artiste, pas le côté gauche.
            def normalise(value):
                return re.sub(r"\W+", "", value).lower()
            if channel and normalise(right) == normalise(channel):
                return right, left
            return left, right
    return (channel, cleaned) if channel else ("", cleaned)


def _read_lines(path: str | Path) -> list[str]:
    file_path = Path(path).expanduser()
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ImportError_(f"Lecture impossible : {exc.strerror}") from exc
    return text.splitlines()


def parse_entries(path: str | Path) -> list[tuple[str, str]]:
    """Entrées d'un fichier, sans doublon, dans l'ordre.

    Chaque entrée est ("youtube", id) ou ("soundcloud", url).
    """
    seen: set[tuple[str, str]] = set()
    entries: list[tuple[str, str]] = []
    for line in _read_lines(path):
        # SoundCloud d'abord : son URL ne contient pas d'id YouTube, mais un
        # titre de piste pourrait ressembler à un id nu.
        url = extract_soundcloud_url(line)
        entry = (("soundcloud", url) if url else
                 ("youtube", extract_video_id(line) or ""))
        if not entry[1] or entry in seen:
            continue
        seen.add(entry)
        entries.append(entry)
    if not entries:
        raise ImportError_(
            "Aucune vidéo trouvée. Attendu : une URL YouTube ou SoundCloud "
            "par ligne, un fichier M3U, ou des identifiants de vidéo.")
    return entries


def parse_playlist_file(path: str | Path) -> list[str]:
    """Identifiants YouTube d'un fichier, sans doublon, dans l'ordre.

    (Les liens SoundCloud sont ignorés ici : voir `parse_entries`.)
    """
    ids = [value for kind, value in parse_entries(path) if kind == "youtube"]
    if not ids:
        raise ImportError_(
            "Aucune vidéo trouvée. Attendu : une URL YouTube par ligne, "
            "un fichier M3U, ou des identifiants de vidéo.")
    return ids


def fetch_metadata(video_id: str, timeout: float = TIMEOUT) -> TrackRef:
    """Interroge oEmbed pour une vidéo. Ne lève jamais.

    En cas d'échec, renvoie un TrackRef minimal : une piste sans titre
    reste jouable, et le moteur complétera à la lecture.
    """
    watch = f"https://www.youtube.com/watch?v={video_id}"
    query = urllib.parse.urlencode({"format": "json", "url": watch})
    try:
        check_url(f"{OEMBED_URL}?{query}")
        request = urllib.request.Request(
            f"{OEMBED_URL}?{query}", headers={"User-Agent": USER_AGENT})
        with urlopen_checked(request, timeout, ("youtube.com",)) as response:
            payload = json.loads(response.read(64 * 1024).decode("utf-8", "replace"))
        if not isinstance(payload, dict):
            raise ValueError("réponse oEmbed inattendue")
    except Exception as exc:
        log.debug("oEmbed indisponible pour %s : %s", video_id, exc)
        return TrackRef(video_id=video_id, title=video_id)

    artist, title = split_artist_title(
        payload.get("title") or "", payload.get("author_name") or "")
    return TrackRef(
        video_id=video_id,
        title=title or video_id,
        artist=artist,
        thumbnail=payload.get("thumbnail_url"),
    )


def resolve_all(video_ids, progress=None, workers: int = MAX_WORKERS
                ) -> list[TrackRef]:
    """Récupère les métadonnées en parallèle, en conservant l'ordre.

    `progress(faits, total)` est appelé depuis les threads de travail :
    l'appelant doit repasser par la boucle principale avant de toucher
    à l'interface.
    """
    video_ids = list(video_ids)
    total = len(video_ids)
    done = 0
    refs: list[TrackRef] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for ref in pool.map(fetch_metadata, video_ids):
            refs.append(ref)
            done += 1
            if progress is not None:
                progress(done, total)
    return refs


def default_playlist_name(path: str | Path) -> str:
    """Nom lisible tiré du nom de fichier."""
    stem = Path(path).stem.replace("_", " ").replace("-", " ")
    stem = re.sub(r"\b\d+\s*tracks?\b", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s{2,}", " ", stem).strip()
    return stem.title() or "Playlist importée"


class ImportResult(tuple):
    """(identifiant de playlist, nom, pistes) + compteurs SoundCloud.

    Reste un triplet pour les appelants existants (`pid, nom, refs = ...`).
    """

    def __new__(cls, playlist_id, name, refs, sc_disabled=0, sc_failed=0,
                sc_over_limit=0):
        self = super().__new__(cls, (playlist_id, name, refs))
        self.sc_disabled = sc_disabled      # liens ignorés, SoundCloud coupé
        self.sc_failed = sc_failed          # liens non résolus (erreur)
        self.sc_over_limit = sc_over_limit  # liens au-delà du plafond
        return self

    playlist_id = property(lambda self: self[0])
    name = property(lambda self: self[1])
    refs = property(lambda self: self[2])

    @property
    def notes(self) -> list[str]:
        out = []
        if self.sc_disabled:
            out.append(f"{self.sc_disabled} liens SoundCloud ignorés "
                       "(SoundCloud désactivé)")
        if self.sc_over_limit:
            out.append(f"{self.sc_over_limit} liens SoundCloud ignorés "
                       f"(plafond de {MAX_SC_LINKS} par import)")
        if self.sc_failed:
            out.append(f"{self.sc_failed} liens SoundCloud illisibles")
        return out


def _resolve_soundcloud(url: str, client) -> list[TrackRef]:
    """Pistes d'un lien SoundCloud. Lève en cas d'échec.

    Piste -> elle-même ; set -> toutes ses pistes ; autre (profil) -> rien.
    Choix : on garde les pistes non lisibles d'un set. L'utilisateur a
    demandé cette playlist ; elles s'afficheront grisées. Pour une piste
    isolée non lisible, idem (le lien est explicite). Ce sont les files de
    lecture qui filtrent (spec §5.4), pas l'import.
    """
    found = client.resolve_permalink(url)
    if isinstance(found, ScTrack):
        return [found.ref]
    if isinstance(found, ScPlaylist):
        _meta, tracks = client.playlist(found.id)
        return [track.ref for track in tracks]
    return []


def import_playlist(path: str | Path, library, name: str | None = None,
                    progress=None, soundcloud=None) -> ImportResult:
    """Importe un fichier en playlist locale.

    Renvoie (identifiant de playlist, nom, pistes) avec en plus les
    compteurs SoundCloud. Bloquant. `soundcloud` est un client injecté
    (None = SoundCloud désactivé : ses liens sont ignorés et comptés).
    """
    entries = parse_entries(path)
    total = len(entries)
    youtube = [value for kind, value in entries if kind == "youtube"]
    links = [value for kind, value in entries if kind == "soundcloud"]
    done = 0

    def tick():
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total)

    resolved = dict(zip(youtube, resolve_all(
        youtube, progress=lambda *_: tick()), strict=True))

    disabled = failed = over_limit = 0
    sc_refs: dict[str, list[TrackRef]] = {}
    if soundcloud is None:
        disabled = len(links)
    else:
        for index, url in enumerate(links):
            if index >= MAX_SC_LINKS:
                over_limit = len(links) - MAX_SC_LINKS
                break
            try:
                sc_refs[url] = _resolve_soundcloud(url, soundcloud)
            except Exception as exc:   # NotPlayable, SoundCloudUnavailable…
                log.debug("Lien SoundCloud non résolu (%s) : %s", url, exc)
                failed += 1
    for _ in links:
        tick()

    refs: list[TrackRef] = []
    seen: set[str] = set()
    for kind, value in entries:
        batch = ([resolved[value]] if kind == "youtube"
                 else sc_refs.get(value, []))
        for ref in batch:
            if ref.video_id not in seen:
                seen.add(ref.video_id)
                refs.append(ref)

    result = ImportResult(None, "", refs, disabled, failed, over_limit)
    if not refs:
        raise ImportError_("Aucune piste importée. " + "; ".join(result.notes))
    playlist_name = (name or "").strip() or default_playlist_name(path)
    playlist_id = library.create_playlist(playlist_name)
    library.add_to_playlist(playlist_id, refs)
    log.info("Playlist « %s » importée : %d pistes", playlist_name, len(refs))
    result = ImportResult(playlist_id, playlist_name, refs, disabled, failed,
                          over_limit)
    return result


# ---- Export ---------------------------------------------------------------

EXPORT_FORMATS = ("m3u8", "txt", "json", "csv")


def export_playlist(refs, path, fmt: str | None = None) -> Path:
    """Écrit une playlist sur disque.

    Le format est déduit de l'extension quand il n'est pas donné. L'écriture
    est atomique : un fichier existant n'est remplacé qu'une fois le nouveau
    complet.
    """
    target = Path(path).expanduser()
    fmt = (fmt or target.suffix.lstrip(".") or "m3u8").lower()
    if fmt not in EXPORT_FORMATS:
        raise ImportError_(
            f"Format inconnu : {fmt}. Attendu : {', '.join(EXPORT_FORMATS)}")

    refs = list(refs)
    if fmt == "m3u8":
        lines = ["#EXTM3U"]
        for ref in refs:
            duration = int(ref.duration) if ref.duration else -1
            # Un saut de ligne dans un titre injecterait des directives M3U.
            artist = " ".join((ref.artist or "").split())
            title = " ".join((ref.title or "").split())
            lines.append(f"#EXTINF:{duration},"
                         + (f"{artist} - {title}" if artist else title))
            lines.append(ref.page_url)
        body = "\n".join(lines) + "\n"
    elif fmt == "txt":
        body = "\n".join(ref.page_url for ref in refs) + "\n"
    elif fmt == "json":
        body = json.dumps([{**ref.as_dict(), "source": ref.source}
                           for ref in refs],
                          indent=2, ensure_ascii=False) + "\n"
    else:   # csv
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["title", "artist", "album", "duration", "url", "source"])
        for ref in refs:
            writer.writerow([ref.title, ref.artist, ref.album or "",
                             int(ref.duration or 0), ref.page_url, ref.source])
        body = buffer.getvalue()

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, target)
    log.info("Playlist exportée : %s (%d pistes)", target.name, len(refs))
    return target

"""Modèles de données partagés par le core."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from . import display as _display
from .net import UnsafeURL, check_url

YOUTUBE, SOUNDCLOUD = "youtube", "soundcloud"

# Alphabet des identifiants YouTube (11 caractères en pratique ; on reste
# souple sur la longueur pour les identifiants de test et de playlist).
_YT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# URN SoundCloud : le « : » est hors de l'alphabet YouTube, donc aucune
# confusion possible entre les deux familles d'identifiants.
_SC_ID = re.compile(r"soundcloud:tracks:[0-9]{1,20}")


def source_of(track_id: str) -> str:
    """Source d'une piste, déduite de son identifiant (jamais stockée)."""
    if isinstance(track_id, str) and _SC_ID.fullmatch(track_id):
        return SOUNDCLOUD
    return YOUTUBE


def is_valid_track_id(track_id) -> bool:
    """Identifiant YouTube ou URN SoundCloud bien formé (il finit dans une
    URL et un nom de fichier : on reste strict)."""
    return isinstance(track_id, str) and bool(
        _YT_ID.fullmatch(track_id) or _SC_ID.fullmatch(track_id))


def sc_numeric_id(track_id) -> int | None:
    """Identifiant numérique d'une URN SoundCloud, sinon None."""
    if isinstance(track_id, str) and _SC_ID.fullmatch(track_id):
        return int(track_id.rsplit(":", 1)[1])
    return None


def valid_web_url(value) -> str | None:
    """Permalink SoundCloud (https://soundcloud.com/…) ou None."""
    if not isinstance(value, str) or len(value) > 500:
        return None
    try:
        check_url(value, allow_hosts=("soundcloud.com",))
    except UnsafeURL:
        return None
    return value if value.lower().startswith("https://") else None


@dataclass(slots=True)
class TrackRef:
    """Référence légère à une piste, sans URL de flux.

    C'est ce que contiennent la file d'attente, les playlists et la
    bibliothèque : résoudre l'URL de flux coûte un appel réseau et expire
    au bout de quelques heures, donc on ne le fait qu'au dernier moment.
    """

    video_id: str
    title: str = "Sans titre"
    artist: str = ""
    duration: float = 0.0
    thumbnail: str | None = None
    album: str | None = None
    # Permalink SoundCloud (attribution) ; None pour YouTube.
    web_url: str | None = None

    @property
    def label(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title

    @property
    def source(self) -> str:
        return source_of(self.video_id)

    @property
    def url(self) -> str:
        number = sc_numeric_id(self.video_id)
        if number is not None:
            return f"https://api.soundcloud.com/tracks/{number}"
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def page_url(self) -> str:
        """Page à ouvrir ou à citer : le permalink s'il existe."""
        return self.web_url or self.url

    @property
    def display_title(self) -> str:
        """Titre nettoyé pour l'affichage (la donnée n'est pas modifiée)."""
        return _display.display_title(self.title, self.artist)

    @property
    def display_artist(self) -> str:
        """Artiste nettoyé pour l'affichage (la donnée n'est pas modifiée)."""
        return _display.display_artist(self.artist, self.title)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TrackRef":
        """Reconstruit une piste depuis un JSON non fiable (réglages).

        L'identifiant finit dans une URL et un nom de fichier : on n'accepte
        que l'alphabet des identifiants YouTube. Les autres champs sont
        convertis au bon type, ou abandonnés.
        """
        video_id = data.get("video_id")
        if not is_valid_track_id(video_id):
            raise ValueError("Identifiant de vidéo invalide")
        fields: dict = {"video_id": video_id}
        for key in ("title", "artist", "thumbnail", "album"):
            value = data.get(key)
            if isinstance(value, str):
                fields[key] = value[:500]
        duration = data.get("duration")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) \
                and 0 <= duration < 1e6:
            fields["duration"] = float(duration)
        web_url = valid_web_url(data.get("web_url"))
        if web_url:
            fields["web_url"] = web_url
        return cls(**fields)


@dataclass(slots=True)
class Track:
    """Une piste résolue, prête à être lue."""

    video_id: str
    title: str
    artist: str
    duration: float
    stream_url: str
    webpage_url: str
    thumbnail: str | None = None
    album: str | None = None
    http_headers: dict[str, str] = field(default_factory=dict)
    # Vrai si le flux vient d'un fichier téléchargé localement.
    is_local: bool = False
    web_url: str | None = None

    @property
    def label(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title

    @property
    def display_title(self) -> str:
        """Titre nettoyé pour l'affichage (la donnée n'est pas modifiée)."""
        return _display.display_title(self.title, self.artist)

    @property
    def display_artist(self) -> str:
        """Artiste nettoyé pour l'affichage (la donnée n'est pas modifiée)."""
        return _display.display_artist(self.artist, self.title)

    def to_ref(self) -> TrackRef:
        return TrackRef(
            video_id=self.video_id,
            title=self.title,
            artist=self.artist,
            duration=self.duration,
            thumbnail=self.thumbnail,
            album=self.album,
            web_url=self.web_url,
        )


@dataclass(slots=True)
class Segment:
    """Un segment SponsorBlock, en secondes."""

    start: float
    end: float
    category: str
    uuid: str = ""

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)

    def contains(self, position: float, margin: float = 0.0) -> bool:
        return self.start - margin <= position < self.end


def parse_length(value) -> float:
    """Accepte 213, "3:33" ou "1:02:05" et renvoie des secondes."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value).strip().split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return 0.0
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds

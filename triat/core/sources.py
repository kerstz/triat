"""Routeur de sources : même interface publique que `Extractor`.

Le moteur, les téléchargements et MPRIS parlent à un seul objet ; le routeur
aiguille selon l'URL : `https://api.soundcloud.com/tracks/<n>` va vers
SoundCloud, tout le reste vers YouTube (`Extractor`), inchangé.

Pistes non lisibles : `NotPlayable` est convertie en `ScUnplayable`, sous-classe
d'`ExtractionError` qui porte `.reason` (snip | drm | blocked | removed). Les
appelants qui attrapent déjà `ExtractionError` n'ont rien à changer ; ceux qui
veulent un message précis utilisent `unplayable_reason(exc)` et `UNPLAYABLE_LABELS`.

Règles SoundCloud : jamais de téléchargement (CGU), jamais de clip vidéo, et
`ExtractionError("SoundCloud désactivé")` si le réglage est coupé ou le client
absent. Aucun appel réseau à la construction.
"""

from __future__ import annotations

import logging

from .extractor import ExtractionError
from .models import SOUNDCLOUD, Track, TrackRef, sc_numeric_id, source_of
from .net import UnsafeURL, check_url
from .soundcloud import NotPlayable, ScTrack

log = logging.getLogger(__name__)

# Libellés affichés à l'utilisateur, par raison de non-lisibilité.
UNPLAYABLE_LABELS: dict[str, str] = {
    "snip": "Extrait Go+",
    "drm": "Protégé",
    "blocked": "Indisponible dans ton pays",
    "removed": "Retiré par son auteur",
}

_API_PREFIX = "https://api.soundcloud.com/tracks/"
DISABLED_MESSAGE = "SoundCloud désactivé"


class ScUnplayable(ExtractionError):
    """Piste SoundCloud non lisible ; `reason` : snip | drm | blocked | removed."""

    def __init__(self, reason: str):
        super().__init__(UNPLAYABLE_LABELS.get(reason, "Piste indisponible"))
        self.reason = reason


def unplayable_reason(exc: BaseException) -> str | None:
    """Raison de non-lisibilité portée par l'erreur, sinon None."""
    if isinstance(exc, (ScUnplayable, NotPlayable)):
        return exc.reason
    return None


def unplayable_label(reason: str) -> str:
    return UNPLAYABLE_LABELS.get(reason, "Piste indisponible")


def _sc_id_of_url(url: str) -> int | None:
    """Numéro de piste d'une URL d'API SoundCloud, sinon None."""
    text = (url or "").strip()
    if not text.startswith(_API_PREFIX):
        return None
    tail = text[len(_API_PREFIX):].split("?", 1)[0].strip("/")
    return int(tail) if tail.isdigit() else None


class SourceRouter:
    def __init__(self, youtube, soundcloud, settings) -> None:
        self._youtube = youtube
        self._sc = soundcloud
        self._settings = settings

    @property
    def youtube(self):
        """Extracteur YouTube sous-jacent."""
        return self._youtube

    @property
    def soundcloud(self):
        """Client SoundCloud, ou None tant que la source est désactivée."""
        if self._sc is None or not self._settings.get("soundcloud_enabled"):
            return None
        return self._sc

    @soundcloud.setter
    def soundcloud(self, client) -> None:
        self._sc = client

    # -- Aide ----------------------------------------------------------------
    def _sc_client(self):
        if self._sc is None or not self._settings.get("soundcloud_enabled"):
            raise ExtractionError(DISABLED_MESSAGE)
        return self._sc

    def _is_sc_url(self, url: str) -> bool:
        return _sc_id_of_url(url) is not None

    # -- Interface d'Extractor -------------------------------------------------
    def resolve(self, url: str) -> Track:
        number = _sc_id_of_url(url)
        if number is None:
            return self._youtube.resolve(url)
        return self._resolve_sc(number)

    def _resolve_sc(self, number: int) -> Track:
        client = self._sc_client()
        # Lecture fraîche : la policy peut avoir changé depuis la recherche.
        found = client.tracks([number])
        item: ScTrack | None = next(
            (t for t in found if sc_numeric_id(t.ref.video_id) == number), None)
        if item is None:
            raise ScUnplayable("removed")
        if not item.playable:
            raise ScUnplayable(item.reason or "blocked")
        try:
            url, headers = client.stream(
                number, self._settings.get("soundcloud_quality"))
        except NotPlayable as exc:
            raise ScUnplayable(exc.reason) from exc
        ref = item.ref
        return Track(
            video_id=ref.video_id,
            title=ref.title,
            artist=ref.artist,
            duration=ref.duration,
            stream_url=url,
            webpage_url=ref.web_url or "",
            thumbnail=ref.thumbnail,
            album=ref.album,
            http_headers=dict(headers or {}),
            web_url=ref.web_url,
        )

    def resolve_video(self, url: str, max_height: int = 1080):
        if self._is_sc_url(url):
            raise ExtractionError("Pas de clip pour SoundCloud")
        return self._youtube.resolve_video(url, max_height)

    def search(self, query: str, limit: int = 20) -> list[TrackRef]:
        return self._youtube.search(query, limit)

    def download(self, url: str, options: dict) -> None:
        if self._is_sc_url(url):
            raise ExtractionError(
                "Téléchargement impossible pour SoundCloud (conditions d'utilisation)")
        return self._youtube.download(url, options)

    # -- MPRIS -----------------------------------------------------------------
    def open_uri(self, uri: str) -> Track:
        """OpenUri : permalink soundcloud.com -> piste (SoundCloud activé
        requis) ; le reste suit le chemin YouTube existant (`resolve`)."""
        text = (uri or "").strip()
        if is_sc_permalink(text):
            client = self._sc_client()
            try:
                found = client.resolve_permalink(text)
            except NotPlayable as exc:
                raise ScUnplayable(exc.reason) from exc
            if not isinstance(found, ScTrack):
                raise ExtractionError("Ce lien SoundCloud n'est pas une piste")
            return self.resolve(found.ref.url)
        return self.resolve(text)

    # -- Repli -----------------------------------------------------------------
    def __getattr__(self, name: str):
        # Dernier recours : tout attribut inconnu est celui de YouTube.
        if name.startswith("__") or name in ("_youtube", "_sc", "_settings"):
            raise AttributeError(name)
        return getattr(self._youtube, name)


def is_sc_permalink(text: str) -> bool:
    if "://" not in text:
        return False
    try:
        check_url(text, allow_hosts=("soundcloud.com",))
    except UnsafeURL:
        return False
    return True


__all__ = ["SourceRouter", "ScUnplayable", "UNPLAYABLE_LABELS",
           "unplayable_reason", "unplayable_label", "SOUNDCLOUD", "source_of"]

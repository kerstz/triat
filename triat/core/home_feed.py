"""Accueil distant (YouTube Music ou InnerTube) avec cache et TTL.

L'accueil de l'app s'ouvre sur des données locales ; ce module fournit, en
plus, les sections distantes déjà reçues (cache `reco_cache['home:remote']`)
et sait dire quand les rafraîchir. `refresh` bloque (réseau) : à appeler
depuis un fil, jamais depuis le fil GTK. Aucune forme de réponse nouvelle
n'est interprétée ici : tout vient de `Recommender.remote_home`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from .models import TrackRef

log = logging.getLogger(__name__)

HOME_TTL = 6 * 3600.0            # au-delà : à rafraîchir (affiché « périmé »)
HOME_MAX_STALE = 7 * 86400.0     # au-delà : jeté, on n'affiche plus rien
HOME_RETRY = 300.0               # pause après un échec ou une réponse vide
CACHE_KEY = "home:remote"


@dataclass(slots=True)
class HomeSnapshot:
    source: str                  # 'ytmusic' | 'innertube'
    sections: list[dict] = field(default_factory=list)   # {'title', 'tracks'}
    fetched_at: float = 0.0
    stale: bool = False


class HomeFeed:
    def __init__(self, library, recommender, clock=time.time) -> None:
        self._library = library
        self._recommender = recommender
        self._clock = clock
        self._retry_at = 0.0
        # Garde de génération : un refresh() encore en vol pendant reset()
        # (ou un wipe de la bibliothèque, cf. `Library.epoch`) n'écrit rien.
        self._generation = 0
        self._guard = threading.Lock()

    def _stamp(self) -> tuple[int, int]:
        return self._generation, getattr(self._library, "epoch", 0)

    def cached(self) -> HomeSnapshot | None:
        entry = self._library.reco_cache_get(CACHE_KEY)
        if entry is None:
            return None
        payload, fetched_at = entry
        age = self._clock() - fetched_at
        if age > HOME_MAX_STALE:
            return None
        sections = []
        for section in payload.get("sections") or ():
            if not isinstance(section, dict):
                continue
            tracks = []
            for item in section.get("tracks") or ():
                try:
                    tracks.append(TrackRef.from_dict(item))
                except (ValueError, TypeError, AttributeError):
                    continue
            if tracks:
                sections.append({"title": str(section.get("title") or ""),
                                 "tracks": tracks})
        if not sections:
            return None
        return HomeSnapshot(str(payload.get("source") or ""), sections,
                            fetched_at, stale=age > HOME_TTL)

    def needs_refresh(self) -> bool:
        snapshot = self.cached()
        return ((snapshot is None or snapshot.stale)
                and self._clock() >= self._retry_at)

    def reset(self) -> None:
        """Cookies changés ou données effacées : le cache est jeté et le
        prochain rafraîchissement n'attend pas."""
        with self._guard:
            self._generation += 1
            self._retry_at = 0.0
            self._library.reco_cache_clear("home:")

    def refresh(self) -> HomeSnapshot | None:
        """Interroge le réseau (bloquant). N'écrase le cache que par un
        résultat non vide ; sinon garde l'ancien et patiente `HOME_RETRY`."""
        now = self._clock()
        stamp = self._stamp()
        try:
            source, sections = self._recommender.remote_home()
        except Exception as exc:
            log.info("Accueil distant indisponible : %s", exc)
            source, sections = "", []
        with self._guard:
            if stamp != self._stamp():
                # Données effacées ou cookies changés pendant la requête :
                # la réponse appartient à l'ancien état, on la jette.
                return self.cached()
            if source and sections:
                self._retry_at = 0.0
                self._library.reco_cache_put(CACHE_KEY, {
                    "source": source,
                    "sections": [{"title": s.get("title", ""),
                                  "tracks": [t.as_dict() for t in s["tracks"]]}
                                 for s in sections]}, at=now)
            else:
                self._retry_at = now + HOME_RETRY
        return self.cached()

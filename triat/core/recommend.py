"""Moteur de recommandation.

Quatre niveaux, du meilleur au plus dégradé :
1. YouTube Music — le meilleur pour de la musique, mais muet sans cookies,
2. InnerTube client WEB — les recommandations anonymes, liées à
   l'identifiant de visiteur : c'est ce qui marche sans compte,
3. co-occurrence dans l'historique local — hors ligne, sans rien,
4. les titres du moment (score local), pour ne jamais rendre une page vide.

Les niveaux s'accumulent tant que le résultat filtré est mince, puis la
suite est dédoublonnée (historique récent, file), plafonnée par artiste et
débarrassée de ce qui est écarté, sauté ou hors sujet. En session privée,
rien n'est écrit dans la bibliothèque et InnerTube est appelé sans identité.
"""

from __future__ import annotations

import datetime as _dt
import logging
import random
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .mixes import _seed
from .models import TrackRef
from .reco import (DAY, HALF_LIFE_PLAYS, SKIP_HALF_LIFE, diversify,
                   is_blocked, score, weighted_order)

log = logging.getLogger(__name__)

# Une piste écoutée depuis moins de ça n'est pas reproposée par la radio.
RADIO_RECENT_WINDOW = 3 * DAY
# Hors de cette fourchette (si la durée est connue) : jingle, compilation.
RADIO_MIN_DURATION, RADIO_MAX_DURATION = 60.0, 720.0
RADIO_MAX_PER_ARTIST = 3
# Nombre de pistes de la file transmises pour espacer les artistes.
RADIO_CONTEXT = 10
# Poids de sauts à partir duquel une piste est écartée de la radio.
RADIO_SKIP_DROP = 1.0
# Part de « hors musique » SponsorBlock (cache local) à partir de laquelle
# une piste est écartée.
OFFTOPIC_MAX_SHARE = 0.5
# Une source qui rend moins que ça (après filtres) n'arrête pas la cascade.
MIN_TIER_YIELD = 8
# Après un échec de toutes les sources distantes, on ne les réinterroge pas
# avant ce délai : hors ligne, chaque rechargement coûterait un délai réseau.
REMOTE_BACKOFF = 120.0


@dataclass(slots=True)
class RadioResult:
    refs: list[TrackRef] = field(default_factory=list)
    source: str = ""    # 'ytmusic' | 'innertube' | 'local-co' | 'local-top' | ''
    reason: str = ""


def _dedupe(refs: Iterable[TrackRef], exclude: set[str]) -> list[TrackRef]:
    seen = set(exclude)
    unique = []
    for ref in refs:
        if ref.video_id and ref.video_id not in seen:
            seen.add(ref.video_id)
            unique.append(ref)
    return unique


class Recommender:
    def __init__(self, library, ytmusic=None, extractor=None,
                 innertube=None, is_private=lambda: False,
                 clock=time.time) -> None:
        self._library = library
        self._ytmusic = ytmusic
        self._extractor = extractor
        self._innertube = innertube
        self._is_private = is_private
        self._clock = clock
        self._remote_down_until = 0.0
        # Raison de la dernière radio, pour l'en-tête de « Lecture en cours ».
        self.last_reason = ""

    # ---- Recherche -----------------------------------------------------

    def search(self, query: str, limit: int = 25) -> list[TrackRef]:
        """YouTube Music d'abord, yt-dlp en repli. Bloquant."""
        query = query.strip()
        if not query:
            return []
        if self._ytmusic is not None:
            refs = self._ytmusic.search_tracks(query, limit=limit)
            if refs:
                self._remember(refs)
                return refs
        if self._innertube is not None:
            refs = self._innertube.search(query, limit=limit)
            if refs:
                self._remember(refs)
                return refs
        if self._extractor is not None:
            refs = self._extractor.search(query, limit=limit)
            self._remember(refs)
            return refs
        return []

    # ---- Radio / lecture automatique -----------------------------------

    def radio_for(self, ref: TrackRef, limit: int = 25,
                  exclude: Sequence[str] = (), **kw) -> list[TrackRef]:
        """Suite de lecture à partir d'une piste (compatibilité : la liste
        seule). Bloquant."""
        return self.radio(ref, limit, exclude, **kw).refs

    def radio(self, ref: TrackRef, limit: int = 25,
              exclude: Sequence[str] = (), context: Sequence[TrackRef] = (),
              private: bool | None = None) -> RadioResult:
        """Suite de lecture, avec sa provenance et sa raison. Bloquant."""
        now = self._clock()
        if private is None:
            private = bool(self._is_private())
        library = self._library
        blocked = library.blocked()
        banned = (set(exclude) | {ref.video_id}
                  | library.recent_ids(now - RADIO_RECENT_WINDOW)
                  | set(blocked.get("track", ())))
        want = max(limit, MIN_TIER_YIELD)

        filtered: list[TrackRef] = []
        sources: dict[str, str] = {}
        seen = set(banned)

        def absorb(candidates: Iterable[TrackRef], source: str) -> None:
            fresh = _dedupe(candidates, seen)
            kept = self._filter(fresh, blocked, now)
            for item in fresh:
                seen.add(item.video_id)      # écarté une fois, écarté partout
            for item in kept:
                sources[item.video_id] = source
            filtered.extend(kept)

        # a. YouTube Music : seulement connecté (muet sinon) et hors session
        # privée (la requête porte le compte).
        remote_ran = remote_got = False
        remote_ok = now >= self._remote_down_until
        if (remote_ok and not private and self._ytmusic is not None
                and self._ytmusic.authenticated):
            remote_ran = True
            found = self._safe(lambda: self._ytmusic.radio(ref.video_id,
                                                           limit=limit))
            remote_got = remote_got or bool(found)
            absorb(found, "ytmusic")

        # b. InnerTube WEB : la seule source qui réponde sans compte. En
        # session privée : sans identité de visiteur ni cookies.
        if len(filtered) < want and remote_ok and self._innertube is not None:
            remote_ran = True
            if private:
                found = self._safe(lambda: self._innertube.related(
                    ref.video_id, limit, ephemeral=True))
            else:
                found = self._safe(lambda: self._innertube.related(
                    ref.video_id, limit))
            remote_got = remote_got or bool(found)
            absorb(found, "innertube")

        if remote_ran:
            self._remote_down_until = (
                0.0 if remote_got else now + REMOTE_BACKOFF)

        # c. Co-écoute locale : hors ligne, sans compte.
        if len(filtered) < want:
            absorb(library.co_played(ref.video_id, limit=limit * 2, now=now),
                   "local-co")

        # d. Les titres du moment, pour ne jamais rendre une page vide.
        if len(filtered) < want:
            signals = library.track_signals(now, HALF_LIFE_PLAYS, SKIP_HALF_LIFE)
            day = _dt.date.fromtimestamp(now).isoformat()
            rng = random.Random(_seed("radio", ref.video_id, day))
            order = weighted_order(signals,
                                   [score(s, now) for s in signals], rng)
            absorb([s.ref for s in order[: limit * 3]], "local-top")

        out = diversify(filtered, RADIO_MAX_PER_ARTIST, context=context,
                        min_gap=1)
        if len(out) < min(5, limit):
            out = diversify(filtered, RADIO_MAX_PER_ARTIST * 2,
                            context=context, min_gap=1)
        out = out[:limit]
        if not private:
            self._remember([r for r in out
                            if sources.get(r.video_id) in ("ytmusic",
                                                           "innertube")])
        source = sources.get(out[0].video_id, "") if out else ""
        result = RadioResult(out, source, _reason(source, ref))
        self.last_reason = result.reason
        if source.startswith("local"):
            log.info("Radio locale (%s) pour %s", source, ref.video_id)
        return result

    def _filter(self, refs: list[TrackRef], blocked: dict, now: float
                ) -> list[TrackRef]:
        """Écarte l'écarté, le trop court ou trop long, le sauté et le
        hors-musique (cache SponsorBlock local seulement)."""
        refs = [r for r in refs if not is_blocked(r, blocked)
                and (r.duration <= 0
                     or RADIO_MIN_DURATION <= r.duration <= RADIO_MAX_DURATION)]
        if not refs:
            return []
        ids = [r.video_id for r in refs]
        skips = self._library.skip_weights(ids, now, SKIP_HALF_LIFE)
        off = self._library.offtopic_seconds(ids)
        kept = []
        for r in refs:
            if skips.get(r.video_id, 0.0) >= RADIO_SKIP_DROP:
                continue
            if r.duration > 0 and off.get(r.video_id, 0.0) / r.duration \
                    >= OFFTOPIC_MAX_SHARE:
                continue
            kept.append(r)
        return kept

    @staticmethod
    def _safe(call) -> list[TrackRef]:
        try:
            return list(call() or [])
        except Exception as exc:
            log.info("Source de radio indisponible : %s", exc)
            return []

    # ---- Page d'accueil -------------------------------------------------

    def remote_home(self) -> tuple[str, list[dict]]:
        """Accueil distant : `(source, [{'title', 'tracks'}])`, ou `("", [])`.
        Bloquant (réseau). La mise en cache est le travail de `HomeFeed`."""
        if self._ytmusic is not None and self._ytmusic.authenticated:
            try:
                sections = [s for s in self._ytmusic.home()
                            if isinstance(s, dict) and s.get("tracks")]
            except Exception as exc:
                log.info("Accueil YouTube Music indisponible: %s", exc)
                sections = []
            if sections:
                for section in sections:
                    self._remember(section["tracks"])
                return "ytmusic", sections
        if self._innertube is not None:
            anonymous = self._safe(lambda: self._innertube.home())
            if anonymous:
                self._remember(anonymous)
                return "innertube", [{"title": "Recommandé pour vous",
                                      "tracks": anonymous}]
        return "", []

    # ---- Interne -------------------------------------------------------

    def _remember(self, refs: Sequence[TrackRef]) -> None:
        """Alimente la bibliothèque locale pour la recherche hors ligne.
        Jamais en session privée : la recherche locale révélerait ce qui a
        été cherché ou écouté en privé."""
        if not refs or self._is_private():
            return
        try:
            self._library.upsert_many(refs)
        except Exception as exc:
            log.warning("Mémorisation locale impossible: %s", exc)


def _reason(source: str, ref: TrackRef) -> str:
    if source in ("ytmusic", "innertube"):
        return f"Radio d'après « {ref.title} »"
    if source == "local-co":
        return f"Écoutés autour de « {ref.title} » · hors ligne"
    if source == "local-top":
        return "Vos titres du moment · hors ligne"
    return ""

"""Logique pure des recommandations : signaux, score, tirage, diversité.

Aucun GTK, aucune base : tout se teste directement. La bibliothèque fournit
les `TrackSignal` (SQL), les mixes et la radio les consomment.

Constantes de réglage : toutes ici, en tête de module.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

from .models import TrackRef
from .search_results import clean_artist, normalize

T = TypeVar("T")

DAY = 86400.0
# Demi-vie d'une écoute : au bout de 30 jours elle pèse moitié moins.
HALF_LIFE_PLAYS = 30 * DAY
# Un saut s'oublie plus vite qu'une écoute.
SKIP_HALF_LIFE = 14 * DAY
FAV_BONUS = 1.5
LISTED_BONUS = 0.3
# Plancher : une piste jamais écoutée garde une chance d'être tirée.
BASE_WEIGHT = 0.05
SKIP_K = 1.5
# Facteur appliqué aux pistes du mix du jour d'hier / d'avant-hier.
REPEAT_PENALTY = (0.35, 0.6)


@dataclass(frozen=True, slots=True)
class TrackSignal:
    """Ce que l'on sait d'une piste « à soi » à un instant donné."""
    ref: TrackRef
    affinity: float = 0.0       # somme des écoutes, décroissance et complétion
    plays: int = 0
    last_at: float | None = None
    skip_weight: float = 0.0    # somme des sauts récents, décroissance
    favorite: bool = False
    listed: bool = False        # rangée dans au moins une playlist
    added_at: float = 0.0


def artist_key(name: str) -> str:
    """Identité d'un artiste : « Daft Punk - Topic » et « daft punk » ne font
    qu'un. Vide si le nom est vide."""
    return normalize(clean_artist(name))


def fatigue(last_at: float | None, now: float) -> float:
    """Une piste entendue il y a quelques heures n'a pas à revenir."""
    if last_at is None:
        return 1.0
    hours = (now - last_at) / 3600.0
    if hours < 24:
        return 0.15
    if hours < 72:
        return 0.5
    return 1.0


def score(sig: TrackSignal, now: float,
          previous: Sequence[set[str]] = ()) -> float:
    """Poids de tirage d'une piste : plus c'est haut, plus elle sort."""
    base = (math.log1p(max(sig.affinity, 0.0)) + FAV_BONUS * sig.favorite
            + LISTED_BONUS * sig.listed + BASE_WEIGHT)
    repeat = 1.0
    for ids, factor in zip(previous, REPEAT_PENALTY, strict=False):
        if sig.ref.video_id in ids:
            repeat *= factor
    return (base * fatigue(sig.last_at, now) * repeat
            / (1.0 + SKIP_K * max(sig.skip_weight, 0.0)))


def weighted_order(items: Sequence[T], weights: Sequence[float],
                   rng: random.Random) -> list[T]:
    """Tirage pondéré sans remise (Efraimidis-Spirakis).

    Chaque élément reçoit une clé `u ** (1 / w)` ; on trie par clé
    décroissante. Calculé en logarithmes : `log(u) / w` donne le même ordre
    sans underflow pour les petits poids. Un poids <= 0 exclut l'élément.
    """
    keyed: list[tuple[float, int]] = []
    for index, weight in enumerate(weights):
        draw = 1.0 - rng.random()       # (0, 1] : jamais de log(0)
        if weight <= 0:
            continue
        keyed.append((math.log(draw) / weight, index))
    keyed.sort(key=lambda pair: (-pair[0], pair[1]))
    return [items[i] for _key, i in keyed]


def _identity(ref: TrackRef) -> str:
    """Clé de diversité : l'artiste, ou la piste elle-même si inconnu (un
    artiste vide ne doit pas se plafonner lui-même)."""
    return artist_key(ref.artist) or f"\0{ref.video_id}"


def diversify(refs: Sequence[TrackRef], max_per_artist: int,
              context: Sequence[TrackRef] = (), min_gap: int = 1
              ) -> list[TrackRef]:
    """Plafonne et espace les artistes en conservant l'ordre autant que
    possible.

    Glouton : à chaque pas, la première piste dont l'artiste n'a pas atteint
    le plafond (`context` compte) et diffère des `min_gap` derniers émis.
    Sans candidat, on relâche l'espacement, jamais le plafond.
    """
    counts: dict[str, int] = {}
    tail: list[str] = []
    for ref in context:
        key = _identity(ref)
        counts[key] = counts.get(key, 0) + 1
        tail.append(key)
    pending = list(refs)
    out: list[TrackRef] = []
    while pending:
        recent = set(tail[-min_gap:]) if min_gap > 0 else set()
        chosen = None
        fallback = None
        for index, ref in enumerate(pending):
            key = _identity(ref)
            if counts.get(key, 0) >= max_per_artist:
                continue
            if fallback is None:
                fallback = index
            if key not in recent:
                chosen = index
                break
        if chosen is None:
            chosen = fallback
        if chosen is None:
            break               # tout le reste a atteint le plafond
        ref = pending.pop(chosen)
        key = _identity(ref)
        counts[key] = counts.get(key, 0) + 1
        tail.append(key)
        out.append(ref)
    return out


def is_blocked(ref: TrackRef, blocked: dict[str, dict[str, str]]) -> bool:
    """La piste ou son artiste sont-ils écartés des recommandations ?"""
    if not blocked:
        return False
    if ref.video_id in blocked.get("track", ()):
        return True
    artists = blocked.get("artist")
    if artists:
        key = artist_key(ref.artist)
        return bool(key) and key in artists
    return False

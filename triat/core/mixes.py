"""Mixes de l'accueil, fabriqués depuis la bibliothèque locale.

Aucun appel réseau : l'accueil s'affiche instantanément et hors ligne.
Chaque mix est tiré avec une graine qui dépend du jour, donc stable
toute la journée et renouvelé le lendemain.

`build_mixes` est pur (pas d'écriture) ; `MixStore` fige le résultat du
jour dans `reco_cache`. Le score, la décroissance temporelle et les
constantes de réglage sont dans `reco.py`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import math
import random
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from .models import TrackRef
from .reco import (DAY, HALF_LIFE_PLAYS, SKIP_HALF_LIFE, TrackSignal,
                   artist_key, diversify, fatigue, is_blocked, score,
                   weighted_order)
from .search_results import clean_artist

MIX_SIZE = 30
ARTIST_MIXES = 4
# Un artiste en dessous de ce nombre de pistes ne fait pas un mix crédible.
MIN_ARTIST_TRACKS = 4
# Mix du jour : (réservoir, places). La somme vaut MIX_SIZE.
DAILY_QUOTAS = (("loved", 18), ("rediscover", 6), ("fresh", 6))
DAILY_MAX_PER_ARTIST = 3
# Une piste aimée mais muette depuis deux mois mérite des retrouvailles.
REDISCOVER_AFTER = 60 * DAY
# Une piste entrée depuis moins de deux semaines passe devant dans « À découvrir ».
NEW_WINDOW = 14 * DAY
NEW_BONUS = 0.5
# Au-delà, une piste est sautée trop souvent pour entrer dans un mix tant
# qu'il reste d'autres choix.
SKIP_AVOID = 1.0
MIN_SECTION = MIN_ARTIST_TRACKS * 2

# En dessous : l'accueil construit les mixes tout de suite (petites
# bibliothèques, tests). Au-dessus : en arrière-plan.
SYNC_BUILD_LIMIT = 2000
MIX_CACHE_DAYS = 7
_CACHE_PREFIX = "mixes:"


@dataclass(slots=True)
class Mix:
    key: str
    title: str
    refs: list[TrackRef] = field(default_factory=list)
    # Nourrit le motif de points de la vignette : un mix garde son visage.
    seed: int = 0
    # « Parce que… » : pourquoi ce mix est proposé (info-bulle de la vignette).
    reason: str = ""

    @property
    def subtitle(self) -> str:
        """« 30 titres, avec Vialice, LEDOUBLE et Luther »."""
        names: list[str] = []
        for ref in self.refs:
            artist = (ref.artist or "").strip()
            if artist and artist not in names:
                names.append(artist)
            if len(names) == 3:
                break
        count = f"{len(self.refs)} titre{'s' if len(self.refs) > 1 else ''}"
        if not names:
            return count
        joined = names[0] if len(names) == 1 else (
            ", ".join(names[:-1]) + " et " + names[-1])
        return f"{count}, avec {joined}"

    def as_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "seed": self.seed,
                "reason": self.reason, "refs": [r.as_dict() for r in self.refs]}

    @classmethod
    def from_dict(cls, data: dict) -> "Mix":
        refs = []
        for item in data.get("refs") or ():
            try:
                refs.append(TrackRef.from_dict(item))
            except (ValueError, TypeError, AttributeError):
                continue
        return cls(key=str(data.get("key", "")), title=str(data.get("title", "")),
                   refs=refs, seed=int(data.get("seed", 0) or 0),
                   reason=str(data.get("reason", "")))


def _seed(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _unique(refs, exclude: set[str] | None = None) -> list[TrackRef]:
    seen = set(exclude or ())
    out = []
    for ref in refs:
        if ref.video_id and ref.video_id not in seen:
            seen.add(ref.video_id)
            out.append(ref)
    return out


def _identity(ref: TrackRef) -> str:
    return artist_key(ref.artist) or f"\0{ref.video_id}"


def _daily(signals: Sequence[TrackSignal], pools: dict[str, list[TrackSignal]],
           now: float, prev_daily: list[set[str]], rng: random.Random
           ) -> list[TrackRef]:
    """Mix du jour : quotas par réservoir, plafond d'artiste partagé,
    puis fusion mélangée et espacée."""
    distinct = len({_identity(s.ref) for s in signals}) or 1
    cap = max(DAILY_MAX_PER_ARTIST, math.ceil(MIX_SIZE / distinct))
    counts: dict[str, int] = {}
    picked: list[TrackSignal] = []
    chosen: set[str] = set()

    def take(order: Sequence[TrackSignal], room: int) -> list[TrackSignal]:
        taken = []
        for sig in order:
            if room <= 0:
                break
            ident = _identity(sig.ref)
            if sig.ref.video_id in chosen or counts.get(ident, 0) >= cap:
                continue
            counts[ident] = counts.get(ident, 0) + 1
            chosen.add(sig.ref.video_id)
            taken.append(sig)
            room -= 1
        return taken

    def eligible(sig: TrackSignal) -> bool:
        # Écoutée dans la journée ou sautée à répétition : en dernier recours.
        return fatigue(sig.last_at, now) > 0.15 and sig.skip_weight < SKIP_AVOID

    ordered: dict[str, list[TrackSignal]] = {}
    for name, _quota in DAILY_QUOTAS:
        pool = pools[name]
        weights = [score(s, now, prev_daily) for s in pool]
        ordered[name] = weighted_order(pool, weights, rng)
    primary = {name: [s for s in order if eligible(s)]
               for name, order in ordered.items()}
    spare = [s for name, _q in DAILY_QUOTAS for s in ordered[name]
             if not eligible(s)]

    for name, quota in DAILY_QUOTAS:
        picked += take(primary[name], quota)
    # Réservoir trop petit : on complète par ce qui reste, des plus sûrs
    # aux plus inédits, puis seulement par les pistes à éviter.
    for name in ("loved", "fresh", "rediscover"):
        if len(picked) >= MIX_SIZE:
            break
        picked += take(primary[name], MIX_SIZE - len(picked))
    if len(picked) < MIX_SIZE:
        picked += take(spare, MIX_SIZE - len(picked))

    refs = [s.ref for s in picked]
    shuffled = weighted_order(refs, [1.0] * len(refs), rng)
    return diversify(shuffled, cap, min_gap=1)[:MIX_SIZE]


def build_mixes(library, today: _dt.date | None = None, *,
                now: float | None = None, previous: Sequence[dict] = (),
                blocked: dict | None = None) -> list[Mix]:
    """Les mixes du jour. Pur : aucune écriture.

    `now` vaut midi du jour par défaut (reproductible). `previous` : les
    mixes des jours précédents (`MixStore.previous`), pour ne pas resservir
    les mêmes pistes. `blocked` : `library.blocked()` par défaut.
    """
    today = today or _dt.date.today()
    day = today.isoformat()
    if now is None:
        now = _dt.datetime.combine(today, _dt.time(12)).timestamp()
    if blocked is None:
        blocked = library.blocked()
    signals = [s for s in library.track_signals(now, HALF_LIFE_PLAYS,
                                                SKIP_HALF_LIFE)
               if s.ref.video_id and not is_blocked(s.ref, blocked)]
    if not signals:
        return []

    prev_daily = [set(p.get("daily") or ()) for p in previous]
    prev_artists = [set(p.get("artists") or ()) for p in previous]

    rediscover = [s for s in signals if s.plays >= 2 and s.last_at is not None
                  and s.last_at < now - REDISCOVER_AFTER]
    redisc_ids = {s.ref.video_id for s in rediscover}
    fresh = [s for s in signals if s.plays == 0 and not s.favorite]
    fresh_ids = {s.ref.video_id for s in fresh}
    loved = [s for s in signals if s.ref.video_id not in redisc_ids
             and s.ref.video_id not in fresh_ids]
    by_id = {s.ref.video_id: s for s in signals}

    mixes: list[Mix] = []

    # --- Mix du jour : valeurs sûres, retrouvailles et inédits -------------
    seed = _seed("jour", day)
    rng = random.Random(seed)
    daily = _daily(signals, {"loved": loved, "rediscover": rediscover,
                             "fresh": fresh}, now, prev_daily, rng)
    daily_ids = {r.video_id for r in daily}
    mixes.append(Mix("daily", "Mix du jour", daily, seed,
                     reason="Vos titres du moment, des retrouvailles et "
                            "des inédits"))
    used = set(daily_ids)

    # --- Un mix par artiste, élargi par le voisinage en playlist ----------
    groups: dict[str, list[TrackSignal]] = {}
    for sig in signals:
        key = artist_key(sig.ref.artist)
        if key:
            groups.setdefault(key, []).append(sig)
    artist_affinity = {k: sum(s.affinity for s in g) for k, g in groups.items()}
    eligible_keys = sorted(
        (k for k, g in groups.items() if len(g) >= MIN_ARTIST_TRACKS),
        key=lambda k: (-(artist_affinity[k] + 0.1 * len(groups[k])), k))
    # Rotation : un artiste déjà mis en avant deux jours de suite cède sa place.
    if len(eligible_keys) >= ARTIST_MIXES + 2 and len(prev_artists) >= 2:
        both = prev_artists[0] & prev_artists[1]
        rested = [k for k in eligible_keys if k not in both]
        if len(rested) >= ARTIST_MIXES:
            eligible_keys = rested

    artist_ids: set[str] = set()
    made = 0
    for key in eligible_keys:
        if made >= ARTIST_MIXES:
            break
        group = groups[key]
        seed = _seed("artiste", key, day)
        rng = random.Random(seed)
        spelling = Counter((s.ref.artist or "").strip() for s in group)
        display = clean_artist(
            sorted(spelling, key=lambda n: (-spelling[n], n))[0])
        order = weighted_order(group, [score(s, now, prev_daily) for s in group],
                               rng)
        own = [s.ref for s in order[: MIX_SIZE // 2]]
        own_ids = [r.video_id for r in own]
        together: list[TrackRef] = []
        for vid in own_ids[:3]:
            together += library.co_played(vid, limit=15, now=now)
        near_all = _unique(library.playlist_neighbours(own_ids) + together)
        near_all = [r for r in near_all if r.video_id in by_id
                    and r.video_id not in own_ids
                    and by_id[r.video_id].skip_weight < SKIP_AVOID]
        near = [r for r in near_all
                if r.video_id not in used]
        if len(near) < MIN_ARTIST_TRACKS:
            # Pas assez de voisins inédits : on tolère ceux du mix du jour
            # plutôt que de servir une simple discographie.
            near = [r for r in near_all if r.video_id not in artist_ids]
        near = weighted_order(
            near, [score(by_id[r.video_id], now, prev_daily) for r in near], rng)
        picked: list[TrackRef] = []
        for index in range(max(len(own), len(near))):
            if index < len(own):
                picked.append(own[index])
            if index < len(near):
                picked.append(near[index])
        picked = _unique(picked)[:MIX_SIZE]
        if len(picked) < MIN_ARTIST_TRACKS * 2:
            continue
        mixes.append(Mix(f"artist:{key}", f"Mix {display}", picked, seed,
                         reason=f"Parce que vous écoutez {display}"))
        artist_ids.update(r.video_id for r in picked)
        made += 1
    used |= artist_ids

    # --- À découvrir : dans la bibliothèque, jamais écouté -----------------
    day_seed = _seed("decouvrir", day)
    rng = random.Random(day_seed)
    pool = [s for s in fresh if s.ref.video_id not in used]
    if len(pool) < MIN_SECTION:
        pool = [s for s in fresh if s.ref.video_id not in daily_ids]
    weights = []
    for sig in pool:
        bonus = NEW_BONUS if sig.added_at >= now - NEW_WINDOW else 0.0
        affinity = artist_affinity.get(artist_key(sig.ref.artist), 0.0)
        weights.append(1.0 + math.log1p(affinity) + bonus)
    order = weighted_order(pool, weights, rng)[: MIX_SIZE * 4]
    discover = diversify([s.ref for s in order], DAILY_MAX_PER_ARTIST,
                         min_gap=1)[:MIX_SIZE]
    if len(discover) >= MIN_SECTION:
        mixes.append(Mix("fresh", "À découvrir", discover, day_seed,
                         reason="Dans vos playlists, jamais écoutés"))
        used |= {r.video_id for r in discover}

    # --- À réécouter : aimé, puis oublié ----------------------------------
    seed = _seed("reecouter", day)
    rng = random.Random(seed)
    pool = [s for s in rediscover if s.ref.video_id not in used]
    order = weighted_order(pool, [score(s, now, prev_daily) for s in pool], rng)
    again = diversify([s.ref for s in order], DAILY_MAX_PER_ARTIST,
                      min_gap=1)[:MIX_SIZE]
    if len(again) >= MIN_SECTION:
        mixes.append(Mix("rediscover", "À réécouter", again, seed,
                         reason="Pas écoutés depuis plus de deux mois"))
    return mixes


def filter_blocked(mixes: Sequence[Mix], blocked: dict) -> list[Mix]:
    """Retire des mixes (même déjà figés) ce qui est écarté depuis."""
    if not blocked or not (blocked.get("track") or blocked.get("artist")):
        return list(mixes)
    out = []
    for mix in mixes:
        refs = [r for r in mix.refs if not is_blocked(r, blocked)]
        if refs:
            out.append(Mix(mix.key, mix.title, refs, mix.seed, mix.reason))
    return out


class MixStore:
    """Mixes du jour figés dans `reco_cache` : l'accueil ne les recalcule
    qu'au changement de jour ou après un gros changement de bibliothèque."""

    def __init__(self, library) -> None:
        self._library = library

    @staticmethod
    def _key(day: _dt.date) -> str:
        return _CACHE_PREFIX + day.isoformat()

    def load(self, today: _dt.date) -> list[Mix] | None:
        """Les mixes figés du jour, ou None (absents, vides ou périmés : la
        bibliothèque a trop changé depuis)."""
        entry = self._library.reco_cache_get(self._key(today))
        if entry is None:
            return None
        payload, _created = entry
        mixes = [Mix.from_dict(m) for m in payload.get("mixes") or ()
                 if isinstance(m, dict)]
        mixes = [m for m in mixes if m.refs]
        if not mixes:
            return None
        try:
            then = int(payload.get("owned", 0))
        except (TypeError, ValueError):
            then = 0
        if abs(self._library.count_owned() - then) > max(20, 0.1 * then):
            return None
        return mixes

    def save(self, today: _dt.date, mixes: Sequence[Mix], owned: int,
             now: float) -> None:
        if not mixes:
            return
        self._library.reco_cache_put(self._key(today), {
            "day": today.isoformat(), "owned": int(owned), "built_at": now,
            "mixes": [m.as_dict() for m in mixes]}, at=now)
        # Les jours trop anciens ne servent plus à rien.
        oldest = today - _dt.timedelta(days=MIX_CACHE_DAYS)
        self._library.reco_cache_clear(_CACHE_PREFIX, before=self._key(oldest))

    def previous(self, today: _dt.date, days: int = 2) -> list[dict]:
        """`[{"daily": [ids], "artists": [clés]}]`, hier d'abord ; les jours
        sans mixes sont sautés."""
        out = []
        for back in range(1, days + 1):
            entry = self._library.reco_cache_get(
                self._key(today - _dt.timedelta(days=back)))
            if entry is None:
                continue
            mixes = entry[0].get("mixes") or ()
            daily: list[str] = []
            artists: list[str] = []
            for m in mixes:
                if not isinstance(m, dict):
                    continue
                key = str(m.get("key", ""))
                if key == "daily":
                    daily = [r.get("video_id") for r in m.get("refs") or ()
                             if isinstance(r, dict) and r.get("video_id")]
                elif key.startswith("artist:"):
                    artists.append(key[len("artist:"):])
            out.append({"daily": daily, "artists": artists})
        return out

    def get_or_build(self, today: _dt.date | None = None,
                     now: float | None = None) -> list[Mix]:
        """Mixes du jour : figés s'ils existent, sinon construits et figés.
        Bloquant sur une grosse bibliothèque : à appeler hors du fil GTK."""
        if today is None:
            now = time.time() if now is None else now
            today = _dt.date.fromtimestamp(now)
        cached = self.load(today)
        if cached is not None:
            return cached
        mixes = build_mixes(self._library, today, now=now,
                            previous=self.previous(today))
        stamp = time.time() if now is None else now
        self.save(today, mixes, self._library.count_owned(), stamp)
        return mixes

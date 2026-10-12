"""Logique pure des recommandations : score, tirage, diversité."""

from __future__ import annotations

import random

from triat.core.models import TrackRef
from triat.core.reco import (DAY, HALF_LIFE_PLAYS, TrackSignal, artist_key,
                             diversify, fatigue, is_blocked, score,
                             weighted_order)

NOW = 1_790_000_000.0


def sig(vid="a", artist="X", **kw) -> TrackSignal:
    return TrackSignal(ref=TrackRef(vid, vid, artist, 200.0), **kw)


def test_artist_key_merges_topic_and_case():
    assert (artist_key("Daft Punk - Topic") == artist_key("daft punk")
            == "daft punk")
    assert artist_key("") == ""


def test_fatigue_steps():
    assert fatigue(NOW - 3600, NOW) == 0.15
    assert fatigue(NOW - 48 * 3600, NOW) == 0.5
    assert fatigue(NOW - 4 * DAY, NOW) == 1.0
    assert fatigue(None, NOW) == 1.0


def test_score_orders_signals():
    plain = sig(affinity=2.0, plays=2, last_at=NOW - 10 * DAY)
    skipped = sig(affinity=2.0, plays=2, last_at=NOW - 10 * DAY, skip_weight=2.0)
    assert score(skipped, NOW) <= 0.25 * score(plain, NOW) + 1e-12
    # 50 écoutes d'il y a un an (pesées par la décroissance) < 5 d'hier.
    old_affinity = 50 * 0.5 ** (365 * DAY / HALF_LIFE_PLAYS)
    old = sig(affinity=old_affinity, plays=50, last_at=NOW - 365 * DAY)
    recent = sig(affinity=5.0, plays=5, last_at=NOW - 7 * DAY)
    assert score(old, NOW) < score(recent, NOW)
    # Reconduit d'hier : pénalité de répétition.
    assert abs(score(plain, NOW, [{"a"}]) - 0.35 * score(plain, NOW)) < 1e-9
    assert abs(score(plain, NOW, [set(), {"a"}]) - 0.6 * score(plain, NOW)) < 1e-9
    assert score(sig(favorite=True), NOW) > score(sig(listed=True), NOW)


def test_weighted_order_is_deterministic_and_respects_zero():
    items = list("abcdef")
    weights = [1, 2, 0, 3, 1, 0.5]
    first = weighted_order(items, weights, random.Random(5))
    again = weighted_order(items, weights, random.Random(5))
    assert first == again
    assert "c" not in first and len(first) == 5


def test_weighted_order_prefers_heavy_items():
    items = ["heavy"] + [f"l{i}" for i in range(9)]
    weights = [10.0] + [1 / 9] * 9
    wins = sum(weighted_order(items, weights, random.Random(i))[0] == "heavy"
               for i in range(2000))
    assert wins / 2000 > 0.8


def _refs(artist: str, n: int, prefix: str) -> list[TrackRef]:
    return [TrackRef(f"{prefix}{i}", f"T{i}", artist) for i in range(n)]


def test_diversify_caps_and_spaces_artists():
    refs = _refs("A", 10, "a") + _refs("B", 3, "b")
    out = diversify(refs, max_per_artist=3)
    names = [r.artist for r in out]
    assert names.count("A") <= 3 and names.count("B") == 3
    # Deux A de suite ne sont tolérés que faute de B : il y en a assez ici.
    assert all(not (x == y == "A") for x, y in zip(names, names[1:], strict=False))
    assert diversify(refs, 3, context=_refs("A", 3, "c")) \
        and not any(r.artist == "A"
                    for r in diversify(refs, 3, context=_refs("A", 3, "c")))


def test_diversify_relaxes_gap_not_cap():
    out = diversify(_refs("A", 5, "a"), max_per_artist=3)
    assert len(out) == 3


def test_diversify_treats_unknown_artists_as_unique():
    out = diversify(_refs("", 6, "u"), max_per_artist=1)
    assert len(out) == 6


def test_is_blocked_by_track_or_artist_key():
    blocked = {"track": {"x1": "t"}, "artist": {"daft punk": "Daft Punk"}}
    assert is_blocked(TrackRef("x1", "t", "Z"), blocked)
    assert is_blocked(TrackRef("y", "t", "Daft Punk - Topic"), blocked)
    assert not is_blocked(TrackRef("y", "t", "Autre"), blocked)
    assert not is_blocked(TrackRef("y", "t", ""), blocked)

"""Stats d'écoute : requêtes par période et mise en forme."""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from triat.core import stats as fmt
from triat.core.library import Library
from triat.core.models import TrackRef

DAY = 86400.0


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path / "l.db")
    yield library
    library.close()


def _ref(n: int, artist: str = "Art") -> TrackRef:
    return TrackRef(f"{n:011d}", f"Titre {n}", artist, 100.0)


def _noon(days_ago: int) -> float:
    """Midi local d'il y a `days_ago` jours (à l'abri des changements d'heure)."""
    today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    return today.timestamp() - days_ago * DAY


def _play(lib, ref, at, seconds=60.0):
    assert lib.add_history_entry(ref, at, seconds)


def test_empty_library(lib):
    data = lib.stats_period(None)
    assert data["plays"] == 0 and data["seconds"] == 0
    assert data["days"] == 0 and data["streak"] == 0
    assert data["top_tracks"] == [] and data["top_artists"] == []


def test_periods_tops_and_streak(lib):
    a, b, c = _ref(1, "Alpha"), _ref(2, "Alpha"), _ref(3, "Beta")
    for ago in (0, 1, 2):
        _play(lib, a, _noon(ago) + ago)           # +ago : instants distincts
    _play(lib, b, _noon(1) + 100, 30.0)
    _play(lib, c, _noon(40), 120.0)
    everything = lib.stats_period(None)
    assert everything["plays"] == 5
    assert everything["seconds"] == pytest.approx(60 * 3 + 30 + 120)
    assert everything["days"] == 4
    assert everything["streak"] == 3
    assert everything["top_tracks"][0][0].video_id == a.video_id
    assert everything["top_tracks"][0][1] == 3
    assert everything["top_artists"] == [("Alpha", 4), ("Beta", 1)]

    week = lib.stats_period(fmt.period_start("7d", time.time()))
    assert week["plays"] == 4 and week["days"] == 3
    assert [n for _, n in week["top_artists"]] == [4]
    assert week["streak"] == 3                      # indépendante de la période

    old = lib.stats_period(None, until=_noon(10))
    assert old["plays"] == 1 and old["top_tracks"][0][0].video_id == c.video_id


def test_streak_alive_from_yesterday_and_broken(lib):
    _play(lib, _ref(1), _noon(1))
    _play(lib, _ref(1), _noon(2))
    _play(lib, _ref(1), _noon(4))
    assert lib.stats_period(None)["streak"] == 2    # rien aujourd'hui, hier oui
    lib2 = lib
    lib2._db.execute("DELETE FROM history WHERE played_at > ?", (_noon(2) + 1,))
    assert lib.stats_period(None)["streak"] == 0    # trou depuis plus d'un jour


def test_top_lists_are_capped_and_negative_seconds_ignored(lib):
    for n in range(15):
        _play(lib, _ref(n, f"Artiste {n}"), _noon(0) + n, -5.0 if n == 0 else 10.0)
    data = lib.stats_period(None)
    assert len(data["top_tracks"]) == 10 and len(data["top_artists"]) == 10
    assert data["seconds"] == pytest.approx(140.0)


def test_period_start():
    now = datetime(2026, 5, 17, 10, 0).timestamp()
    assert fmt.period_start("7d", now) == now - 7 * DAY
    assert fmt.period_start("30d", now) == now - 30 * DAY
    assert fmt.period_start("year", now) == datetime(2026, 1, 1).timestamp()
    assert fmt.period_start("all", now) is None
    with pytest.raises(ValueError):
        fmt.period_start("zzz", now)


def test_formatting():
    assert fmt.format_listening(0) == "moins d'une minute"
    assert fmt.format_listening(47 * 60) == "47 min"
    assert fmt.format_listening(12 * 3600 + 5 * 60) == "12 h 05"
    assert fmt.format_days(1) == "1 jour" and fmt.format_days(3) == "3 jours"
    assert "Tu " in fmt.streak_text(5) and "5 jours" in fmt.streak_text(5)
    assert "Écoute" in fmt.streak_text(0)
    assert fmt.summary_line({"plays": 0}) == "Rien ici pour l'instant"
    assert fmt.summary_line({"plays": 2, "seconds": 600}) == \
        "Tu as écouté 2 titres, soit 10 min."

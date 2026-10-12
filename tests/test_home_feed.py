"""Accueil distant : cache, péremption, reprise après échec."""

from __future__ import annotations

import pytest

from triat.core.home_feed import (HOME_MAX_STALE, HOME_RETRY, HOME_TTL,
                                  HomeFeed)
from triat.core.library import Library
from triat.core.models import TrackRef
from triat.core.recommend import Recommender

START = 1_790_000_000.0


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


class Reco:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def remote_home(self):
        self.calls += 1
        return self.result


SECTIONS = [{"title": "Pour vous",
             "tracks": [TrackRef("h1", "Un", "A"), TrackRef("h2", "Deux", "B")]}]


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path / "l.db")
    yield library
    library.close()


def test_home_feed_serves_cache_then_marks_stale(lib):
    clock, reco = Clock(), Reco(("innertube", SECTIONS))
    feed = HomeFeed(lib, reco, clock=clock)
    assert feed.cached() is None and feed.needs_refresh()
    snap = feed.refresh()
    assert snap.source == "innertube" and not snap.stale
    assert [t.video_id for t in snap.sections[0]["tracks"]] == ["h1", "h2"]
    clock.now = START + 3600
    assert not feed.cached().stale and not feed.needs_refresh()
    clock.now = START + HOME_TTL + 3600
    assert feed.cached().stale and feed.needs_refresh()
    clock.now = START + HOME_MAX_STALE + 1
    assert feed.cached() is None


def test_home_feed_keeps_old_cache_on_failure(lib):
    clock, reco = Clock(), Reco(("innertube", SECTIONS))
    feed = HomeFeed(lib, reco, clock=clock)
    feed.refresh()
    clock.now = START + HOME_TTL + 10
    reco.result = ("", [])
    snap = feed.refresh()
    assert snap is not None and snap.stale and len(snap.sections) == 1
    assert not feed.needs_refresh()
    clock.now += HOME_RETRY + 1
    assert feed.needs_refresh()


def test_home_feed_reset_drops_cache(lib):
    feed = HomeFeed(lib, Reco(("ytmusic", SECTIONS)), clock=Clock())
    feed.refresh()
    feed.reset()
    assert feed.cached() is None


class YT:
    def __init__(self, authenticated, sections=()):
        self.authenticated = authenticated
        self._sections = list(sections)
        self.calls = 0

    def home(self):
        self.calls += 1
        return self._sections


class IT:
    def __init__(self, refs=()):
        self._refs = list(refs)

    def home(self):
        return list(self._refs)


def test_remote_home_prefers_ytmusic_when_authenticated(lib):
    reco = Recommender(lib, YT(True, SECTIONS), innertube=IT([TrackRef("i1")]))
    source, sections = reco.remote_home()
    assert source == "ytmusic" and sections == SECTIONS
    assert lib.get_track("h1") is not None


def test_remote_home_falls_back_to_innertube_then_nothing(lib):
    signed_out = YT(False, SECTIONS)
    reco = Recommender(lib, signed_out, innertube=IT([TrackRef("i1", "T", "A")]))
    source, sections = reco.remote_home()
    assert source == "innertube" and signed_out.calls == 0
    assert sections[0]["tracks"][0].video_id == "i1"
    assert Recommender(lib, YT(False), innertube=IT()).remote_home() == ("", [])

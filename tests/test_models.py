"""Modèle d'identifiant et de source (YouTube / SoundCloud)."""

from __future__ import annotations

import pytest

from triat.core.models import (
    SOUNDCLOUD, YOUTUBE, Track, TrackRef, is_valid_track_id, sc_numeric_id,
    source_of,
)

URN = "soundcloud:tracks:123456789"


def test_source_of():
    assert source_of(URN) == SOUNDCLOUD
    assert source_of("dQw4w9WgXcQ") == YOUTUBE
    assert source_of("soundcloud:users:1") == YOUTUBE
    assert source_of("") == YOUTUBE


@pytest.mark.parametrize("value", ["dQw4w9WgXcQ", "a", URN, "soundcloud:tracks:1"])
def test_valid_ids(value):
    assert is_valid_track_id(value)


@pytest.mark.parametrize("value", [
    "", "soundcloud:tracks:", "soundcloud:users:1", "soundcloud:tracks:12a",
    "soundcloud:tracks:" + "1" * 21, "../etc/passwd", "a/b", "a b", None, 5,
    URN + "\n", "x" * 65,
])
def test_invalid_ids(value):
    assert not is_valid_track_id(value)


def test_sc_numeric_id():
    assert sc_numeric_id(URN) == 123456789
    assert sc_numeric_id("dQw4w9WgXcQ") is None
    assert sc_numeric_id(None) is None


def test_url_and_page_url():
    yt = TrackRef("dQw4w9WgXcQ")
    assert yt.source == YOUTUBE
    assert yt.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert yt.page_url == yt.url
    sc = TrackRef(URN)
    assert sc.source == SOUNDCLOUD
    assert sc.url == "https://api.soundcloud.com/tracks/123456789"
    assert sc.page_url == sc.url
    sc.web_url = "https://soundcloud.com/a/b"
    assert sc.page_url == "https://soundcloud.com/a/b"
    assert sc.url == "https://api.soundcloud.com/tracks/123456789"


def test_from_dict_accepts_soundcloud_and_permalink():
    ref = TrackRef.from_dict({"video_id": URN, "title": "T",
                              "web_url": "https://soundcloud.com/a/b"})
    assert ref.video_id == URN and ref.web_url == "https://soundcloud.com/a/b"
    assert TrackRef.from_dict(ref.as_dict()) == ref


@pytest.mark.parametrize("bad", [
    "https://evil.example/a", "http://soundcloud.com/a", "javascript:alert(1)",
    "https://soundcloud.com.evil.example/a", "https://127.0.0.1/a",
    "file:///etc/passwd", 12, "",
])
def test_from_dict_drops_foreign_web_url(bad):
    ref = TrackRef.from_dict({"video_id": URN, "web_url": bad})
    assert ref.web_url is None


def test_from_dict_rejects_bad_id():
    for bad in ("soundcloud:tracks:", "../x", None):
        with pytest.raises(ValueError):
            TrackRef.from_dict({"video_id": bad})


def test_old_as_dict_without_web_url():
    old = {"video_id": "dQw4w9WgXcQ", "title": "T", "artist": "A",
           "duration": 10, "thumbnail": None, "album": None}
    ref = TrackRef.from_dict(old)
    assert ref.web_url is None and ref.title == "T" and ref.duration == 10.0


def test_track_to_ref_keeps_web_url():
    track = Track(URN, "T", "A", 1.0, "https://x/stream", "https://soundcloud.com/a/b",
                  web_url="https://soundcloud.com/a/b")
    assert track.to_ref().web_url == "https://soundcloud.com/a/b"

"""Pochettes SoundCloud : jamais de rétention dans `covers/` (spec §7.3, 5)."""

from __future__ import annotations

import tempfile

import pytest

from triat.core import artwork

JPEG = b"\xff\xd8\xff" + b"0" * 32
SC_URL = "https://i1.sndcdn.com/artworks-x-t500x500.jpg"
YT_URL = "https://i.ytimg.com/vi/abc/hq.jpg"


class _Resp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, n=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Cache XDG et dossier temporaire isolés ; faux téléchargeur."""
    cache = tmp_path / "cache"
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    monkeypatch.setattr(artwork, "image_is_sane", lambda _p: True)
    artwork._sc_clear()
    calls: list[str] = []

    def opener(request, timeout=None, **_kw):
        calls.append(request.full_url)
        return _Resp(JPEG)
    monkeypatch.setattr("urllib.request.urlopen", opener)
    yield {"calls": calls, "cache": cache, "tmp": tmp}
    artwork._sc_clear()


def _cache_files(cache):
    return [p for p in cache.rglob("*") if p.is_file()]


def test_soundcloud_image_never_written_to_covers(env):
    path = artwork.fetch(SC_URL)
    assert path is not None and path.exists()
    assert path.read_bytes() == JPEG
    assert _cache_files(env["cache"]) == []
    assert list(artwork.covers_dir().iterdir()) == []


def test_youtube_image_is_written_to_covers(env):
    path = artwork.fetch(YT_URL)
    assert path is not None
    assert path.parent == artwork.covers_dir()
    assert path.exists()


def test_soundcloud_second_fetch_is_served_from_memory(env):
    first = artwork.fetch(SC_URL)
    assert artwork.fetch(SC_URL) == first
    assert len(env["calls"]) == 1


def test_soundcloud_memory_cache_is_bounded(env, monkeypatch):
    monkeypatch.setattr(artwork, "SC_MEMORY_LIMIT", 2)
    urls = [f"https://i{k}.sndcdn.com/artworks-{k}-t500x500.jpg"
            for k in range(3)]
    paths = [artwork.fetch(u) for u in urls]
    assert all(paths)
    assert len(artwork._sc_files) == 2
    assert not paths[0].exists()          # évincée : fichier supprimé
    assert paths[2].exists()
    assert _cache_files(env["cache"]) == []


def test_soundcloud_host_detection():
    assert artwork.is_soundcloud_host(SC_URL)
    assert artwork.is_soundcloud_host("https://sndcdn.com/a.jpg")
    assert not artwork.is_soundcloud_host(YT_URL)
    assert not artwork.is_soundcloud_host("https://evilsndcdn.com/a.jpg")

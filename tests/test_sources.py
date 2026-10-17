"""Routeur de sources : aiguillage YouTube / SoundCloud."""

from __future__ import annotations

import inspect

import pytest

from tests.fakes import FakeExtractor, FakeSoundCloud
from triat.core.extractor import ExtractionError, Extractor
from triat.core.sources import (
    UNPLAYABLE_LABELS,
    ScUnplayable,
    SourceRouter,
    unplayable_reason,
)

SC_URL = "https://api.soundcloud.com/tracks/42"
PERMALINK = "https://soundcloud.com/auteur/t42"


class Settings:
    def __init__(self, enabled=True, quality="high"):
        self.values = {"soundcloud_enabled": enabled, "soundcloud_quality": quality}

    def get(self, key, default=None):
        return self.values.get(key, default)


def make(enabled=True, **kw):
    sc = FakeSoundCloud([FakeSoundCloud.make_track(42), FakeSoundCloud.make_track(7)], **kw)
    yt = FakeExtractor()
    return SourceRouter(yt, sc, Settings(enabled)), yt, sc


def test_url_sc_vers_soundcloud():
    router, yt, sc = make()
    track = router.resolve(SC_URL)
    assert track.video_id == "soundcloud:tracks:42"
    assert track.stream_url.endswith("sc42.mp3")
    assert track.web_url == PERMALINK and track.webpage_url == PERMALINK
    assert ("stream", 42, "high") in sc.calls and ("tracks", (42,)) in sc.calls
    assert yt.resolved == []


def test_youtube_vers_extracteur():
    router, yt, sc = make()
    track = router.resolve("https://www.youtube.com/watch?v=abc")
    assert track.video_id == "abc" and sc.calls == []
    assert router.search("x")[0].video_id == "s0"


def test_download_sc_leve():
    router, _, sc = make()
    with pytest.raises(ExtractionError):
        router.download(SC_URL, {})
    assert sc.calls == []


def test_resolve_video_sc_leve():
    router, _, _ = make()
    with pytest.raises(ExtractionError, match="Pas de clip"):
        router.resolve_video(SC_URL)
    assert router.resolve_video("https://www.youtube.com/watch?v=abc").video_id == "abc"


def test_sc_desactive_ou_absent():
    router, _, sc = make(enabled=False)
    with pytest.raises(ExtractionError, match="SoundCloud désactivé"):
        router.resolve(SC_URL)
    assert sc.calls == []
    nothing = SourceRouter(FakeExtractor(), None, Settings(True))
    with pytest.raises(ExtractionError, match="SoundCloud désactivé"):
        nothing.resolve(SC_URL)


@pytest.mark.parametrize("reason", ["snip", "drm", "blocked", "removed"])
def test_non_lisible_propage_raison(reason):
    router, _, _ = make(unplayable={42: reason})
    with pytest.raises(ExtractionError) as info:
        router.resolve(SC_URL)
    assert isinstance(info.value, ScUnplayable)
    assert unplayable_reason(info.value) == reason
    assert str(info.value) == UNPLAYABLE_LABELS[reason]


def test_libelles():
    assert UNPLAYABLE_LABELS == {
        "snip": "Extrait Go+", "drm": "Protégé",
        "blocked": "Indisponible dans ton pays", "removed": "Retiré par son auteur"}
    assert unplayable_reason(ValueError("x")) is None


def test_piste_absente_est_retiree():
    router, _, _ = make()
    with pytest.raises(ScUnplayable) as info:
        router.resolve("https://api.soundcloud.com/tracks/999")
    assert info.value.reason == "removed"


def test_open_uri_permalink_active():
    router, yt, sc = make()
    sc.permalinks[PERMALINK] = FakeSoundCloud.make_track(42)
    assert router.open_uri(PERMALINK).video_id == "soundcloud:tracks:42"
    assert ("resolve_permalink", PERMALINK) in sc.calls


def test_open_uri_permalink_desactive():
    router, _, sc = make(enabled=False)
    with pytest.raises(ExtractionError, match="désactivé"):
        router.open_uri(PERMALINK)
    assert sc.calls == []


def test_open_uri_youtube_et_non_piste():
    router, yt, sc = make()
    assert router.open_uri("https://www.youtube.com/watch?v=zz").video_id == "zz"
    sc.permalinks["https://soundcloud.com/auteur"] = object()
    with pytest.raises(ExtractionError):
        router.open_uri("https://soundcloud.com/auteur")


def test_constructeur_sans_reseau():
    sc = FakeSoundCloud()
    SourceRouter(FakeExtractor(), sc, Settings())
    assert sc.calls == []


def test_expose_toute_l_interface_d_extractor():
    public = [n for n, _ in inspect.getmembers(Extractor, callable)
              if not n.startswith("_")]
    assert {"resolve", "resolve_video", "search", "download"} <= set(public)
    for name in public:
        assert callable(getattr(SourceRouter, name, None)), name
        assert list(inspect.signature(getattr(SourceRouter, name)).parameters) == \
            list(inspect.signature(getattr(Extractor, name)).parameters), name


def test_repli_getattr_vers_youtube():
    router, yt, _ = make()
    yt.marqueur = 5
    assert router.marqueur == 5
    with pytest.raises(AttributeError):
        router.inexistant

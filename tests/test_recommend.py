"""Radio v2 : dédoublonnage, diversité, filtres, replis, vie privée."""

from __future__ import annotations

import pytest

from triat.core.library import Library
from triat.core.models import Segment, TrackRef
from triat.core.recommend import (REMOTE_BACKOFF, Recommender)

NOW = 1_790_000_000.0
DAY = 86400.0
SEED = TrackRef("seed", "Départ", "Chanteur", 200.0)


class YT:
    def __init__(self, out=(), authenticated=True):
        self.out = list(out)
        self.authenticated = authenticated
        self.calls = 0

    def radio(self, video_id, limit=25):
        self.calls += 1
        return list(self.out)

    def search_tracks(self, query, limit=25):
        return list(self.out)


class IT:
    def __init__(self, out=()):
        self.out = list(out)
        self.calls: list[dict] = []

    def related(self, video_id, limit=25, ephemeral=False):
        self.calls.append({"video_id": video_id, "ephemeral": ephemeral})
        return list(self.out)

    def home(self):
        return []


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path / "l.db")
    yield library
    library.close()


def make(lib, yt=None, it=None, **kw):
    kw.setdefault("clock", lambda: NOW)
    return Recommender(lib, ytmusic=yt, innertube=it, **kw)


def many(n, artist=None, prefix="r", duration=200.0):
    return [TrackRef(f"{prefix}{i}", f"Titre {i}", artist or f"Artiste {i}",
                     duration) for i in range(n)]


def ids(refs):
    return [r.video_id for r in refs]


def test_radio_excludes_recent_history_and_queue(lib):
    cands = many(12)
    lib.add_history_entry(cands[0], NOW - 3600, 200)
    reco = make(lib, it=IT(cands))
    out = reco.radio_for(SEED, limit=20, exclude=["r1"])
    assert "r0" not in ids(out) and "r1" not in ids(out)
    assert len(out) == 10


def test_radio_accumulates_tiers_when_first_is_thin(lib):
    yt = YT(many(2, prefix="y"))
    it = IT(many(10, prefix="i"))
    result = make(lib, yt, it).radio(SEED, limit=25)
    assert len(result.refs) >= 8 and result.source == "ytmusic"
    assert any(r.video_id.startswith("i") for r in result.refs)


def test_radio_skips_ytmusic_when_signed_out(lib):
    yt = YT(many(5, prefix="y"), authenticated=False)
    result = make(lib, yt, IT(many(5, prefix="i"))).radio(SEED)
    assert yt.calls == 0
    assert result.source == "innertube"


def test_radio_artist_diversity(lib):
    same = many(20, artist="Meme", prefix="m")
    others = many(5, prefix="o")
    out = make(lib, it=IT(same + others)).radio_for(SEED, limit=25)
    assert [r.artist for r in out].count("Meme") <= 3
    artists = [r.artist for r in out]
    assert all(not (a == b == "Meme") for a, b in zip(artists, artists[1:],
                                                      strict=False))
    ctx = many(3, artist="Meme", prefix="c")
    out = make(lib, it=IT(same + others)).radio_for(SEED, limit=25, context=ctx)
    assert [r.artist for r in out].count("Meme") == 0


def test_radio_duration_and_offtopic_filters(lib):
    cands = [TrackRef("long", "Compil", "A", 7200.0),
             TrackRef("short", "Jingle", "B", 30.0),
             TrackRef("unknown", "Inconnue", "C", 0.0),
             TrackRef("off", "Vlog", "D", 200.0),
             TrackRef("ok", "Bonne", "E", 200.0)]
    lib.cache_segments("off", [Segment(0, 150, "music_offtopic")])
    out = ids(make(lib, it=IT(cands)).radio_for(SEED))
    assert set(out) == {"unknown", "ok"}


def test_radio_drops_recently_skipped(lib):
    cands = many(6)
    lib.record_skip(cands[0], 4.0, at=NOW)
    out = ids(make(lib, it=IT(cands)).radio_for(SEED))
    assert "r0" not in out and len(out) == 5


def test_radio_respects_blocklist_in_every_tier(lib):
    banned_artist = TrackRef("b_art", "X", "Banni", 200.0)
    banned_track = TrackRef("b_trk", "Y", "Autre", 200.0)
    # Voisins locaux (co-écoute) : mêmes pistes interdites.
    for k, r in enumerate([SEED, banned_artist, banned_track, *many(3)]):
        lib.add_history_entry(r, NOW - 5 * DAY + k * 60, 200)
    lib.block("artist", "Banni")
    lib.block("track", "b_trk")
    for reco in (make(lib, it=IT([banned_artist, banned_track, *many(2, prefix="i")])),
                 make(lib)):
        out = ids(reco.radio_for(SEED))
        assert "b_art" not in out and "b_trk" not in out and out


def test_radio_backoff_when_remote_down(lib):
    now = [NOW]
    yt, it = YT([]), IT([])
    for k, r in enumerate([SEED, *many(3)]):
        lib.add_history_entry(r, NOW - 5 * DAY + k * 60, 200)
    reco = Recommender(lib, ytmusic=yt, innertube=it, clock=lambda: now[0])
    first = reco.radio_for(SEED)
    assert first and yt.calls == 1 and len(it.calls) == 1
    now[0] = NOW + 10
    assert reco.radio_for(SEED)             # toujours le local
    assert yt.calls == 1 and len(it.calls) == 1
    now[0] = NOW + REMOTE_BACKOFF + 1
    reco.radio_for(SEED)
    assert yt.calls == 2 and len(it.calls) == 2


def test_radio_private_does_not_remember_and_is_ephemeral(lib):
    remote = many(3, prefix="priv")
    it = IT(remote)
    yt = YT(many(3, prefix="ytp"))
    reco = make(lib, yt, it, is_private=lambda: True)
    result = reco.radio(SEED)
    assert result.refs and lib.get_track("priv0") is None
    assert it.calls[-1]["ephemeral"] is True
    assert yt.calls == 0                    # le compte ne voit pas la session
    reco.search("peu importe")
    assert lib.get_track("ytp0") is None


def test_radio_public_remembers_remote_results(lib):
    it = IT(many(3, prefix="pub"))
    make(lib, it=it).radio(SEED)
    assert lib.get_track("pub0") is not None
    assert it.calls[-1]["ephemeral"] is False


def test_radio_result_reason_by_source(lib):
    for k, r in enumerate([SEED, *many(3)]):
        lib.add_history_entry(r, NOW - 5 * DAY + k * 60, 200)
    result = make(lib).radio(SEED)
    assert result.source == "local-co"
    assert "hors ligne" in result.reason and "Départ" in result.reason
    assert make(lib).last_reason == ""


def test_radio_local_top_is_deterministic_per_seed_and_day(lib):
    lib.add_to_playlist(lib.create_playlist("P"), many(30, prefix="p"))
    a = ids(make(lib).radio_for(SEED, limit=10))
    b = ids(make(lib).radio_for(SEED, limit=10))
    other = ids(make(lib).radio_for(TrackRef("autre", "Autre"), limit=10))
    later = ids(Recommender(lib, clock=lambda: NOW + 3 * DAY).radio_for(SEED, limit=10))
    assert a == b and len(a) == 10
    assert a != other and a != later


def test_radio_soundcloud_seed_never_calls_youtube(lib):
    # Une piste SoundCloud n'est jamais envoyée à YouTube Music ni InnerTube.
    seed = TrackRef("soundcloud:tracks:5", "Départ SC", "Artiste", 200.0)
    yt, it = YT(many(5, prefix="yt")), IT(many(5, prefix="yt"))
    for k, r in enumerate([seed, *many(3, prefix="loc")]):
        lib.add_history_entry(r, NOW - 5 * DAY + k * 60, 200)
    reco = make(lib, yt=yt, it=it)
    result = reco.radio(seed)
    assert result.refs
    assert result.source.startswith("local")
    assert yt.calls == 0 and it.calls == []

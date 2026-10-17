"""Client API SoundCloud v2 : faux réseau, aucune requête réelle.

Les fixtures (tests/fixtures/soundcloud/) sont réduites des formes sondées
dans la spec ; le client_id y est FACTICE (« X » * 32).
"""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import stat
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from triat.core import soundcloud as sc
from triat.core.constants import BROWSER_USER_AGENT
from triat.core.net import RedirectRefused, UnsafeURL, check_url
from triat.core.soundcloud import (
    NotPlayable,
    SoundCloudClient,
    SoundCloudUnavailable,
)

FIXTURES = Path(__file__).parent / "fixtures" / "soundcloud"
CID = "X" * 32          # client_id factice des fixtures
OLD = "O" * 32          # ancien client_id (cache périmé simulé)
API = "api-v2.soundcloud.com"
HOME = "soundcloud.com"
ASSETS = "a-v2.sndcdn.com"


def fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fxj(name: str):
    return json.loads(fx(name))


# ---- Faux réseau --------------------------------------------------------------

@dataclass
class Call:
    url: str
    host: str
    path: str
    query: dict
    timeout: float
    allow_hosts: tuple | None
    user_agent: str | None
    headers: dict = field(default_factory=dict)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._io = io.BytesIO(body)
        self.status = status

    def read(self, n=-1):
        return self._io.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSoundCloudHTTP:
    """Opener compatible `urlopen_checked(request, timeout, allow_hosts)`.

    Routes : (hôte, préfixe de chemin) -> octets, (statut, octets), fonction
    `call -> réponse`, ou liste de réponses consommées dans l'ordre (la
    dernière se répète). Chaque appel est enregistré dans `calls`.
    """

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.calls: list[Call] = []

    def add(self, host: str, prefix: str, response) -> FakeSoundCloudHTTP:
        self.routes[(host, prefix)] = response
        return self

    def __call__(self, request, timeout, allow_hosts=None):
        url = request.full_url if hasattr(request, "full_url") else request
        try:                       # comme `urlopen_checked`
            check_url(url, allow_hosts=allow_hosts)
        except UnsafeURL as exc:
            raise RedirectRefused(str(exc)) from exc
        parts = urllib.parse.urlsplit(url)
        call = Call(
            url=url, host=parts.hostname or "", path=parts.path,
            query={k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()},
            timeout=timeout, allow_hosts=allow_hosts,
            user_agent=request.get_header("User-agent") if hasattr(request, "get_header") else None)
        self.calls.append(call)
        matches = [(len(p), p) for (h, p) in self.routes if h == call.host and call.path.startswith(p)]
        if not matches:
            raise urllib.error.URLError(f"route inconnue: {call.host}{call.path}")
        response = self.routes[(call.host, max(matches)[1])]
        if isinstance(response, list):
            response = response.pop(0) if len(response) > 1 else response[0]
        if callable(response):
            response = response(call)
        status, body = response if isinstance(response, tuple) else (200, response)
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "erreur", {}, io.BytesIO(body))
        return FakeResponse(body, status)

    def to(self, host: str, prefix: str = "") -> list[Call]:
        return [c for c in self.calls if c.host == host and c.path.startswith(prefix)]


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_fake() -> FakeSoundCloudHTTP:
    """Page d'accueil + scripts avec client_id, comme sur le vrai site."""
    return (FakeSoundCloudHTTP()
            .add(HOME, "/", fx("home.html"))
            .add(ASSETS, "/assets/", fx("asset_with_id.js")))


def make_client(tmp_path, fake, clock=None, preload: str | None = CID):
    """Client avec cache disque ; `preload` évite le scraping."""
    clock = clock or Clock()
    cache = tmp_path / "soundcloud.json"
    if preload:
        cache.write_text(json.dumps({"client_id": preload, "found_at": clock.t}))
    return SoundCloudClient(opener=fake, cache_path=cache, clock=clock), clock, cache


def obj(i: int, *, transcodings=None, policy="ALLOW", **extra) -> dict:
    """Piste JSON minimale et lisible (HLS aac_96k)."""
    base = {
        "kind": "track", "id": i, "urn": f"soundcloud:tracks:{i}", "title": f"Piste {i}",
        "duration": 180000, "full_duration": 180000, "streamable": True, "policy": policy,
        "track_authorization": f"AUTH{i}",
        "permalink_url": f"https://soundcloud.com/u/p-{i}", "artwork_url": None,
        "user": {"id": 1, "username": "Uploader", "avatar_url": "https://i1.sndcdn.com/av-large.jpg"},
        "media": {"transcodings": transcodings if transcodings is not None else [
            t("aac_96k", "hls", i)]},
    }
    base.update(extra)
    return base


def t(preset: str, protocol: str, i: int = 1, **extra) -> dict:
    tr = {"url": f"https://{API}/media/soundcloud:tracks:{i}/{preset}-{protocol}/stream",
          "preset": preset, "snipped": False, "format": {"protocol": protocol, "mime_type": "audio/mp4"}}
    tr.update(extra)
    return tr


# ---- Scraping du client_id ----------------------------------------------------

def home_with(n: int) -> bytes:
    scripts = "".join(f'<script crossorigin src="https://a-v2.sndcdn.com/assets/{k}-h{k}.js"></script>'
                      for k in range(n))
    return f"<html><head>{scripts}</head></html>".encode()


def test_scraping_dernier_script_d_abord_arret_au_premier(tmp_path):
    fake = (FakeSoundCloudHTTP().add(HOME, "/", fx("home.html"))
            .add(ASSETS, "/assets/", fx("asset_with_id.js")))
    client, _, _ = make_client(tmp_path, fake, preload=None)
    assert client.client_id() == CID
    assets = [c.path for c in fake.to(ASSETS)]
    assert assets == ["/assets/2-cccccccc.js"]          # le dernier, puis arrêt


def test_scraping_remonte_jusqu_au_script_qui_porte_l_id(tmp_path):
    fake = (FakeSoundCloudHTTP().add(HOME, "/", fx("home.html"))
            .add(ASSETS, "/assets/0-", fx("asset_with_id.js"))
            .add(ASSETS, "/assets/1-", fx("asset_without_id.js"))
            .add(ASSETS, "/assets/2-", fx("asset_without_id.js")))
    client, _, _ = make_client(tmp_path, fake, preload=None)
    assert client.client_id() == CID
    assert [c.path for c in fake.to(ASSETS)] == [
        "/assets/2-cccccccc.js", "/assets/1-bbbbbbbb.js", "/assets/0-aaaaaaaa.js"]


def test_scraping_au_plus_cinq_scripts(tmp_path):
    fake = (FakeSoundCloudHTTP().add(HOME, "/", home_with(8))
            .add(ASSETS, "/assets/", fx("asset_without_id.js")))
    client, _, _ = make_client(tmp_path, fake, preload=None)
    with pytest.raises(SoundCloudUnavailable):
        client.client_id()
    paths = [c.path for c in fake.to(ASSETS)]
    assert len(paths) == 5
    assert paths[0].startswith("/assets/7-") and paths[-1].startswith("/assets/3-")


def test_scraping_hote_exact_et_en_tetes(tmp_path):
    fake = make_fake()
    client, _, _ = make_client(tmp_path, fake, preload=None)
    client.client_id()
    home = fake.to(HOME)[0]
    assert home.url == "https://soundcloud.com/"
    assert home.allow_hosts == ("soundcloud.com",)
    assert home.timeout == 15 and home.user_agent == BROWSER_USER_AGENT
    asset = fake.to(ASSETS)[0]
    assert asset.allow_hosts == (ASSETS,) and asset.timeout == 15


def test_script_hors_hote_attendu_ignore(tmp_path):
    html = (b'<script src="https://evil.example/assets/1-x.js"></script>'
            b'<script src="https://a-v2.sndcdn.com.evil.example/assets/2-x.js"></script>')
    fake = FakeSoundCloudHTTP().add(HOME, "/", html).add(ASSETS, "/", fx("asset_with_id.js"))
    client, _, _ = make_client(tmp_path, fake, preload=None)
    with pytest.raises(SoundCloudUnavailable):
        client.client_id()
    assert fake.calls and all(c.host == HOME for c in fake.calls)


def test_un_scraping_par_dix_minutes_meme_en_echec(tmp_path):
    fake = (FakeSoundCloudHTTP().add(HOME, "/", home_with(1))
            .add(ASSETS, "/assets/", fx("asset_without_id.js")))
    client, clock, _ = make_client(tmp_path, fake, preload=None)
    for _ in range(3):
        with pytest.raises(SoundCloudUnavailable):
            client.client_id()
    assert len(fake.to(HOME)) == 1
    clock.t += 601
    with pytest.raises(SoundCloudUnavailable):
        client.client_id()
    assert len(fake.to(HOME)) == 2


def test_page_d_accueil_en_erreur(tmp_path):
    fake = FakeSoundCloudHTTP().add(HOME, "/", (503, b""))
    client, _, _ = make_client(tmp_path, fake, preload=None)
    with pytest.raises(SoundCloudUnavailable):
        client.client_id()


# ---- Cache disque -------------------------------------------------------------

def test_cache_ecrit_en_0600_atomiquement(tmp_path):
    fake = make_fake()
    client, clock, cache = make_client(tmp_path, fake, preload=None)
    client.client_id()
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    assert json.loads(cache.read_text()) == {"client_id": CID, "found_at": clock.t}
    assert [p.name for p in tmp_path.iterdir()] == ["soundcloud.json"]   # pas de .tmp


def test_cache_relu_par_un_autre_client_sans_scraping(tmp_path):
    fake = make_fake()
    client, clock, cache = make_client(tmp_path, fake, preload=None)
    client.client_id()
    fake2 = make_fake()
    other = SoundCloudClient(opener=fake2, cache_path=cache, clock=clock)
    assert other.client_id() == CID
    assert fake2.calls == []


def test_cache_ttl_24h(tmp_path):
    fake = make_fake()
    client, clock, cache = make_client(tmp_path, fake, preload=OLD)
    assert client.client_id() == OLD and fake.calls == []
    clock.t += 24 * 3600 - 1
    assert client.client_id() == OLD and fake.calls == []
    clock.t += 2
    assert client.client_id() == CID                      # périmé : nouveau scraping
    assert len(fake.to(HOME)) == 1
    assert json.loads(cache.read_text())["client_id"] == CID


def test_cache_corrompu_ou_invalide_ignore(tmp_path):
    fake = make_fake()
    client, clock, cache = make_client(tmp_path, fake, preload=None)
    cache.write_text(json.dumps({"client_id": "pas-un-id", "found_at": clock.t}))
    assert client.client_id() == CID


# ---- 401 / 403 / next_href ----------------------------------------------------

def test_401_un_seul_rescrape_puis_succes(tmp_path):
    fake = make_fake().add(API, "/search/tracks", [(401, b"{}"), fx("search_tracks.json")])
    client, _, cache = make_client(tmp_path, fake, preload=OLD)
    tracks, _ = client.search_tracks("daft")
    assert len(tracks) == 3
    api = fake.to(API)
    assert [c.query["client_id"] for c in api] == [OLD, CID]
    assert len(fake.to(HOME)) == 1
    assert json.loads(cache.read_text())["client_id"] == CID


def test_403_repete_sans_boucle(tmp_path):
    fake = make_fake().add(API, "/search/tracks", (403, b"{}"))
    client, _, _ = make_client(tmp_path, fake, preload=OLD)
    with pytest.raises(SoundCloudUnavailable) as info:
        client.search_tracks("daft")
    assert len(fake.to(API)) == 2 and len(fake.to(HOME)) == 1
    assert OLD not in str(info.value) and CID not in str(info.value)
    # Appel suivant : une seule tentative, le re-scrape est bridé (10 min).
    with pytest.raises(SoundCloudUnavailable):
        client.search_tracks("daft")
    assert len(fake.to(API)) == 3 and len(fake.to(HOME)) == 1


def test_next_href_etranger_refuse_sans_requete(tmp_path):
    fake = make_fake()
    client, _, _ = make_client(tmp_path, fake)
    for href in ("https://evil.example/users/5/tracks?offset=2",
                 "https://api-v2.soundcloud.com.evil.example/users/5/tracks",
                 "http://api-v2.soundcloud.com/users/5/tracks",
                 "file:///etc/passwd"):
        with pytest.raises(UnsafeURL):
            client.user_tracks(5, next_href=href)
    assert fake.calls == []


def test_next_href_etranger_non_retourne_par_la_recherche(tmp_path):
    data = fxj("search_tracks.json")
    data["next_href"] = "https://evil.example/search/tracks?offset=3"
    fake = make_fake().add(API, "/search/tracks", json.dumps(data).encode())
    client, _, _ = make_client(tmp_path, fake)
    _, nxt = client.search_tracks("x")
    assert nxt is None


def test_pagination_user_tracks_client_id_remplace(tmp_path):
    fake = (make_fake()
            .add(API, "/users/5/tracks", [fx("user_tracks_page1.json"), fx("user_tracks_page2.json")]))
    client, _, _ = make_client(tmp_path, fake)
    page1, nxt = client.user_tracks(5, limit=2)
    assert len(page1) == 2 and nxt and "client_id" not in nxt     # jamais exposé
    page2, nxt2 = client.user_tracks(5, next_href=nxt)
    assert [p.ref.video_id for p in page2] == ["soundcloud:tracks:4003"] and nxt2 is None
    last = fake.to(API)[-1]
    assert last.query["client_id"] == CID and last.query["offset"] == "2"
    assert last.url.count("client_id=") == 1


# ---- Lisibilité et mapping ----------------------------------------------------

def test_classification_des_trois_cas(tmp_path):
    fake = make_fake().add(API, "/search/tracks", fx("search_tracks.json"))
    client, _, _ = make_client(tmp_path, fake)
    tracks, nxt = client.search_tracks("daft", limit=3)
    drm, ok, snip = tracks
    assert (drm.playable, drm.reason) == (False, "drm")
    assert (ok.playable, ok.reason) == (True, "")
    assert (snip.playable, snip.reason) == (False, "snip")
    assert nxt and nxt.startswith("https://api-v2.soundcloud.com/search/tracks")
    assert "client_id" not in nxt
    call = fake.to(API)[0]
    assert call.query["q"] == "daft" and call.query["limit"] == "3" and call.timeout == 10
    assert call.user_agent == BROWSER_USER_AGENT and call.allow_hosts == (API,)


@pytest.mark.parametrize("patch,reason", [
    ({"policy": "BLOCK"}, "blocked"),
    ({"policy": "SNIP"}, "snip"),
    ({"streamable": False}, "blocked"),
    ({"media": {"transcodings": []}}, "blocked"),
])
def test_classification_autres_cas(patch, reason):
    track = sc.to_sc_track(obj(1, **patch))
    assert (track.playable, track.reason) == (False, reason)


def test_transcodage_preview_abr_snipped_exclus():
    only_bad = obj(1, transcodings=[
        t("abr_sq", "hls"), t("aac_96k", "hls", snipped=True),
        {**t("mp3_1_0", "progressive"), "url": f"https://{API}/media/x/preview/progressive"}])
    track = sc.to_sc_track(only_bad)
    assert not track.playable and track.reason == "snip"


def test_mapping_ref():
    data = fxj("search_tracks.json")["collection"]
    drm, ok, snip = (sc.to_sc_track(o) for o in data)
    assert ok.ref.video_id == "soundcloud:tracks:1002"
    assert ok.ref.title == "Titre libre"
    assert ok.ref.artist == "Uploader Libre"              # publisher_metadata.artist
    assert ok.ref.duration == 200.0
    assert ok.ref.thumbnail == "https://i1.sndcdn.com/artworks-1002-t500x500.jpg"
    assert ok.ref.album == "Album test"                   # publisher_metadata.album_title
    assert ok.ref.web_url == "https://soundcloud.com/uploader-libre/titre-1002"
    assert drm.ref.artist == "Label Officiel"             # repli sur user.username
    assert drm.ref.album is None                           # publisher_metadata nul
    assert snip.ref.duration == pytest.approx(320.357)    # SNIP : full_duration
    assert snip.full_duration == pytest.approx(320.357)


def test_mapping_album_et_avatar_de_repli():
    track = sc.to_sc_track(obj(
        2, publisher_metadata={"artist": "Art", "album_title": "Alb"}, artwork_url=None))
    assert track.ref.album == "Alb" and track.ref.artist == "Art"
    assert track.ref.thumbnail == "https://i1.sndcdn.com/av-large.jpg"   # pas de -t500x500 pour l'avatar
    stub = {"id": 5, "kind": "track", "policy": "ALLOW", "monetization_model": "X"}
    assert sc.to_sc_track(stub) is None


# ---- Qualité ------------------------------------------------------------------

def preset(track, quality):
    chosen = sc.pick_transcoding(track, quality)
    return (chosen["preset"], chosen["format"]["protocol"]) if chosen else None


def test_qualite_auto_et_high():
    full = obj(1, transcodings=[t("aac_96k", "hls"), t("opus_64", "hls"),
                                t("mp3_128", "progressive"), t("aac_160k", "hls")])
    for quality in ("auto", "high"):
        assert preset(full, quality) == ("aac_160k", "hls")
    no160 = obj(1, transcodings=[t("aac_96k", "hls"), t("opus_64", "hls"), t("mp3_128", "progressive")])
    assert preset(no160, "auto") == ("mp3_128", "progressive")
    no_mp3 = obj(1, transcodings=[t("aac_96k", "hls"), t("opus_64", "hls")])
    assert preset(no_mp3, "high") == ("opus_64", "hls")


def test_qualite_low():
    full = obj(1, transcodings=[t("aac_160k", "hls"), t("mp3_128", "progressive"),
                                t("opus_64", "hls"), t("aac_96k", "hls")])
    assert preset(full, "low") == ("aac_96k", "hls")
    no96 = obj(1, transcodings=[t("aac_160k", "hls"), t("mp3_128", "progressive"), t("opus_64", "hls")])
    assert preset(no96, "low") == ("opus_64", "hls")
    only = obj(1, transcodings=[t("aac_160k", "hls"), t("mp3_128", "progressive")])
    assert preset(only, "low") == ("mp3_128", "progressive")


def test_progressif_prefere_a_hls_a_preset_egal():
    track = obj(1, transcodings=[t("aac_96k", "hls"), t("aac_96k", "progressive")])
    assert preset(track, "auto") == ("aac_96k", "progressive")
    assert preset(track, "low") == ("aac_96k", "progressive")


def test_preset_inconnu_en_dernier_recours():
    track = obj(1, transcodings=[t("zzz_1", "hls")])
    assert preset(track, "auto") == ("zzz_1", "hls")


# ---- stream() -----------------------------------------------------------------

def stream_fake(tracks_payload=None, transcode=None):
    fake = make_fake()
    fake.add(API, "/tracks", tracks_payload if tracks_payload is not None else fx("tracks_ids.json"))
    fake.add(API, "/media/", transcode if transcode is not None else fx("transcoding_url.json"))
    return fake


@pytest.mark.parametrize("ident", [1002, "1002", "soundcloud:tracks:1002"])
def test_stream_enchainement_des_requetes(tmp_path, ident):
    fake = stream_fake()
    client, _, _ = make_client(tmp_path, fake)
    url, headers = client.stream(ident, "auto")
    assert url.startswith("https://playback.media-streaming.soundcloud.cloud/")
    assert headers == {}
    first, second = fake.to(API)
    assert first.path == "/tracks" and first.query["ids"] == "1002"      # frais juste avant
    assert second.path.startswith("/media/soundcloud:tracks:1002/")
    assert second.query["client_id"] == CID
    assert second.query["track_authorization"] == "AUTH-FRAIS"           # celle de la réponse fraîche


def test_stream_piste_absente_est_retiree(tmp_path):
    fake = stream_fake(tracks_payload=b"[]")
    client, _, _ = make_client(tmp_path, fake)
    with pytest.raises(NotPlayable) as info:
        client.stream("soundcloud:tracks:1002", "auto")
    assert info.value.reason == "removed"
    assert len(fake.to(API)) == 1                         # rien demandé ensuite


def test_stream_policy_reverifiee(tmp_path):
    for policy, reason in (("SNIP", "snip"), ("BLOCK", "blocked")):
        payload = fxj("tracks_ids.json")
        payload[0]["policy"] = policy
        fake = stream_fake(tracks_payload=json.dumps(payload).encode())
        client, _, _ = make_client(tmp_path, fake)
        with pytest.raises(NotPlayable) as info:
            client.stream(1002, "auto")
        assert info.value.reason == reason
        assert not fake.to(API, "/media/")


def test_stream_url_finale_hors_hotes_media_refusee(tmp_path):
    bad = json.dumps({"url": "https://evil.example/playlist.m3u8"}).encode()
    client, _, _ = make_client(tmp_path, stream_fake(transcode=bad))
    with pytest.raises(UnsafeURL):
        client.stream(1002, "auto")
    for url in ("http://127.0.0.1/x.m3u8", "https://playback.media-streaming.soundcloud.cloud.evil.example/x"):
        client, _, _ = make_client(tmp_path, stream_fake(transcode=json.dumps({"url": url}).encode()))
        with pytest.raises(UnsafeURL):
            client.stream(1002, "auto")


def test_stream_progressif_sur_cdn_sndcdn_accepte(tmp_path):
    ok = json.dumps({"url": "https://cf-media.sndcdn.com/abc.mp3?x=1"}).encode()
    client, _, _ = make_client(tmp_path, stream_fake(transcode=ok))
    assert client.stream(1002, "auto")[0].startswith("https://cf-media.sndcdn.com/")


def test_stream_identifiant_invalide(tmp_path):
    client, _, _ = make_client(tmp_path, make_fake())
    for bad in ("soundcloud:users:5", "abc", "../1", "soundcloud:tracks:"):
        with pytest.raises(ValueError):
            client.stream(bad, "auto")


def test_stream_transcodage_404_piste_retiree(tmp_path):
    client, _, _ = make_client(tmp_path, stream_fake(transcode=(404, b"{}")))
    with pytest.raises(NotPlayable) as info:
        client.stream(1002, "auto")
    assert info.value.reason == "removed"


# ---- DRM : jamais de requête vers un transcodage chiffré -----------------------

def test_aucune_requete_vers_un_transcodage_chiffre(tmp_path):
    drm = fxj("search_tracks.json")["collection"][0]
    mixed = copy.deepcopy(fxj("tracks_ids.json")[0])
    mixed["media"]["transcodings"] = drm["media"]["transcodings"] + mixed["media"]["transcodings"]
    fake = make_fake()
    fake.add(API, "/search/tracks", fx("search_tracks.json"))
    fake.add(API, "/tracks", lambda call: json.dumps(
        [drm] if call.query["ids"] == "1001" else [mixed]).encode())
    fake.add(API, "/media/", fx("transcoding_url.json"))
    client, _, _ = make_client(tmp_path, fake)
    client.search_tracks("daft")
    with pytest.raises(NotPlayable) as info:
        client.stream(1001, "high")                       # DRM uniquement
    assert info.value.reason == "drm"
    client.stream(1002, "high")                            # clair + chiffré mélangés
    client.stream(1002, "low")
    assert fake.calls
    assert not [c for c in fake.calls if "encrypted" in c.url.lower()]
    media = fake.to(API, "/media/")
    assert media and all("/hls/stream/aac_96k" in c.path for c in media)


# ---- Playlist et lots ---------------------------------------------------------

def test_playlist_stubs_completes_par_lots(tmp_path):
    def tracks(call):
        ids = [int(i) for i in call.query["ids"].split(",")]
        return json.dumps([obj(i) for i in ids]).encode()
    fake = make_fake().add(API, "/playlists/77", fx("playlist_with_stubs.json")).add(API, "/tracks", tracks)
    client, _, _ = make_client(tmp_path, fake)
    meta, items = client.playlist(77)
    assert meta.title == "Playlist de test" and meta.id == 77
    assert [i.ref.video_id for i in items] == [
        f"soundcloud:tracks:{n}" for n in (2001, 2002, 2003, 2004, 2005,
                                           3001, 3002, 3003, 3004, 3005, 3006, 3007)]
    batches = fake.to(API, "/tracks")
    assert len(batches) == 1 and batches[0].query["ids"] == "3001,3002,3003,3004,3005,3006,3007"


def test_tracks_lots_de_50_au_plus(tmp_path):
    def tracks(call):
        return json.dumps([obj(int(i)) for i in call.query["ids"].split(",")]).encode()
    fake = make_fake().add(API, "/tracks", tracks)
    client, _, _ = make_client(tmp_path, fake)
    result = client.tracks(list(range(1, 121)))
    assert len(result) == 120
    sizes = [len(c.query["ids"].split(",")) for c in fake.to(API, "/tracks")]
    assert sizes == [50, 50, 20]


def test_playlist_stub_retire_disparait(tmp_path):
    playlist = fxj("playlist_with_stubs.json")
    fake = (make_fake().add(API, "/playlists/77", json.dumps(playlist).encode())
            .add(API, "/tracks", lambda c: json.dumps(
                [obj(int(i)) for i in c.query["ids"].split(",") if i != "3004"]).encode()))
    client, _, _ = make_client(tmp_path, fake)
    _, items = client.playlist(77)
    assert len(items) == 11 and "soundcloud:tracks:3004" not in {i.ref.video_id for i in items}


# ---- Disjoncteur 429 ----------------------------------------------------------

def test_disjoncteur_429(tmp_path):
    fake = (make_fake()
            .add(API, "/search/tracks", (429, fx("rate_limited_429.json")))
            .add(API, "/users/5/tracks", fx("user_tracks_page2.json"))
            .add(API, "/playlists/77", fx("playlist_with_stubs.json"))
            .add(API, "/tracks", lambda c: json.dumps(
                [obj(int(i)) for i in c.query["ids"].split(",")]).encode())
            .add(API, "/media/", fx("transcoding_url.json")))
    client, clock, _ = make_client(tmp_path, fake)
    with pytest.raises(SoundCloudUnavailable):
        client.search_tracks("x")
    assert client.rate_limited
    before = len(fake.calls)
    # Discrétionnaire : aucune requête pendant 10 min.
    with pytest.raises(SoundCloudUnavailable):
        client.search_tracks("x")
    with pytest.raises(SoundCloudUnavailable):
        client.search_users("x")
    with pytest.raises(SoundCloudUnavailable):
        client.search_playlists("x")
    with pytest.raises(SoundCloudUnavailable):
        client.user_tracks(5)
    with pytest.raises(SoundCloudUnavailable):
        client.playlist(77)                    # stubs = discrétionnaire (la playlist elle-même passe)
    assert [c.path for c in fake.calls[before:]] == ["/playlists/77"]
    # Actions déclenchées par l'utilisateur : elles continuent.
    assert len(client.tracks([1, 2])) == 2
    assert client.stream(1, "auto")[0].startswith("https://playback.")
    # Après 10 min, la recherche repart.
    clock.t += 601
    assert not client.rate_limited
    with pytest.raises(SoundCloudUnavailable):
        client.search_tracks("x")
    assert fake.calls[-1].path == "/search/tracks"


# ---- Confidentialité du client_id ----------------------------------------------

def test_client_id_jamais_dans_les_journaux_ni_les_exceptions(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fake = (make_fake()
            .add(API, "/search/tracks", [fx("search_tracks.json"), (403, b"{}")])
            .add(API, "/users/5/tracks", (403, b"{}")))
    client, clock, _ = make_client(tmp_path, fake, preload=OLD)
    client.search_tracks("x")
    errors = []
    for call in (lambda: client.search_tracks("y"), lambda: client.user_tracks(5)):
        try:
            call()
        except SoundCloudUnavailable as exc:
            errors.append(exc)
            chain = exc
            while chain is not None:
                errors.append(chain)
                chain = chain.__cause__
    assert errors
    texts = [caplog.text] + [f"{e!s} {e!r}" for e in errors]
    for text in texts:
        assert CID not in text and OLD not in text
    assert "client_id=***" in caplog.text                  # l'URL est journalisée, masquée


def test_masque_url():
    assert sc._mask(f"https://x/a?client_id={CID}&q=1") == "https://x/a?client_id=***&q=1"


# ---- Divers -------------------------------------------------------------------

def test_recherche_utilisateurs_et_playlists(tmp_path):
    users = {"collection": [{"kind": "user", "id": 5, "username": "Alice", "verified": True,
                             "permalink_url": "https://soundcloud.com/alice", "track_count": 3}]}
    lists = {"collection": [{"kind": "playlist", "id": 9, "title": "Set", "set_type": "album",
                             "track_count": 4, "user": {"username": "Alice"},
                             "permalink_url": "https://soundcloud.com/alice/sets/set"}]}
    fake = (make_fake().add(API, "/search/users", json.dumps(users).encode())
            .add(API, "/search/playlists", json.dumps(lists).encode()))
    client, _, _ = make_client(tmp_path, fake)
    (user,) = client.search_users("alice")
    (playlist,) = client.search_playlists("set")
    assert (user.id, user.username, user.verified, user.track_count) == (5, "Alice", True, 3)
    assert (playlist.id, playlist.set_type, playlist.track_count) == (9, "album", 4)


def test_resolve_permalink(tmp_path):
    user = {"kind": "user", "id": 5, "username": "Alice", "permalink_url": "https://soundcloud.com/alice"}
    fake = make_fake().add(API, "/resolve", lambda c: json.dumps(
        user if c.query["url"].endswith("/alice") else obj(7)).encode())
    client, _, _ = make_client(tmp_path, fake)
    assert isinstance(client.resolve_permalink("https://soundcloud.com/alice"), sc.ScUser)
    assert isinstance(client.resolve_permalink("https://soundcloud.com/u/p-7"), sc.ScTrack)
    with pytest.raises(UnsafeURL):
        client.resolve_permalink("https://evil.example/alice")
    assert len(fake.to(API)) == 2


def test_resolve_404_piste_retiree(tmp_path):
    fake = make_fake().add(API, "/resolve", (404, b"{}"))
    client, _, _ = make_client(tmp_path, fake)
    with pytest.raises(NotPlayable) as info:
        client.resolve_permalink("https://soundcloud.com/u/gone")
    assert info.value.reason == "removed"


def test_related_phase_2(tmp_path):
    client, _, _ = make_client(tmp_path, make_fake())
    with pytest.raises(NotImplementedError):
        client.related(1)


def test_reponse_trop_grosse_ou_illisible(tmp_path):
    fake = make_fake().add(API, "/search/tracks", b"<html>pas du json</html>")
    client, _, _ = make_client(tmp_path, fake)
    with pytest.raises(SoundCloudUnavailable):
        client.search_tracks("x")


def test_reseau_coupe(tmp_path):
    client, _, _ = make_client(tmp_path, make_fake())          # aucune route API
    with pytest.raises(SoundCloudUnavailable):
        client.search_tracks("x")

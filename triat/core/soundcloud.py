"""Client de l'API SoundCloud v2 (non documentée, non officielle).

Voir docs/review/SPEC-SOUNDCLOUD-DESKTOP.md (§2, §4.3, §5, §7.3). Points
d'attention :

- le `client_id` est découvert à l'exécution (scraping de soundcloud.com),
  mis en cache sur disque et JAMAIS écrit dans un journal ni dans un message
  d'exception ;
- aucun transcodage chiffré (DRM) n'est jamais demandé, pas même son URL ;
- une piste « SNIP » (extrait Go+) n'est jamais lue ;
- tout passe par `net.urlopen_checked` (hôtes autorisés, redirections
  revalidées, taille de réponse bornée).

Les formes d'API sont celles sondées le 2026-10-17 ; ce qui n'a pas été sondé
est marqué « non vérifié » en commentaire.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import paths
from .constants import BROWSER_USER_AGENT
from .models import SOUNDCLOUD, TrackRef, is_valid_track_id, sc_numeric_id, source_of, valid_web_url
from .net import (
    MEDIA_HOST_SUFFIXES,
    ResponseTooLarge,
    UnsafeURL,
    check_url,
    read_capped,
    urlopen_checked,
)

log = logging.getLogger(__name__)

API_HOST = "api-v2.soundcloud.com"
API_BASE = f"https://{API_HOST}/"
HOME_URL = "https://soundcloud.com/"          # hôte exact
ASSET_HOST = "a-v2.sndcdn.com"

API_TIMEOUT = 10.0          # secondes, appels d'API
SCRAPE_TIMEOUT = 15.0       # secondes, page d'accueil et scripts
MAX_SCRIPTS = 5             # scripts parcourus au plus par scraping
MAX_SCRIPT_BYTES = 8 * 1024 * 1024
CLIENT_ID_TTL = 24 * 3600   # [décision] durée de vie du cache
SCRAPE_MIN_INTERVAL = 600   # au plus un scraping par 10 min, même en échec
BREAKER_SECONDS = 600       # disjoncteur après un 429
# Taille de lot de `tracks?ids=` : 50 est l'usage courant, maximum réel
# NON VÉRIFIÉ.
BATCH_SIZE = 50

_CLIENT_ID_RE = re.compile(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"')
_VALID_CLIENT_ID = re.compile(r"[0-9a-zA-Z]{32}")
_SCRIPT_RE = re.compile(
    r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"\s]+\.js)"')
_MASK_RE = re.compile(r"(client_id=)[^&#\s]*")

# Presets par ordre de préférence (§5.5). OBSERVÉS : aac_160k, aac_96k,
# abr_sq, mp3_1_0 (extraits). NON VÉRIFIÉS (probables, jamais vus sur une
# piste complète) : mp3_128 progressif et opus_64 hls.
_RANK_HIGH = ("aac_160k", "mp3_128", "opus_64", "aac_96k")
_RANK_LOW = ("aac_96k", "opus_64", "mp3_128")
_PROTOCOL_ORDER = {"progressive": 0, "hls": 1}   # progressif d'abord à preset égal

# Suffixe d'artwork : « -large » (100 px) -> « -t500x500 ». Suffixe NON
# VÉRIFIÉ par requête.
_ARTWORK_LARGE = "-large"
_ARTWORK_BIG = "-t500x500"


class SoundCloudUnavailable(RuntimeError):
    """Réseau, 403 Datadome, client_id introuvable, disjoncteur ouvert."""


class NotPlayable(RuntimeError):
    """Piste non lisible. `reason` : snip | drm | blocked | removed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _NotFound(Exception):
    """404 interne ; chaque appelant décide de ce que cela signifie."""


@dataclass(slots=True, frozen=True)
class ScTrack:
    """Vue normalisée d'un objet piste."""

    ref: TrackRef
    playable: bool
    reason: str = ""            # "" | "snip" | "drm" | "blocked"
    full_duration: float = 0.0


@dataclass(slots=True, frozen=True)
class ScUser:
    id: int
    username: str
    permalink_url: str | None = None
    avatar_url: str | None = None
    verified: bool = False
    track_count: int = 0


@dataclass(slots=True, frozen=True)
class ScPlaylist:
    id: int
    title: str
    username: str = ""
    permalink_url: str | None = None
    artwork_url: str | None = None
    track_count: int = 0
    set_type: str = ""          # « album », etc. (valeurs complètes non vérifiées)


# ---- Utilitaires purs ---------------------------------------------------------

def _mask(text: str) -> str:
    """Masque le client_id dans une URL ou un texte destiné à un journal."""
    return _MASK_RE.sub(r"\1***", text)


def _describe(url: str) -> str:
    """Hôte + chemin, sans requête : sûr pour un message d'exception."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.hostname or '?'}{parts.path}"


def _with_params(url: str, params: dict[str, Any]) -> str:
    """Ajoute des paramètres à une URL, en remplaçant ceux de même nom."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k not in params]
    query.extend((k, str(v)) for k, v in params.items())
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query)))


def _without_client_id(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k != "client_id"]
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query)))


def _transcodings(obj: dict) -> list[dict]:
    media = obj.get("media")
    items = media.get("transcodings") if isinstance(media, dict) else None
    return [t for t in items if isinstance(t, dict)] if isinstance(items, list) else []


def _protocol(transcoding: dict) -> str:
    fmt = transcoding.get("format")
    return str(fmt.get("protocol", "")) if isinstance(fmt, dict) else ""


def _usable_transcodings(obj: dict) -> list[dict]:
    """Transcodages lisibles (spec §2.4) : protocole progressive/hls (jamais
    *-encrypted-hls), ni snipped ni /preview/, preset non « abr* »."""
    usable = []
    for t in _transcodings(obj):
        url = t.get("url")
        if not isinstance(url, str) or not url:
            continue
        if _protocol(t) not in _PROTOCOL_ORDER:
            continue
        if "encrypted" in url.lower():
            continue                    # ceinture et bretelles : jamais de DRM
        if t.get("snipped") or "/preview/" in url:
            continue
        if str(t.get("preset", "")).startswith("abr"):
            continue                    # maître adaptatif, signalé cassé (yt-dlp)
        usable.append(t)
    return usable


def classify(obj: dict) -> tuple[bool, str]:
    """(lisible, raison) d'un objet piste ; raison vaut snip|drm|blocked."""
    policy = str(obj.get("policy") or "").upper()
    if policy == "SNIP":
        return False, "snip"
    if policy == "BLOCK":                # géo-blocage ; valeur NON OBSERVÉE
        return False, "blocked"
    if not obj.get("streamable", False):
        return False, "blocked"
    if _usable_transcodings(obj):
        return True, ""
    all_t = _transcodings(obj)
    if any("encrypted" in _protocol(t).lower() for t in all_t):
        return False, "drm"
    if any(t.get("snipped") or "/preview/" in str(t.get("url", "")) for t in all_t):
        return False, "snip"
    return False, "blocked"


def pick_transcoding(obj: dict, quality: str) -> dict | None:
    """Meilleur transcodage lisible selon la qualité ("auto"/"high"/"low")."""
    ranking = _RANK_LOW if quality == "low" else _RANK_HIGH

    def key(t: dict) -> tuple[int, int]:
        preset = str(t.get("preset", ""))
        rank = ranking.index(preset) if preset in ranking else len(ranking)
        return rank, _PROTOCOL_ORDER.get(_protocol(t), 9)

    candidates = _usable_transcodings(obj)
    return min(candidates, key=key) if candidates else None


def _artwork(obj: dict) -> str | None:
    art = obj.get("artwork_url")
    user = obj.get("user") if isinstance(obj.get("user"), dict) else {}
    url = art.replace(_ARTWORK_LARGE, _ARTWORK_BIG) if isinstance(art, str) and art \
        else user.get("avatar_url")
    if not isinstance(url, str) or not url:
        return None
    try:
        return check_url(url, allow_hosts=MEDIA_HOST_SUFFIXES)
    except UnsafeURL:
        return None


def _seconds(value) -> float:
    try:
        return max(0.0, float(value) / 1000.0)
    except (TypeError, ValueError):
        return 0.0


def to_sc_track(obj: Any) -> ScTrack | None:
    """Objet JSON piste -> ScTrack, ou None si ce n'est pas une piste complète."""
    if not isinstance(obj, dict) or obj.get("kind", "track") != "track":
        return None
    number = obj.get("id")
    if not isinstance(number, int) or isinstance(number, bool) or "title" not in obj:
        return None                      # stub (id, kind, policy seulement)
    urn = obj.get("urn") or f"soundcloud:tracks:{number}"
    if not isinstance(urn, str) or not is_valid_track_id(urn) or source_of(urn) != SOUNDCLOUD:
        return None
    playable, reason = classify(obj)
    user = obj.get("user") if isinstance(obj.get("user"), dict) else {}
    meta = obj.get("publisher_metadata") if isinstance(obj.get("publisher_metadata"), dict) \
        else {}
    full = _seconds(obj.get("full_duration") or obj.get("duration"))
    # Piste SNIP : `duration` vaut 30 s, on affiche la vraie durée.
    duration = full if reason == "snip" else _seconds(obj.get("duration"))
    ref = TrackRef(
        video_id=urn,
        title=str(obj.get("title") or "Sans titre"),
        artist=str(meta.get("artist") or user.get("username") or ""),
        duration=duration,
        thumbnail=_artwork(obj),
        album=meta.get("album_title") or None,       # champ NON VÉRIFIÉ
        web_url=valid_web_url(obj.get("permalink_url")),
    )
    return ScTrack(ref=ref, playable=playable, reason=reason, full_duration=full)


def _to_user(obj: Any) -> ScUser | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("id"), int):
        return None
    return ScUser(
        id=obj["id"], username=str(obj.get("username") or ""),
        permalink_url=valid_web_url(obj.get("permalink_url")),
        avatar_url=obj.get("avatar_url") if isinstance(obj.get("avatar_url"), str) else None,
        verified=bool(obj.get("verified")),
        track_count=int(obj.get("track_count") or 0))


def _to_playlist(obj: Any) -> ScPlaylist | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("id"), int):
        return None
    user = obj.get("user") if isinstance(obj.get("user"), dict) else {}
    return ScPlaylist(
        id=obj["id"], title=str(obj.get("title") or "Sans titre"),
        username=str(user.get("username") or ""),
        permalink_url=valid_web_url(obj.get("permalink_url")),
        artwork_url=_artwork(obj), track_count=int(obj.get("track_count") or 0),
        set_type=str(obj.get("set_type") or ""))


# ---- Client -------------------------------------------------------------------

class SoundCloudClient:
    def __init__(self, opener=urlopen_checked, cache_path=None, clock=time.time):
        self._opener = opener
        self._cache_path = cache_path
        self._clock = clock
        self._lock = threading.RLock()
        self._client_id: str | None = None
        self._found_at = 0.0
        self._last_scrape: float | None = None      # dernier scraping tenté
        self._breaker_until = 0.0

    # -- Disjoncteur 429 --------------------------------------------------------

    @property
    def rate_limited(self) -> bool:
        return self._clock() < self._breaker_until

    def _trip(self) -> None:
        self._breaker_until = self._clock() + BREAKER_SECONDS
        log.warning("SoundCloud: 429, requêtes discrétionnaires suspendues %d s",
                    BREAKER_SECONDS)

    # -- client_id --------------------------------------------------------------

    def _path(self):
        return self._cache_path if self._cache_path is not None \
            else paths.cache_dir() / "soundcloud.json"

    def _read_cache(self) -> tuple[str, float] | None:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
            cid, found = data["client_id"], float(data["found_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(cid, str) or not _VALID_CLIENT_ID.fullmatch(cid):
            return None
        return cid, found

    def _write_cache(self, cid: str, found_at: float) -> None:
        path = self._path()
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        payload = json.dumps({"client_id": cid, "found_at": found_at}).encode()
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)        # atomique
        except OSError as exc:
            log.warning("Cache SoundCloud non écrit: %s", exc.__class__.__name__)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def client_id(self, refresh: bool = False) -> str:
        """client_id courant : mémoire, puis disque (TTL 24 h), puis scraping."""
        with self._lock:
            now = self._clock()
            if not refresh:
                if self._client_id and now - self._found_at < CLIENT_ID_TTL:
                    return self._client_id
                cached = self._read_cache()
                if cached and 0 <= now - cached[1] < CLIENT_ID_TTL:
                    self._client_id, self._found_at = cached
                    return self._client_id
            self._client_id = None
            cid = self._scrape()
            self._client_id, self._found_at = cid, self._clock()
            self._write_cache(cid, self._found_at)
            return cid

    def _fetch(self, url: str, timeout: float, allow_hosts: tuple[str, ...],
               accept: str, limit: int | None = None) -> bytes:
        """Une requête GET ; les erreurs HTTP remontent en `HTTPError`."""
        request = urllib.request.Request(url, headers={
            "User-Agent": BROWSER_USER_AGENT, "Accept": accept})
        log.debug("GET %s", _mask(url))
        try:
            with self._opener(request, timeout=timeout, allow_hosts=allow_hosts) as response:
                return read_capped(response) if limit is None else read_capped(response, limit)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, ResponseTooLarge) as exc:
            raise SoundCloudUnavailable(
                f"Réseau: {exc.__class__.__name__} ({_describe(url)})") from exc

    def _scrape(self) -> str:
        """Cherche le client_id dans les scripts de soundcloud.com. Au plus un
        scraping par 10 min, même en échec."""
        now = self._clock()
        if self._last_scrape is not None and now - self._last_scrape < SCRAPE_MIN_INTERVAL:
            raise SoundCloudUnavailable("client_id introuvable (scraping récent)")
        self._last_scrape = now
        try:
            html = self._fetch(HOME_URL, SCRAPE_TIMEOUT, ("soundcloud.com",),
                               "text/html").decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise SoundCloudUnavailable(f"Page d'accueil: HTTP {exc.code}") from exc
        scripts = _SCRIPT_RE.findall(html)
        for url in reversed(scripts[-MAX_SCRIPTS:]):    # du dernier au premier
            try:
                check_url(url, allow_hosts=(ASSET_HOST,))
                body = self._fetch(url, SCRAPE_TIMEOUT, (ASSET_HOST,),
                                   "*/*", MAX_SCRIPT_BYTES).decode("utf-8", "replace")
            except (urllib.error.HTTPError, SoundCloudUnavailable, UnsafeURL):
                continue
            match = _CLIENT_ID_RE.search(body)
            if match:
                return match.group(1)
        raise SoundCloudUnavailable("client_id introuvable")

    # -- Requêtes JSON ----------------------------------------------------------

    def _json(self, build: Callable[[str], str], *, discretionary: bool,
              allow_hosts: tuple[str, ...] = (API_HOST,)) -> Any:
        """GET JSON avec client_id. 401/403 : un seul re-scrape et une seule
        nouvelle tentative. 429 : ouvre le disjoncteur. 404 : `_NotFound`."""
        if discretionary and self.rate_limited:
            raise SoundCloudUnavailable("SoundCloud: pause après limitation (429)")
        for attempt in (0, 1):
            cid = self.client_id(refresh=attempt == 1)
            url = build(cid)
            try:
                raw = self._fetch(url, API_TIMEOUT, allow_hosts, "application/json")
            except urllib.error.HTTPError as exc:
                code = exc.code
                if code in (401, 403):
                    if attempt == 0:
                        log.info("SoundCloud: HTTP %d, nouvelle recherche du client_id", code)
                        continue
                    raise SoundCloudUnavailable(
                        f"SoundCloud refuse l'accès (HTTP {code})") from exc
                if code == 429:
                    self._trip()
                    raise SoundCloudUnavailable("SoundCloud: trop de requêtes (429)") from exc
                if code in (404, 410):
                    raise _NotFound(_describe(url)) from exc
                raise SoundCloudUnavailable(f"SoundCloud: HTTP {code}") from exc
            try:
                return json.loads(raw)
            except ValueError as exc:
                raise SoundCloudUnavailable("Réponse SoundCloud illisible") from exc
        raise SoundCloudUnavailable("SoundCloud refuse l'accès")   # inatteignable

    def _api(self, path: str, params: dict[str, Any], *, discretionary: bool) -> Any:
        def build(cid: str) -> str:
            return _with_params(API_BASE + path, {**params, "client_id": cid})
        try:
            return self._json(build, discretionary=discretionary)
        except _NotFound as exc:
            raise SoundCloudUnavailable("SoundCloud: ressource introuvable") from exc

    def _next_href(self, value: Any) -> str | None:
        """next_href renvoyé, validé (hôte API, https) et sans client_id."""
        if not isinstance(value, str) or not value:
            return None
        try:
            return _without_client_id(self._check_href(value))
        except UnsafeURL:
            log.warning("SoundCloud: next_href hors périmètre ignoré")
            return None

    @staticmethod
    def _check_href(href: str) -> str:
        if not href.lower().startswith("https://"):
            raise UnsafeURL("next_href non https")
        return check_url(href, allow_hosts=(API_HOST,))

    @staticmethod
    def _tracks_of(items: Any) -> list[ScTrack]:
        found = (to_sc_track(i) for i in items) if isinstance(items, list) else ()
        return [t for t in found if t is not None]

    # -- Recherche --------------------------------------------------------------

    def search_tracks(self, q: str, limit: int = 20,
                      offset: int = 0) -> tuple[list[ScTrack], str | None]:
        data = self._api("search/tracks", {"q": q, "limit": limit, "offset": offset},
                         discretionary=True)
        data = data if isinstance(data, dict) else {}
        return self._tracks_of(data.get("collection")), self._next_href(data.get("next_href"))

    def search_users(self, q: str, limit: int = 5) -> list[ScUser]:
        data = self._api("search/users", {"q": q, "limit": limit}, discretionary=True)
        items = data.get("collection") if isinstance(data, dict) else None
        found = (_to_user(i) for i in items) if isinstance(items, list) else ()
        return [u for u in found if u is not None]

    def search_playlists(self, q: str, limit: int = 5) -> list[ScPlaylist]:
        data = self._api("search/playlists", {"q": q, "limit": limit}, discretionary=True)
        items = data.get("collection") if isinstance(data, dict) else None
        found = (_to_playlist(i) for i in items) if isinstance(items, list) else ()
        return [p for p in found if p is not None]

    # -- Pistes -----------------------------------------------------------------

    def _tracks_batches(self, ids: Sequence[int], *, discretionary: bool) -> list[ScTrack]:
        wanted = list(dict.fromkeys(int(i) for i in ids))
        result: list[ScTrack] = []
        for start in range(0, len(wanted), BATCH_SIZE):
            chunk = wanted[start:start + BATCH_SIZE]
            # Réponse : liste JSON NUE (pas de `collection`).
            data = self._api("tracks", {"ids": ",".join(map(str, chunk))},
                             discretionary=discretionary)
            result.extend(self._tracks_of(data))
        return result

    def tracks(self, ids: Sequence[int]) -> list[ScTrack]:
        """Pistes par lots de 50 ; déclenché par l'utilisateur, donc non
        soumis au disjoncteur 429."""
        return self._tracks_batches(ids, discretionary=False)

    def user_tracks(self, user_id: int, limit: int = 50,
                    next_href: str | None = None) -> tuple[list[ScTrack], str | None]:
        if next_href is None:
            data = self._api(f"users/{int(user_id)}/tracks", {"limit": limit},
                             discretionary=True)
        else:
            href = _without_client_id(self._check_href(next_href))   # UnsafeURL sinon
            try:
                data = self._json(lambda cid: _with_params(href, {"client_id": cid}),
                                  discretionary=True)
            except _NotFound as exc:
                raise SoundCloudUnavailable("SoundCloud: page introuvable") from exc
        data = data if isinstance(data, dict) else {}
        return self._tracks_of(data.get("collection")), self._next_href(data.get("next_href"))

    def playlist(self, playlist_id: int) -> tuple[ScPlaylist, list[ScTrack]]:
        """Playlist ; `tracks` = quelques pistes complètes puis des stubs
        {id, kind, policy}, complétés par lots (discrétionnaire)."""
        data = self._api(f"playlists/{int(playlist_id)}", {}, discretionary=False)
        meta = _to_playlist(data)
        if meta is None:
            raise SoundCloudUnavailable("Playlist SoundCloud illisible")
        items = data.get("tracks") if isinstance(data.get("tracks"), list) else []
        order: list[int] = []
        full: dict[int, ScTrack] = {}
        stubs: list[int] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                continue
            order.append(item["id"])
            track = to_sc_track(item)
            if track is not None:
                full[item["id"]] = track
            else:
                stubs.append(item["id"])
        if stubs:
            for track in self._tracks_batches(stubs, discretionary=True):
                number = sc_numeric_id(track.ref.video_id)
                if number is not None:
                    full[number] = track
        # Une piste retirée (stub sans réponse) disparaît de la liste.
        return meta, [full[i] for i in dict.fromkeys(order) if i in full]

    def related(self, track_id, limit: int = 20):
        """Radio (`tracks/<id>/related`) : phase 2, non implémentée."""
        raise NotImplementedError("SoundCloud related: phase 2 (spec §9)")

    def resolve_permalink(self, url: str) -> ScTrack | ScUser | ScPlaylist:
        """URL web -> objet. Les liens courts on.soundcloud.com sont NON
        VÉRIFIÉS ; pistes et sets le sont aussi pour `resolve`."""
        check_url(url, allow_hosts=("soundcloud.com",))
        def build(cid: str) -> str:
            return _with_params(API_BASE + "resolve", {"url": url, "client_id": cid})
        try:
            data = self._json(build, discretionary=False)
        except _NotFound as exc:
            raise NotPlayable("removed") from exc
        kind = data.get("kind") if isinstance(data, dict) else None
        found = {"track": to_sc_track, "user": _to_user,
                 "playlist": _to_playlist}.get(kind)
        result = found(data) if found else None
        if result is None:
            raise SoundCloudUnavailable("Lien SoundCloud non géré")
        return result

    # -- Flux -------------------------------------------------------------------

    def stream(self, track_id_or_urn, quality: str = "auto") -> tuple[str, dict]:
        """(url, en-têtes) d'un flux lisible, ou `NotPlayable`.

        Piste relue juste avant (policy et autorisation fraîches) ; jamais de
        requête vers un transcodage chiffré.
        """
        if isinstance(track_id_or_urn, int):
            number: int | None = track_id_or_urn
        else:
            text = str(track_id_or_urn)
            number = sc_numeric_id(text) if ":" in text else (
                int(text) if text.isdigit() else None)
        if number is None:
            raise ValueError("Identifiant SoundCloud invalide")
        data = self._api("tracks", {"ids": number}, discretionary=False)
        obj = next((o for o in data if isinstance(o, dict) and o.get("id") == number),
                   None) if isinstance(data, list) else None
        if obj is None:
            raise NotPlayable("removed")
        playable, reason = classify(obj)
        if not playable:
            raise NotPlayable(reason)
        chosen = pick_transcoding(obj, quality)
        if chosen is None:
            raise NotPlayable("blocked")
        base = check_url(chosen["url"], allow_hosts=(API_HOST,))
        auth = obj.get("track_authorization")
        extra = {"track_authorization": auth} if auth else {}

        def build(cid: str) -> str:
            return _with_params(base, {"client_id": cid, **extra})
        try:
            data = self._json(build, discretionary=False)
        except _NotFound as exc:
            raise NotPlayable("removed") from exc
        final = data.get("url") if isinstance(data, dict) else None
        if not isinstance(final, str):
            raise SoundCloudUnavailable("Flux SoundCloud illisible")
        # Échec franc (UnsafeURL) si l'hôte n'est pas un hôte média connu.
        return check_url(final, allow_hosts=MEDIA_HOST_SUFFIXES), {}

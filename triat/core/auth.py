"""Gestion des cookies YouTube.

Un cookie de session Google donne accès au compte : il est traité comme un
secret. Le contenu est conservé chiffré dans le trousseau système (Secret
Service via `keyring`) et n'est jamais écrit en clair sur disque : yt-dlp
le reçoit en mémoire (`load_into`). Il n'est jamais journalisé.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import keyring
import keyring.errors
from yt_dlp.cookies import SUPPORTED_BROWSERS, extract_cookies_from_browser

from .constants import KEYRING_SERVICE

log = logging.getLogger(__name__)

KEYRING_KEY = "youtube-cookies"
# Après un échec du trousseau (D-Bus absent, trousseau verrouillé), on ne
# réessaie pas avant ce délai : chaque tentative peut bloquer.
KEYRING_RETRY_DELAY = 60.0

# Cookies qui prouvent une session authentifiée sur YouTube.
SESSION_COOKIES = ("SID", "__Secure-3PSID", "SAPISID", "__Secure-3PAPISID")

# Un profil Firefox actif porte des centaines de cookies YouTube, dont une
# majorité de traces de lecture (« ST-… »). Les envoyer tous produit un
# en-tête de plusieurs kilo-octets, que YouTube rejette en HTTP 413. Seuls
# ceux-ci servent à l'authentification et aux préférences.
USEFUL_COOKIES = frozenset({
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "LOGIN_INFO", "SIDCC", "PREF", "YSC",
    "VISITOR_INFO1_LIVE", "VISITOR_PRIVACY_METADATA",
    "CONSENT", "SOCS", "__Secure-YNID",
})
YOUTUBE_DOMAINS = (".youtube.com", "youtube.com", ".google.com")


class CookieError(RuntimeError):
    pass


@dataclass(slots=True)
class CookieStatus:
    present: bool
    source: str = ""
    count: int = 0
    authenticated: bool = False
    expires_at: float | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at < time.time()

    @property
    def days_left(self) -> float | None:
        if self.expires_at is None:
            return None
        return max(0.0, (self.expires_at - time.time()) / 86400)


def _json_to_netscape(text: str) -> str | None:
    """Convertit un export JSON d'extension navigateur en format Netscape.

    Cookie-Editor, Cookie Quick Manager et EditThisCookie exportent une
    liste d'objets plutôt qu'un cookies.txt. On accepte les deux.
    Renvoie None si ce n'est pas du JSON exploitable.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        # Certaines extensions enveloppent la liste.
        data = data.get("cookies") or data.get("Request Cookies") or []
    if not isinstance(data, list):
        return None

    lines = ["# Netscape HTTP Cookie File", "# Converti depuis un export JSON"]
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        value = entry.get("value")
        domain = entry.get("domain")
        if not name or value is None or not domain:
            continue
        # hostOnly=False signifie « ce domaine et ses sous-domaines ».
        include_sub = not entry.get("hostOnly", not domain.startswith("."))
        expires = entry.get("expirationDate") or entry.get("expires") or 0
        try:
            expires = int(float(expires))
        except (TypeError, ValueError):
            expires = 0
        lines.append("\t".join([
            domain,
            "TRUE" if include_sub else "FALSE",
            entry.get("path") or "/",
            "TRUE" if entry.get("secure") else "FALSE",
            str(expires),
            str(name),
            str(value),
        ]))
    return "\n".join(lines) + "\n" if len(lines) > 2 else None


def _parse_netscape(text: str) -> list[list[str]]:
    """Valide et découpe un fichier cookies au format Netscape."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        rows.append(fields)
    if not rows:
        raise CookieError(
            "Format non reconnu : attendu un cookies.txt Netscape "
            "(7 colonnes tabulées) ou un export JSON d'extension navigateur."
        )
    return rows


def _rows_to_netscape(rows: list[list[str]]) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    lines.extend("\t".join(fields) for fields in rows)
    return "\n".join(lines) + "\n"


def _is_youtube_domain(domain: str) -> bool:
    """`.youtube.com` ou `music.youtube.com`, pas `notyoutube.com`."""
    host = (domain or "").lstrip(".").lower()
    return any(host == d.lstrip(".") or host.endswith("." + d.lstrip("."))
               for d in YOUTUBE_DOMAINS)


_UNSET = object()


class CookieManager:
    """Source unique de vérité pour les cookies, côté yt-dlp comme ytmusicapi."""

    def __init__(self, settings=None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._clock = clock
        # Échec du trousseau mémorisé jusqu'à cette date (horloge `clock`).
        self._failed_until = 0.0
        # Texte des cookies gardé en mémoire : le trousseau est un appel
        # D-Bus, trop lent pour chaque requête InnerTube ou résolution.
        # Invalidé par `_store` et `clear`.
        self._text_cache = _UNSET

    # ---- Import --------------------------------------------------------

    def import_file(self, path: str | Path) -> CookieStatus:
        """Importe un cookies.txt Netscape exporté depuis un navigateur."""
        source_path = Path(path).expanduser()
        try:
            text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CookieError(f"Lecture impossible : {exc.strerror}") from exc

        # Un export d'extension est du JSON : on le convertit avant de valider.
        converted = _json_to_netscape(text)
        if converted is not None:
            text = converted
        rows = _parse_netscape(text)   # valide avant de stocker
        # Un export « tous sites » contient des cookies sans rapport : on ne
        # garde (et ne stocke dans le trousseau) que YouTube / Google.
        kept = [fields for fields in rows if _is_youtube_domain(fields[0])]
        if not kept:
            raise CookieError(
                "Aucun cookie YouTube / Google dans ce fichier. "
                "Exporte les cookies d'une session connectée à YouTube."
            )
        text = _rows_to_netscape(kept)
        self._store(text, source=f"file:{source_path.name}")
        return self.status()

    def import_from_browser(self, browser: str, profile: str | None = None
                            ) -> CookieStatus:
        """Extrait les cookies YouTube d'un navigateur installé.

        L'extraction est faite une fois puis mémorisée : on ne relit pas la
        base du navigateur à chaque lancement (elle peut être verrouillée
        quand le navigateur tourne).
        """
        name = browser.lower().strip()
        if name not in SUPPORTED_BROWSERS:
            raise CookieError(
                f"Navigateur non pris en charge : {browser}. "
                f"Disponibles : {', '.join(sorted(SUPPORTED_BROWSERS))}"
            )
        try:
            jar = extract_cookies_from_browser(name, profile=profile,
                                               logger=_SilentLogger())
        except Exception as exc:
            raise CookieError(f"Extraction depuis {browser} impossible : {exc}") from exc

        rows = [
            [
                cookie.domain,
                "TRUE" if cookie.domain.startswith(".") else "FALSE",
                cookie.path or "/",
                "TRUE" if cookie.secure else "FALSE",
                str(int(cookie.expires or 0)),
                cookie.name,
                cookie.value or "",
            ]
            for cookie in jar if _is_youtube_domain(cookie.domain)
        ]
        if not rows:
            raise CookieError(
                f"Aucun cookie YouTube trouvé dans {browser}. "
                "Connecte-toi à YouTube dans ce navigateur, puis réessaie."
            )

        text = _rows_to_netscape(rows)     # en mémoire, jamais sur disque
        self._store(text, source=f"browser:{name}")
        return self.status()

    # ---- Consommation --------------------------------------------------

    def load_into(self, ydl) -> bool:
        """Charge les cookies dans un `YoutubeDL`, sans fichier sur disque."""
        text = self._load()
        if not text:
            return False
        try:
            # `load` accepte un objet fichier : rien n'est écrit sur disque.
            ydl.cookiejar.load(io.StringIO(text), ignore_discard=True,
                               ignore_expires=True)
        except Exception as exc:
            log.warning("Cookies non chargés dans yt-dlp : %s", exc)
            return False
        return True

    def cookie_header(self) -> str | None:
        """En-tête `Cookie:` pour ytmusicapi. None si pas de cookies."""
        text = self._load()
        if text is None:
            return None
        pairs = []
        seen: set[str] = set()
        for fields in _parse_netscape(text):
            domain, _flag, _path, _secure, _expires, name, value = fields
            if name in seen or name not in USEFUL_COOKIES:
                continue
            if _is_youtube_domain(domain):
                seen.add(name)
                pairs.append(f"{name}={value}")
        return "; ".join(pairs) if pairs else None

    # ---- État ----------------------------------------------------------

    def sapisid(self) -> str | None:
        """Valeur du cookie SAPISID (ou son équivalent), si présente."""
        text = self._load()
        if text is None:
            return None
        try:
            rows = _parse_netscape(text)
        except CookieError:
            return None
        found = {fields[5]: fields[6] for fields in rows}
        for name in ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID"):
            if found.get(name):
                return found[name]
        return None

    def authorization_header(
            self, origin: str = "https://music.youtube.com") -> str | None:
        """En-tête `Authorization: SAPISIDHASH …` attendu par les API Google.

        Algorithme public des clients web Google : SHA-1 de
        « horodatage espace SAPISID espace origine ». Sans ce cookie,
        aucune session à authentifier : on renvoie None.
        """
        sapisid = self.sapisid()
        if not sapisid:
            return None
        stamp = str(int(time.time()))
        digest = hashlib.sha1(
            f"{stamp} {sapisid} {origin}".encode()).hexdigest()
        return f"SAPISIDHASH {stamp}_{digest}"

    def status(self) -> CookieStatus:
        text = self._load()
        if text is None:
            return CookieStatus(present=False)
        try:
            rows = _parse_netscape(text)
        except CookieError:
            return CookieStatus(present=False)

        names = {fields[5] for fields in rows}
        soonest: float | None = None
        for fields in rows:
            if fields[5] not in SESSION_COOKIES:
                continue
            try:
                expires = float(fields[4])
            except ValueError:
                continue
            if expires > 0 and (soonest is None or expires < soonest):
                soonest = expires

        source = ""
        if self._settings is not None:
            source = (self._settings.get("cookie_source") or {}).get("label", "")
        return CookieStatus(
            present=True,
            source=source,
            count=len(rows),
            authenticated=bool(names & set(SESSION_COOKIES)),
            expires_at=soonest,
        )

    def release(self) -> None:
        """Appelé à la fermeture. Rien n'est matérialisé sur disque : sans
        effet, conservé pour les appelants existants."""

    def clear(self) -> None:
        """Efface les cookies du trousseau."""
        self._text_cache = _UNSET
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_KEY)
        except keyring.errors.PasswordDeleteError:
            self._text_cache = None
            self._failed_until = 0.0
        except Exception as exc:
            log.warning("Suppression dans le trousseau impossible: %s", exc)
        else:
            self._text_cache = None
            self._failed_until = 0.0
        if self._settings is not None:
            self._settings.set("cookie_source", None)
            self._settings.save()

    # ---- Interne -------------------------------------------------------

    def _store(self, text: str, source: str) -> None:
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_KEY, text)
        except Exception as exc:
            raise CookieError(
                f"Écriture dans le trousseau impossible : {exc}. "
                "Un service de trousseau (gnome-keyring, kwallet) est requis."
            ) from exc
        self._text_cache = text
        self._failed_until = 0.0
        if self._settings is not None:
            kind, _, detail = source.partition(":")
            self._settings.set("cookie_source",
                               {"kind": kind, "detail": detail, "label": source})
            self._settings.save()

    def _load(self) -> str | None:
        cached = self._text_cache
        if cached is not _UNSET:
            return cached
        if self._clock() < self._failed_until:
            return None                  # échec récent : pas d'aller-retour D-Bus
        try:
            text = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY)
        except Exception as exc:
            log.warning("Lecture du trousseau impossible: %s", exc)
            self._failed_until = self._clock() + KEYRING_RETRY_DELAY
            return None
        self._text_cache = text
        return text

    def preload(self) -> None:
        """Lit le trousseau maintenant. À appeler hors du fil GTK (l'appel
        D-Bus peut bloquer) pour que les lectures suivantes viennent de la
        mémoire."""
        self._load()


class _SilentLogger:
    """yt-dlp journalise le détail de l'extraction : on le coupe."""

    def debug(self, *_args, **_kwargs) -> None: ...
    def info(self, *_args, **_kwargs) -> None: ...
    def warning(self, *_args, **_kwargs) -> None: ...
    def error(self, *_args, **_kwargs) -> None: ...

"""Scrobbling ListenBrainz (TODO §4.13).

ListenBrainz plutôt que Last.fm : l'API est ouverte, un simple jeton
utilisateur suffit, et les données restent exportables. Le jeton est traité
comme un secret — trousseau système, jamais en clair.

Les envois sont différés et non bloquants : une panne du service ne doit
jamais gêner la lecture. Une écoute dont l'envoi échoue (réseau coupé,
service en panne) est gardée dans une petite file locale, rejouée au
prochain envoi réussi.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

import keyring
import keyring.errors

from .constants import KEYRING_SERVICE
from .net import USER_AGENT_FALLBACK, read_capped, urlopen_checked
from .paths import state_dir

log = logging.getLogger(__name__)

API = "https://api.listenbrainz.org/1"
KEYRING_KEY = "listenbrainz-token"
TIMEOUT = 10.0

# Règles ListenBrainz : une écoute compte au-delà de 4 minutes ou de la
# moitié de la piste, et une piste de moins de 30 s ne compte pas.
MIN_TRACK_SECONDS = 30.0
HALF_OR_FOUR_MINUTES = 240.0

# File des écoutes non envoyées : bornée, la plus ancienne part la première.
QUEUE_FILENAME = "scrobble-queue.json"
QUEUE_MAX = 500
# ListenBrainz accepte un lot (`import`) ; on reste raisonnable.
QUEUE_BATCH = 50

_UNSET = object()


class ScrobbleError(RuntimeError):
    pass


def should_submit(played: float, duration: float) -> bool:
    """Vrai si l'écoute mérite d'être déclarée."""
    if duration and duration < MIN_TRACK_SECONDS:
        return False
    if played < 30.0:
        return False
    if not duration:
        return played >= HALF_OR_FOUR_MINUTES
    return played >= min(duration / 2, HALF_OR_FOUR_MINUTES)


class Scrobbler:
    """Client ListenBrainz. Ne lève jamais pendant la lecture."""

    def __init__(self, settings=None, queue_path=None) -> None:
        self._settings = settings
        self._queue_path = queue_path
        # Jeton gardé en mémoire : le trousseau est un appel D-Bus, qui ne
        # doit jamais se produire sur le fil GTK (`enabled`).
        self._token_cache = _UNSET
        self._token_loading = False
        self._token_lock = threading.Lock()
        self._queue_lock = threading.Lock()

    # ---- Jeton -----------------------------------------------------------

    @property
    def token(self) -> str | None:
        """Jeton ; lit le trousseau au premier appel (bloquant), puis la
        mémoire. À n'appeler que depuis un fil de travail."""
        cached = self._token_cache
        if cached is not _UNSET:
            return cached
        try:
            value = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY)
        except Exception as exc:
            log.warning("Jeton ListenBrainz illisible : %s", exc)
            return None
        with self._token_lock:
            # Un set_token / clear concurrent a le dernier mot.
            if self._token_cache is _UNSET:
                self._token_cache = value
            return self._token_cache

    def preload(self) -> None:
        """Charge le jeton en arrière-plan, si la déclaration est activée."""
        if self._token_cache is not _UNSET or self._token_loading:
            return
        if self._settings is not None and not self._settings.get("scrobble_enabled"):
            return
        self._token_loading = True

        def load() -> None:
            try:
                _ = self.token
            finally:
                self._token_loading = False

        threading.Thread(target=load, daemon=True).start()

    @property
    def enabled(self) -> bool:
        """Sans bloquer : tant que le jeton n'est pas en mémoire, répond
        False et le charge en arrière-plan (appelé depuis le fil GTK)."""
        if self._settings is not None and not self._settings.get("scrobble_enabled"):
            return False
        cached = self._token_cache
        if cached is _UNSET:
            self.preload()
            return False
        return bool(cached)

    def set_token(self, token: str) -> str:
        """Enregistre le jeton après l'avoir validé. Renvoie le nom d'utilisateur."""
        token = (token or "").strip()
        if not token:
            raise ScrobbleError("Jeton vide")
        name = self.validate(token)
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_KEY, token)
        except Exception as exc:
            raise ScrobbleError(f"Écriture dans le trousseau impossible : {exc}") from exc
        with self._token_lock:
            self._token_cache = token
        if self._settings is not None:
            self._settings.set("scrobble_user", name)
            self._settings.set("scrobble_enabled", True)
            self._settings.save()
        return name

    def clear_queue(self) -> None:
        """Vide la file des écoutes en attente (le jeton est conservé)."""
        with self._queue_lock:
            self._write_queue([])

    def clear(self) -> None:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_KEY)
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception as exc:
            log.warning("Suppression du jeton impossible : %s", exc)
        with self._token_lock:
            self._token_cache = None
        self._write_queue([])
        if self._settings is not None:
            self._settings.set("scrobble_user", "")
            self._settings.set("scrobble_enabled", False)
            self._settings.save()

    def validate(self, token: str) -> str:
        """Vérifie le jeton et renvoie le nom d'utilisateur associé."""
        request = urllib.request.Request(
            f"{API}/validate-token",
            headers={"Authorization": f"Token {token}",
                     "User-Agent": USER_AGENT_FALLBACK})
        try:
            with urlopen_checked(request, TIMEOUT, ("listenbrainz.org",)) as response:
                payload = json.loads(read_capped(response).decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise ScrobbleError(
                "Jeton refusé" if exc.code in (401, 403)
                else f"ListenBrainz a répondu {exc.code}") from exc
        except Exception as exc:
            raise ScrobbleError(f"ListenBrainz injoignable : {exc}") from exc
        if not payload.get("valid"):
            raise ScrobbleError("Jeton refusé")
        return payload.get("user_name") or "inconnu"

    # ---- Envois ----------------------------------------------------------

    @staticmethod
    def _metadata(ref) -> dict:
        data = {
            "artist_name": (ref.artist or "").strip() or "Inconnu",
            "track_name": (ref.title or "").strip() or "Inconnu",
        }
        if getattr(ref, "album", None):
            data["release_name"] = ref.album
        data["additional_info"] = {
            "media_player": "Triat",
            "submission_client": "Triat",
            "music_service": ("soundcloud.com" if ref.source == "soundcloud"
                              else "youtube.com"),
            "origin_url": ref.page_url,
        }
        return data

    def _post(self, body: dict) -> str:
        """Envoie `body`. Renvoie "ok", "retry" (panne passagère : à garder)
        ou "drop" (refus définitif : inutile de réessayer)."""
        token = self.token
        if not token:
            return "drop"
        request = urllib.request.Request(
            f"{API}/submit-listens",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Token {token}",
                     "Content-Type": "application/json",
                     "User-Agent": USER_AGENT_FALLBACK})
        try:
            with urlopen_checked(request, TIMEOUT, ("listenbrainz.org",)) as response:
                response.read(1024)
        except urllib.error.HTTPError as exc:
            log.info("Envoi ListenBrainz refusé : %s", exc)
            return "retry" if exc.code in (408, 429) or exc.code >= 500 else "drop"
        except Exception as exc:
            log.info("Envoi ListenBrainz échoué : %s", exc)
            return "retry"
        return "ok"

    def _post_async(self, body: dict) -> None:
        threading.Thread(target=self._post, args=(body,), daemon=True).start()

    def now_playing(self, ref) -> None:
        """Signale la piste en cours, sans l'enregistrer."""
        if not self.enabled or ref is None:
            return
        self._post_async({
            "listen_type": "playing_now",
            "payload": [{"track_metadata": self._metadata(ref)}],
        })

    def submit(self, ref, played: float, duration: float,
               started_at: float | None = None) -> bool:
        """Déclare une écoute terminée. Renvoie False si elle ne compte pas.

        `started_at` : début de l'écoute (epoch). ListenBrainz date l'écoute
        à son début, pas à sa fin ; à défaut on le déduit de `played`.
        """
        if not self.enabled or ref is None:
            return False
        if not should_submit(played, duration or getattr(ref, "duration", 0)):
            return False
        if started_at is None:
            started_at = time.time() - max(0.0, played)
        listen = {"listened_at": int(started_at),
                  "track_metadata": self._metadata(ref)}
        threading.Thread(target=self._deliver, args=(listen,),
                         daemon=True).start()
        return True

    def _deliver(self, listen: dict) -> None:
        """Envoie l'écoute ; en cas de panne la garde, et à la réussite
        rejoue ce qui attendait."""
        result = self._post({"listen_type": "single", "payload": [listen]})
        if result == "ok":
            self.flush_queue()
        elif result == "retry":
            self._enqueue(listen)

    # ---- File de reprise -------------------------------------------------

    def _path(self):
        return self._queue_path or (state_dir() / QUEUE_FILENAME)

    def pending(self) -> list[dict]:
        """Écoutes en attente d'envoi (copie)."""
        with self._queue_lock:
            return self._read_queue()

    def _read_queue(self) -> list[dict]:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)
                and isinstance(d.get("listened_at"), int)
                and isinstance(d.get("track_metadata"), dict)][-QUEUE_MAX:]

    def _write_queue(self, items: list[dict]) -> None:
        path = self._path()
        try:
            if not items:
                path.unlink(missing_ok=True)
                return
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(items[-QUEUE_MAX:], handle)
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("File de scrobbles illisible/inscriptible : %s", exc)

    def _enqueue(self, listen: dict) -> None:
        with self._queue_lock:
            items = self._read_queue()
            items.append(listen)
            self._write_queue(items[-QUEUE_MAX:])

    def flush_queue(self) -> int:
        """Renvoie les écoutes en attente. Retourne le nombre envoyé."""
        sent = 0
        with self._queue_lock:
            items = self._read_queue()
            while items:
                batch = items[:QUEUE_BATCH]
                result = self._post({"listen_type": "import", "payload": batch})
                if result == "retry":
                    break
                # "ok" : envoyé ; "drop" : refusé pour de bon, on abandonne.
                items = items[QUEUE_BATCH:]
                if result == "ok":
                    sent += len(batch)
            self._write_queue(items)
        return sent

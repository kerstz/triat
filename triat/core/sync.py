"""Synchro sur le Wi-Fi entre Triat bureau et Triat mobile.

Le bureau est le serveur : il s'annonce sur le réseau local en
`_triat._tcp` (mDNS) et répond en HTTP. Le téléphone le trouve, s'appaire
une fois avec un code affiché ici, puis envoie son instantané ; le bureau
fusionne (`sync_state`) et renvoie le résultat.

Sécurité
- Appairage : échange ECDH P-256 ; la clé commune est dérivée du secret
  ECDH ET du code à 8 caractères (HKDF, sel = SHA-256 du code). Écouter le
  Wi-Fi ne donne rien. Le téléphone prouve qu'il connaît le code en premier :
  un faux téléphone n'obtient rien à attaquer hors ligne. Code valable
  2 minutes. Les essais ratés sont comptés par adresse cliente (5 par IP) et
  au total (20) : un voisin du réseau ne peut pas, seul, épuiser les essais
  d'un appairage légitime. Entropie du code : 31^8 ≈ 8,5·10^11 (~39,6 bits) ;
  20 essais par code en 2 minutes donnent ~2·10^-11 de chances de deviner
  (et sans le code, aucune donnée à attaquer hors ligne, voir plus haut).
- Ensuite, tout est chiffré et authentifié (AES-256-GCM). Les données
  associées lient le message à son sens : `<appareil>|req` pour une requête,
  `<appareil>|resp|<n de la requête>` pour la réponse ; la requête porte en
  plus `ts` (ms) et `n` (aléa) : une copie rejouée, ou une requête renvoyée
  comme réponse, est refusée. Les anciens téléphones (données associées =
  identifiant seul, sans `ts`/`n`) restent acceptés tant que l'appareil n'a
  jamais utilisé le format lié, et la réponse reprend le format de la
  requête. Anti-downgrade : dès qu'un appareil a réussi un échange au format
  lié, la marque `bound` est écrite dans son entrée d'appairage et l'ancien
  format (sans fraîcheur, donc rejouable) est refusé pour lui définitivement.
  Le réglage `sync_refuse_legacy` refuse l'ancien format pour tous les
  appareils (défaut : non, pour ne pas casser les vieux téléphones) ; un
  WARNING est écrit une fois par appareil quand l'ancien format est utilisé.
  Une requête d'un appareil inconnu ou mal chiffrée est refusée sans détail.
- Monotonie : le dernier `ts` accepté (et son `n`) est conservé par appareil
  avec ses métadonnées, donc après un redémarrage aussi. Un `ts` antérieur
  est refusé. Le téléphone envoie l'heure de son horloge en ms (Android :
  `System.currentTimeMillis()`) avec un `n` aléatoire de 128 bits : deux
  requêtes dans la même milliseconde sont possibles en théorie, donc un `ts`
  égal au dernier n'est accepté que si son `n` est inédit.
- Clés : la clé AES de chaque appareil est dans le trousseau (service
  `triat-sync`, une entrée par appareil) ; `settings.json` ne garde que les
  métadonnées. Sans trousseau, la clé reste dans `settings.json` (0600), avec
  un WARNING unique, pour ne pas casser la synchro.
- Rien ne sort du réseau local : le serveur n'écoute que si la synchro est
  activée dans les Réglages.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import copy
import gzip
import ipaddress
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import threading
import time
import zlib
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import sync_state

log = logging.getLogger(__name__)

SERVICE_TYPE = "_triat._tcp.local."
DEFAULT_PORT = 47823
PROTOCOL = 1
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # sans 0/O, 1/I/L
CODE_LENGTH = 8
PAIRING_SECONDS = 120
PAIRING_ATTEMPTS = 5                    # essais ratés par adresse cliente
PAIRING_ATTEMPTS_TOTAL = 20             # plafond global (anti brute-force réparti)
MAX_BODY = 16 * 1024 * 1024
MAX_PAIR_BODY = 4096
MAX_UNZIPPED = 64 * 1024 * 1024
MAX_PENDING = 8
MAX_PENDING_PER_IP = 3
MAX_CONNECTIONS = 8                     # requêtes POST traitées en même temps
MAX_OPEN_CONNECTIONS = 16               # connexions (fils) ouvertes en même temps
MAX_HEADER_BYTES = 16 * 1024            # taille totale ligne de requête + en-têtes
DEFAULT_NAME = "Triat"                  # nom mDNS neutre : ne révèle pas le nom d'hôte
REQUEST_TIMEOUT = 15
ADDRESS_WATCH_SECONDS = 30
REPLAY_WINDOW_MS = 10 * 60 * 1000       # écart d'horloge toléré (ts)
NONCE_MEMORY = 256                      # derniers `n` retenus par appareil
UNCONFIRMED_SECONDS = 10 * 60           # appairage jamais confirmé : oublié
TOMBSTONE_SECONDS = 180 * 24 * 3600     # traces de suppression gardées 180 jours
PORT_FILE = "sync.port"
KEYRING_SERVICE = "triat-sync"
_VIRTUAL_PREFIXES = ("docker", "br-", "veth", "virbr", "lxc", "lxd", "tailscale",
                     "tun", "tap", "wg", "zt", "vmnet", "vboxnet", "podman")


# ---- Crypto ----------------------------------------------------------------

def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def new_keypair() -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint)
    return private, public


def derive_key(private: ec.EllipticCurvePrivateKey, peer_public: bytes,
               code: str) -> bytes:
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_public)
    shared = private.exchange(ec.ECDH(), peer)
    salt = hashlib.sha256(normalize_code(code).encode("ascii")).digest()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=b"triat-sync-v1").derive(shared)


def normalize_code(code: str) -> str:
    return "".join(c for c in (code or "").upper() if c.isalnum())


def pairing_uri(host: str | list[str], port: int, code: str, server_id: str) -> str:
    """Contenu du QR d'appairage, lu par l'app Android. Plusieurs adresses
    possibles : la première reste lisible par les anciennes versions."""
    from urllib.parse import urlencode
    hosts = [host] if isinstance(host, str) else list(host)
    query = urlencode([("host", h) for h in hosts] + [
        ("port", port), ("code", normalize_code(code)), ("id", server_id)])
    return f"triat://pair?{query}"


def pairing_qr_svg(uri: str) -> bytes | None:
    """QR en SVG (fond blanc, marge de 4 modules), ou None sans `qrcode`."""
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError:
        log.info("qrcode absent : appairage par code seul")
        return None
    import io
    out = io.BytesIO()
    qrcode.make(uri, image_factory=SvgPathImage, border=4).save(out)
    return out.getvalue()


def transcript(device_id: str, client_pub: bytes, server_pub: bytes) -> bytes:
    return device_id.encode() + b"|" + client_pub + b"|" + server_pub


def confirmation(key: bytes, role: bytes, data: bytes) -> bytes:
    return hmac.new(key, role + b"|" + data, hashlib.sha256).digest()


def request_aad(device_id: str) -> bytes:
    """Données associées d'une requête (format lié au sens)."""
    return device_id.encode() + b"|req"


def response_aad(device_id: str, request_nonce: str) -> bytes:
    """Données associées d'une réponse : liées à la requête qu'elle clôt."""
    return device_id.encode() + b"|resp|" + request_nonce.encode()


def seal(key: bytes, payload: dict, device_id: str,
         aad: bytes | None = None) -> bytes:
    """`aad` absent : format historique (identifiant de l'appareil seul)."""
    nonce = os.urandom(12)
    raw = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
    return nonce + AESGCM(key).encrypt(
        nonce, raw, device_id.encode() if aad is None else aad)


def open_sealed(key: bytes, blob: bytes, device_id: str,
                aad: bytes | None = None) -> dict:
    nonce, body = blob[:12], blob[12:]
    raw = AESGCM(key).decrypt(
        nonce, body, device_id.encode() if aad is None else aad)
    unzip = zlib.decompressobj(16 + zlib.MAX_WBITS)
    data = unzip.decompress(raw, MAX_UNZIPPED)
    if unzip.unconsumed_tail:
        raise ValueError("message décompressé trop gros")
    return json.loads(data)


def port_file_path() -> Path:
    """Fichier où le serveur écrit son port réel (lu par `triat doctor`)."""
    from .paths import state_dir
    return state_dir() / PORT_FILE


def read_port_file(path: Path | None = None) -> int | None:
    """Port écrit par le serveur en cours, ou None (absent, illisible)."""
    try:
        port = int((path or port_file_path()).read_text(encoding="ascii").strip())
    except (OSError, ValueError):       # UnicodeDecodeError est une ValueError
        return None
    return port if 0 < port < 65536 else None


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


class KeyringStore:
    """Clés des appareils dans le trousseau du système (une entrée par
    appareil). Injectable : tout objet avec `get`/`set`/`delete` convient."""

    def get(self, device_id: str) -> str | None:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, device_id)

    def set(self, device_id: str, value: str) -> None:
        import keyring
        keyring.set_password(KEYRING_SERVICE, device_id, value)

    def delete(self, device_id: str) -> None:
        import keyring
        import keyring.errors
        try:
            keyring.delete_password(KEYRING_SERVICE, device_id)
        except keyring.errors.PasswordDeleteError:
            pass                        # déjà absente


# ---- Serveur ---------------------------------------------------------------

class SyncServer:
    """Serveur de synchro du bureau. `on_changed` est appelé (hors du fil
    GTK) après une synchro qui a modifié la bibliothèque."""

    def __init__(self, library, settings, cookie_header=None,
                 on_changed=None, name: str | None = None,
                 port_file="auto", keystore=None) -> None:
        self.library = library
        self._keystore = keystore if keystore is not None else KeyringStore()
        self._keyring_warned = False
        self._legacy_warned: set[str] = set()
        self.settings = settings
        self._cookie_header = cookie_header or (lambda: None)
        self._on_changed = on_changed or (lambda changes: None)
        self._fixed_name = name
        self._announced_name: str | None = None
        # "auto" : état de l'utilisateur ; None : aucun fichier ; ou un chemin.
        self._port_file = port_file
        # Port souhaité quand on a dû en prendre un autre (None sinon).
        self.fallback_from: int | None = None
        self._nonce_lock = threading.Lock()
        self._nonces: dict[str, collections.OrderedDict] = {}
        self._lock = threading.Lock()
        self._pairing: dict | None = None
        self._pending: dict[str, dict] = {}
        self._httpd: _BoundedServer | None = None
        self._zeroconf = None
        self._service = None
        self._announced: list[str] = []
        # Une génération par démarrage : un enregistrement mDNS lancé par un
        # démarrage précédent, et qui se termine après un arrêt/redémarrage,
        # est reconnu périmé et défait au lieu de fuir.
        self._generation = 0
        self._stopping = threading.Event()
        self._sync_lock = threading.Lock()
        self._devices_lock = threading.Lock()
        self.port: int | None = None
        if not settings.get("sync_server_id"):
            settings.set("sync_server_id", secrets.token_hex(8))
            settings.save()

    # -- Cycle de vie --

    @property
    def name(self) -> str:
        """Nom annoncé : neutre par défaut (le nom d'hôte fuirait sur le
        réseau local), réglable par `sync_name`. Relu à chaque annonce."""
        custom = self.settings.get("sync_name")
        return (self._fixed_name
                or (custom.strip()[:60] if isinstance(custom, str) and custom.strip() else "")
                or DEFAULT_NAME)

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def _port_path(self) -> Path | None:
        if self._port_file != "auto":
            return self._port_file
        wanted = self.settings.get("sync_port")
        try:
            if wanted is not None and int(wanted) == 0:
                return None             # port au hasard : tests, rien à publier
            return port_file_path()
        except (OSError, ValueError, TypeError):
            return None

    def _write_port_file(self) -> None:
        path = self._port_path()
        if path is None or self.port is None:
            return
        try:
            path.write_text(f"{self.port}\n", encoding="ascii")
        except OSError as exc:
            log.debug("Fichier de port illisible : %s", exc)

    def _remove_port_file(self) -> None:
        path = self._port_path()
        if path is None:
            return
        try:
            path.unlink()
        except OSError:
            pass

    def start(self, announce: bool = True) -> bool:
        if self.running:
            return True
        handler = _make_handler(self)
        wanted = self.settings.get("sync_port")
        first = DEFAULT_PORT if wanted is None else int(wanted)
        self.fallback_from = None
        for port in (first, 0):
            try:
                self._httpd = _BoundedServer(("0.0.0.0", port), handler)
                break
            except OSError as exc:
                log.info("Port %s indisponible pour la synchro : %s", port, exc)
        if self._httpd is None:
            return False
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        if first != 0 and self.port != first:
            self.fallback_from = first
            log.warning("Synchro : port %s occupé, port %s utilisé",
                        first, self.port)
        self._write_port_file()
        self._migrate_keys()
        self._maintenance()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._stopping = stopping = threading.Event()
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         name="synchro").start()
        if announce:
            # L'enregistrement mDNS attend ~1,5 s : jamais sur le fil GTK.
            threading.Thread(target=self._announce,
                             args=(generation, stopping), daemon=True,
                             name="synchro-mdns").start()
        log.info("Synchro : écoute sur le port %s", self.port)
        return True

    def stop(self) -> None:
        with self._lock:
            self._stopping.set()
            self._generation += 1
            zc, service = self._zeroconf, self._service
            self._zeroconf = self._service = None
        if zc is not None:
            def _close() -> None:
                try:
                    zc.unregister_service(service)
                    zc.close()
                except Exception as exc:
                    log.debug("mDNS : %s", exc)
            threading.Thread(target=_close, daemon=True,
                             name="synchro-mdns-stop").start()
        if self._httpd is not None:
            httpd, self._httpd = self._httpd, None
            httpd.shutdown()
            httpd.server_close()
            self._remove_port_file()

    @contextlib.contextmanager
    def drain(self):
        """Attend qu'une fusion en vol se termine, puis empêche toute
        nouvelle fusion tant que le bloc s'exécute. À utiliser avant de
        fermer ou d'effacer la bibliothèque :

            with sync.drain():
                library.close()
        """
        with self._sync_lock:
            yield

    def _maintenance(self) -> None:
        """Au démarrage : traces de suppression périmées, appairages
        inachevés. Jamais bloquant pour la synchro elle-même."""
        try:
            prune = getattr(self.library, "prune_tombstones", None)
            if prune is not None:
                removed = prune(TOMBSTONE_SECONDS)
                if removed:
                    log.info("Synchro : %s traces de suppression périmées", removed)
            self.prune_unconfirmed()
        except Exception as exc:
            log.warning("Synchro : entretien impossible : %s", exc)

    def _service_info(self, ips: list[str]):
        from zeroconf import ServiceInfo
        name = self.name
        self._announced_name = name
        return ServiceInfo(
            SERVICE_TYPE, f"{self.settings.get('sync_server_id')}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip) for ip in ips], port=self.port,
            properties={"v": str(PROTOCOL), "name": name,
                        "id": self.settings.get("sync_server_id")})

    def _announce(self, generation: int,
                  stopping: threading.Event) -> None:
        try:
            from zeroconf import Zeroconf
        except ImportError:
            log.warning("zeroconf absent : le téléphone devra saisir l'adresse")
            return
        try:
            ips = _local_ipv4()
            service = self._service_info(ips)
            zc = Zeroconf()
            zc.register_service(service)
        except Exception as exc:
            log.warning("Annonce mDNS impossible : %s", exc)
            return
        with self._lock:
            if (stopping.is_set() or generation != self._generation
                    or not self.running):
                stale = True               # arrêtée pendant l'enregistrement
            else:
                stale = False
                self._zeroconf, self._service, self._announced = zc, service, ips
        if stale:
            try:
                zc.unregister_service(service)
                zc.close()
            except Exception as exc:
                log.debug("mDNS : %s", exc)
            return
        threading.Thread(target=self._watch_addresses, args=(stopping,),
                         daemon=True, name="synchro-adresses").start()

    def _watch_addresses(self, stopping: threading.Event) -> None:
        """Réannonce quand le réseau change (Wi-Fi, DHCP, VPN)."""
        while not stopping.wait(ADDRESS_WATCH_SECONDS):
            self.refresh_announcement()
            self.prune_unconfirmed()

    def refresh_announcement(self) -> bool:
        ips = _local_ipv4()
        with self._lock:
            zc, old = self._zeroconf, self._announced
            generation = self._generation
        renamed = self.name != self._announced_name
        if zc is None or not ips or (ips == old and not renamed):
            return False
        # Le réseau (~0,7 s) se fait hors verrou : l'interface interroge
        # `pairing_active` chaque seconde.
        try:
            service = self._service_info(ips)
            zc.update_service(service)
        except Exception as exc:
            log.warning("Réannonce mDNS impossible : %s", exc)
            return False
        with self._lock:
            if generation != self._generation or self._zeroconf is not zc:
                return False            # arrêtée ou relancée entre-temps
            self._service, self._announced = service, ips
        log.info("Synchro : adresses annoncées %s", ips)
        return True

    # -- Appairage --

    def begin_pairing(self) -> str:
        """Nouveau code, valable deux minutes. Remplace le précédent."""
        self.prune_unconfirmed()
        with self._lock:
            code = new_code()
            self._pairing = {"code": code, "until": time.time() + PAIRING_SECONDS,
                             "attempts": 0, "by_ip": {}}
            self._pending.clear()
            return code

    def cancel_pairing(self) -> None:
        with self._lock:
            self._pairing = None
            self._pending.clear()

    @property
    def pairing_active(self) -> bool:
        with self._lock:
            return self._pairing is not None and time.time() < self._pairing["until"]

    def devices(self) -> dict:
        return copy.deepcopy(self.settings.get("sync_devices") or {})

    def _update_devices(self, change) -> None:
        """Lecture-modification-écriture sous verrou : un appareil oublié
        pendant une synchro ne revient pas. Les clés des appareils qui
        disparaissent sont aussi retirées du trousseau."""
        with self._devices_lock:
            devices = self.devices()
            before = {k: bool(isinstance(v, dict) and v.get("keyring"))
                      for k, v in devices.items()}
            change(devices)
            self.settings.set("sync_devices", devices)
            self.settings.save()
            for device_id, in_keyring in before.items():
                if in_keyring and device_id not in devices:
                    self._delete_key(device_id)

    # -- Clés des appareils (trousseau) --

    def _warn_no_keyring(self, exc: Exception) -> None:
        if not self._keyring_warned:
            self._keyring_warned = True
            log.warning("Synchro : trousseau indisponible (%s) ; les clés "
                        "restent dans les réglages (0600)", type(exc).__name__)

    def _delete_key(self, device_id: str) -> None:
        try:
            self._keystore.delete(device_id)
        except Exception as exc:
            log.warning("Synchro : suppression dans le trousseau impossible (%s)",
                        type(exc).__name__)

    def _device_key(self, device_id: str, info: dict) -> bytes | None:
        """Clé AES de l'appareil : réglages (repli ou ancien format), sinon
        trousseau. None si introuvable."""
        try:
            text = info.get("key")
            if not text and info.get("keyring"):
                text = self._keystore.get(device_id)
            return unb64(text) if text else None
        except Exception:
            return None

    def _migrate_keys(self) -> None:
        """Clés encore dans `settings.json` : copiées au trousseau, puis
        retirées des réglages seulement si l'écriture a réussi."""
        if not any(isinstance(i, dict) and i.get("key")
                   for i in self.devices().values()):
            return

        def change(devices: dict) -> None:
            for device_id, info in devices.items():
                if not isinstance(info, dict) or not info.get("key"):
                    continue
                try:
                    self._keystore.set(device_id, info["key"])
                except Exception as exc:
                    self._warn_no_keyring(exc)
                    return              # trousseau absent : inutile d'insister
                info.pop("key")
                info["keyring"] = True

        self._update_devices(change)

    def forget_device(self, device_id: str) -> None:
        self._update_devices(lambda d: d.pop(device_id, None))

    def forget_all_devices(self) -> None:
        self._update_devices(lambda d: d.clear())
        with self._nonce_lock:
            self._nonces.clear()

    def prune_unconfirmed(self, now: float | None = None) -> int:
        """Oublie les appareils dont l'appairage n'a jamais été confirmé
        par une première synchro et qui datent de plus de 10 minutes (le
        téléphone n'a pas reçu la preuve du bureau). Renvoie leur nombre."""
        now = time.time() if now is None else now

        def stale(info) -> bool:
            return (isinstance(info, dict) and info.get("confirmed") is False
                    and now - float(info.get("paired_at") or 0) > UNCONFIRMED_SECONDS)

        if not any(stale(i) for i in self.devices().values()):
            return 0
        removed: list[str] = []

        def change(devices: dict) -> None:
            for device_id in [k for k, v in devices.items() if stale(v)]:
                del devices[device_id]
                removed.append(device_id)

        self._update_devices(change)
        if removed:
            log.info("Synchro : %s appairage(s) inachevé(s) oublié(s)", len(removed))
        return len(removed)

    def _ip_blocked(self, client: str) -> bool:
        """Appelé sous `self._lock` : cette adresse a épuisé ses essais."""
        return self._pairing["by_ip"].get(client, 0) >= PAIRING_ATTEMPTS

    def _pair_start(self, body: dict, client: str = "") -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"error": "bad-request"}
        device_id = str(body.get("device_id") or "")[:64]
        if not device_id or not self.pairing_active:
            return 409, {"error": "pairing-closed"}
        try:
            client_pub = unb64(body["pub"])
            private, server_pub = new_keypair()
            with self._lock:
                if self._pairing is None:
                    return 409, {"error": "pairing-closed"}
                if self._ip_blocked(client):
                    return 429, {"error": "busy"}
                if device_id not in self._pending:
                    mine = sum(1 for p in self._pending.values()
                               if p.get("client") == client)
                    if len(self._pending) >= MAX_PENDING or mine >= MAX_PENDING_PER_IP:
                        return 429, {"error": "busy"}
                key = derive_key(private, client_pub, self._pairing["code"])
                self._pending[device_id] = {
                    "key": key, "name": str(body.get("name") or "Téléphone")[:60],
                    "client": client,
                    "transcript": transcript(device_id, client_pub, server_pub)}
        except (KeyError, ValueError, TypeError, AttributeError):
            return 400, {"error": "bad-request"}
        return 200, {"server_id": self.settings.get("sync_server_id"),
                     "name": self.name, "pub": b64(server_pub)}

    def _pair_finish(self, body: dict, client: str = "") -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"error": "bad-request"}
        device_id = str(body.get("device_id") or "")[:64]
        with self._lock:
            pending = self._pending.get(device_id)
            if pending is None or self._pairing is None \
                    or time.time() >= self._pairing["until"]:
                return 409, {"error": "pairing-closed"}
            if self._ip_blocked(client):
                return 429, {"error": "busy"}
            try:
                proof = unb64(body.get("confirm") or "")
            except (ValueError, TypeError, AttributeError):
                proof = b""
            expected = confirmation(pending["key"], b"client", pending["transcript"])
            if not hmac.compare_digest(proof, expected):
                by_ip = self._pairing["by_ip"]
                by_ip[client] = by_ip.get(client, 0) + 1
                self._pairing["attempts"] += 1
                self._pending.pop(device_id, None)
                if self._pairing["attempts"] >= PAIRING_ATTEMPTS_TOTAL:
                    self._pairing = None          # trop d'essais au total : on ferme
                return 403, {"error": "wrong-code"}
            self._pairing = None                  # un code, un appareil
            self._pending.clear()
        entry = {"name": pending["name"],
                 "paired_at": time.time(), "last_sync": None,
                 "confirmed": False}      # vrai à la première synchro
        try:
            self._keystore.set(device_id, b64(pending["key"]))
            entry["keyring"] = True
        except Exception as exc:
            self._warn_no_keyring(exc)
            entry["key"] = b64(pending["key"])
        self._update_devices(lambda d: d.__setitem__(device_id, entry))
        log.info("Synchro : appareil appairé (%s)", pending["name"])
        return 200, {"confirm": b64(confirmation(pending["key"], b"server",
                                                 pending["transcript"]))}

    # -- Synchro --

    def known_device(self, device_id: str) -> bool:
        return device_id in (self.settings.get("sync_devices") or {})

    def _open_request(self, device_id: str, key: bytes, blob: bytes,
                      device: dict | None = None):
        """Ouvre une requête. Renvoie (contenu, données associées de la
        réponse, erreur). Format lié au sens d'abord, puis historique, sauf
        si l'appareil a déjà utilisé le format lié (anti-downgrade) ou si
        `sync_refuse_legacy` est actif."""
        try:
            remote = open_sealed(key, blob, device_id, aad=request_aad(device_id))
        except Exception:
            pass
        else:
            error = self._check_fresh(device_id, remote)
            if error is not None:
                return None, None, error
            if not (device or {}).get("bound"):
                self._mark_bound(device_id)
            return remote, response_aad(device_id, remote["n"]), None
        if (device or {}).get("bound") or self.settings.get("sync_refuse_legacy"):
            return None, None, (401, b"")          # pas de retour à l'ancien format
        try:
            remote = open_sealed(key, blob, device_id)             # ancien format
        except Exception:
            return None, None, (401, b"")          # clé fausse ou message altéré
        if device_id not in self._legacy_warned:
            self._legacy_warned.add(device_id)
            log.warning("Synchro : l'appareil %s utilise l'ancien format, "
                        "rejouable (activer sync_refuse_legacy ou mettre le "
                        "téléphone à jour)", (device or {}).get("name") or "?")
        return remote, None, None

    def _mark_bound(self, device_id: str) -> None:
        """L'appareil parle le format lié : l'ancien lui est fermé."""
        def mark(devices: dict) -> None:
            if isinstance(devices.get(device_id), dict):
                devices[device_id]["bound"] = True
        self._update_devices(mark)

    def _check_fresh(self, device_id: str, remote) -> tuple[int, bytes] | None:
        """Fraîcheur d'une requête au format lié au sens : `ts` dans la
        fenêtre, `n` jamais vu. None si tout va bien."""
        bad = (400, b'{"error":"bad-request"}')
        if not isinstance(remote, dict):
            return bad
        stamp, nonce = remote.get("ts"), remote.get("n")
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not isinstance(nonce, str) or not 8 <= len(nonce) <= 64):
            return bad
        if abs(time.time() * 1000 - stamp) > REPLAY_WINDOW_MS:
            return 400, b'{"error":"clock"}'
        with self._devices_lock:
            info = self.devices().get(device_id)
            if info is None:
                return 401, b""
            last = info.get("last_ts")
            if isinstance(last, (int, float)) and not isinstance(last, bool):
                # Monotonie, même après un redémarrage. Égalité : seulement
                # avec un `n` différent du dernier (même milliseconde).
                if stamp < last or (stamp == last and nonce == info.get("last_n")):
                    return 409, b'{"error":"replay"}'
            with self._nonce_lock:
                seen = self._nonces.setdefault(device_id, collections.OrderedDict())
                if nonce in seen:
                    return 409, b'{"error":"replay"}'
                seen[nonce] = stamp
                while len(seen) > NONCE_MEMORY:
                    seen.popitem(last=False)
            devices = self.devices()
            devices[device_id]["last_ts"] = stamp
            devices[device_id]["last_n"] = nonce
            self.settings.set("sync_devices", devices)
            self.settings.save()
        return None

    def _sync(self, device_id: str, blob: bytes) -> tuple[int, bytes]:
        device = self.devices().get(device_id)
        if device is None:
            return 401, b""
        key = self._device_key(device_id, device)
        if key is None:
            return 401, b""
        remote, reply_aad, error = self._open_request(
            device_id, key, blob, device)
        if error is not None:
            return error
        if not isinstance(remote, dict) or remote.get("v") != sync_state.VERSION:
            return 400, b""
        remote = sync_state.sanitize(remote)
        if remote is None:
            return 400, b""
        with self._sync_lock:
            # Oublié (ou « Tout effacer ») pendant qu'on attendait le verrou.
            if not self.known_device(device_id):
                return 401, b""
            return self._sync_locked(device_id, device, key, remote, reply_aad)

    def _unpair(self, device_id: str, blob: bytes) -> tuple[int, bytes]:
        """Le téléphone se retire : même authentification que `/v1/sync`
        (format lié au sens exigé), puis l'appareil est oublié."""
        device = self.devices().get(device_id)
        if device is None:
            return 401, b""
        key = self._device_key(device_id, device)
        if key is None:
            return 401, b""
        try:
            remote = open_sealed(key, blob, device_id, aad=request_aad(device_id))
        except Exception:
            return 401, b""
        error = self._check_fresh(device_id, remote)
        if error is not None:
            return error
        with self._sync_lock:           # après une synchro en cours
            self.forget_device(device_id)
        with self._nonce_lock:
            self._nonces.pop(device_id, None)
        log.info("Synchro : appareil retiré par le téléphone (%s)", device["name"])
        return 200, seal(key, {"ok": True}, device_id,
                         aad=response_aad(device_id, remote["n"]))

    def _sync_locked(self, device_id: str, device: dict, key: bytes,
                     remote: dict, reply_aad: bytes | None = None
                     ) -> tuple[int, bytes]:
        merged = sync_state.merge(sync_state.snapshot(self.library), remote)
        # Sens de la synchro, choisi dans les Réglages du bureau.
        # - "both" (défaut, comportement d'origine) : les deux côtés
        #   convergent vers la fusion.
        # - "from_phone" (téléphone -> PC) : le PC absorbe le téléphone, le
        #   téléphone ne change pas (on lui renvoie ce qu'il a envoyé).
        # - "to_phone" (PC -> téléphone) : le PC ne change pas, le téléphone
        #   reçoit la fusion.
        direction = self.settings.get("sync_direction") or "both"
        if direction == "to_phone":
            changes = {"playlists": 0, "likes": 0, "history": 0}
            reply = dict(merged)
        elif direction == "from_phone":
            changes = sync_state.apply(self.library, merged)
            reply = dict(remote)
        else:
            changes = sync_state.apply(self.library, merged)
            reply = dict(merged)
        reply["now"] = int(time.time() * 1000)     # le téléphone détecte une horloge décalée
        if self.settings.get("sync_share_cookies"):
            cookies = self._cookie_header()
            if cookies:
                reply["cookies"] = cookies
        def _stamp(devices: dict) -> None:
            if device_id in devices:
                devices[device_id]["last_sync"] = time.time()
                devices[device_id]["confirmed"] = True
        self._update_devices(_stamp)
        if any(changes.values()):
            self._on_changed(changes)
        log.info("Synchro avec %s : %s", device["name"], changes)
        return 200, seal(key, reply, device_id, aad=reply_aad)


def _usable_ipv4(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != 4 or addr.is_loopback or addr.is_link_local \
            or addr.is_unspecified or addr.is_multicast:
        return False
    return addr not in ipaddress.ip_network("100.64.0.0/10")   # CGNAT, Tailscale


def _local_ipv4() -> list[str]:
    """Adresses IPv4 joignables depuis le réseau local, la route par défaut
    en premier (pas de boucle locale, Docker, VPN ni lien-local)."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.168.0.1", 9))       # aucun paquet envoyé
            ip = probe.getsockname()[0]
            if _usable_ipv4(ip):
                found.append(ip)
    except OSError:
        pass
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            name = (adapter.nice_name or adapter.name or "").lower()
            if name.startswith(_VIRTUAL_PREFIXES):
                continue
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and _usable_ipv4(ip.ip) \
                        and ip.ip not in found:
                    found.append(ip.ip)
    except ImportError:
        pass
    return found


def _is_local_client(address: str) -> bool:
    """Seuls les voisins du réseau local ont à parler au serveur."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


class _BoundedServer(ThreadingHTTPServer):
    """Serveur HTTP à un fil par connexion, mais plafonné : au-delà de
    `MAX_OPEN_CONNECTIONS` connexions simultanées, la suivante est refusée
    (503) avant qu'un fil ne soit créé. Le plafond compte aussi les
    connexions qui envoient leurs en-têtes au compte-gouttes."""

    def __init__(self, *args, **kwargs) -> None:
        self._open_slots = threading.BoundedSemaphore(MAX_OPEN_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        if not self._open_slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._open_slots.release()      # le fil n'a pas pu démarrer
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._open_slots.release()


class _HeaderBudget:
    """Enveloppe de `rfile` qui compte les octets lus ligne à ligne
    (ligne de requête et en-têtes) ; au-delà de la limite, la lecture
    s'arrête comme à la fin des en-têtes et `exceeded` est levé."""

    def __init__(self, raw) -> None:
        self._raw = raw
        self.left: int | None = MAX_HEADER_BYTES
        self.exceeded = False

    def reset(self) -> None:
        self.left, self.exceeded = MAX_HEADER_BYTES, False

    def readline(self, size: int = -1) -> bytes:
        if self.left is None:
            return self._raw.readline(size)
        limit = self.left + 1 if size is None or size < 0 else min(size, self.left + 1)
        line = self._raw.readline(limit)
        self.left -= len(line)
        if self.left < 0:
            self.exceeded = True
            return b""
        return line

    def __getattr__(self, name):
        return getattr(self._raw, name)


def _make_handler(server: SyncServer):
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Triat"
        sys_version = ""
        timeout = REQUEST_TIMEOUT

        def log_message(self, fmt, *args):     # pas de journal par requête
            return

        def setup(self):
            super().setup()
            self.rfile = _HeaderBudget(self.rfile)

        def handle_one_request(self):
            self.rfile.reset()
            super().handle_one_request()

        def parse_request(self):
            ok = super().parse_request()
            budget = self.rfile
            if budget.exceeded:
                budget.left = None
                self.close_connection = True
                if ok:
                    self._json(431, {"error": "headers-too-large"})
                return False
            budget.left = None              # le corps n'est pas compté ici
            return ok

        def _send(self, status: int, body: bytes, kind: str) -> None:
            if status >= 400:
                self.close_connection = True   # le corps non lu ne doit pas
                                               # être pris pour une requête
            self.send_response(status)
            self.send_header("Content-Type", kind)
            if status >= 400:
                self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _body(self, limit: int = MAX_BODY) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > limit:
                return None
            return self.rfile.read(length)

        def _guard(self) -> bool:
            if _is_local_client(self.client_address[0]):
                return True
            self._json(403, {"error": "forbidden"})
            self.close_connection = True
            return False

        def do_GET(self):
            if not self._guard():
                return
            try:
                if self.path == "/v1/hello":
                    self._json(200, {"app": "triat", "v": PROTOCOL,
                                     "server_id": server.settings.get("sync_server_id"),
                                     "name": server.name,
                                     "pairing": server.pairing_active,
                                     "now": int(time.time() * 1000)})
                else:
                    self._json(404, {"error": "not-found"})
            except OSError:
                self.close_connection = True

        def do_POST(self):
            if not self._guard():
                return
            if not slots.acquire(blocking=False):
                self._json(503, {"error": "busy"})
                self.close_connection = True
                return
            try:
                self._post()
            except OSError:
                self.close_connection = True       # client parti ou trop lent
            except Exception:
                log.exception("Synchro : requête %s en échec", self.path)
                try:
                    self._json(500, {"error": "internal"})
                except OSError:
                    pass
                self.close_connection = True
            finally:
                slots.release()

        def _post(self):
            if self.path in ("/v1/pair/start", "/v1/pair/finish"):
                raw = self._body(MAX_PAIR_BODY)
                if raw is None:
                    self._json(400, {"error": "bad-request"})
                    return
                try:
                    body = json.loads(raw)
                except ValueError:
                    self._json(400, {"error": "bad-request"})
                    return
                handler = (server._pair_start if self.path.endswith("start")
                           else server._pair_finish)
                self._json(*handler(body, self.client_address[0].split("%")[0]))
            elif self.path in ("/v1/sync", "/v1/unpair"):
                device_id = (self.headers.get("X-Triat-Device") or "")[:64]
                if not server.known_device(device_id):
                    self._json(401, {"error": "unauthorized"})
                    self.close_connection = True   # corps jamais lu
                    return
                unpair = self.path == "/v1/unpair"
                raw = self._body(MAX_PAIR_BODY if unpair else MAX_BODY)
                if raw is None:
                    self._json(400, {"error": "bad-request"})
                    return
                status, blob = (server._unpair if unpair else server._sync)(
                    device_id, raw)
                self._send(status, blob, "application/octet-stream"
                           if status == 200 else "application/json")
            else:
                self._json(404, {"error": "not-found"})

    return Handler

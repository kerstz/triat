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
  2 minutes, 5 essais.
- Ensuite, tout est chiffré et authentifié (AES-256-GCM, l'identifiant de
  l'appareil en données associées). Une requête d'un appareil inconnu ou
  mal chiffrée est refusée sans détail.
- Rien ne sort du réseau local : le serveur n'écoute que si la synchro est
  activée dans les Réglages.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import threading
import time
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
PAIRING_ATTEMPTS = 5
MAX_BODY = 16 * 1024 * 1024


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


def transcript(device_id: str, client_pub: bytes, server_pub: bytes) -> bytes:
    return device_id.encode() + b"|" + client_pub + b"|" + server_pub


def confirmation(key: bytes, role: bytes, data: bytes) -> bytes:
    return hmac.new(key, role + b"|" + data, hashlib.sha256).digest()


def seal(key: bytes, payload: dict, device_id: str) -> bytes:
    nonce = os.urandom(12)
    raw = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
    return nonce + AESGCM(key).encrypt(nonce, raw, device_id.encode())


def open_sealed(key: bytes, blob: bytes, device_id: str) -> dict:
    nonce, body = blob[:12], blob[12:]
    raw = AESGCM(key).decrypt(nonce, body, device_id.encode())
    return json.loads(gzip.decompress(raw))


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


# ---- Serveur ---------------------------------------------------------------

class SyncServer:
    """Serveur de synchro du bureau. `on_changed` est appelé (hors du fil
    GTK) après une synchro qui a modifié la bibliothèque."""

    def __init__(self, library, settings, cookie_header=None,
                 on_changed=None, name: str | None = None) -> None:
        self.library = library
        self.settings = settings
        self._cookie_header = cookie_header or (lambda: None)
        self._on_changed = on_changed or (lambda changes: None)
        self.name = name or f"Triat — {socket.gethostname()}"
        self._lock = threading.Lock()
        self._pairing: dict | None = None
        self._pending: dict[str, dict] = {}
        self._httpd: ThreadingHTTPServer | None = None
        self._zeroconf = None
        self._service = None
        self.port: int | None = None
        if not settings.get("sync_server_id"):
            settings.set("sync_server_id", secrets.token_hex(8))
            settings.save()

    # -- Cycle de vie --

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self, announce: bool = True) -> bool:
        if self.running:
            return True
        handler = _make_handler(self)
        wanted = self.settings.get("sync_port")
        for port in (DEFAULT_PORT if wanted is None else int(wanted), 0):
            try:
                self._httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
                break
            except OSError as exc:
                log.info("Port %s indisponible pour la synchro : %s", port, exc)
        if self._httpd is None:
            return False
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         name="synchro").start()
        if announce:
            self._announce()
        log.info("Synchro : écoute sur le port %s", self.port)
        return True

    def stop(self) -> None:
        if self._zeroconf is not None:
            try:
                self._zeroconf.unregister_service(self._service)
                self._zeroconf.close()
            except Exception as exc:
                log.debug("mDNS : %s", exc)
            self._zeroconf = self._service = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def _announce(self) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            log.warning("zeroconf absent : le téléphone devra saisir l'adresse")
            return
        try:
            addresses = [socket.inet_aton(ip) for ip in _local_ipv4()]
            self._service = ServiceInfo(
                SERVICE_TYPE, f"{self.settings.get('sync_server_id')}.{SERVICE_TYPE}",
                addresses=addresses, port=self.port,
                properties={"v": str(PROTOCOL), "name": self.name,
                            "id": self.settings.get("sync_server_id")})
            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(self._service)
        except Exception as exc:
            log.warning("Annonce mDNS impossible : %s", exc)
            self._zeroconf = None

    # -- Appairage --

    def begin_pairing(self) -> str:
        """Nouveau code, valable deux minutes. Remplace le précédent."""
        with self._lock:
            code = new_code()
            self._pairing = {"code": code, "until": time.time() + PAIRING_SECONDS,
                             "attempts": 0}
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
        return dict(self.settings.get("sync_devices") or {})

    def forget_device(self, device_id: str) -> None:
        devices = self.devices()
        devices.pop(device_id, None)
        self.settings.set("sync_devices", devices)
        self.settings.save()

    def _pair_start(self, body: dict) -> tuple[int, dict]:
        device_id = str(body.get("device_id") or "")[:64]
        if not device_id or not self.pairing_active:
            return 409, {"error": "pairing-closed"}
        try:
            client_pub = unb64(body["pub"])
            private, server_pub = new_keypair()
            with self._lock:
                key = derive_key(private, client_pub, self._pairing["code"])
                self._pending[device_id] = {
                    "key": key, "name": str(body.get("name") or "Téléphone")[:60],
                    "transcript": transcript(device_id, client_pub, server_pub)}
        except (KeyError, ValueError, TypeError):
            return 400, {"error": "bad-request"}
        return 200, {"server_id": self.settings.get("sync_server_id"),
                     "name": self.name, "pub": b64(server_pub)}

    def _pair_finish(self, body: dict) -> tuple[int, dict]:
        device_id = str(body.get("device_id") or "")[:64]
        with self._lock:
            pending = self._pending.get(device_id)
            if pending is None or self._pairing is None \
                    or time.time() >= self._pairing["until"]:
                return 409, {"error": "pairing-closed"}
            try:
                proof = unb64(body.get("confirm") or "")
            except ValueError:
                proof = b""
            expected = confirmation(pending["key"], b"client", pending["transcript"])
            if not hmac.compare_digest(proof, expected):
                self._pairing["attempts"] += 1
                self._pending.pop(device_id, None)
                if self._pairing["attempts"] >= PAIRING_ATTEMPTS:
                    self._pairing = None          # trop d'essais : on ferme
                return 403, {"error": "wrong-code"}
            self._pairing = None                  # un code, un appareil
            self._pending.clear()
        devices = self.devices()
        devices[device_id] = {"name": pending["name"], "key": b64(pending["key"]),
                              "paired_at": time.time(), "last_sync": None}
        self.settings.set("sync_devices", devices)
        self.settings.save()
        log.info("Synchro : appareil appairé (%s)", pending["name"])
        return 200, {"confirm": b64(confirmation(pending["key"], b"server",
                                                 pending["transcript"]))}

    # -- Synchro --

    def _sync(self, device_id: str, blob: bytes) -> tuple[int, bytes]:
        device = self.devices().get(device_id)
        if device is None:
            return 401, b""
        key = unb64(device["key"])
        try:
            remote = open_sealed(key, blob, device_id)
        except Exception:
            return 401, b""                        # clé fausse ou message altéré
        if remote.get("v") != sync_state.VERSION:
            return 400, b""
        merged = sync_state.merge(sync_state.snapshot(self.library), remote)
        changes = sync_state.apply(self.library, merged)
        reply = dict(merged)
        if self.settings.get("sync_share_cookies"):
            cookies = self._cookie_header()
            if cookies:
                reply["cookies"] = cookies
        devices = self.devices()
        if device_id in devices:
            devices[device_id]["last_sync"] = time.time()
            self.settings.set("sync_devices", devices)
            self.settings.save()
        if any(changes.values()):
            self._on_changed(changes)
        log.info("Synchro avec %s : %s", device["name"], changes)
        return 200, seal(key, reply, device_id)


def _local_ipv4() -> list[str]:
    """Adresses IPv4 du réseau local (pas la boucle locale)."""
    found: list[str] = []
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and not ip.ip.startswith("127.") \
                        and ip.ip.count(".") == 3:
                    found.append(ip.ip)
    except ImportError:
        pass
    if not found:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.168.0.1", 9))   # aucun paquet envoyé
                found.append(probe.getsockname()[0])
        except OSError:
            pass
    return found


def _make_handler(server: SyncServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Triat"
        sys_version = ""

        def log_message(self, fmt, *args):     # pas de journal par requête
            return

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > MAX_BODY:
                return None
            return self.rfile.read(length)

        def do_GET(self):
            if self.path == "/v1/hello":
                self._json(200, {"app": "triat", "v": PROTOCOL,
                                 "server_id": server.settings.get("sync_server_id"),
                                 "name": server.name,
                                 "pairing": server.pairing_active})
            else:
                self._json(404, {"error": "not-found"})

        def do_POST(self):
            raw = self._body()
            if raw is None:
                self._json(400, {"error": "bad-request"})
                return
            if self.path in ("/v1/pair/start", "/v1/pair/finish"):
                try:
                    body = json.loads(raw)
                except ValueError:
                    self._json(400, {"error": "bad-request"})
                    return
                handler = (server._pair_start if self.path.endswith("start")
                           else server._pair_finish)
                self._json(*handler(body))
            elif self.path == "/v1/sync":
                device_id = (self.headers.get("X-Triat-Device") or "")[:64]
                status, blob = server._sync(device_id, raw)
                self._send(status, blob, "application/octet-stream")
            else:
                self._json(404, {"error": "not-found"})

    return Handler

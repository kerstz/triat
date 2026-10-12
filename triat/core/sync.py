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
MAX_PAIR_BODY = 4096
MAX_UNZIPPED = 64 * 1024 * 1024
MAX_PENDING = 8
MAX_CONNECTIONS = 8
REQUEST_TIMEOUT = 15
ADDRESS_WATCH_SECONDS = 30
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


def seal(key: bytes, payload: dict, device_id: str) -> bytes:
    nonce = os.urandom(12)
    raw = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
    return nonce + AESGCM(key).encrypt(nonce, raw, device_id.encode())


def open_sealed(key: bytes, blob: bytes, device_id: str) -> dict:
    nonce, body = blob[:12], blob[12:]
    raw = AESGCM(key).decrypt(nonce, body, device_id.encode())
    unzip = zlib.decompressobj(16 + zlib.MAX_WBITS)
    data = unzip.decompress(raw, MAX_UNZIPPED)
    if unzip.unconsumed_tail:
        raise ValueError("message décompressé trop gros")
    return json.loads(data)


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

    def _service_info(self, ips: list[str]):
        from zeroconf import ServiceInfo
        return ServiceInfo(
            SERVICE_TYPE, f"{self.settings.get('sync_server_id')}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip) for ip in ips], port=self.port,
            properties={"v": str(PROTOCOL), "name": self.name,
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

    def refresh_announcement(self) -> bool:
        ips = _local_ipv4()
        with self._lock:
            zc, old = self._zeroconf, self._announced
            generation = self._generation
        if zc is None or not ips or ips == old:
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
        return copy.deepcopy(self.settings.get("sync_devices") or {})

    def _update_devices(self, change) -> None:
        """Lecture-modification-écriture sous verrou : un appareil oublié
        pendant une synchro ne revient pas."""
        with self._devices_lock:
            devices = self.devices()
            change(devices)
            self.settings.set("sync_devices", devices)
            self.settings.save()

    def forget_device(self, device_id: str) -> None:
        self._update_devices(lambda d: d.pop(device_id, None))

    def forget_all_devices(self) -> None:
        self._update_devices(lambda d: d.clear())

    def _pair_start(self, body: dict) -> tuple[int, dict]:
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
                if device_id not in self._pending and len(self._pending) >= MAX_PENDING:
                    return 429, {"error": "busy"}
                key = derive_key(private, client_pub, self._pairing["code"])
                self._pending[device_id] = {
                    "key": key, "name": str(body.get("name") or "Téléphone")[:60],
                    "transcript": transcript(device_id, client_pub, server_pub)}
        except (KeyError, ValueError, TypeError, AttributeError):
            return 400, {"error": "bad-request"}
        return 200, {"server_id": self.settings.get("sync_server_id"),
                     "name": self.name, "pub": b64(server_pub)}

    def _pair_finish(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"error": "bad-request"}
        device_id = str(body.get("device_id") or "")[:64]
        with self._lock:
            pending = self._pending.get(device_id)
            if pending is None or self._pairing is None \
                    or time.time() >= self._pairing["until"]:
                return 409, {"error": "pairing-closed"}
            try:
                proof = unb64(body.get("confirm") or "")
            except (ValueError, TypeError, AttributeError):
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
        entry = {"name": pending["name"], "key": b64(pending["key"]),
                 "paired_at": time.time(), "last_sync": None}
        self._update_devices(lambda d: d.__setitem__(device_id, entry))
        log.info("Synchro : appareil appairé (%s)", pending["name"])
        return 200, {"confirm": b64(confirmation(pending["key"], b"server",
                                                 pending["transcript"]))}

    # -- Synchro --

    def known_device(self, device_id: str) -> bool:
        return device_id in (self.settings.get("sync_devices") or {})

    def _sync(self, device_id: str, blob: bytes) -> tuple[int, bytes]:
        device = self.devices().get(device_id)
        if device is None:
            return 401, b""
        try:
            key = unb64(device["key"])
            remote = open_sealed(key, blob, device_id)
        except Exception:
            return 401, b""                        # clé fausse ou message altéré
        if not isinstance(remote, dict) or remote.get("v") != sync_state.VERSION:
            return 400, b""
        remote = sync_state.sanitize(remote)
        if remote is None:
            return 400, b""
        with self._sync_lock:
            # Oublié (ou « Tout effacer ») pendant qu'on attendait le verrou.
            if not self.known_device(device_id):
                return 401, b""
            return self._sync_locked(device_id, device, key, remote)

    def _sync_locked(self, device_id: str, device: dict, key: bytes,
                     remote: dict) -> tuple[int, bytes]:
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
        self._update_devices(_stamp)
        if any(changes.values()):
            self._on_changed(changes)
        log.info("Synchro avec %s : %s", device["name"], changes)
        return 200, seal(key, reply, device_id)


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


def _make_handler(server: SyncServer):
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Triat"
        sys_version = ""
        timeout = REQUEST_TIMEOUT

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
                self._json(*handler(body))
            elif self.path == "/v1/sync":
                device_id = (self.headers.get("X-Triat-Device") or "")[:64]
                if not server.known_device(device_id):
                    self._json(401, {"error": "unauthorized"})
                    self.close_connection = True   # corps jamais lu
                    return
                raw = self._body()
                if raw is None:
                    self._json(400, {"error": "bad-request"})
                    return
                status, blob = server._sync(device_id, raw)
                self._send(status, blob, "application/octet-stream")
            else:
                self._json(404, {"error": "not-found"})

    return Handler

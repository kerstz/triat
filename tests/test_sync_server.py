"""Serveur de synchro (core/sync.py) : un faux téléphone, en vrai HTTP."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from triat.core import sync, sync_state
from triat.core.library import Library
from triat.core.models import TrackRef
from triat.core.settings import Settings


class FakeKeys:
    """Trousseau en mémoire : le vrai n'est jamais touché."""

    def __init__(self, broken: bool = False) -> None:
        self.data: dict[str, str] = {}
        self.broken = broken

    def get(self, device_id):
        if self.broken:
            raise RuntimeError("pas de trousseau")
        return self.data.get(device_id)

    def set(self, device_id, value):
        if self.broken:
            raise RuntimeError("pas de trousseau")
        self.data[device_id] = value

    def delete(self, device_id):
        if self.broken:
            raise RuntimeError("pas de trousseau")
        self.data.pop(device_id, None)


class Phone:
    """Ce que fera l'app Android, en Python."""

    def __init__(self, port: int, device_id: str = "telephone-1") -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.device_id = device_id
        self.key: bytes | None = None

    def post(self, path: str, data: bytes, headers=None) -> tuple[int, bytes]:
        request = urllib.request.Request(self.base + path, data=data, method="POST",
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def pair(self, code: str) -> bool:
        private, public = sync.new_keypair()
        status, body = self.post("/v1/pair/start", json.dumps(
            {"device_id": self.device_id, "name": "Pixel", "pub": sync.b64(public)}).encode())
        if status != 200:
            return False
        server_pub = sync.unb64(json.loads(body)["pub"])
        key = sync.derive_key(private, server_pub, code)
        record = sync.transcript(self.device_id, public, server_pub)
        status, body = self.post("/v1/pair/finish", json.dumps(
            {"device_id": self.device_id,
             "confirm": sync.b64(sync.confirmation(key, b"client", record))}).encode())
        if status != 200:
            return False
        # Le téléphone vérifie à son tour que le bureau connaît le code.
        assert sync.unb64(json.loads(body)["confirm"]) == sync.confirmation(key, b"server", record)
        self.key = key
        return True

    def sync(self, snapshot: dict) -> tuple[int, dict | None]:
        blob = sync.seal(self.key, snapshot, self.device_id)
        status, body = self.post("/v1/sync", blob, {"X-Triat-Device": self.device_id})
        return status, (sync.open_sealed(self.key, body, self.device_id)
                        if status == 200 else None)


@pytest.fixture
def server(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("sync_port", 0)
    library = Library(tmp_path / "l.db")
    changed = []
    srv = sync.SyncServer(library, settings, cookie_header=lambda: "SID=abc",
                          on_changed=changed.append, name="Bureau",
                          keystore=FakeKeys())
    assert srv.start(announce=False)
    srv.changed = changed
    yield srv
    srv.stop()
    library.close()


def track(i: int) -> dict:
    return sync_state.track_to_wire(TrackRef(f"{i:011d}", f"T{i}", "A", 120.0))


def test_pair_then_sync_merges_both_ways(server):
    pid = server.library.create_playlist("Bureau")
    server.library.add_to_playlist(pid, [TrackRef(f"{1:011d}", "T1", "A", 120.0)])
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    assert "telephone-1" in server.devices()

    mine = {"v": 1, "playlists": [{"uid": "pl-phone", "name": "Téléphone",
                                   "updated": 1_700_000_000_000, "deleted": False,
                                   "tracks": [track(2)]}],
            "likes": [{"id": track(2)["id"], "at": 1_700_000_000_000,
                       "liked": True, "track": track(2)}],
            "history": []}
    status, merged = phone.sync(mine)
    assert status == 200
    assert {p["name"] for p in merged["playlists"]} == {"Bureau", "Téléphone"}
    assert "cookies" not in merged                 # partage désactivé par défaut
    names = {p["name"] for p in server.library.list_playlists()}
    assert names == {"Bureau", "Téléphone"}
    assert server.changed                          # l'interface sera prévenue


def test_direction_from_phone_updates_desktop_only(server):
    server.settings.set("sync_direction", "from_phone")
    pid = server.library.create_playlist("Bureau")
    server.library.add_to_playlist(pid, [TrackRef(f"{1:011d}", "T1", "A", 120.0)])
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    mine = {"v": 1, "playlists": [{"uid": "pl-phone", "name": "Téléphone",
                                   "updated": 1_700_000_000_000, "deleted": False,
                                   "tracks": [track(2)]}],
            "likes": [], "history": []}
    status, reply = phone.sync(mine)
    assert status == 200
    # Le téléphone ne reçoit que ce qu'il a envoyé : il ne change pas.
    assert {p["name"] for p in reply["playlists"]} == {"Téléphone"}
    # Le PC a bien absorbé la playlist du téléphone.
    assert {p["name"] for p in server.library.list_playlists()} == {"Bureau", "Téléphone"}


def test_direction_to_phone_updates_phone_only(server):
    server.settings.set("sync_direction", "to_phone")
    pid = server.library.create_playlist("Bureau")
    server.library.add_to_playlist(pid, [TrackRef(f"{1:011d}", "T1", "A", 120.0)])
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    mine = {"v": 1, "playlists": [{"uid": "pl-phone", "name": "Téléphone",
                                   "updated": 1_700_000_000_000, "deleted": False,
                                   "tracks": [track(2)]}],
            "likes": [], "history": []}
    status, reply = phone.sync(mine)
    assert status == 200
    # Le téléphone reçoit la fusion : il gagne la playlist du PC.
    assert {p["name"] for p in reply["playlists"]} == {"Bureau", "Téléphone"}
    # Le PC, lui, ne change pas.
    assert {p["name"] for p in server.library.list_playlists()} == {"Bureau"}
    assert not server.changed


def test_cookies_only_when_shared(server):
    server.settings.set("sync_share_cookies", True)
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    _status, merged = phone.sync({"v": 1, "playlists": [], "likes": [], "history": []})
    assert merged["cookies"] == "SID=abc"


def test_wrong_code_is_refused_and_attempts_are_limited_per_ip(server):
    code = server.begin_pairing()
    phone = Phone(server.port)
    for _ in range(sync.PAIRING_ATTEMPTS):
        assert not phone.pair("ZZZZZZZZ")
    assert not phone.pair(code)                    # cette IP a épuisé ses essais
    assert server.devices() == {}


def test_one_neighbour_cannot_burn_everyone_elses_attempts(server):
    code = server.begin_pairing()
    pair = server._pair_start
    finish = server._pair_finish
    _priv, pub = sync.new_keypair()
    for i in range(sync.PAIRING_ATTEMPTS + 2):     # le voisin insiste
        pair({"device_id": f"evil{i}", "name": "x", "pub": sync.b64(pub)}, "10.0.0.66")
        finish({"device_id": f"evil{i}", "confirm": ""}, "10.0.0.66")
    assert server._ip_blocked("10.0.0.66")
    assert server.pairing_active                   # le vrai téléphone peut venir
    assert Phone(server.port).pair(code)


def test_global_attempt_cap_closes_pairing(server):
    server.begin_pairing()
    _priv, pub = sync.new_keypair()
    for i in range(sync.PAIRING_ATTEMPTS_TOTAL):
        ip = f"10.0.{i // 4}.{i % 4}"              # 4 essais par IP, jamais bloquée
        server._pair_start({"device_id": f"d{i}", "name": "x",
                            "pub": sync.b64(pub)}, ip)
        server._pair_finish({"device_id": f"d{i}", "confirm": ""}, ip)
    assert not server.pairing_active


def test_no_pairing_without_a_code_on_screen(server):
    assert not Phone(server.port).pair("ABCDEFGH")


def test_unknown_or_tampered_requests_are_rejected(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    blob = bytearray(sync.seal(phone.key, {"v": 1}, phone.device_id))
    blob[-1] ^= 1                                  # un bit modifié
    status, _ = phone.post("/v1/sync", bytes(blob), {"X-Triat-Device": phone.device_id})
    assert status == 401
    stranger = Phone(server.port, "inconnu")
    stranger.key = phone.key
    assert stranger.sync({"v": 1})[0] == 401


def test_a_code_pairs_one_device_only(server):
    code = server.begin_pairing()
    assert Phone(server.port, "a").pair(code)
    assert not Phone(server.port, "b").pair(code)


def test_hello_announces_the_desktop(server):
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/v1/hello") as r:
        info = json.loads(r.read())
    assert info["app"] == "triat" and info["name"] == "Bureau"


def test_pairing_uri_se_relit():
    from urllib.parse import urlparse, parse_qs
    from triat.core.sync import pairing_uri
    u = urlparse(pairing_uri("192.168.1.5", 47823, "abcd-efgh", "id&x=1"))
    q = parse_qs(u.query)
    assert (u.scheme, u.netloc) == ("triat", "pair")
    assert q == {"host": ["192.168.1.5"], "port": ["47823"],
                 "code": ["ABCDEFGH"], "id": ["id&x=1"]}


def test_malformed_payloads_get_a_clean_error(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    status, _ = phone.sync({"v": 1, "playlists": "oops"})
    assert status == 400
    status, body = phone.post("/v1/pair/start", b"[1, 2]")
    assert status == 400
    status, merged = phone.sync({"v": 1, "playlists": [], "likes": [], "history": []})
    assert status == 200                           # le serveur continue de répondre


def test_internal_error_is_a_500_not_a_dropped_socket(server, monkeypatch):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(sync_state, "apply", boom)
    status, _ = phone.sync({"v": 1, "playlists": [], "likes": [], "history": []})
    assert status == 500
    monkeypatch.undo()
    assert phone.sync({"v": 1, "playlists": [], "likes": [], "history": []})[0] == 200


def test_unknown_device_is_refused_before_reading_the_body(server):
    phone = Phone(server.port, "inconnu")
    status, _ = phone.post("/v1/sync", b"x" * 100, {"X-Triat-Device": "inconnu"})
    assert status == 401


def test_forgotten_device_does_not_come_back(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    server.forget_device("telephone-1")
    assert phone.sync({"v": 1, "playlists": [], "likes": [], "history": []})[0] == 401
    assert server.devices() == {}


def test_pending_pairings_are_bounded(server):
    server.begin_pairing()
    codes = []
    for i in range(sync.MAX_PENDING_PER_IP + 1):
        _priv, pub = sync.new_keypair()
        codes.append(Phone(server.port, f"p{i}").post("/v1/pair/start", json.dumps(
            {"device_id": f"p{i}", "name": "x", "pub": sync.b64(pub)}).encode())[0])
    assert codes[-1] == 429 and codes[0] == 200
    # Global : d'autres adresses remplissent le reste, puis 429.
    _priv, pub = sync.new_keypair()
    results = [server._pair_start({"device_id": f"q{i}", "name": "x",
                                   "pub": sync.b64(pub)}, f"10.1.0.{i}")[0]
               for i in range(sync.MAX_PENDING)]
    assert results[-1] == 429


def test_only_local_clients_are_served():
    assert sync._is_local_client("192.168.1.5")
    assert sync._is_local_client("::ffff:10.0.0.2")
    assert not sync._is_local_client("8.8.8.8")


def test_local_ipv4_skips_virtual_interfaces(monkeypatch):
    import types
    import ifaddr

    def adapter(name, ip):
        return types.SimpleNamespace(nice_name=name, name=name,
                                     ips=[types.SimpleNamespace(ip=ip)])
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [
        adapter("docker0", "172.17.0.1"), adapter("tailscale0", "100.101.1.2"),
        adapter("wlan0", "192.168.1.10"), adapter("eth0", "169.254.3.4")])
    monkeypatch.setattr(sync.socket, "socket", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert sync._local_ipv4() == ["192.168.1.10"]


def test_start_does_not_block_on_mdns(tmp_path, monkeypatch):
    import threading
    import time
    import zeroconf
    registered = threading.Event()

    class Slow:
        def register_service(self, info):
            time.sleep(0.5)
            registered.set()

        def unregister_service(self, info): ...
        def close(self): ...
        def update_service(self, info): ...

    monkeypatch.setattr(zeroconf, "Zeroconf", Slow)
    settings = Settings(tmp_path / "s.json")
    settings.set("sync_port", 0)
    library = Library(tmp_path / "l.db")
    srv = sync.SyncServer(library, settings, name="Bureau")
    t0 = time.time()
    assert srv.start(announce=True)
    assert time.time() - t0 < 0.3
    assert registered.wait(3)
    srv.stop()
    library.close()


def test_pairing_uri_carries_several_hosts():
    uri = sync.pairing_uri(["192.168.1.2", "10.0.0.2"], 47823, "ABCD2345", "id1")
    assert uri.count("host=") == 2 and uri.index("192.168.1.2") < uri.index("10.0.0.2")


# ---- Format lié au sens, fraîcheur, retrait, entretien ----------------------

def _bound(phone, snapshot, ts=None, n=None):
    """Requête au format lié au sens ; renvoie (statut, corps, n)."""
    import os
    import time
    n = n or sync.b64(os.urandom(16))
    payload = dict(snapshot, ts=int(time.time() * 1000) if ts is None else ts, n=n)
    blob = sync.seal(phone.key, payload, phone.device_id,
                     aad=sync.request_aad(phone.device_id))
    status, body = phone.post("/v1/sync", blob, {"X-Triat-Device": phone.device_id})
    return status, body, n


EMPTY = {"v": 1, "playlists": [], "likes": [], "history": []}


def test_bound_request_gets_a_bound_response(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    status, body, n = _bound(phone, EMPTY)
    assert status == 200
    reply = sync.open_sealed(phone.key, body, phone.device_id,
                             aad=sync.response_aad(phone.device_id, n))
    assert reply["v"] == 1
    # Ni lisible comme réponse historique, ni pour une autre requête.
    with pytest.raises(Exception):
        sync.open_sealed(phone.key, body, phone.device_id)
    with pytest.raises(Exception):
        sync.open_sealed(phone.key, body, phone.device_id,
                         aad=sync.response_aad(phone.device_id, "autre-nonce-xx"))


def test_legacy_request_gets_a_legacy_response(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    assert phone.sync(EMPTY)[0] == 200             # Phone.sync = ancien format


def test_replayed_request_is_refused(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    blob = sync.seal(phone.key, dict(EMPTY, ts=int(__import__("time").time() * 1000),
                                     n="AAAAAAAAAAAAAAAAAAAAAA=="),
                     phone.device_id, aad=sync.request_aad(phone.device_id))
    headers = {"X-Triat-Device": phone.device_id}
    assert phone.post("/v1/sync", blob, headers)[0] == 200
    assert phone.post("/v1/sync", blob, headers)[0] == 409


def test_request_is_not_a_response_and_direction_is_bound(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    # Une réponse du bureau, renvoyée comme requête : refusée.
    status, body, n = _bound(phone, EMPTY)
    again = phone.post("/v1/sync", body, {"X-Triat-Device": phone.device_id})
    assert again[0] == 401


def test_stale_or_missing_timestamp_is_refused(server):
    import time
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    old = int((time.time() - 3600) * 1000)
    status, body, _ = _bound(phone, EMPTY, ts=old)
    assert status == 400 and json.loads(body)["error"] == "clock"
    blob = sync.seal(phone.key, dict(EMPTY), phone.device_id,
                     aad=sync.request_aad(phone.device_id))     # ni ts ni n
    assert phone.post("/v1/sync", blob, {"X-Triat-Device": phone.device_id})[0] == 400


def test_nonce_memory_is_bounded(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    for _ in range(sync.NONCE_MEMORY + 5):
        assert _bound(phone, EMPTY)[0] == 200
    assert len(server._nonces["telephone-1"]) == sync.NONCE_MEMORY


def test_unpair_forgets_the_device(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    import os
    import time
    n = sync.b64(os.urandom(16))
    blob = sync.seal(phone.key, {"ts": int(time.time() * 1000), "n": n},
                     phone.device_id, aad=sync.request_aad(phone.device_id))
    status, body = phone.post("/v1/unpair", blob, {"X-Triat-Device": phone.device_id})
    assert status == 200
    ack = sync.open_sealed(phone.key, body, phone.device_id,
                           aad=sync.response_aad(phone.device_id, n))
    assert ack == {"ok": True}
    assert server.devices() == {}
    assert phone.sync(EMPTY)[0] == 401


def test_unpair_needs_authentication(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    stranger = Phone(server.port, "inconnu")
    assert stranger.post("/v1/unpair", b"x" * 40,
                         {"X-Triat-Device": "inconnu"})[0] == 401
    forged = sync.seal(b"k" * 32, {"ts": 1, "n": "abcdefghijkl"}, phone.device_id,
                       aad=sync.request_aad(phone.device_id))
    assert phone.post("/v1/unpair", forged, {"X-Triat-Device": phone.device_id})[0] == 401
    legacy = sync.seal(phone.key, EMPTY, phone.device_id)     # format historique
    assert phone.post("/v1/unpair", legacy, {"X-Triat-Device": phone.device_id})[0] == 401
    assert "telephone-1" in server.devices()


def test_pairing_is_unconfirmed_until_the_first_sync(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    assert server.devices()["telephone-1"]["confirmed"] is False
    assert phone.sync(EMPTY)[0] == 200
    assert server.devices()["telephone-1"]["confirmed"] is True


def test_unconfirmed_devices_are_pruned_after_ten_minutes(server):
    import time
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    assert server.prune_unconfirmed() == 0
    assert server.prune_unconfirmed(now=time.time() + 601) == 1
    assert server.devices() == {}
    # Un appareil confirmé, ou d'avant le drapeau, n'est jamais retiré.
    server._update_devices(lambda d: d.update({
        "ancien": {"name": "A", "key": "", "paired_at": 1.0, "last_sync": 2.0},
        "ok": {"name": "B", "key": "", "paired_at": 1.0, "confirmed": True}}))
    assert server.prune_unconfirmed(now=time.time() + 10_000) == 0


def test_port_file_is_written_and_removed(tmp_path):
    settings = Settings(tmp_path / "s.json")
    settings.set("sync_port", 0)
    library = Library(tmp_path / "l.db")
    path = tmp_path / "sync.port"
    srv = sync.SyncServer(library, settings, port_file=path)
    try:
        assert srv.start(announce=False)
        assert sync.read_port_file(path) == srv.port
    finally:
        srv.stop()
        library.close()
    assert not path.exists()
    assert sync.read_port_file(path) is None


def test_fallback_port_is_reported(tmp_path):
    import socket
    busy = socket.socket()
    busy.bind(("0.0.0.0", 0))
    busy.listen()
    taken = busy.getsockname()[1]
    settings = Settings(tmp_path / "s.json")
    settings.set("sync_port", taken)
    library = Library(tmp_path / "l.db")
    srv = sync.SyncServer(library, settings, port_file=None)
    try:
        assert srv.start(announce=False)
        assert srv.fallback_from == taken and srv.port != taken
    finally:
        srv.stop()
        library.close()
        busy.close()


def test_default_name_is_neutral_and_never_reads_the_hostname(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "s.json")
    library = Library(tmp_path / "l.db")
    srv = sync.SyncServer(library, settings)

    def boom():
        raise AssertionError("le nom d'hôte ne doit pas être lu")
    monkeypatch.setattr(sync.socket, "gethostname", boom)
    assert srv.name == "Triat"
    settings.set("sync_name", "Salon")
    assert srv.name == "Salon"          # réglable
    library.close()


def test_start_prunes_old_tombstones(tmp_path):
    import time
    settings = Settings(tmp_path / "s.json")
    settings.set("sync_port", 0)
    library = Library(tmp_path / "l.db")
    now = time.time()
    library.record_tombstone("like", "ancien", now - 200 * 86400)
    library.record_tombstone("like", "recent", now - 10 * 86400)
    srv = sync.SyncServer(library, settings, port_file=None)
    try:
        assert srv.start(announce=False)
        assert set(library.tombstones("like")) == {"recent"}
    finally:
        srv.stop()
        library.close()


# ---- Golden : le format v1 ne dérive pas ------------------------------------

def _fixture():
    from pathlib import Path
    path = Path(__file__).parent / "fixtures" / "sync_v1_snapshot.json"
    return json.loads(path.read_text())


def test_golden_blobs_open_with_their_framing_only():
    fx = _fixture()
    key, dev, n = bytes.fromhex(fx["key_hex"]), fx["device_id"], fx["request_n"]
    blobs = {k: sync.unb64(v) for k, v in fx["blobs"].items()}
    assert sync.open_sealed(key, blobs["legacy_request"], dev) == fx["snapshot"]
    bound = sync.open_sealed(key, blobs["bound_request"], dev, aad=sync.request_aad(dev))
    assert bound == dict(fx["snapshot"], ts=fx["request_ts"], n=n)
    assert sync.open_sealed(key, blobs["legacy_response"], dev) == fx["merged"]
    assert sync.open_sealed(key, blobs["bound_response"], dev,
                            aad=sync.response_aad(dev, n)) == fx["merged"]
    with pytest.raises(Exception):      # réflexion : requête lue comme réponse
        sync.open_sealed(key, blobs["bound_request"], dev,
                         aad=sync.response_aad(dev, n))
    with pytest.raises(Exception):
        sync.open_sealed(key, blobs["bound_request"], dev)


def test_golden_snapshot_survives_sanitize_and_merge():
    fx = _fixture()
    clean = sync_state.sanitize(json.loads(json.dumps(fx["snapshot"])))
    assert clean is not None
    for name in ("playlists", "likes", "history"):
        assert len(clean[name]) == len(fx["snapshot"][name])
    merged = sync_state.merge(fx["snapshot"], {"v": 1, "playlists": [],
                                               "likes": [], "history": []})
    assert merged == {k: v for k, v in fx["merged"].items() if k != "now"}


def test_golden_snapshot_applies_to_a_library(tmp_path):
    fx = _fixture()
    library = Library(tmp_path / "l.db")
    try:
        changes = sync_state.apply(library, fx["merged"])
        assert changes == {"playlists": 1, "likes": 1, "history": 2}
        assert [p["name"] for p in library.list_playlists()] == ["Route"]
        assert library.stats()["seconds"] == 42.5       # 0 = durée inconnue
    finally:
        library.close()


# ---- Anti-downgrade ---------------------------------------------------------

def test_legacy_stays_accepted_for_a_device_never_seen_bound(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    assert phone.sync(EMPTY)[0] == 200
    assert phone.sync(EMPTY)[0] == 200             # ancien format, toujours là
    assert not server.devices()[phone.device_id].get("bound")


def test_legacy_is_refused_after_a_bound_exchange(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    legacy = sync.seal(phone.key, EMPTY, phone.device_id)   # capturé avant
    headers = {"X-Triat-Device": phone.device_id}
    assert phone.post("/v1/sync", legacy, headers)[0] == 200
    assert _bound(phone, EMPTY)[0] == 200
    assert server.devices()[phone.device_id]["bound"] is True
    assert phone.post("/v1/sync", legacy, headers)[0] == 401   # rejeu refusé
    assert phone.sync(EMPTY)[0] == 401
    assert _bound(phone, EMPTY)[0] == 200                      # lié toujours OK


def test_refuse_legacy_setting_refuses_everyone(server):
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    server.settings.set("sync_refuse_legacy", True)
    assert phone.sync(EMPTY)[0] == 401
    assert _bound(phone, EMPTY)[0] == 200


# ---- Pistes SoundCloud : jamais envoyées à Android ---------------------------

SC = "soundcloud:tracks:123"


def test_soundcloud_never_appears_in_the_encrypted_reply(server):
    lib = server.library
    sc = TrackRef(SC, "Titre SC", "Artiste SC", 100.0)
    yt = TrackRef(f"{1:011d}", "T1", "A", 120.0)
    pid = lib.create_playlist("Mixte")
    lib.add_to_playlist(pid, [yt, sc])
    lib.set_favorite(sc, True)
    lib.set_favorite(sc, False)         # laisse une tombstone `like`
    lib.set_favorite(sc, True)
    lib.record_play(sc, 60, True)
    lib.record_tombstone("like", "soundcloud:tracks:9", 1_700_000_000.0)
    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    status, reply = phone.sync(EMPTY)
    assert status == 200
    assert "soundcloud" not in json.dumps(reply)
    assert [t["id"] for p in reply["playlists"] for t in p["tracks"]] == [yt.video_id]
    # Et la piste SC est toujours là, côté bureau.
    assert [r.video_id for r in lib.playlist_items(pid)] == [yt.video_id, SC]


# ---- P2-5 : plafond de connexions, 4xx fermantes, en-têtes bornés -----------

def _raw(server, payload: bytes, read_all=True) -> bytes:
    import socket
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as conn:
        conn.sendall(payload)
        data = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
            if not read_all:
                break
        return data


def test_4xx_answers_close_the_connection(server):
    # Corps hors limite : 400 et fermeture (sinon le corps non lu serait
    # relu comme une nouvelle requête).
    raw = _raw(server, b"POST /v1/pair/start HTTP/1.1\r\nHost: x\r\n"
                       b"Content-Length: 999999\r\n\r\n")
    assert raw.startswith(b"HTTP/1.1 400") and b"Connection: close" in raw
    raw = _raw(server, b"GET /nope HTTP/1.1\r\nHost: x\r\n\r\n")
    assert raw.startswith(b"HTTP/1.1 404") and b"Connection: close" in raw


def test_oversized_headers_are_refused(server):
    big = b"X-Junk: " + b"a" * 1000 + b"\r\n"
    raw = _raw(server, b"GET /v1/hello HTTP/1.1\r\nHost: x\r\n"
               + big * 40 + b"\r\n")
    assert raw.startswith(b"HTTP/1.1 431")
    ok = _raw(server, b"GET /v1/hello HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert ok.startswith(b"HTTP/1.1 200")      # le serveur reste sain


def test_open_connections_are_capped(server):
    import socket
    held = []
    try:
        # Connexions muettes (slowloris) : chacune occupe un fil.
        for _ in range(sync.MAX_OPEN_CONNECTIONS):
            held.append(socket.create_connection(("127.0.0.1", server.port), timeout=5))
        import time
        time.sleep(0.3)
        raw = _raw(server, b"GET /v1/hello HTTP/1.1\r\nHost: x\r\n\r\n")
        assert raw.startswith(b"HTTP/1.1 503")
    finally:
        for conn in held:
            conn.close()
    import time
    for _ in range(50):                  # les places se libèrent
        raw = _raw(server, b"GET /v1/hello HTTP/1.1\r\nHost: x\r\n"
                           b"Connection: close\r\n\r\n")
        if raw.startswith(b"HTTP/1.1 200"):
            break
        time.sleep(0.1)
    assert raw.startswith(b"HTTP/1.1 200")


# ---- P2-6 : drain ------------------------------------------------------------

def test_drain_waits_for_a_merge_in_flight(server):
    import threading
    import time
    entered, release, order = threading.Event(), threading.Event(), []
    original = sync_state.apply

    def slow_apply(library, merged):
        entered.set()
        release.wait(5)
        order.append("fusion finie")
        return original(library, merged)

    phone = Phone(server.port)
    assert phone.pair(server.begin_pairing())
    sync_state.apply = slow_apply
    try:
        worker = threading.Thread(target=phone.sync, args=(EMPTY,))
        worker.start()
        assert entered.wait(5)

        def drained():
            with server.drain():
                order.append("drain")
        waiter = threading.Thread(target=drained)
        waiter.start()
        time.sleep(0.2)
        assert order == []               # drain attend la fusion en vol
        release.set()
        worker.join(5)
        waiter.join(5)
    finally:
        sync_state.apply = original
        release.set()
    assert order == ["fusion finie", "drain"]


def test_read_port_file_is_ascii(tmp_path):
    path = tmp_path / "sync.port"
    path.write_bytes("51234\n".encode("ascii"))
    assert sync.read_port_file(path) == 51234
    path.write_bytes(b"\xff\xfe")
    assert sync.read_port_file(path) is None

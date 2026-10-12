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
                          on_changed=changed.append, name="Bureau")
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


def test_wrong_code_is_refused_and_attempts_are_limited(server):
    code = server.begin_pairing()
    phone = Phone(server.port)
    for _ in range(sync.PAIRING_ATTEMPTS):
        assert not phone.pair("ZZZZZZZZ")
    assert not server.pairing_active               # fermé après 5 essais
    assert not phone.pair(code)                    # même le bon code, trop tard
    assert server.devices() == {}


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
    for i in range(sync.MAX_PENDING + 1):
        _priv, pub = sync.new_keypair()
        codes.append(Phone(server.port, f"p{i}").post("/v1/pair/start", json.dumps(
            {"device_id": f"p{i}", "name": "x", "pub": sync.b64(pub)}).encode())[0])
    assert codes[-1] == 429 and codes[0] == 200


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

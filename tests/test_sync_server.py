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

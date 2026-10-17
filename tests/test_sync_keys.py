"""Synchro : monotonie de `ts` (P2-7) et clés des appareils au trousseau (P2-8)."""

from __future__ import annotations

import json
import logging
import time

import pytest

from triat.core import sync
from triat.core.library import Library
from triat.core.settings import Settings
from tests.test_sync_server import EMPTY, FakeKeys, Phone, _bound


def make(tmp_path, keys, name="s.json"):
    settings = Settings(tmp_path / name)
    settings.set("sync_port", 0)
    library = Library(tmp_path / "l.db")
    srv = sync.SyncServer(library, settings, name="Bureau", keystore=keys)
    assert srv.start(announce=False)
    return srv, library


@pytest.fixture
def env(tmp_path):
    made = []

    def factory(keys=None, name="s.json"):
        keys = FakeKeys() if keys is None else keys
        srv, lib = make(tmp_path, keys, name)
        made.append((srv, lib))
        return srv, keys

    yield factory
    for srv, lib in made:
        srv.stop()
        lib.close()


def now_ms() -> int:
    return int(time.time() * 1000)


# ---- P2-7 : monotonie ---------------------------------------------------------

def test_older_ts_is_refused(env):
    srv, _ = env()
    phone = Phone(srv.port)
    assert phone.pair(srv.begin_pairing())
    base = now_ms()
    assert _bound(phone, EMPTY, ts=base)[0] == 200
    status, body, _ = _bound(phone, EMPTY, ts=base - 1000)     # dans la fenêtre
    assert status == 409 and json.loads(body)["error"] == "replay"
    assert _bound(phone, EMPTY, ts=base + 1)[0] == 200


def test_same_ts_needs_a_new_nonce(env):
    srv, _ = env()
    phone = Phone(srv.port)
    assert phone.pair(srv.begin_pairing())
    base = now_ms()
    assert _bound(phone, EMPTY, ts=base, n="A" * 22)[0] == 200
    assert _bound(phone, EMPTY, ts=base, n="A" * 22)[0] == 409
    assert _bound(phone, EMPTY, ts=base, n="B" * 22)[0] == 200


def test_replay_is_refused_after_server_restart(env):
    srv, keys = env()
    phone = Phone(srv.port)
    assert phone.pair(srv.begin_pairing())
    base = now_ms()
    assert _bound(phone, EMPTY, ts=base, n="A" * 22)[0] == 200
    assert _bound(phone, EMPTY, ts=base + 5, n="C" * 22)[0] == 200
    srv.stop()
    srv2, _ = env(keys)                    # mêmes réglages, mémoire de nonces vide
    assert srv2._nonces == {}
    phone.base = f"http://127.0.0.1:{srv2.port}"
    assert _bound(phone, EMPTY, ts=base, n="A" * 22)[0] == 409       # ts antérieur
    assert _bound(phone, EMPTY, ts=base + 5, n="C" * 22)[0] == 409   # même requête
    assert _bound(phone, EMPTY, ts=base + 6)[0] == 200


def test_legacy_warning_once_per_device(env, caplog):
    srv, _ = env()
    phone = Phone(srv.port)
    assert phone.pair(srv.begin_pairing())
    with caplog.at_level(logging.WARNING, logger="triat.core.sync"):
        assert phone.sync(EMPTY)[0] == 200
        assert phone.sync(EMPTY)[0] == 200
    warnings = [r for r in caplog.records if "ancien format" in r.getMessage()]
    assert len(warnings) == 1
    assert srv.settings.get("sync_refuse_legacy") is False        # défaut inchangé


# ---- P2-8 : trousseau ---------------------------------------------------------

def test_pairing_puts_the_key_in_the_keyring_only(env):
    srv, keys = env()
    phone = Phone(srv.port)
    assert phone.pair(srv.begin_pairing())
    info = srv.devices()[phone.device_id]
    assert "key" not in info and info["keyring"] is True
    assert sync.unb64(keys.data[phone.device_id]) == phone.key
    assert keys.data[phone.device_id] not in srv.settings._path.read_text()


def test_full_encrypted_round_trip_with_keyring(env):
    srv, _ = env()
    phone = Phone(srv.port)
    assert phone.pair(srv.begin_pairing())
    srv.library.create_playlist("Bureau")
    status, body, n = _bound(phone, dict(EMPTY, playlists=[]))
    assert status == 200
    reply = sync.open_sealed(phone.key, body, phone.device_id,
                             aad=sync.response_aad(phone.device_id, n))
    assert reply["v"] == 1 and reply["playlists"]


def test_migration_moves_keys_from_settings(env, tmp_path):
    key = b"k" * 32
    settings = Settings(tmp_path / "s.json")
    settings.set("sync_devices", {"tel": {"name": "P", "key": sync.b64(key),
                                          "paired_at": 0, "last_sync": None}})
    settings.save()
    srv, keys = env()
    assert keys.data == {"tel": sync.b64(key)}
    info = srv.devices()["tel"]
    assert "key" not in info and info["keyring"] is True
    assert sync.b64(key) not in (tmp_path / "s.json").read_text()
    # Et la synchro marche toujours avec la clé du trousseau.
    phone = Phone(srv.port, "tel")
    phone.key = key
    assert _bound(phone, EMPTY)[0] == 200


def test_failed_keyring_write_keeps_key_in_settings(env, tmp_path, caplog):
    key = b"k" * 32
    settings = Settings(tmp_path / "s.json")
    settings.set("sync_devices", {"tel": {"name": "P", "key": sync.b64(key),
                                          "paired_at": 0, "last_sync": None}})
    settings.save()
    with caplog.at_level(logging.WARNING, logger="triat.core.sync"):
        srv, _ = env(FakeKeys(broken=True))
    assert srv.devices()["tel"]["key"] == sync.b64(key)
    assert "keyring" not in srv.devices()["tel"]
    assert sum("trousseau indisponible" in r.getMessage()
               for r in caplog.records) == 1
    phone = Phone(srv.port, "tel")
    phone.key = key
    assert _bound(phone, EMPTY)[0] == 200


def test_unavailable_keyring_falls_back_to_settings_on_pairing(env, caplog):
    srv, _ = env(FakeKeys(broken=True))
    with caplog.at_level(logging.WARNING, logger="triat.core.sync"):
        phone = Phone(srv.port)
        assert phone.pair(srv.begin_pairing())
        srv.cancel_pairing()
        phone2 = Phone(srv.port, "telephone-2")
        assert phone2.pair(srv.begin_pairing())
    assert srv.devices()[phone.device_id]["key"] == sync.b64(phone.key)
    assert _bound(phone, EMPTY)[0] == 200
    assert sum("trousseau indisponible" in r.getMessage()
               for r in caplog.records) == 1
    assert sync.b64(phone.key) not in "".join(r.getMessage() for r in caplog.records)


def test_forget_device_and_all_clear_the_keyring(env):
    srv, keys = env()
    a, b = Phone(srv.port, "tel-a"), Phone(srv.port, "tel-b")
    assert a.pair(srv.begin_pairing())
    assert b.pair(srv.begin_pairing())
    assert set(keys.data) == {"tel-a", "tel-b"}
    srv.forget_device("tel-a")
    assert set(keys.data) == {"tel-b"}
    srv.forget_all_devices()
    assert keys.data == {}


def test_unpair_by_phone_clears_the_keyring(env):
    srv, keys = env()
    phone = Phone(srv.port)
    assert phone.pair(srv.begin_pairing())
    import os
    blob = sync.seal(phone.key, {"ts": now_ms(), "n": sync.b64(os.urandom(16))},
                     phone.device_id, aad=sync.request_aad(phone.device_id))
    assert phone.post("/v1/unpair", blob, {"X-Triat-Device": phone.device_id})[0] == 200
    assert keys.data == {}


def test_missing_keyring_entry_refuses_without_crash(env):
    srv, keys = env()
    phone = Phone(srv.port)
    assert phone.pair(srv.begin_pairing())
    keys.data.clear()
    assert _bound(phone, EMPTY)[0] == 401

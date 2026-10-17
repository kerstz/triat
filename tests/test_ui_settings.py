"""Réglages : synchro (adresses, appairage), confirmations, balisage."""

from __future__ import annotations

import threading
import time
import types

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import pytest
from gi.repository import Adw, GLib, Gio

from triat.ui import settings_dialog as sd
from triat.ui.settings_dialog import SettingsDialog
from tests.test_window import engine, window  # noqa: F401  (fixtures)


@pytest.fixture(scope="session")
def application():
    """Application de test propre à ce module (autre identifiant que
    test_window : un chemin D-Bus ne s'enregistre qu'une fois)."""
    app = Adw.Application(application_id="org.triat.TriatTestSettings",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    app.emit("startup")
    return app


def spin(predicate, timeout: float = 3.0) -> bool:
    context = GLib.MainContext.default()
    end = GLib.get_monotonic_time() + timeout * 1_000_000
    while GLib.get_monotonic_time() < end:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
    return predicate()


@pytest.fixture
def dialog(window, engine, monkeypatch):               # noqa: F811
    monkeypatch.setattr(engine.sync, "_announce", lambda *_a: None)
    monkeypatch.setattr(sd, "local_addresses", lambda: ["192.168.1.5", "10.0.0.2"])
    dialog = SettingsDialog(engine)
    dialog.present(window)
    yield dialog
    dialog._close_pairing()
    dialog._stop_devices_timer()


def _device(name="Téléphone", last_sync=None):
    return {"name": name, "key": "AAAA", "paired_at": 1.0,
            "last_sync": last_sync}


# ---- Adresses ---------------------------------------------------------------

def test_addresses_are_comma_separated_and_copyable(dialog, engine):
    dialog.sync_switch.set_active(True)
    port = engine.sync.port
    assert dialog.sync_address.get_subtitle() == (
        f"192.168.1.5:{port}, 10.0.0.2:{port}")
    assert " · " not in dialog.sync_address.get_subtitle()
    assert dialog.copy_address_button.get_sensitive()
    assert dialog._addresses == [f"192.168.1.5:{port}", f"10.0.0.2:{port}"]


def test_copy_is_disabled_when_the_server_is_off(dialog):
    assert not dialog.copy_address_button.get_sensitive()
    assert dialog._addresses == []


def test_qr_carries_every_usable_address(dialog, engine, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    seen = []
    monkeypatch.setattr(sd, "pairing_qr_svg", lambda uri: seen.append(uri))
    dialog.sync_switch.set_active(True)
    dialog.pair_button.emit("clicked")
    assert seen, "le QR n'a pas été demandé"
    query = parse_qs(urlparse(seen[0]).query)
    assert query["host"] == ["192.168.1.5", "10.0.0.2"]
    assert query["port"] == [str(engine.sync.port)]


# ---- Appareils --------------------------------------------------------------

def test_last_sync_is_refreshed_in_place(dialog, engine):
    now = time.time()
    engine.settings.set("sync_devices", {"t1": _device(last_sync=now - 5)})
    dialog._refresh_devices()
    row = dialog._device_rows[0]
    assert "à l'instant" in row.get_subtitle()

    engine.settings.set("sync_devices", {"t1": _device(last_sync=now - 1800)})
    dialog._refresh_last_sync()
    assert dialog._device_rows[0] is row, "les lignes ne sont pas reconstruites"
    assert "il y a 30 min" in row.get_subtitle()


def test_device_refresh_timer_starts_and_stops(dialog):
    dialog._start_devices_timer()
    assert dialog._devices_timer
    dialog._start_devices_timer()            # idempotent
    dialog._stop_devices_timer()
    assert dialog._devices_timer == 0


def test_device_name_is_not_read_as_markup(dialog, engine):
    engine.settings.set("sync_devices",
                        {"t1": _device("Pixel <b>& Co</b>")})
    dialog._refresh_devices()
    row = dialog._device_rows[0]
    assert row.get_title() == "Pixel <b>& Co</b>"
    assert not row.get_use_markup()


def test_dynamic_rows_disable_markup(dialog):
    for row in (dialog.status_row, dialog.library_stats, dialog.scrobble_status,
                dialog.sync_address):
        assert not row.get_use_markup()


# ---- Appairage --------------------------------------------------------------

def test_countdown_stops_when_the_server_closes_pairing(dialog, engine):
    dialog.sync_switch.set_active(True)
    dialog.pair_button.emit("clicked")
    assert dialog._tick_pairing() is True
    engine.sync.cancel_pairing()
    assert dialog._tick_pairing() is False
    assert dialog._pairing_timer == 0
    assert dialog.pairing_countdown.get_label() == "Appairage fermé : relance-le"


# ---- Confirmations ----------------------------------------------------------

def test_clearing_cookies_asks_first(dialog, engine, monkeypatch):
    cleared = []
    engine.cookies.clear = lambda: cleared.append(True)
    monkeypatch.setattr(dialog, "_after_cookie_change", lambda: None)
    dialog._clear_cookies(None)
    assert not cleared, "rien n'est effacé avant la réponse"
    dialog._on_clear_cookies_response(None, "cancel")
    assert not cleared
    dialog._on_clear_cookies_response(None, "clear")
    assert spin(lambda: cleared == [True])


def test_fill_durations_does_not_leak_its_handler(dialog, engine, monkeypatch):
    monkeypatch.setattr(sd, "DURATIONS_TIMEOUT", 0)
    monkeypatch.setattr(engine, "backfill_durations_async", lambda: None)
    ids = []
    original = engine.connect

    def spy(signal, callback, *args):
        handler = original(signal, callback, *args)
        if signal == "library-changed":
            ids.append(handler)
        return handler

    monkeypatch.setattr(engine, "connect", spy)
    row = sd._plain_row(title="Compléter les durées")
    button = sd.Gtk.Button()
    dialog._fill_durations(button, row)
    assert ids and engine.handler_is_connected(ids[-1])
    assert spin(lambda: not engine.handler_is_connected(ids[-1]))
    assert row.get_subtitle() == "Toutes les durées sont connues"


# ---- Tout effacer (régression : état de synchro périmé) -----------------------

def test_wipe_resets_the_sync_switch_and_devices(dialog, engine, monkeypatch):
    from triat.core import artwork
    monkeypatch.setattr(artwork, "clear_cache", lambda: None)
    monkeypatch.setattr(engine, "cookies", types.SimpleNamespace(
        clear=lambda: None, cookie_header=lambda: None), raising=False)
    monkeypatch.setattr(dialog, "_after_cookie_change", lambda: None)
    dialog.sync_switch.set_active(True)
    assert engine.sync.running
    engine.settings.set("sync_devices", {"t1": _device()})
    dialog._refresh_devices()
    dialog._on_wipe_response(None, "wipe")
    assert not dialog.sync_switch.get_active()
    assert not engine.sync.running
    assert engine.sync.devices() == {}
    assert dialog._device_rows and not dialog._device_ids
    assert dialog._device_rows[0].get_title() == "Aucun téléphone associé"
    assert not dialog.copy_address_button.get_sensitive()


@pytest.mark.parametrize("checked", [False, True])
def test_wipe_passes_the_downloads_checkbox(dialog, engine, monkeypatch, checked):
    calls = []
    monkeypatch.setattr(engine, "wipe_user_data",
                        lambda delete_downloads=False: calls.append(delete_downloads))
    monkeypatch.setattr(dialog, "_after_cookie_change", lambda: None)
    dialog._confirm_wipe(None)
    assert not dialog._wipe_downloads.get_active(), "décochée par défaut"
    dialog._wipe_downloads.set_active(checked)
    dialog._on_wipe_response(None, "wipe")
    assert calls == [checked]


def test_keyring_calls_leave_the_gtk_thread(dialog, engine, monkeypatch):
    seen = {}
    main = threading.current_thread()

    def record(name, result=None):
        def fn(*_a, **_k):
            seen[name] = threading.current_thread()
            return result
        return fn

    status = types.SimpleNamespace(present=False)
    monkeypatch.setattr(engine, "cookies", types.SimpleNamespace(
        status=record("status", status), import_file=record("import"),
        clear=record("clear")), raising=False)
    monkeypatch.setattr(dialog, "_after_cookie_change", lambda: None)

    dialog._refresh_status()
    assert dialog.status_row.get_subtitle() == "Vérification…"
    assert spin(lambda: dialog.status_row.get_subtitle()
                == "Aucun cookie enregistré")

    class _File:
        def get_path(self):
            return "/tmp/cookies.txt"

    class _Chooser:
        def open_finish(self, _result):
            return _File()

    dialog._on_cookie_file(_Chooser(), None)
    dialog._on_clear_cookies_response(None, "clear")
    assert spin(lambda: {"status", "import", "clear"} <= set(seen))
    assert all(t is not main for t in seen.values())


# ---- Recommandations écartées -----------------------------------------------

def test_blocklist_row_lists_and_undoes(dialog, engine):
    assert "Rien" in dialog.blocked_row.get_subtitle()
    engine.library.block("artist", "Daft Punk - Topic", "Daft Punk")
    engine.library.block("track", "abc", "Un titre")
    changes = []
    dialog._on_change = lambda key, value: changes.append(key)
    dialog.blocked_row.set_subtitle(dialog._blocked_summary())
    assert dialog.blocked_row.get_subtitle() == "1 titre et 1 artiste"
    dialog._manage_blocked()
    assert dialog.blocked_dialog is not None
    # Retirer le premier élément annule le blocage et prévient l'accueil.
    listing = dialog.blocked_dialog.get_extra_child().get_child().get_child()
    first = listing.get_first_child()
    first_button = [w for w in _descendants(first) if hasattr(w, "get_label")
                    and w.get_label() == "Retirer"][0]
    first_button.emit("clicked")
    assert engine.library.blocked()["artist"] == {}
    assert changes == ["blocklist"]
    assert dialog.blocked_row.get_subtitle() == "1 titre et 0 artiste"


def _descendants(widget):
    stack = [widget]
    while stack:
        item = stack.pop()
        yield item
        child = item.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()


def test_format_device_status_marks_unfinished_pairings():
    assert "inachevé" in sd.format_device_status({"confirmed": False})
    assert sd.format_device_status({"last_sync": None}) == "Jamais synchronisé"
    assert "à l'instant" in sd.format_device_status(
        {"confirmed": True, "last_sync": time.time()})


# ---- SoundCloud -------------------------------------------------------------

def _fake_cache(tmp_path, monkeypatch, found_ago=3 * 3600):
    import json
    import time
    cache = tmp_path / "cache"
    (cache / "triat").mkdir(parents=True)
    (cache / "triat" / "soundcloud.json").write_text(json.dumps(
        {"client_id": "SECRETVALUE123", "found_at": time.time() - found_ago}))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))


def test_soundcloud_is_off_by_default(dialog, engine):
    assert not dialog.sc_switch.row.get_active()
    assert not engine.settings.get("soundcloud_enabled")


def test_soundcloud_dependents_follow_the_switch(dialog):
    for widget in dialog._sc_dependents:
        assert not widget.get_sensitive()
    dialog.sc_switch.row.set_active(True)
    dialog.sc_switch.alert.emit("response", "enable")
    for widget in dialog._sc_dependents:
        assert widget.get_sensitive()
    dialog.sc_switch.row.set_active(False)
    for widget in dialog._sc_dependents:
        assert not widget.get_sensitive()


def test_soundcloud_cancel_writes_nothing(dialog, engine):
    dialog.sc_switch.row.set_active(True)
    alert = dialog.sc_switch.alert
    assert alert is not None
    assert "n'autorise pas officiellement" in alert.get_body()
    alert.emit("response", "cancel")
    assert not dialog.sc_switch.row.get_active()
    assert not engine.settings.get("soundcloud_enabled")
    assert not engine.settings.get("soundcloud_tos_ack")


def test_soundcloud_enable_sets_ack_then_skips_the_box(dialog, engine):
    dialog.sc_switch.row.set_active(True)
    dialog.sc_switch.alert.emit("response", "enable")
    assert engine.settings.get("soundcloud_tos_ack") is True
    assert engine.settings.get("soundcloud_enabled") is True
    dialog.sc_switch.row.set_active(False)
    assert not engine.settings.get("soundcloud_enabled")
    dialog.sc_switch.row.set_active(True)
    assert dialog.sc_switch.alert is None, "pas de seconde boîte après accord"
    assert engine.settings.get("soundcloud_enabled") is True


def test_soundcloud_purge_calls_the_library(dialog, engine, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.library, "purge_source",
                        lambda source: calls.append(source) or 4)
    dialog._on_purge_response(None, "cancel")
    assert calls == []
    dialog._on_purge_response(None, "purge")
    assert calls == ["soundcloud"]


def test_soundcloud_status_shows_age_never_the_client_id(
        window, engine, tmp_path, monkeypatch):              # noqa: F811
    _fake_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(engine.sync, "_announce", lambda *_a: None)
    other = SettingsDialog(engine)
    subtitle = other.sc_client.get_subtitle()
    assert "3 h" in subtitle
    assert "SECRETVALUE123" not in subtitle


def test_soundcloud_status_when_missing(dialog, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "vide"))
    assert sd.format_client_id_status(sd.client_id_age()) == (
        "Identifiant client : introuvable")


# ---- Fondu à la pause -------------------------------------------------------

def test_pause_fade_switch_calls_engine(dialog, engine, monkeypatch):   # noqa: F811
    seen = []
    monkeypatch.setattr(engine, "set_pause_fade", seen.append)
    dialog.pause_fade_row.set_active(not dialog.pause_fade_row.get_active())
    assert seen == [dialog.pause_fade_row.get_active()]

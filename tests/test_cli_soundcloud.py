"""`triat doctor` : section SoundCloud."""

from __future__ import annotations

import json
import re

from triat import cli
from triat.core import paths
from triat.core.settings import Settings

FAUX_ID = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"


class FauxClient:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def client_id(self, refresh=False):
        return FAUX_ID

    def search_tracks(self, q, limit=20, offset=0):
        self.calls.append((q, limit))
        if self.fail:
            raise RuntimeError(f"boom {FAUX_ID}")
        return [], None


def _affichage(lines):
    return "\n".join(f"{n} {lvl} {d}" for n, lvl, d in lines)


def _settings(tmp_path, on):
    s = Settings(tmp_path / "s.json")
    s.set("soundcloud_enabled", on)
    return s


def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path)
    (tmp_path / "soundcloud.json").write_text(
        json.dumps({"client_id": FAUX_ID, "found_at": 1.0}))


def test_desactive_une_ligne_sans_reseau(tmp_path):
    client = FauxClient()
    lines = cli._lignes_soundcloud(_settings(tmp_path, False), client)
    assert len(lines) == 1 and "désactivé" in lines[0][2]
    assert client.calls == []


def test_active_n_imprime_jamais_le_client_id(tmp_path, monkeypatch, capsys):
    _cache(tmp_path, monkeypatch)
    client = FauxClient()
    lines = cli._lignes_soundcloud(_settings(tmp_path, True), client)
    print(_affichage(lines))
    out = capsys.readouterr().out
    assert client.calls == [("test", 1)]
    assert FAUX_ID not in out
    assert not re.search(r"[0-9a-zA-Z]{32}", out)
    assert any("en cache depuis" in d for _, _, d in lines)


def test_echec_reseau_ne_fuit_pas_le_client_id(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    lines = cli._lignes_soundcloud(_settings(tmp_path, True), FauxClient(fail=True))
    texte = _affichage(lines)
    assert any(lvl == "fail" for _, lvl, _ in lines)
    assert not re.search(r"[0-9a-zA-Z]{32}", texte)


def test_active_sans_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path)
    lines = cli._lignes_soundcloud(_settings(tmp_path, True), FauxClient())
    assert any("pas encore en cache" in d for _, _, d in lines)

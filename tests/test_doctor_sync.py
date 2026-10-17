"""`triat doctor` : ligne « synchro » (décision pure, sondes simulées)."""

from __future__ import annotations

import http.server
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from triat import cli
from triat.core.sync import DEFAULT_PORT


# ---- Décision du message ---------------------------------------------------

def test_desactivee_ignore_le_reste():
    niveau, texte = cli.message_synchro(False, False, ["ufw"], DEFAULT_PORT)
    assert niveau == "ok"
    assert "désactivée" in texte
    assert "pare-feu" not in texte


def test_activee_et_a_l_ecoute_sans_pare_feu():
    niveau, texte = cli.message_synchro(True, True, [], DEFAULT_PORT)
    assert niveau == "ok"
    assert f"écoute sur le port {DEFAULT_PORT}" in texte
    assert "pare-feu" not in texte


def test_activee_mais_serveur_arrete():
    niveau, texte = cli.message_synchro(True, False, [], DEFAULT_PORT)
    assert niveau == "warn"
    assert "serveur arrêté" in texte
    assert str(DEFAULT_PORT) in texte


def test_pare_feu_firewalld_propose_la_commande():
    niveau, texte = cli.message_synchro(True, True, ["firewalld"], 47823)
    assert niveau == "warn"
    assert "firewall-cmd --permanent --add-port=47823/tcp" in texte
    assert "--add-service=mdns" in texte and "--reload" in texte
    assert "ufw" not in texte


def test_pare_feu_ufw_propose_la_commande():
    niveau, texte = cli.message_synchro(True, True, ["ufw"], 47823)
    assert niveau == "warn"
    assert "ufw allow 47823/tcp" in texte
    assert "ufw allow 5353/udp" in texte


def test_les_deux_pare_feux_donnent_les_deux_commandes():
    _, texte = cli.message_synchro(True, False, ["firewalld", "ufw"], 5000)
    assert "firewall-cmd --permanent --add-port=5000/tcp" in texte
    assert "ufw allow 5000/tcp" in texte


@pytest.mark.parametrize("active,ecoute,pare_feux", [
    (a, e, f) for a in (False, True) for e in (False, True)
    for f in ([], ["ufw"], ["firewalld"])])
def test_toutes_les_combinaisons_donnent_un_niveau_valide(active, ecoute, pare_feux):
    niveau, texte = cli.message_synchro(active, ecoute, pare_feux, DEFAULT_PORT)
    assert niveau in ("ok", "warn")
    assert texte


# ---- Port ------------------------------------------------------------------

@pytest.mark.parametrize("valeur,attendu", [
    (None, DEFAULT_PORT), (0, DEFAULT_PORT), (-3, DEFAULT_PORT),
    (70000, DEFAULT_PORT), ("abc", DEFAULT_PORT), (5000, 5000), ("6001", 6001)])
def test_port_synchro(valeur, attendu):
    reglages = SimpleNamespace(get=lambda cle, defaut=None: valeur)
    assert cli.port_synchro(reglages) == attendu


# ---- Pare-feu : sous-processus simulés ------------------------------------

def _faux_lancer(reponses: dict):
    """Simule subprocess.run : `reponses` associe l'outil à (code, sortie),
    ou à une exception à lever. Un outil absent lève FileNotFoundError."""
    appels = []

    def lancer(commande, **options):
        appels.append((commande, options))
        reponse = reponses.get(commande[0], FileNotFoundError(commande[0]))
        if isinstance(reponse, Exception):
            raise reponse
        code, sortie = reponse
        return subprocess.CompletedProcess(commande, code, stdout=sortie, stderr="")

    lancer.appels = appels
    return lancer


def test_firewalld_actif():
    lancer = _faux_lancer({"firewall-cmd": (0, "running\n")})
    assert cli.pare_feux_actifs(lancer) == ["firewalld"]


def test_firewalld_arrete():
    lancer = _faux_lancer({"firewall-cmd": (252, "not running\n")})
    assert cli.pare_feux_actifs(lancer) == []


def test_ufw_actif():
    lancer = _faux_lancer({"ufw": (0, "Status: active\n\nTo  Action  From\n")})
    assert cli.pare_feux_actifs(lancer) == ["ufw"]


def test_ufw_inactif():
    lancer = _faux_lancer({"ufw": (0, "Status: inactive\n")})
    assert cli.pare_feux_actifs(lancer) == []


def test_ufw_sans_droits_ne_leve_rien():
    lancer = _faux_lancer({"ufw": (1, "ERROR: You need to be root to run this script\n")})
    assert cli.pare_feux_actifs(lancer) == []


def test_outils_absents_ne_levent_rien():
    assert cli.pare_feux_actifs(_faux_lancer({})) == []


def test_delai_depasse_ne_leve_rien():
    delai = subprocess.TimeoutExpired("ufw", 2)
    lancer = _faux_lancer({"firewall-cmd": delai, "ufw": delai})
    assert cli.pare_feux_actifs(lancer) == []


def test_erreur_de_permission_ne_leve_rien():
    lancer = _faux_lancer({"firewall-cmd": PermissionError("refusé"),
                           "ufw": RuntimeError("inattendu")})
    assert cli.pare_feux_actifs(lancer) == []


def test_les_deux_actifs_et_delai_de_2_secondes():
    lancer = _faux_lancer({"firewall-cmd": (0, "running"),
                           "ufw": (0, "Status: active")})
    assert cli.pare_feux_actifs(lancer) == ["firewalld", "ufw"]
    assert len(lancer.appels) == 2
    for _commande, options in lancer.appels:
        assert options["timeout"] == 2
        assert options["env"]["LC_ALL"] == "C"


# ---- Sonde d'écoute locale -------------------------------------------------

@pytest.fixture
def faux_serveur():
    """Petit serveur HTTP local qui répond à /v1/hello ; `corps` est modifiable."""
    etat = {"status": 200, "corps": json.dumps({"app": "triat", "v": 1}).encode()}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(etat["status"])
            self.send_header("Content-Length", str(len(etat["corps"])))
            self.end_headers()
            self.wfile.write(etat["corps"])

        def log_message(self, *_args):
            return

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], etat
    httpd.shutdown()
    httpd.server_close()


def test_sonde_reconnait_un_serveur_triat(faux_serveur):
    port, _ = faux_serveur
    assert cli.serveur_synchro_ecoute(port) is True


def test_sonde_refuse_un_autre_programme(faux_serveur):
    port, etat = faux_serveur
    etat["corps"] = b"<html>autre chose</html>"
    assert cli.serveur_synchro_ecoute(port) is False
    etat["corps"] = json.dumps({"app": "autre"}).encode()
    assert cli.serveur_synchro_ecoute(port) is False
    etat["status"] = 404
    assert cli.serveur_synchro_ecoute(port) is False


def test_sonde_sur_port_ferme_renvoie_faux():
    # Réserve un port libre puis le libère : personne n'y écoute.
    import socket

    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    assert cli.serveur_synchro_ecoute(port, delai=0.5) is False


def test_sonde_avec_le_vrai_serveur(tmp_path):
    from triat.core.library import Library
    from triat.core.settings import Settings
    from triat.core.sync import SyncServer

    reglages = Settings(tmp_path / "settings.json")
    reglages.set("sync_port", 0)
    serveur = SyncServer(Library(tmp_path / "lib.db"), reglages)
    try:
        assert serveur.start(announce=False)
        assert cli.serveur_synchro_ecoute(serveur.port) is True
    finally:
        serveur.stop()
    assert cli.serveur_synchro_ecoute(serveur.port, delai=0.5) is False


# ---- Assemblage ------------------------------------------------------------

def test_ligne_synchro_desactivee_ne_sonde_rien(monkeypatch):
    def interdit(*_a, **_k):
        raise AssertionError("aucune sonde attendue quand la synchro est désactivée")

    monkeypatch.setattr(cli, "serveur_synchro_ecoute", interdit)
    monkeypatch.setattr(cli, "pare_feux_actifs", interdit)
    reglages = SimpleNamespace(get=lambda cle, defaut=None: False)
    nom, niveau, texte = cli._ligne_synchro(reglages)
    assert (nom, niveau) == ("synchro", "ok")
    assert "désactivée" in texte


def test_ligne_synchro_activee(monkeypatch):
    valeurs = {"sync_enabled": True, "sync_port": 5555}
    monkeypatch.setattr(cli, "serveur_synchro_ecoute", lambda port, delai=1.0: port == 5555)
    monkeypatch.setattr(cli, "pare_feux_actifs", lambda *a, **k: ["ufw"])
    monkeypatch.setattr(cli, "port_ecoute_reel", lambda: None)
    reglages = SimpleNamespace(get=lambda cle, defaut=None: valeurs.get(cle, defaut))
    nom, niveau, texte = cli._ligne_synchro(reglages)
    assert nom == "synchro"
    assert niveau == "warn"
    assert "écoute sur le port 5555" in texte
    assert "ufw allow 5555/tcp" in texte


def test_js_runtime_check_reports_missing_pieces(monkeypatch):
    import importlib.util
    import shutil
    from triat import cli
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    name, status, detail = cli.js_runtime_check()
    assert status == "warn" and "deno ou node" in detail and "yt-dlp-ejs" in detail
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/deno" if n == "deno" else None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert cli.js_runtime_check()[1] == "ok"


def test_ligne_synchro_lit_le_port_reel_du_fichier(monkeypatch, tmp_path):
    """Après un repli de port, doctor sonde le port réellement lié."""
    from triat.core import sync

    fichier = tmp_path / "sync.port"
    fichier.write_text("51234\n")
    monkeypatch.setattr(sync, "port_file_path", lambda: fichier)
    sondes = []
    monkeypatch.setattr(cli, "serveur_synchro_ecoute",
                        lambda port, delai=1.0: sondes.append(port) or True)
    monkeypatch.setattr(cli, "pare_feux_actifs", lambda *a, **k: [])
    reglages = SimpleNamespace(
        get=lambda cle, defaut=None: {"sync_enabled": True}.get(cle, defaut))
    _, niveau, texte = cli._ligne_synchro(reglages)
    assert sondes == [51234]
    assert "port 51234" in texte and niveau == "ok"
    fichier.write_text("n'importe quoi")           # illisible : le réglage
    sondes.clear()
    cli._ligne_synchro(reglages)
    assert sondes == [DEFAULT_PORT]

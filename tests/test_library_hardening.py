"""Robustesse de la bibliothèque : quarantaine, atomicité, schéma, époque."""

import logging
import sqlite3

import pytest

from triat.core.library import Library, LibraryTooNew
from triat.core.models import TrackRef


def _ref(vid="a", title="Titre"):
    return TrackRef(video_id=vid, title=title, artist="Art")


def _corrompues(tmp_path):
    return list(tmp_path.glob("library.db.corrompue-*"))


def _base_avec_piste(tmp_path):
    path = tmp_path / "library.db"
    lib = Library(path)
    lib.upsert_track(_ref())
    lib.close()
    return path


# ---- P1-1 : quarantaine -------------------------------------------------

def test_base_verrouillee_pas_de_quarantaine(tmp_path):
    path = _base_avec_piste(tmp_path)
    autre = sqlite3.connect(path, isolation_level=None)
    # Mode journal classique : en WAL, un verrou exclusif ne bloque pas l'ouverture.
    autre.execute("PRAGMA journal_mode=DELETE")
    autre.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            Library(path, busy_timeout=0.05)
    finally:
        autre.execute("ROLLBACK")
        autre.close()
    assert _corrompues(tmp_path) == []
    lib = Library(path)
    assert lib.get_track("a") is not None
    lib.close()


@pytest.mark.parametrize("code", ["SQLITE_FULL", "SQLITE_READONLY",
                                  "SQLITE_CANTOPEN", "SQLITE_IOERR"])
def test_autres_erreurs_levees_sans_toucher_au_fichier(tmp_path, monkeypatch, code):
    path = _base_avec_piste(tmp_path)

    def boom(self, timeout=None):
        exc = sqlite3.OperationalError("simulée")
        exc.sqlite_errorname = code
        raise exc

    monkeypatch.setattr(Library, "_open", boom)
    with pytest.raises(sqlite3.OperationalError):
        Library(path)
    assert _corrompues(tmp_path) == []
    assert path.exists()


def test_fichier_non_sqlite_mis_en_quarantaine(tmp_path):
    path = tmp_path / "library.db"
    path.write_bytes(b"ceci n'est pas une base SQLite " * 200)
    lib = Library(path)
    assert len(_corrompues(tmp_path)) == 1
    assert lib.get_track("a") is None
    assert len(lib.corrupt_copies()) == 1
    lib.close()


# ---- P2-3 : atomicité ---------------------------------------------------

def test_set_playlist_order_invalide_laisse_la_playlist_intacte(tmp_path):
    lib = Library(tmp_path / "library.db")
    pid = lib.create_playlist("P")
    lib.add_to_playlist(pid, [_ref("a"), _ref("b")])
    with pytest.raises(sqlite3.IntegrityError):
        lib.set_playlist_order(pid, ["b", "inconnu"])
    lib.upsert_track(_ref("c"))   # valide la transaction d'un tiers, si elle fuit
    assert [t.video_id for t in lib.playlist_items(pid)] == ["a", "b"]
    lib.close()


def test_record_play_atomique(tmp_path, monkeypatch):
    lib = Library(tmp_path / "library.db")
    lib.upsert_track(_ref())
    real = lib._db

    class Piege:
        def __init__(self):
            self.n = 0

        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *a):
            if sql.lstrip().startswith("UPDATE tracks SET play_count"):
                raise sqlite3.OperationalError("simulée")
            return real.execute(sql, *a)

    lib._db = Piege()
    with pytest.raises(sqlite3.OperationalError):
        lib.record_play(_ref(), 10, True)
    lib._db = real
    lib.upsert_track(_ref("z"))
    assert lib.history_entries() == []
    lib.close()


# ---- P3-14 : schéma trop récent -----------------------------------------

def test_base_trop_recente_refusee_sans_quarantaine(tmp_path):
    path = _base_avec_piste(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 999")
    conn.commit()
    conn.close()
    with pytest.raises(LibraryTooNew):
        Library(path)
    assert _corrompues(tmp_path) == []
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 999
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    conn.close()


# ---- Époque -------------------------------------------------------------

def test_epoch_incremente_par_wipe_et_clear_history(tmp_path):
    lib = Library(tmp_path / "library.db")
    assert lib.epoch == 0
    lib.wipe()
    assert lib.epoch == 1
    lib.clear_history()
    assert lib.epoch == 2
    lib.close()


# ---- P2-6 : écriture après fermeture ------------------------------------

def test_ecriture_apres_fermeture_journalisee(tmp_path, caplog):
    lib = Library(tmp_path / "library.db")
    lib.close()
    with caplog.at_level(logging.WARNING, logger="triat.core.library"):
        lib.upsert_track(_ref())
    assert any("fermée" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)


def test_lecture_apres_fermeture_silencieuse(tmp_path, caplog):
    lib = Library(tmp_path / "library.db")
    lib.close()
    with caplog.at_level(logging.WARNING, logger="triat.core.library"):
        assert lib.get_track("a") is None
    assert not caplog.records


# ---- Copies corrompues --------------------------------------------------

def test_corrupt_copies_et_suppression(tmp_path):
    lib = Library(tmp_path / "library.db")
    assert lib.corrupt_copies() == []
    for nom in ("library.db.corrompue-20240101-000000",
                "library.db.corrompue-20240101-000000-wal"):
        (tmp_path / nom).write_bytes(b"x")
    (tmp_path / "autre.txt").write_text("garder")
    assert len(lib.corrupt_copies()) == 2
    assert lib.delete_corrupt_copies() == 2
    assert lib.corrupt_copies() == []
    assert (tmp_path / "autre.txt").exists()
    assert lib.delete_corrupt_copies() == 0
    lib.close()

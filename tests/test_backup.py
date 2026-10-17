import sqlite3
import stat
from datetime import datetime, timedelta

import pytest

from triat.cli import build_parser
from triat.core import backup, library

T0 = datetime(2026, 10, 17, 12, 0, 0)


def make_db(path, version=library.SCHEMA_VERSION, rows=("a",)):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    conn.commit()
    return conn


def test_backup_open_wal_has_data_and_mode(tmp_path):
    db = tmp_path / "library.db"
    conn = make_db(db, rows=("x", "y"))
    out = backup.create_backup(db, tmp_path / "b", now=T0)
    conn.close()
    assert out.name == "library-20261017-120000.db"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    got = sqlite3.connect(out).execute("SELECT v FROM t ORDER BY v").fetchall()
    assert got == [("x",), ("y",)]
    assert not list((tmp_path / "b").glob("*.tmp"))


def test_rotate_keeps_min_and_recent(tmp_path):
    db = tmp_path / "library.db"
    make_db(db).close()
    d = tmp_path / "b"
    for days in (30, 20, 10, 8, 2, 0):
        backup.create_backup(db, d, now=T0 - timedelta(days=days))
    backup.rotate(d, now=T0)
    assert len(backup.list_backups(d)) == 3  # 0, 2 jours + 8 jours gardé par keep_min
    d2 = tmp_path / "c"
    for days in (30, 20, 10):
        backup.create_backup(db, d2, now=T0 - timedelta(days=days))
    backup.rotate(d2, now=T0)
    assert len(backup.list_backups(d2)) == 3
    assert backup.list_backups(d)[0].name.startswith("library-20261017")


def test_restore_sets_aside(tmp_path):
    db = tmp_path / "library.db"
    make_db(db, rows=("old",)).close()
    out = backup.create_backup(db, tmp_path / "b", now=T0)
    sqlite3.connect(db).execute("DELETE FROM t").connection.commit()
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"library.db{suffix}").unlink(missing_ok=True)
    aside = backup.restore_backup(out, db, now=T0)
    assert aside.name == "library.db.avant-restauration-20261017-120000"
    assert aside.exists()
    assert sqlite3.connect(db).execute("SELECT v FROM t").fetchall() == [("old",)]


def test_restore_refuses_newer_schema(tmp_path):
    src = tmp_path / "new.db"
    make_db(src, version=library.SCHEMA_VERSION + 1).close()
    with pytest.raises(backup.BackupError):
        backup.restore_backup(src, tmp_path / "library.db")


def test_restore_refuses_symlink_and_garbage(tmp_path):
    real = tmp_path / "real.db"
    make_db(real).close()
    link = tmp_path / "link.db"
    link.symlink_to(real)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(link, tmp_path / "library.db")
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not sqlite" * 50)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(junk, tmp_path / "library.db")


def test_restore_refuses_open_db(tmp_path):
    src = tmp_path / "s.db"
    make_db(src).close()
    db = tmp_path / "library.db"
    conn = make_db(db)  # reste ouverte : WAL non vide
    (tmp_path / "library.db-wal").write_bytes(b"x")
    with pytest.raises(backup.BackupError):
        backup.restore_backup(src, db)
    conn.close()


def test_auto_backup_once_per_day(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from triat.core import paths
    make_db(paths.db_path()).close()
    assert backup.maybe_auto_backup(T0) is not None
    assert backup.maybe_auto_backup(T0 + timedelta(hours=23)) is None
    assert backup.maybe_auto_backup(T0 + timedelta(hours=25)) is not None
    assert len(backup.list_backups()) == 2


def test_cli_parser():
    p = build_parser()
    assert p.parse_args(["backup"]).action == "create"
    a = p.parse_args(["backup", "restore", "f.db", "--yes"])
    assert (a.action, a.file, a.yes) == ("restore", "f.db", True)
    assert p.parse_args(["backup", "list"]).action == "list"


def test_backup_leaves_a_single_self_contained_file(tmp_path):
    import sqlite3 as _sqlite3

    from triat.core import backup as _backup

    src = tmp_path / "library.db"
    con = _sqlite3.connect(src)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    dest = tmp_path / "backups"
    out = _backup.create_backup(library_path=src, dest_dir=dest)
    con.close()
    assert sorted(p.name for p in dest.iterdir()) == [out.name]
    check = _sqlite3.connect(out)
    try:
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert check.execute("SELECT x FROM t").fetchall() == [(1,)]
    finally:
        check.close()

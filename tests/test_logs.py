"""Tests de la journalisation : vidage du journal et de ses rotations."""

from __future__ import annotations

import logging
import logging.handlers

from triat.core import logs


def test_truncate_empties_log_and_rotations(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "state_dir", lambda: tmp_path)
    path = tmp_path / "triat.log"
    handler = logging.handlers.RotatingFileHandler(path, encoding="utf-8")
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        log = logging.getLogger("test_logs")
        log.warning("avant")
        (tmp_path / "triat.log.1").write_text("vieux")
        (tmp_path / "triat.log.2").write_text("plus vieux")
        (tmp_path / "autre.txt").write_text("reste")
        logs.truncate()
        assert path.read_text() == ""
        assert not (tmp_path / "triat.log.1").exists()
        assert not (tmp_path / "triat.log.2").exists()
        assert (tmp_path / "autre.txt").exists()
        log.warning("après")                 # le handler écrit toujours
        handler.flush()
        assert "après" in path.read_text()
    finally:
        root.removeHandler(handler)
        handler.close()


def test_truncate_without_log_file_is_harmless(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "state_dir", lambda: tmp_path)
    logs.truncate()

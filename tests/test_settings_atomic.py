"""Écriture atomique de settings.json et repli du dossier runtime."""

import os
import stat
from pathlib import Path

from triat.core import paths
from triat.core.settings import Settings


def test_settings_file_is_0600_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)
    settings = Settings(target)
    settings.set("volume", 40.0)
    settings.save()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not (tmp_path / "settings.json.tmp").exists()
    assert Settings(target).get("volume") == 40.0


def test_settings_tmp_is_created_0600(tmp_path, monkeypatch):
    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, **kw):
        if str(path).endswith(".tmp"):
            modes.append(mode)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", spy)
    Settings(tmp_path / "settings.json").save()
    assert modes == [0o600]


def test_runtime_dir_without_xdg_uses_private_tmp_not_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(paths, "_fallback_runtime", None)
    registered = []
    monkeypatch.setattr(paths.atexit, "register",
                        lambda fn, *a, **k: registered.append((fn, a)))
    path = paths.runtime_dir()
    try:
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert not str(path).startswith(str(tmp_path / "cache"))
        assert not (tmp_path / "cache").exists()
        assert paths.runtime_dir() == path
        assert registered and registered[0][1][0] == path
    finally:
        import shutil
        shutil.rmtree(path, ignore_errors=True)


def test_runtime_dir_with_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = paths.runtime_dir()
    assert path == Path(tmp_path) / paths.APP_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o700

"""Paquetage : script d'installation, manifeste Flatpak, métadonnées, CSS."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "packaging/nothing-os/install.sh"
FLATPAK = ROOT / "packaging/flatpak"
CSS = ROOT / "data/style.css"


def _run_install(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "PREFIX": str(home / ".local"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    return subprocess.run(["bash", str(INSTALL), *args], env=env,
                          capture_output=True, text=True, timeout=60)


# ---- install.sh : les données de l'utilisateur ne sont jamais effacées ----------

def test_uninstall_keeps_the_library(tmp_path):
    share = tmp_path / "home/.local/share/triat"
    share.mkdir(parents=True)
    (share / "library.db").write_text("ma bibliothèque")
    (share / "settings.json").write_text("{}")
    app = tmp_path / "home/.local/lib/triat"
    (app / "triat").mkdir(parents=True)
    (app / "venv").mkdir()
    launcher = tmp_path / "home/.local/bin/triat"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")

    result = _run_install(tmp_path, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert (share / "library.db").read_text() == "ma bibliothèque"
    assert (share / "settings.json").exists()
    assert not app.exists()
    assert not launcher.exists()


def test_uninstall_removes_only_legacy_code_from_the_data_dir(tmp_path):
    """Ancienne installation : le code vivait à côté des données."""
    share = tmp_path / "home/.local/share/triat"
    for sub in ("triat", "data", "venv"):
        (share / sub).mkdir(parents=True)
        (share / sub / "x").write_text("x")
    (share / "library.db").write_text("ma bibliothèque")
    (share / "cookies.enc").write_text("secret")

    result = _run_install(tmp_path, "--uninstall")
    assert result.returncode == 0, result.stderr
    for sub in ("triat", "data", "venv"):
        assert not (share / sub).exists()
    assert (share / "library.db").exists()
    assert (share / "cookies.enc").exists()


def test_install_script_never_removes_the_share_dir():
    script = INSTALL.read_text()
    assert not re.search(r'rm -rf[^\n]*"\$PREFIX/share/triat"', script)
    assert 'APP_DIR="$PREFIX/lib/triat"' in script
    assert 'PYTHONPATH="$APP_DIR:' in script


# ---- Flatpak -----------------------------------------------------------------

def _manifest() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((FLATPAK / "org.triat.Triat.yml").read_text())


def test_flatpak_command_matches_the_installed_launcher():
    manifest = _manifest()
    assert manifest["command"] == "triat"
    module = next(m for m in manifest["modules"]
                  if isinstance(m, dict) and m["name"] == "triat")
    commands = "\n".join(module["build-commands"])
    assert "/app/bin/triat" in commands
    assert "cp -r triat data /app/share/triat/" in commands
    assert "rien" not in commands


def test_flatpak_sources_are_explicit():
    manifest = _manifest()
    module = next(m for m in manifest["modules"]
                  if isinstance(m, dict) and m["name"] == "triat")
    paths = [source["path"] for source in module["sources"]]
    assert "../.." not in paths, "copier le dépôt entier embarque .venv et les APK"
    assert "../../triat" in paths and "../../data" in paths
    for source in module["sources"]:
        base = FLATPAK / source["path"]
        assert base.exists(), source


def test_flatpak_codecs_and_music_folder():
    manifest = _manifest()
    # ffmpeg-full n'existe plus après 24.08 : codecs-extra vient du runtime.
    assert "org.freedesktop.Platform.ffmpeg-full" not in manifest.get(
        "add-extensions", {})
    assert any(arg.startswith("--filesystem=xdg-music")
               for arg in manifest["finish-args"])
    assert not any(arg.startswith("--filesystem=home")
                   for arg in manifest["finish-args"])


def _git_sources(modules):
    for module in modules:
        if isinstance(module, dict):
            for source in module.get("sources", []):
                if source.get("type") == "git":
                    yield source
            yield from _git_sources(module.get("modules", []))


def test_flatpak_git_sources_are_pinned_to_a_commit():
    sources = list(_git_sources(_manifest()["modules"]))
    assert sources
    for source in sources:
        assert re.fullmatch(r"[0-9a-f]{40}", source.get("commit", "")), source


def test_flatpak_python_requirements_are_generated_and_hashed():
    import json
    manifest = _manifest()
    assert "python3-requirements.json" in manifest["modules"]
    data = json.loads((FLATPAK / "python3-requirements.json").read_text())
    for module in data["modules"]:
        for source in module["sources"]:
            assert source["url"].startswith("https://files.pythonhosted.org/")
            assert re.fullmatch(r"[0-9a-f]{64}", source["sha256"])


def test_flatpak_requirements_generator_exists():
    script = FLATPAK / "gen-requirements.sh"
    assert script.exists() and os.access(script, os.X_OK)
    assert "flatpak-pip-generator" in script.read_text()
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


def test_desktop_entry_declares_no_ignored_scheme():
    entry = (FLATPAK / "org.triat.Triat.desktop").read_text()
    assert "x-scheme-handler" not in entry
    assert "%U" not in entry and "%u" not in entry


def test_metainfo_license_matches_the_license_file():
    metainfo = (FLATPAK / "org.triat.Triat.metainfo.xml").read_text()
    license_file = ROOT / "triat-public/LICENSE"
    if license_file.exists() and license_file.read_text().startswith("MIT"):
        assert "<project_license>MIT</project_license>" in metainfo


# ---- CSS --------------------------------------------------------------------

def test_orphan_stylesheet_is_gone():
    assert not (ROOT / "triat/ui/style.css").exists()


def test_sponsorblock_segments_use_the_live_colour():
    css = CSS.read_text()
    assert re.search(r"\.triat-progress\s*\{\s*color:\s*var\(--triat-live\)", css)


def test_glyph_layer_is_opaque_black():
    css = CSS.read_text()
    assert re.search(r"\.triat-glyph-layer\s*\{[^}]*background-color", css)


def test_text_never_uses_the_faint_token():
    """--triat-faint (≈ 2,2:1 sur noir) reste réservé aux décors."""
    css = CSS.read_text()
    for selector in (".triat-side-heading", ".triat-side-playlist .triat-mono"):
        block = re.search(re.escape(selector) + r"[^{]*\{([^}]*)\}", css)
        assert block and "var(--triat-faint)" not in block.group(1), selector
    lyric = re.search(r"\.triat-lyric\s*\{([^}]*)\}", css).group(1)
    assert "#4a4a4a" not in lyric


@pytest.mark.parametrize("name", [
    "triat-disc", "triat-transport", "triat-live-dot", "triat-status-pill",
    "triat-page-header", "triat-secondary", "triat-primary", "triat-icon",
    "triat-dot", "triat-immersion-field", "triat-dialog"])
def test_dead_css_classes_stay_removed(name):
    css = CSS.read_text()
    assert not re.search(r"\." + re.escape(name) + r"(?![\w-])", css)


def test_no_duplicate_selector_in_chip_rule():
    css = CSS.read_text()
    assert "button.triat-chip, button.triat-chip" not in css

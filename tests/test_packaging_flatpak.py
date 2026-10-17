"""Manifeste Flatpak : contrôles statiques (la construction réelle se fait en CI)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FLATPAK = ROOT / "packaging/flatpak"
MANIFEST = FLATPAK / "org.triat.Triat.yml"
APP_ID = "org.triat.Triat"


def _text() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def _manifest() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(_text())


def _module(data: dict, name: str) -> dict:
    for m in data["modules"]:
        if isinstance(m, dict) and m.get("name") == name:
            return m
    raise AssertionError(f"module {name!r} absent")


def test_manifest_is_parseable_and_app_id_matches_files():
    data = _manifest()
    assert data["app-id"] == APP_ID
    assert (FLATPAK / f"{APP_ID}.desktop").is_file()
    assert (FLATPAK / f"{APP_ID}.metainfo.xml").is_file()
    root = ET.parse(FLATPAK / f"{APP_ID}.metainfo.xml").getroot()
    assert root.findtext("id") == APP_ID
    assert root.find("launchable").text == f"{APP_ID}.desktop"
    desktop = (FLATPAK / f"{APP_ID}.desktop").read_text(encoding="utf-8")
    assert re.search(rf"^Icon={re.escape(APP_ID)}$", desktop, re.M)


def test_manifest_app_id_regex_fallback():
    # Sans PyYAML, on vérifie au moins l'identifiant et le module deno.
    assert re.search(rf"^app-id: {re.escape(APP_ID)}$", _text(), re.M)


def test_deno_module_has_pinned_checksums_for_both_arches():
    data = _manifest()
    sources = _module(data, "deno")["sources"]
    by_arch = {}
    for src in sources:
        assert src["type"] == "archive"
        assert re.fullmatch(r"[0-9a-f]{64}", src["sha256"]), src
        assert src["url"].startswith("https://github.com/denoland/deno/releases/download/v")
        (arch,) = src["only-arches"]
        assert arch in src["url"]
        by_arch[arch] = src
    assert set(by_arch) == {"x86_64", "aarch64"}


def test_ffmpeg_is_provided_and_checked_at_build_time():
    data = _manifest()
    cmds = " ".join(_module(data, "triat")["build-commands"])
    assert "/usr/bin/ffmpeg" in cmds
    text = _text()
    # L'extension ffmpeg-full n'est publiée que jusqu'à 24.08 : elle ne doit pas
    # être déclarée pour un runtime plus récent.
    assert int(data["runtime-version"]) >= 50
    assert not re.search(r"^\s*-\s*org\.freedesktop\.Platform\.ffmpeg-full", text, re.M)
    assert "ffmpeg-full" in text  # la raison de son absence est documentée


def test_rust_extension_declared_while_sdists_remain():
    data = _manifest()
    assert "org.freedesktop.Sdk.Extension.rust-stable" in data["sdk-extensions"]


def test_matrix_without_cava_is_documented():
    assert "cava" in _text()


def test_no_dangerous_permissions():
    data = _manifest()
    args = " ".join(data["finish-args"])
    for bad in ("--filesystem=home", "--filesystem=host", "--device=all",
                "--socket=session-bus", "org.freedesktop.Flatpak"):
        assert bad not in args


def test_ci_builds_flatpak_and_validates_metadata():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--require-hashes" in ci
    assert "appstreamcli validate" in ci
    assert "desktop-file-validate" in ci
    assert "flatpak-builder" in ci
    assert ci.count("persist-credentials: false") >= ci.count("actions/checkout@")
    # Les actions tierces sont épinglées par SHA complet.
    for ref in re.findall(r"uses:\s*(\S+)@(\S+)", ci):
        assert re.fullmatch(r"[0-9a-f]{40}", ref[1]), ref

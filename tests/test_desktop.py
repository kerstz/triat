"""Tests de l'intégration au bureau Nothing OS."""

from __future__ import annotations

import json

import pytest

from triat.core import desktop


@pytest.mark.parametrize("value,expected", [
    ("#d71921", "#d71921"),
    ("#D71921", "#d71921"),
    ("#FFF", "#ffffff"),
    ("#abc", "#aabbcc"),
    ("  #00d68f  ", "#00d68f"),
])
def test_valid_colours_are_normalised(value, expected):
    assert desktop.normalize_hex(value) == expected


@pytest.mark.parametrize("value", [
    "rouge", "#12345", "#gggggg", "d71921", "", None, 42, "#",
])
def test_invalid_colours_are_refused(value):
    assert desktop.normalize_hex(value) is None


def test_accent_is_read_from_the_desktop_config(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"accent": "#00d68f", "barCentre": []}),
                      encoding="utf-8")
    monkeypatch.setattr(desktop, "NOTHING_CONFIG", config)
    assert desktop.desktop_accent() == "#00d68f"


def test_alternative_accent_keys_are_accepted(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"accentColor": "#ff6b00"}), encoding="utf-8")
    monkeypatch.setattr(desktop, "NOTHING_CONFIG", config)
    assert desktop.desktop_accent() == "#ff6b00"


def test_a_broken_config_does_not_raise(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("{ pas du json", encoding="utf-8")
    monkeypatch.setattr(desktop, "NOTHING_CONFIG", config)
    assert desktop.read_desktop_config() == {}
    assert desktop.desktop_accent() is None


def test_a_missing_config_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "NOTHING_CONFIG", tmp_path / "absent.json")
    assert desktop.desktop_accent() is None
    assert desktop.read_desktop_config() == {}


def test_an_accent_that_is_not_a_colour_is_ignored(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"accent": "javascript:alert(1)"}),
                      encoding="utf-8")
    monkeypatch.setattr(desktop, "NOTHING_CONFIG", config)
    assert desktop.desktop_accent() is None, "aucune valeur non validée dans le CSS"


def test_generated_css_only_touches_the_accent():
    css = desktop.accent_css("#00d68f")
    assert "--triat-accent: #00d68f;" in css
    assert "--accent-bg-color: #00d68f;" in css
    assert css.count("{") == 1, "une seule règle, pour ne rien écraser d'autre"


def test_describe_reports_every_field():
    info = desktop.describe()
    assert set(info) == {"dotfiles", "config", "accent", "quickshell",
                         "hypr_custom", "display_font"}


def test_focus_async_is_a_noop_outside_hyprland(monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    assert desktop.focus_under_hyprland_async("org.triat.Triat") is None


def test_focus_async_runs_off_the_calling_thread(monkeypatch):
    import threading

    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "x")
    seen = {}

    def fake(app_id):
        seen["thread"] = threading.current_thread()
        return True

    monkeypatch.setattr(desktop, "focus_under_hyprland", fake)
    results = []
    thread = desktop.focus_under_hyprland_async("id", results.append)
    thread.join(2)
    assert results == [True]
    assert seen["thread"] is not threading.current_thread()

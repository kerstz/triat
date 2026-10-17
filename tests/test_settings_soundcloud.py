"""Réglages SoundCloud : défauts, validation, source effective, suppression."""

import json

from triat.core.settings import CHOICES, DEFAULTS, Settings


def test_soundcloud_defaults_are_opt_in():
    assert DEFAULTS["soundcloud_enabled"] is False
    assert DEFAULTS["soundcloud_tos_ack"] is False
    assert DEFAULTS["content_source"] == "youtube"
    assert DEFAULTS["soundcloud_quality"] == "auto"
    assert DEFAULTS["source_badge"] is True
    assert DEFAULTS["soundcloud_hide_unplayable"] is False


def test_soundcloud_choices_are_closed():
    assert CHOICES["content_source"] == ("youtube", "soundcloud", "both")
    assert CHOICES["soundcloud_quality"] == ("auto", "high", "low")


def test_invalid_soundcloud_values_fall_back_to_default(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({
        "content_source": "napster", "soundcloud_quality": "ultra",
        "soundcloud_enabled": "oui", "source_badge": 1,
        "soundcloud_hide_unplayable": None, "soundcloud_tos_ack": 0}))
    s = Settings(path)
    assert s.get("content_source") == "youtube"
    assert s.get("soundcloud_quality") == "auto"
    assert s.get("soundcloud_enabled") is False
    assert s.get("source_badge") is True
    assert s.get("soundcloud_hide_unplayable") is False
    assert s.get("soundcloud_tos_ack") is False


def test_valid_soundcloud_values_are_kept(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"content_source": "both",
                                "soundcloud_quality": "low",
                                "soundcloud_enabled": True}))
    s = Settings(path)
    assert s.get("content_source") == "both"
    assert s.get("soundcloud_quality") == "low"
    assert s.get("soundcloud_enabled") is True


def test_effective_source_is_youtube_while_soundcloud_is_disabled(tmp_path):
    s = Settings(tmp_path / "s.json")
    s.set("content_source", "both")
    s.set("soundcloud_enabled", False)
    assert s.effective_content_source() == "youtube"


def test_effective_source_follows_stored_value_when_enabled(tmp_path):
    s = Settings(tmp_path / "s.json")
    s.update(soundcloud_enabled=True, content_source="soundcloud")
    assert s.effective_content_source() == "soundcloud"
    s.set("content_source", "both")
    assert s.effective_content_source() == "both"


def test_delete_removes_key_and_ignores_missing(tmp_path):
    s = Settings(tmp_path / "s.json")
    s.set("scratch_key", 1)
    s.delete("scratch_key")
    assert "scratch_key" not in s.as_dict()
    s.delete("scratch_key")          # absente : pas d'erreur
    s.delete("jamais_existe")

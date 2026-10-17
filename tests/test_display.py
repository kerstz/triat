"""Nettoyage des métadonnées à l'affichage (fonctions pures)."""

import pytest

from triat.core.display import (
    canonical_key, display_artist, display_title)
from triat.core.models import TrackRef


@pytest.mark.parametrize("title,artist,expected", [
    ("Vialice - Two Drugs", "Vialice", "Two Drugs"),
    ("vialice – Two Drugs", "Vialice", "Two Drugs"),
    ("Beyoncé - Halo", "BEYONCE", "Halo"),
    ("Halo (Official Video)", "Beyoncé", "Halo"),
    ("Halo (Official Music Video)", "Beyoncé", "Halo"),
    ("Halo (Official Audio)", "Beyoncé", "Halo"),
    ("Halo [Official Video]", "Beyoncé", "Halo"),
    ("Halo (Lyrics)", "Beyoncé", "Halo"),
    ("Halo (Lyric Video)", "Beyoncé", "Halo"),
    ("Halo (Clip officiel)", "Beyoncé", "Halo"),
    ("Halo (Audio)", "Beyoncé", "Halo"),
    ("Halo (Visualizer)", "Beyoncé", "Halo"),
    ("Halo HD", "Beyoncé", "Halo"),
    ("Halo 4K", "Beyoncé", "Halo"),
    ("Halo // LETRAS4K", "Beyoncé", "Halo"),
    ("Halo (Official Video) // Letras", "Beyoncé", "Halo"),
    ("Halo | Columbia Records", "Beyoncé", "Halo"),
    ("Halo | Beyoncé", "Beyoncé", "Halo"),
    ("Halo   (Official  Video)    HD", "Beyoncé", "Halo"),
    ("Beyoncé - Halo (Official Video) HD", "Beyoncé", "Halo"),
    ("Code Red #underground #newmusic", "X", "Code Red"),
    ("Halo - Official Video", "Beyoncé", "Halo"),
])
def test_title_cleaned(title, artist, expected):
    assert display_title(title, artist) == expected


@pytest.mark.parametrize("title,artist", [
    ("Halo (Live)", "Beyoncé"),
    ("Halo (Remix)", "Beyoncé"),
    ("Halo (feat. Jay-Z)", "Beyoncé"),
    ("Halo (Sia Remix)", "Beyoncé"),
    ("Halo (Acoustic)", "Beyoncé"),
    ("Halo (Live at Wembley)", "Beyoncé"),
    ("Hello | Goodbye", "Adele"),          # « | » sans nom de chaîne
    ("HDMI Blues", "Someone"),
    ("Official Secrets", "Someone"),
    ("Sans titre", ""),
    ("", "Someone"),
    ("Halo - Live", "Beyoncé"),            # version, pas un artiste
])
def test_title_preserved(title, artist):
    assert display_title(title, artist) == title


def test_title_never_empty():
    assert display_title("(Official Video)", "X") == "(Official Video)"
    assert display_title("Vialice - ", "Vialice") == "Vialice -"


@pytest.mark.parametrize("artist,title,expected", [
    ("Daft Punk - Topic", "Get Lucky", "Daft Punk"),
    ("Daft Punk – Topic", "Get Lucky", "Daft Punk"),
    ("Daft Punk VEVO", "Get Lucky", "Daft Punk"),
    ("DaftPunkVEVO", "Get Lucky", "DaftPunkVEVO"),   # re-espacement non sûr
    ("Phoenix Official", "1901", "Phoenix"),
    ("Official", "1901", "Official"),
    ("Beyoncé", "Halo", "Beyoncé"),
    ("", "Halo", ""),
])
def test_artist_cleaned(artist, title, expected):
    assert display_artist(artist, title) == expected


def test_artist_deduced_from_title():
    # Chaîne au nom sans rapport : « X - Y » → X
    assert display_artist("welovephoenix", "Phoenix - Lisztomania") == "Phoenix"
    assert display_title("Phoenix - Lisztomania", "welovephoenix") == "Lisztomania"


def test_artist_heuristic_is_prudent():
    # L'artiste apparaît dans le titre : pas de déduction.
    assert display_artist("Phoenix", "Phoenix - Lisztomania") == "Phoenix"
    # Deux séparateurs : ambigu.
    assert display_artist("chan", "A - B - C") == "chan"
    # Aucun séparateur.
    assert display_artist("chan", "Seul") == "chan"
    # Version et non artiste.
    assert display_artist("Beyoncé", "Halo - Live") == "Beyoncé"
    # Topic : fiable, pas de déduction.
    assert display_artist("Phoenix - Topic", "Lisztomania - Remastered") == "Phoenix"
    # Gauche trop long pour un nom d'artiste.
    long_left = "un titre vraiment très long qui ne ressemble pas à un nom"
    assert display_artist("chan", f"{long_left} - suite") == "chan"


def test_artist_from_title_when_empty():
    assert display_artist("", "Phoenix - Lisztomania") == "Phoenix"
    assert display_artist("", "Seul") == ""


def test_canonical_key_merges_variants():
    a = canonical_key("Vialice - Two Drugs (Official Video)", "Vialice")
    b = canonical_key("Two Drugs", "VIALICE - Topic")
    assert a == b
    assert canonical_key("Beyoncé - Halo", "Beyoncé") == \
        canonical_key("halo", "Beyonce")


def test_canonical_key_keeps_versions_apart():
    assert canonical_key("Halo (Live)", "Beyoncé") != \
        canonical_key("Halo", "Beyoncé")
    assert canonical_key("Halo (Remix)", "Beyoncé") != \
        canonical_key("Halo", "Beyoncé")


def test_models_properties_delegate_and_do_not_mutate():
    ref = TrackRef("abc", title="Vialice - Two Drugs (Official Video)",
                   artist="Vialice - Topic")
    assert ref.display_title == "Two Drugs"
    assert ref.display_artist == "Vialice"
    assert ref.title == "Vialice - Two Drugs (Official Video)"
    assert ref.artist == "Vialice - Topic"

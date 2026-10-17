"""Tests de l'import de playlists : analyse, nettoyage, résolution."""

from __future__ import annotations

import io
import json

import pytest

from triat.core.importer import (
    ImportError_,
    clean_title,
    default_playlist_name,
    extract_video_id,
    fetch_metadata,
    import_playlist,
    parse_playlist_file,
    resolve_all,
    split_artist_title,
)
from triat.core.library import Library


# ---- Extraction d'identifiants -------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("https://www.youtube.com/watch?v=VMhTvm0byPE", "VMhTvm0byPE"),
    ("https://youtube.com/watch?v=VMhTvm0byPE&list=RD123", "VMhTvm0byPE"),
    ("https://youtu.be/kJQP7kiw5Fk", "kJQP7kiw5Fk"),
    ("https://youtu.be/kJQP7kiw5Fk?t=42", "kJQP7kiw5Fk"),
    ("https://www.youtube.com/shorts/n57MSToy7tY", "n57MSToy7tY"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("  https://www.youtube.com/watch?v=VMhTvm0byPE  ", "VMhTvm0byPE"),
])
def test_identifiers_are_extracted(line, expected):
    assert extract_video_id(line) == expected


@pytest.mark.parametrize("line", [
    "", "   ", "# un commentaire", "#EXTM3U", "pas une url",
    "https://exemple.test/video", "trop-court",
])
def test_non_videos_are_ignored(line):
    assert extract_video_id(line) is None


# ---- Nettoyage des titres -------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Titre (Official Video)", "Titre"),
    ("Titre [Official Music Video]", "Titre"),
    ("Titre (Clip Officiel)", "Titre"),
    ("Titre (Lyrics)", "Titre"),
    ("Titre (HD) (Remastered 2011)", "Titre"),
    ("Titre normal", "Titre normal"),
])
def test_noise_is_stripped_from_titles(raw, expected):
    assert clean_title(raw) == expected


def test_a_title_made_only_of_noise_is_kept():
    """Mieux vaut un titre bruité qu'un titre vide."""
    assert clean_title("(Official Video)") == "(Official Video)"


def test_artist_and_title_are_split():
    assert split_artist_title("Daft Punk - Get Lucky", "DaftPunkVEVO") == \
        ("Daft Punk", "Get Lucky")


def test_topic_channels_are_cleaned():
    assert split_artist_title("Get Lucky", "Daft Punk - Topic") == \
        ("Daft Punk", "Get Lucky")


def test_reversed_order_is_detected_via_the_channel():
    """« Titre - Artiste » quand le côté droit est le nom de la chaîne."""
    assert split_artist_title("Bubba - Pink Boy", "Pink Boy") == \
        ("Pink Boy", "Bubba")


def test_title_without_separator_falls_back_to_channel():
    assert split_artist_title("Un morceau", "Ma Chaîne") == ("Ma Chaîne", "Un morceau")


# ---- Lecture de fichier ---------------------------------------------------

def test_file_is_parsed_without_duplicates(tmp_path):
    path = tmp_path / "liste.txt"
    path.write_text("\n".join([
        "# ma liste",
        "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "",
        "https://www.youtube.com/watch?v=bbbbbbbbbbb",
        "https://youtu.be/aaaaaaaaaaa",          # doublon
    ]), encoding="utf-8")
    assert parse_playlist_file(path) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_m3u_extinf_lines_are_skipped(tmp_path):
    path = tmp_path / "liste.m3u"
    path.write_text("\n".join([
        "#EXTM3U",
        "#EXTINF:213,Artiste - Titre",
        "https://www.youtube.com/watch?v=ccccccccccc",
    ]), encoding="utf-8")
    assert parse_playlist_file(path) == ["ccccccccccc"]


def test_a_file_without_videos_is_refused(tmp_path):
    path = tmp_path / "vide.txt"
    path.write_text("juste du texte\nsans lien\n", encoding="utf-8")
    with pytest.raises(ImportError_, match="Aucune vidéo"):
        parse_playlist_file(path)


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(ImportError_, match="Lecture impossible"):
        parse_playlist_file(tmp_path / "absent.txt")


def test_playlist_name_is_derived_from_the_filename():
    assert default_playlist_name("youtube-mix-122-tracks.txt") == "Youtube Mix"
    assert default_playlist_name("/tmp/Ma_Super_Liste.m3u") == "Ma Super Liste"


# ---- Résolution des métadonnées ------------------------------------------

class _FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *_): return False


@pytest.fixture
def fake_oembed(monkeypatch):
    def fake_urlopen(request, **_kwargs):
        return _FakeResponse(json.dumps({
            "title": "Artiste Test - Chanson (Official Video)",
            "author_name": "ArtisteTestVEVO",
            "thumbnail_url": "https://i.ytimg.com/vi/x/hq.jpg",
        }).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def test_metadata_are_cleaned_on_the_way_in(fake_oembed):
    ref = fetch_metadata("aaaaaaaaaaa")
    assert ref.video_id == "aaaaaaaaaaa"
    assert ref.artist == "Artiste Test"
    assert ref.title == "Chanson"
    assert ref.thumbnail.startswith("https://i.ytimg.com/")


def test_a_failed_lookup_still_yields_a_playable_track(monkeypatch):
    def explode(*_a, **_k):
        raise OSError("réseau coupé")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    ref = fetch_metadata("bbbbbbbbbbb")
    assert ref.video_id == "bbbbbbbbbbb", "la piste reste jouable"
    assert ref.title == "bbbbbbbbbbb"


def test_order_is_preserved_despite_parallelism(fake_oembed):
    ids = [f"{i:011d}" for i in range(12)]
    refs = resolve_all(ids, workers=4)
    assert [r.video_id for r in refs] == ids


def test_progress_is_reported(fake_oembed):
    seen = []
    resolve_all(["a" * 11, "b" * 11, "c" * 11],
                progress=lambda done, total: seen.append((done, total)), workers=2)
    assert seen[-1] == (3, 3)


# ---- Import complet -------------------------------------------------------

def test_import_creates_a_playlist(tmp_path, fake_oembed):
    source = tmp_path / "mix.txt"
    source.write_text("\n".join(
        f"https://www.youtube.com/watch?v={c * 11}" for c in "abc"), encoding="utf-8")
    library = Library(tmp_path / "l.db")

    playlist_id, name, refs = import_playlist(source, library)

    assert name == "Mix"
    assert len(refs) == 3
    assert [t.video_id for t in library.playlist_items(playlist_id)] == \
        ["a" * 11, "b" * 11, "c" * 11]
    assert library.stats()["tracks"] == 3
    library.close()


def test_imported_tracks_are_searchable(tmp_path, fake_oembed):
    source = tmp_path / "mix.txt"
    source.write_text("https://www.youtube.com/watch?v=" + "a" * 11, encoding="utf-8")
    library = Library(tmp_path / "l.db")
    import_playlist(source, library)
    assert [t.title for t in library.search("chanson")] == ["Chanson"]
    library.close()


def test_custom_name_wins(tmp_path, fake_oembed):
    source = tmp_path / "mix.txt"
    source.write_text("https://www.youtube.com/watch?v=" + "a" * 11, encoding="utf-8")
    library = Library(tmp_path / "l.db")
    _pid, name, _refs = import_playlist(source, library, name="Ma soirée")
    assert name == "Ma soirée"
    library.close()


@pytest.mark.network
def test_real_oembed_contract():
    """Contrat réel de l'endpoint oEmbed (nécessite le réseau)."""
    ref = fetch_metadata("jNQXAC9IVRw")
    assert ref.title and ref.title != "jNQXAC9IVRw"
    assert ref.thumbnail


# ---- Export ---------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["m3u8", "txt", "json", "csv"])
def test_every_export_format_writes_a_file(tmp_path, fmt):
    from triat.core.importer import export_playlist
    from triat.core.models import TrackRef

    refs = [TrackRef("a" * 11, "Un", "Artiste A", 200.0),
            TrackRef("b" * 11, "Deux", "Artiste B", 180.0)]
    path = export_playlist(refs, tmp_path / f"liste.{fmt}")
    assert path.exists() and path.stat().st_size > 0


def test_exported_m3u_can_be_reimported(tmp_path):
    from triat.core.importer import export_playlist, parse_playlist_file
    from triat.core.models import TrackRef

    refs = [TrackRef("a" * 11, "Un"), TrackRef("b" * 11, "Deux")]
    path = export_playlist(refs, tmp_path / "liste.m3u8")
    assert parse_playlist_file(path) == ["a" * 11, "b" * 11]


def test_exported_txt_can_be_reimported(tmp_path):
    from triat.core.importer import export_playlist, parse_playlist_file
    from triat.core.models import TrackRef

    path = export_playlist([TrackRef("c" * 11, "Trois")], tmp_path / "l.txt")
    assert parse_playlist_file(path) == ["c" * 11]


def test_unknown_export_format_is_refused(tmp_path):
    from triat.core.importer import ImportError_, export_playlist

    with pytest.raises(ImportError_, match="Format inconnu"):
        export_playlist([], tmp_path / "liste.xyz")


def test_export_leaves_no_partial_file(tmp_path):
    from triat.core.importer import export_playlist
    from triat.core.models import TrackRef

    export_playlist([TrackRef("d" * 11, "T")], tmp_path / "l.json")
    assert list(tmp_path.glob("*.part")) == []


# ---- SoundCloud -------------------------------------------------------------

from triat.core.importer import (  # noqa: E402
    MAX_SC_LINKS,
    export_playlist,
    extract_soundcloud_url,
    parse_entries,
)
from triat.core.models import TrackRef  # noqa: E402
from triat.core.soundcloud import (  # noqa: E402
    NotPlayable,
    ScPlaylist,
    ScTrack,
    ScUser,
)


def _sc_ref(number: int) -> TrackRef:
    return TrackRef(f"soundcloud:tracks:{number}", f"Titre {number}", "Moi",
                    web_url=f"https://soundcloud.com/moi/titre-{number}")


class FakeSc:
    """Faux client SoundCloud : compte les appels, jamais de réseau."""

    def __init__(self):
        self.calls: list[str] = []

    def resolve_permalink(self, url):
        self.calls.append(url)
        if url.endswith("/sets/mon-set"):
            return ScPlaylist(id=7, title="Mon set")
        if url.endswith("/mort"):
            raise NotPlayable("removed")
        if "on.soundcloud.com" in url:
            return ScTrack(_sc_ref(99), True)
        if url.endswith("/profil"):
            return ScUser(id=1, username="x")
        number = len(self.calls)
        return ScTrack(_sc_ref(number), True)

    def playlist(self, playlist_id):
        assert playlist_id == 7
        return ScPlaylist(id=7, title="Mon set"), [
            ScTrack(_sc_ref(701), True),
            ScTrack(_sc_ref(702), False, "snip"),
        ]


@pytest.mark.parametrize("line,expected", [
    ("https://soundcloud.com/artiste/titre",
     "https://soundcloud.com/artiste/titre"),
    ("https://m.soundcloud.com/artiste/titre?si=abc",
     "https://soundcloud.com/artiste/titre"),
    ("https://soundcloud.com/artiste/sets/mon-set",
     "https://soundcloud.com/artiste/sets/mon-set"),
    ("https://on.soundcloud.com/AbC123", "https://on.soundcloud.com/AbC123"),
    ("Titre,Moi,,0,https://soundcloud.com/a/b,soundcloud",
     "https://soundcloud.com/a/b"),
    ("https://soundcloud.com/artiste", None),
    ("https://soundcloud.com/artiste/likes", None),
    ("# https://soundcloud.com/a/b", None),
    ("https://example.com/a/b", None),
])
def test_soundcloud_links_are_recognised(line, expected):
    assert extract_soundcloud_url(line) == expected


def test_youtube_id_is_anchored():
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQx") is None
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ_x") is None
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ&t=1") == "dQw4w9WgXcQ"


def test_entries_mix_both_sources_in_order(tmp_path):
    path = tmp_path / "mix.txt"
    path.write_text("https://soundcloud.com/a/b\ndQw4w9WgXcQ\n"
                    "https://soundcloud.com/a/b\n", encoding="utf-8")
    assert parse_entries(path) == [("soundcloud", "https://soundcloud.com/a/b"),
                                   ("youtube", "dQw4w9WgXcQ")]


def test_import_resolves_track_set_and_short_link(tmp_path, fake_oembed):
    path = tmp_path / "sc.txt"
    path.write_text("https://soundcloud.com/moi/une\n"
                    "https://soundcloud.com/moi/sets/mon-set\n"
                    "https://on.soundcloud.com/Code1\n"
                    "https://soundcloud.com/moi/mort\n", encoding="utf-8")
    lib = Library(tmp_path / "lib.db")
    client = FakeSc()
    pid, name, refs = (result := import_playlist(
        path, lib, name="SC", soundcloud=client))
    ids = [r.video_id for r in refs]
    # Le set est développé, y compris sa piste non lisible (choix documenté).
    assert ids == ["soundcloud:tracks:1", "soundcloud:tracks:701",
                   "soundcloud:tracks:702", "soundcloud:tracks:99"]
    # Le lien court est passé tel quel, sans redirection suivie.
    assert "https://on.soundcloud.com/Code1" in client.calls
    assert result.sc_failed == 1 and result.notes == ["1 liens SoundCloud illisibles"]
    assert name == "SC" and pid


def test_disabled_soundcloud_counts_ignored_links(tmp_path, fake_oembed):
    path = tmp_path / "m.txt"
    path.write_text("dQw4w9WgXcQ\nhttps://soundcloud.com/a/b\n"
                    "https://soundcloud.com/a/c\n", encoding="utf-8")
    result = import_playlist(path, Library(tmp_path / "lib.db"))
    assert len(result.refs) == 1
    assert result.sc_disabled == 2
    assert "2 liens SoundCloud ignorés (SoundCloud désactivé)" in result.notes


def test_only_soundcloud_links_and_disabled_is_refused(tmp_path):
    path = tmp_path / "m.txt"
    path.write_text("https://soundcloud.com/a/b\n", encoding="utf-8")
    with pytest.raises(ImportError_, match="SoundCloud désactivé"):
        import_playlist(path, Library(tmp_path / "lib.db"))


def test_soundcloud_links_are_capped(tmp_path):
    path = tmp_path / "gros.txt"
    path.write_text("\n".join(f"https://soundcloud.com/a/t{i}"
                              for i in range(MAX_SC_LINKS + 5)),
                    encoding="utf-8")
    client = FakeSc()
    result = import_playlist(path, Library(tmp_path / "lib.db"),
                             soundcloud=client)
    assert MAX_SC_LINKS == 200
    assert len(client.calls) == 200
    assert result.sc_over_limit == 5
    assert len(result.refs) == 200


def test_export_uses_page_url_and_source(tmp_path):
    refs = [_sc_ref(5), TrackRef("a" * 11, "Yt")]
    for fmt in ("m3u8", "txt"):
        body = export_playlist(refs, tmp_path / f"l.{fmt}").read_text()
        assert "https://soundcloud.com/moi/titre-5" in body
        assert "api.soundcloud.com" not in body
        assert "https://www.youtube.com/watch?v=" + "a" * 11 in body
    data = json.loads(export_playlist(refs, tmp_path / "l.json").read_text())
    assert [d["source"] for d in data] == ["soundcloud", "youtube"]
    rows = export_playlist(refs, tmp_path / "l.csv").read_text().splitlines()
    assert rows[0].endswith(",url,source")
    assert rows[1].endswith(",https://soundcloud.com/moi/titre-5,soundcloud")


def test_exported_soundcloud_files_can_be_reimported(tmp_path):
    refs = [_sc_ref(5)]
    for fmt in ("m3u8", "txt", "csv", "json"):
        path = export_playlist(refs, tmp_path / f"l.{fmt}")
        assert parse_entries(path) == [
            ("soundcloud", "https://soundcloud.com/moi/titre-5")]

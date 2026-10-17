"""Tests du gestionnaire de téléchargements."""

from __future__ import annotations

import pytest

from triat.core import downloads as dl
from triat.core.downloads import (CANCELLED, DONE, PENDING, DownloadManager,
                                  Task, safe_name)
from triat.core.models import TrackRef
from triat.core.settings import Settings


@pytest.mark.parametrize("value,expected", [
    ("AC/DC", "ACDC"),
    ("Fichier: test?", "Fichier test"),
    ("a<b>c|d", "abcd"),
    ("  ..cache  ", "cache"),
    ("", "Inconnu"),
    ("nom\x00nul", "nomnul"),
])
def test_filenames_are_sanitised(value, expected):
    assert safe_name(value) == expected


def test_filenames_are_capped():
    assert len(safe_name("a" * 200)) <= 80


def test_no_path_traversal_survives():
    """Un titre hostile ne doit pas sortir du dossier de destination."""
    assert "/" not in safe_name("../../etc/passwd")
    assert ".." not in safe_name("..")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "downloads_dir", lambda: tmp_path)
    settings = Settings(tmp_path / "s.json")
    return DownloadManager(settings=settings)


def test_target_uses_artist_and_album(manager, tmp_path):
    ref = TrackRef("a" * 11, "Titre", "Artiste", album="Album")
    assert manager.target_dir(ref) == tmp_path / "Artiste" / "Album"


def test_target_without_album(manager, tmp_path):
    ref = TrackRef("a" * 11, "Titre", "Artiste")
    assert manager.target_dir(ref) == tmp_path / "Artiste"


def test_unknown_artist_has_a_folder(manager, tmp_path):
    assert manager.target_dir(TrackRef("a" * 11, "T")) == \
        tmp_path / "Artiste inconnu"


def test_local_path_finds_an_existing_file(manager, tmp_path):
    ref = TrackRef("a" * 11, "Ma Piste", "Artiste")
    folder = tmp_path / "Artiste"
    folder.mkdir(parents=True)
    (folder / "Ma Piste.opus").write_bytes(b"audio")
    assert manager.local_path(ref) is not None


def test_local_path_ignores_empty_files(manager, tmp_path):
    ref = TrackRef("a" * 11, "Vide", "Artiste")
    folder = tmp_path / "Artiste"
    folder.mkdir(parents=True)
    (folder / "Vide.opus").write_bytes(b"")
    assert manager.local_path(ref) is None


def test_queue_refuses_duplicates(manager):
    refs = [TrackRef("b" * 11, "B", "A")]
    assert manager.add(refs) == 1
    assert manager.add(refs) == 0, "déjà dans la file"


def test_queue_skips_already_downloaded(manager, tmp_path):
    ref = TrackRef("c" * 11, "Faite", "Artiste")
    folder = tmp_path / "Artiste"
    folder.mkdir(parents=True)
    (folder / "Faite.m4a").write_bytes(b"audio")
    assert manager.add([ref]) == 0


def test_cancel_marks_a_pending_task(manager):
    manager.pause()          # empêche le worker de la prendre
    manager.add([TrackRef("d" * 11, "D", "A")])
    manager.cancel("d" * 11)
    assert manager.tasks[0].state == CANCELLED


def test_clear_finished_keeps_pending(manager):
    manager.pause()
    manager.add([TrackRef("e" * 11, "E", "A"), TrackRef("f" * 11, "F", "A")])
    manager.tasks[0].state = DONE
    task_states = {t.ref.video_id: t for t in manager.tasks}
    task_states["e" * 11].state = DONE
    manager.clear_finished()
    assert all(t.state == PENDING for t in manager.tasks)


def test_options_never_use_an_external_downloader(manager):
    """CVE-2026-50019 : curl en téléchargeur externe fuite les cookies."""
    options = manager._options(Task(ref=TrackRef("g" * 11, "G", "A")))
    assert options.get("external_downloader") is None


def test_sponsorblock_cut_is_configurable(manager):
    task = Task(ref=TrackRef("h" * 11, "H", "A"))
    keys = [p["key"] for p in manager._options(task)["postprocessors"]]
    assert "SponsorBlock" in keys and "ModifyChapters" in keys

    manager._settings.set("download_cut_sponsorblock", False)
    keys = [p["key"] for p in manager._options(task)["postprocessors"]]
    assert "SponsorBlock" not in keys


def test_download_cut_follows_skip_policies_only(manager):
    """Une catégorie « demander » ou « ignorer » n'est jamais coupée."""
    manager._settings.set("sponsorblock_categories", {
        "sponsor": "skip", "intro": "ask", "outro": "ignore",
        "selfpromo": "skip"})
    options = manager._options(Task(ref=TrackRef("j" * 11, "J", "A")))
    by_key = {p["key"]: p for p in options["postprocessors"]}
    assert by_key["SponsorBlock"]["categories"] == ["sponsor", "selfpromo"]
    assert by_key["ModifyChapters"]["remove_sponsor_segments"] == [
        "sponsor", "selfpromo"]


def test_download_cut_skipped_when_no_category_is_skip(manager):
    manager._settings.set("sponsorblock_categories", {"sponsor": "ask"})
    options = manager._options(Task(ref=TrackRef("k" * 11, "K", "A")))
    keys = [p["key"] for p in options["postprocessors"]]
    assert "SponsorBlock" not in keys and "ModifyChapters" not in keys


def test_format_selection(manager):
    task = Task(ref=TrackRef("i" * 11, "I", "A"))
    manager._settings.set("download_format", "mp3")
    options = manager._options(task)
    codec = next(p["preferredcodec"] for p in options["postprocessors"]
                 if p["key"] == "FFmpegExtractAudio")
    assert codec == "mp3"


def test_download_is_silent(manager):
    """yt-dlp ne doit rien écrire sur la sortie standard."""
    options = manager._options(Task(ref=TrackRef("j" * 11, "J", "A")))
    assert options.get("quiet") is True
    assert options.get("noprogress") is True


def test_add_restarts_worker_that_already_decided_to_exit(manager, tmp_path,
                                                          monkeypatch):
    """Course : le worker a vu la file vide mais n'est pas encore sorti
    (is_alive() vrai). Un add() à cet instant ne doit pas laisser la tâche
    PENDING sans worker."""
    import threading
    from pathlib import Path

    decided, gate, done = threading.Event(), threading.Event(), threading.Event()
    real_claim = manager._claim_next

    def claim():
        task = real_claim()
        if task is None:
            decided.set()
            gate.wait(5)            # le fil reste vivant après sa décision
        return task

    def fake_download(task):
        done.set()
        return Path(tmp_path / "x.opus")

    finished = threading.Event()
    manager.on_change = lambda t: finished.set() if t.state == DONE else None
    monkeypatch.setattr(manager, "_claim_next", claim)
    monkeypatch.setattr(manager, "_download", fake_download)
    try:
        manager._ensure_worker()
        assert decided.wait(5)
        assert manager._worker is None       # sortie décidée sous verrou
        assert manager.add([TrackRef("b" * 11, "Titre", "Artiste")]) == 1
        assert done.wait(5), "aucun worker relancé : tâche restée PENDING"
    finally:
        gate.set()
    assert finished.wait(5)
    assert [t.state for t in manager.tasks] == [DONE]


# ---- Lot W1-E ---------------------------------------------------------


def test_cancel_all_empties_the_pending_queue(manager):
    manager.pause()
    manager.add([TrackRef("m" * 11, "M", "A"), TrackRef("n" * 11, "N", "A")])
    manager.cancel_all()
    assert [t.state for t in manager.tasks] == [CANCELLED, CANCELLED]


def test_cancel_all_flags_the_running_task(manager):
    manager.pause()
    manager.add([TrackRef("o" * 11, "O", "A")])
    manager.tasks[0].state = dl.RUNNING
    manager.cancel_all()
    assert manager._cancel.is_set()


def test_delete_all_files_spares_personal_music(manager, tmp_path,
                                                monkeypatch):
    root = tmp_path / "dl"
    outside = tmp_path / "dehors"
    outside.mkdir()
    (outside / "cible.opus").write_bytes(b"x")
    root.mkdir()
    (root / "A").mkdir()
    vid = "a" * 11
    (root / "A" / f"t [{vid}].opus").write_bytes(b"x")
    (root / "A" / f"t [{vid}].jpg").write_bytes(b"x")
    (root / "A" / f"t [{vid}].opus.part").write_bytes(b"x")
    (root / "A" / "ma-chanson.mp3").write_bytes(b"perso")
    (root / "A" / "perso.jpg").write_bytes(b"perso")
    (root / "A" / f"lien [{'b' * 11}].opus").symlink_to(outside / "cible.opus")
    (root / "lien_dossier").symlink_to(outside)
    monkeypatch.setattr(dl, "downloads_dir", lambda: root)
    assert manager.delete_all_files() == 3
    assert (root / "A" / "ma-chanson.mp3").exists()
    assert (root / "A" / "perso.jpg").exists()
    assert (outside / "cible.opus").exists(), "lien symbolique non suivi"
    assert (root / "A").is_dir(), "aucun dossier supprimé"


def test_delete_all_files_removes_indexed_legacy_files(tmp_path, monkeypatch):
    from triat.core.library import Library
    root = tmp_path / "dl"
    (root / "A").mkdir(parents=True)
    old = root / "A" / "Ancien.m4a"
    old.write_bytes(b"x")
    (root / "A" / "Ancien.jpg").write_bytes(b"x")
    (root / "A" / "Autre.m4a").write_bytes(b"perso")
    monkeypatch.setattr(dl, "downloads_dir", lambda: root)
    library = Library(tmp_path / "lib.db")
    library.record_download("c" * 11, str(old))
    manager = DownloadManager(library=library)
    assert manager.delete_all_files() == 2
    assert (root / "A" / "Autre.m4a").exists()
    assert library.download_path("c" * 11) is None


def test_new_files_carry_the_video_id_and_old_name_stays_second(manager):
    ref = TrackRef("a" * 11, "Titre", "Artiste")
    assert manager._stems(ref) == [f"Titre [{'a' * 11}]", "Titre"]


def test_two_videos_with_the_same_title_do_not_collide(manager, tmp_path):
    one = TrackRef("a" * 11, "Même", "Art")
    two = TrackRef("b" * 11, "Même", "Art")
    folder = tmp_path / "Art"
    folder.mkdir()
    (folder / f"Même [{'a' * 11}].opus").write_bytes(b"x")
    assert manager.local_path(one) is not None
    assert manager.local_path(two) is None


def test_legacy_name_is_still_found(manager, tmp_path):
    ref = TrackRef("a" * 11, "Ancien", "Art")
    folder = tmp_path / "Art"
    folder.mkdir()
    (folder / "Ancien.opus").write_bytes(b"x")
    assert manager.local_path(ref) == folder / "Ancien.opus"


def test_non_youtube_ids_are_never_downloaded(manager):
    refs = [TrackRef("soundcloud:123", "S", "A"), TrackRef("x:y", "T", "A"),
            TrackRef("p" * 11, "P", "A")]
    manager.pause()
    assert manager.add(refs) == 1
    assert [t.ref.video_id for t in manager.tasks] == ["p" * 11]


def test_local_path_of_non_youtube_id_skips_the_database(manager):
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("requête SQL inattendue")
    manager._library = Boom()
    assert manager.local_path(TrackRef("soundcloud:9", "S", "A")) is None

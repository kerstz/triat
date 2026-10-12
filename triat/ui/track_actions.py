"""Actions du menu contextuel des pistes (screens.md §4).

Partagé par la Bibliothèque, la Recherche et la file d'attente : une seule
implémentation pour un même menu.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk

from ..core.search_results import clean_artist

log = logging.getLogger(__name__)


class TrackActions:
    """Exécute les actions du menu sur une piste."""

    def __init__(self, engine, toaster=None, parent=None) -> None:
        self._engine = engine
        self._toast = toaster or (lambda _message: None)
        self._parent = parent

    def handle(self, action: str, ref) -> None:
        if ref is None:
            return
        handler = {
            "next": self._play_next,
            "queue": self._enqueue,
            "favorite": self._favorite,
            "playlist": self._add_to_playlist,
            "copy": self._copy_link,
            "download": self._download,
            "block": self._block,
        }.get(action)
        if handler is not None:
            handler(ref)

    # ---- Actions ---------------------------------------------------------

    def _play_next(self, ref) -> None:
        self._engine.enqueue([ref], next_up=True)
        self._toast(f"« {ref.title} » sera lue ensuite")

    def _enqueue(self, ref) -> None:
        self._engine.enqueue([ref])
        self._toast(f"« {ref.title} » ajoutée à la file")

    def _favorite(self, ref) -> None:
        added = self._engine.library.toggle_favorite(ref)
        # Les vues qui montrent l'état du favori (cœur de « Lecture en
        # cours », listes) se resynchronisent sur ce signal.
        emit = getattr(self._engine, "emit", None)
        if callable(emit):
            try:
                emit("library-changed")
            except TypeError:
                pass
        self._toast("Ajoutée aux favoris" if added else "Retirée des favoris")

    def _block(self, ref) -> None:
        """« Ne plus recommander » : le titre seul, ou tout l'artiste."""
        artist = clean_artist(ref.artist)
        dialog = Adw.AlertDialog(
            heading="Ne plus recommander ?",
            body=f"« {ref.title} »\nLa lecture, la recherche et vos playlists "
                 "ne changent pas : seules les suggestions sont concernées.")
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("track", "Ce titre")
        if artist:
            dialog.add_response("artist", f"Tout {artist}")
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")
        dialog.connect("response",
                       lambda _d, response: self.apply_block(ref, response))
        dialog.present(self._parent)

    def apply_block(self, ref, which: str) -> None:
        """Écarte la piste (`which='track'`) ou son artiste (`'artist'`) des
        recommandations. Sans dialogue : c'est ce que la réponse appelle."""
        library = self._engine.library
        if which == "artist":
            artist = clean_artist(ref.artist)
            if not artist:
                return
            library.block("artist", artist, artist)
            label = artist
        elif which == "track":
            library.block("track", ref.video_id, ref.label)
            label = ref.title
        else:
            return
        # La suite déjà ajoutée par la radio ne doit pas contenir ce qu'on
        # vient d'écarter.
        drop = getattr(self._engine, "drop_blocked_upcoming", None)
        if callable(drop):
            drop()
        emit = getattr(self._engine, "emit", None)
        if callable(emit):
            try:
                emit("library-changed")
            except TypeError:
                pass
        self._toast(f"« {label} » ne sera plus recommandé")

    def _copy_link(self, ref) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        display.get_clipboard().set(ref.url)
        self._toast("Lien copié")

    def _download(self, ref) -> None:
        added = self._engine.downloads.add([ref])
        self._toast(f"« {ref.title} » en téléchargement" if added
                    else "Déjà téléchargée")

    def _add_to_playlist(self, ref) -> None:
        playlists = self._engine.library.list_playlists()
        dialog = Adw.AlertDialog(
            heading="Ajouter à une playlist",
            body=f"« {ref.title} »")
        dialog.add_response("cancel", "Annuler")
        dialog.set_close_response("cancel")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        entry = Gtk.Entry(placeholder_text="Nouvelle playlist…")
        entry.update_property([Gtk.AccessibleProperty.LABEL],
                              ["Nom de la nouvelle playlist"])
        box.append(entry)

        chooser = None
        if playlists:
            chooser = Gtk.DropDown.new_from_strings(
                [p["name"] for p in playlists])
            chooser.update_property([Gtk.AccessibleProperty.LABEL], ["Playlist"])
            box.append(chooser)
            dialog.add_response("existing", "Ajouter")
            dialog.set_default_response("existing")
        dialog.add_response("new", "Créer")
        if not playlists:
            dialog.set_default_response("new")
        dialog.set_extra_child(box)

        def on_response(_dialog, response: str) -> None:
            if response == "existing" and chooser is not None:
                playlist = playlists[chooser.get_selected()]
                self._engine.library.add_to_playlist(playlist["id"], [ref])
                self._toast(f"Ajoutée à « {playlist['name']} »")
            elif response == "new":
                name = entry.get_text().strip() or "Nouvelle playlist"
                playlist_id = self._engine.library.create_playlist(name)
                self._engine.library.add_to_playlist(playlist_id, [ref])
                self._toast(f"« {name} » créée")

        dialog.connect("response", on_response)
        dialog.present(self._parent)

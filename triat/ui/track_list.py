"""Liste de pistes virtualisée.

`Gtk.ListView` + `Gio.ListStore` + factory, comme l'impose GTK.md §5 : une
playlist ou un historique peut compter des milliers d'entrées, un `Gtk.Box`
rempli en boucle ne tiendrait pas.
"""

from __future__ import annotations

import gi

gi.require_version("Graphene", "1.0")

from gi.repository import Gdk, Gio, GObject, Graphene, Gtk

from ..core.models import TrackRef, display_title
from .widgets.cover import Cover


def format_time(seconds: float) -> str:
    total = int(max(0, seconds or 0))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


# Hauteur d'une ligne : pochette 40 px et marges.
ROW_HEIGHT = 50


class TrackItem(GObject.Object):
    """Enveloppe GObject autour d'un TrackRef, pour le Gio.ListStore."""

    __gtype_name__ = "TriatTrackItem"

    def __init__(self, ref: TrackRef, index: int) -> None:
        super().__init__()
        self.ref = ref
        self.index = index


class TrackList(Gtk.ListView):
    """Liste de pistes. Émet `track-activated` avec l'index dans le modèle."""

    __gtype_name__ = "TriatTrackList"

    __gsignals__ = {
        "track-activated": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        # Action demandée sur une piste : (nom de l'action, index)
        "track-action": (GObject.SignalFlags.RUN_FIRST, None, (str, int)),
    }

    def __init__(self) -> None:
        self._store = Gio.ListStore(item_type=TrackItem)
        selection = Gtk.SingleSelection(model=self._store, autoselect=False)
        selection.set_can_unselect(True)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", self._on_bind)
        factory.connect("unbind", self._on_unbind)

        super().__init__(model=selection, factory=factory)
        self.add_css_class("triat-track-list")
        self.set_vexpand(True)
        # Un seul clic lance la piste, comme la grille de pochettes.
        self.set_single_click_activate(True)
        self.connect("activate", self._on_activate)
        self._current_id: str | None = None
        self._menu_index = -1
        self._bound: set = set()
        self._favorite_checker = None
        self._build_menu()

    # ---- Menu contextuel -------------------------------------------------

    def _build_menu(self) -> None:
        """Menu au clic droit (screens.md §4)."""
        menu = Gio.Menu()
        lecture = Gio.Menu()
        lecture.append("Lire ensuite", "piste.next")
        lecture.append("Ajouter à la file", "piste.queue")
        menu.append_section(None, lecture)

        organiser = Gio.Menu()
        self._organiser = organiser
        organiser.append("Ajouter aux favoris", "piste.favorite")
        organiser.append("Ajouter à une playlist…", "piste.playlist")
        organiser.append("Télécharger", "piste.download")
        menu.append_section(None, organiser)

        divers = Gio.Menu()
        divers.append("Aller à l'artiste", "piste.artist")
        divers.append("Copier le lien", "piste.copy")
        divers.append("Ne plus recommander…", "piste.block")
        menu.append_section(None, divers)

        self._popover = Gtk.PopoverMenu.new_from_model(menu)
        self._popover.set_parent(self)
        self._popover.set_has_arrow(False)
        self._popover.set_halign(Gtk.Align.START)

        group = Gio.SimpleActionGroup()
        for name in ("next", "queue", "favorite", "playlist", "copy",
                     "download", "artist", "block"):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", self._on_menu_action, name)
            group.add_action(action)
        self.insert_action_group("piste", group)

        # Clavier : Maj+F10 ou touche Menu, sur la ligne sélectionnée.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    def set_favorite_checker(self, checker) -> None:
        """`checker(ref) -> bool` : libellé du menu selon l'état du favori."""
        self._favorite_checker = checker

    def _is_favorite(self, ref) -> bool:
        checker = self._favorite_checker
        if checker is None:
            # Repli : la fenêtre porte le moteur.
            root = self.get_root()
            engine = getattr(root, "engine", None) if root else None
            library = getattr(engine, "library", None)
            if library is None:
                return False
            checker = lambda r: library.is_favorite(r.video_id)   # noqa: E731
        try:
            return bool(checker(ref))
        except Exception:
            return False

    def _refresh_favorite_label(self, index: int) -> None:
        item = self._store.get_item(index)
        favorite = item is not None and self._is_favorite(item.ref)
        self._organiser.remove(0)
        self._organiser.insert(
            0, "Retirer des favoris" if favorite else "Ajouter aux favoris",
            "piste.favorite")

    def _open_menu(self, index: int, anchor: Gtk.Widget | None,
                   x: float, y: float) -> None:
        """Ouvre le menu de la ligne `index`, près de (x, y) dans `anchor`."""
        if not (0 <= index < self._store.get_n_items()):
            return
        self._menu_index = index
        self._refresh_favorite_label(index)
        px, py = x, y
        if anchor is not None and anchor is not self:
            ok, point = anchor.compute_point(self, Graphene.Point().init(x, y))
            if ok:
                px, py = point.x, point.y
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(px), int(py), 1, 1
        self._popover.set_pointing_to(rect)
        if self.get_root() is not None:      # hors fenêtre : rien à afficher
            self._popover.popup()

    def _on_row_right_click(self, _gesture, _n: int, x: float, y: float,
                            list_item: Gtk.ListItem) -> None:
        # La position vient de la ligne elle-même : aucune arithmétique sur
        # les coordonnées, donc juste quel que soit le défilement.
        position = list_item.get_position()
        if position == Gtk.INVALID_LIST_POSITION:
            return
        self._open_menu(position, list_item.get_child(), x, y)

    def _on_key(self, _ctrl, keyval, _code, state) -> bool:
        shift_f10 = (keyval == Gdk.KEY_F10
                     and state & Gdk.ModifierType.SHIFT_MASK)
        if not (shift_f10 or keyval == Gdk.KEY_Menu):
            return False
        index = self.get_model().get_selected()
        if index == Gtk.INVALID_LIST_POSITION:
            return False
        anchor = next((li.get_child() for li in self._bound
                       if li.get_position() == index), None)
        self._open_menu(index, anchor, 24, ROW_HEIGHT / 2)
        return True

    def _on_menu_action(self, _action, _param, name: str) -> None:
        if 0 <= self._menu_index < self._store.get_n_items():
            self.emit("track-action", name, self._menu_index)

    # ---- Modèle ----------------------------------------------------------

    def set_tracks(self, refs) -> None:
        # Un seul splice : un append par piste émettait un signal par ligne
        # (100 ms pour 217 pistes, une saccade à chaque ouverture).
        items = [TrackItem(ref, index) for index, ref in enumerate(refs)]
        self._store.splice(0, self._store.get_n_items(), items)

    def set_current(self, video_id: str | None) -> None:
        """Marque la piste en cours (point accent à la place du numéro)."""
        if video_id == self._current_id:
            return
        previous, self._current_id = self._current_id, video_id
        # Seules les deux lignes concernées sont réaffichées : tout relier
        # rechargeait chaque pochette visible.
        for index in range(self._store.get_n_items()):
            if self._store.get_item(index).ref.video_id in (previous, video_id):
                self._store.items_changed(index, 1, 1)

    @property
    def tracks(self) -> list[TrackRef]:
        return [self._store.get_item(i).ref for i in range(self._store.get_n_items())]

    # ---- Factory ---------------------------------------------------------

    def _on_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("triat-track-row")
        row.set_size_request(-1, ROW_HEIGHT)

        number = Gtk.Label(xalign=0.5, width_chars=3)
        number.add_css_class("triat-mono")
        number.add_css_class("triat-dim")
        row.append(number)

        cover = Cover(size=36, radius=4.0)
        cover.add_css_class("small")
        cover.set_valign(Gtk.Align.CENTER)
        row.append(cover)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                        valign=Gtk.Align.CENTER, hexpand=True)
        title = Gtk.Label(xalign=0, ellipsize=3)       # PANGO_ELLIPSIZE_END
        title.add_css_class("triat-body")
        title.add_css_class("title")
        artist = Gtk.Label(xalign=0, ellipsize=3)
        artist.add_css_class("triat-dim")
        texts.append(title)
        texts.append(artist)
        row.append(texts)

        duration = Gtk.Label(xalign=1)
        duration.add_css_class("triat-mono")
        duration.add_css_class("triat-dim")
        row.append(duration)

        gesture = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        gesture.connect("pressed", self._on_row_right_click, list_item)
        row.add_controller(gesture)

        list_item.set_child(row)
        list_item._widgets = (number, cover, title, artist, duration)

    def _on_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        item = list_item.get_item()
        number, cover, title, artist, duration = list_item._widgets
        playing = item.ref.video_id == self._current_id
        # La piste en cours remplace son numéro par un point accent (screens.md §4).
        number.set_text("●" if playing else f"{item.index + 1:02d}")
        if playing:
            # Le point seul serait lu « disque noir ».
            number.update_property([Gtk.AccessibleProperty.LABEL], ["En cours"])
        else:
            number.reset_property(Gtk.AccessibleProperty.LABEL)
        self._bound.add(list_item)
        row = number.get_parent()
        if playing:
            number.add_css_class("triat-accent-text")
            row.add_css_class("playing")
        else:
            number.remove_css_class("triat-accent-text")
            row.remove_css_class("playing")
        title.set_text(display_title(item.ref.title, item.ref.artist))
        title.set_tooltip_text(item.ref.title)
        cover.set_track(item.ref)
        artist.set_text(item.ref.artist or "—")
        duration.set_text(format_time(item.ref.duration) if item.ref.duration else "—")

    def _on_unbind(self, _factory, list_item: Gtk.ListItem) -> None:
        self._bound.discard(list_item)

    def _on_activate(self, _view, position: int) -> None:
        self.emit("track-activated", position)

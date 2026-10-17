"""Écran Hors-ligne (screens.md §9)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from ..core.downloads import CANCELLED, DONE, FAILED, PENDING, RUNNING
from ..core.paths import downloads_dir
from ..i18n import _, nformat

STATE_LABELS = {
    PENDING: _("en attente"),
    RUNNING: _("en cours"),
    DONE: _("terminé"),
    FAILED: _("échec"),
    CANCELLED: _("annulé"),
}
FORMATS = (("opus", "Opus"), ("m4a", "M4A"), ("mp3", "MP3"))


def _label(text: str, *css: str, **kwargs) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=0, **kwargs)
    for name in css:
        widget.add_css_class(name)
    return widget


class DownloadsPage(Gtk.Box):
    def __init__(self, engine) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("triat-content")
        self._engine = engine
        # video_id -> (ligne, libellé de détail, état affiché, zone d'action)
        self._rows: dict[str, list] = {}

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        column.set_margin_bottom(24)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title = _label(_("Hors-ligne"), "triat-display", "lg")
        title.set_hexpand(True)
        header.append(title)

        self.pause_button = Gtk.Button(label=_("Tout suspendre"),
                                       valign=Gtk.Align.CENTER)
        self.pause_button.add_css_class("pill")
        self.pause_button.connect("clicked", self._toggle_pause)
        header.append(self.pause_button)
        self.clear_button = Gtk.Button(label=_("Vider"), valign=Gtk.Align.CENTER)
        self.clear_button.add_css_class("pill")
        self.clear_button.set_tooltip_text(_("Retirer les téléchargements terminés"))
        self.clear_button.set_visible(False)
        self.clear_button.connect("clicked", self._clear_finished)
        header.append(self.clear_button)
        column.append(header)

        self.summary = _label("", "triat-label", "triat-dim")
        column.append(self.summary)

        options = Adw.PreferencesGroup(title=_("Options"))
        fmt = Adw.ComboRow(title=_("Format"),
                           subtitle=_("Opus offre le meilleur rapport qualité/poids"))
        model = Gtk.StringList()
        for _value, name in FORMATS:
            model.append(name)
        fmt.set_model(model)
        current = engine.settings.get("download_format")
        fmt.set_selected(next((i for i, (v, _) in enumerate(FORMATS)
                               if v == current), 0))
        fmt.connect("notify::selected", lambda r, _p: self._set(
            "download_format", FORMATS[r.get_selected()][0]))
        options.add(fmt)

        cut = Adw.SwitchRow(
            title=_("Couper les segments SponsorBlock"),
            subtitle=_("Le fichier produit ne contient plus les passages non musicaux"))
        cut.set_active(bool(engine.settings.get("download_cut_sponsorblock")))
        cut.connect("notify::active", lambda r, _p: self._set(
            "download_cut_sponsorblock", r.get_active()))
        options.add(cut)

        # Un chemin peut contenir « & » : pas de balisage Pango.
        folder = Adw.ActionRow(title=_("Dossier"), use_markup=False)
        folder.set_subtitle(str(downloads_dir()))
        options.add(folder)
        column.append(options)

        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        column.append(self.list_box)

        self.empty = _label(
            _("Aucun téléchargement. Clic droit sur une piste pour l'ajouter."),
            "triat-body", "triat-dim", wrap=True, max_width_chars=54)
        column.append(self.empty)

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(column)
        self.append(scroller)

        self._refresh_pending = False
        engine.downloads.on_change = self._on_change
        self.refresh()

    # ---- Réglages --------------------------------------------------------

    def _set(self, key: str, value) -> None:
        self._engine.settings.set(key, value)
        self._engine.settings.save()

    def _toggle_pause(self, _button) -> None:
        manager = self._engine.downloads
        if manager.paused:
            manager.resume()
        else:
            manager.pause()
        self.refresh()

    # ---- Affichage -------------------------------------------------------

    def _on_change(self, _task) -> None:
        # Appelé depuis le thread de téléchargement, à chaque tranche de
        # progression : on regroupe en un rafraîchissement au plus toutes
        # les 250 ms au lieu de reconstruire la page des dizaines de fois
        # par seconde.
        # Le drapeau n'est lu et modifié que sur le fil principal : on y
        # repasse toujours par idle_add, jamais depuis ce fil de travail.
        GLib.idle_add(self._schedule_refresh)

    def _schedule_refresh(self) -> bool:
        if not self._refresh_pending:
            self._refresh_pending = True
            GLib.timeout_add(250, self._deferred_refresh)
        return GLib.SOURCE_REMOVE

    def _deferred_refresh(self) -> bool:
        self._refresh_pending = False
        self.refresh()
        return GLib.SOURCE_REMOVE

    def _clear_finished(self, _button) -> None:
        self._engine.downloads.clear_finished()
        self.refresh()

    def refresh(self) -> bool:
        """Met les lignes à jour en place.

        Les lignes ne sont créées ou retirées que si l'ensemble des tâches
        change : reconstruire à chaque tranche de progression faisait perdre
        le focus clavier du bouton « Annuler » toutes les 250 ms.
        """
        manager = self._engine.downloads
        tasks = manager.tasks

        self.empty.set_visible(not tasks)
        self.pause_button.set_label(
            _("Reprendre") if manager.paused else _("Tout suspendre"))

        done = sum(1 for t in tasks if t.state == DONE)
        self.summary.set_text(
            _("{done}, {queued} en file").format(
                done=nformat("{n} terminé", "{n} terminés", done),
                queued=len(tasks) - done)
            if tasks else "")
        self.clear_button.set_visible(
            any(t.state in (DONE, FAILED, CANCELLED) for t in tasks))

        wanted = {task.ref.video_id: task for task in tasks}
        for video_id in [v for v in self._rows if v not in wanted]:
            self.list_box.remove(self._rows.pop(video_id)[0])

        previous = None
        for task in tasks:
            entry = self._rows.get(task.ref.video_id)
            if entry is None:
                row, detail, actions = self._row(task)
                entry = self._rows[task.ref.video_id] = [row, detail, None,
                                                         actions]
                self.list_box.append(row)
            row, detail, shown, actions = entry
            detail.set_text(self._detail_text(task))
            if shown != task.state:
                entry[2] = task.state
                self._fill_actions(actions, task)
            # Ordre de la file respecté sans recréer les lignes.
            self.list_box.reorder_child_after(row, previous)
            previous = row
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _detail_text(task) -> str:
        state = STATE_LABELS.get(task.state, task.state)
        artist = task.ref.artist or "—"
        if task.state == RUNNING:
            return _("{artist}, {state}, {percent} %").format(
                artist=artist, state=state, percent=f"{task.progress * 100:.0f}")
        if task.state == FAILED and task.error:
            return _("{artist}, {state}, {error}").format(
                artist=artist, state=state, error=task.error[:40])
        return _("{artist}, {state}").format(artist=artist, state=state)

    def _row(self, task):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("triat-track-row")
        row.set_size_request(-1, 56)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                        hexpand=True, valign=Gtk.Align.CENTER)
        title = _label(task.ref.title, "triat-body")
        title.set_ellipsize(3)
        title.set_max_width_chars(40)
        texts.append(title)
        detail = _label("", "triat-body", "triat-dim")
        detail.set_ellipsize(3)
        texts.append(detail)
        row.append(texts)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                          valign=Gtk.Align.CENTER)
        row.append(actions)
        return row, detail, actions

    def _icon_button(self, icon: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon)
        button.add_css_class("flat")
        button.add_css_class("circular")
        button.set_valign(Gtk.Align.CENTER)
        button.set_size_request(36, 36)
        button.set_tooltip_text(tooltip)
        button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        button.connect("clicked", lambda *_: callback())
        return button

    def _fill_actions(self, actions: Gtk.Box, task) -> None:
        while (child := actions.get_first_child()) is not None:
            actions.remove(child)
        manager = self._engine.downloads
        if task.state in (PENDING, RUNNING):
            actions.append(self._icon_button(
                "triat-close-symbolic", _("Annuler"),
                lambda: (manager.cancel(task.ref.video_id), self.refresh())))
        elif task.state == FAILED:
            actions.append(self._icon_button(
                "triat-play-symbolic", _("Réessayer"),
                lambda: self._retry(task)))

    def _retry(self, task) -> None:
        """Remet la tâche en file ; la reprise relance le thread si besoin."""
        manager = self._engine.downloads
        task.state = PENDING
        task.error = ""
        task.progress = 0.0
        if not manager.paused:
            manager.resume()
        self.refresh()

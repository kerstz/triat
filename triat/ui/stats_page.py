"""Stats d'écoute locales : chiffres clés et classements par période."""

from __future__ import annotations

import logging
import threading
import time

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

from ..core import stats as fmt
from .widgets.common import EmptyState, SectionTitle, label
from ..i18n import _, nformat

log = logging.getLogger(__name__)


class _Figure(Gtk.Box):
    """Un grand chiffre et sa légende."""

    def __init__(self, caption: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                         hexpand=True)
        self.add_css_class("triat-card")
        self.value = label("–", "triat-display", "md")
        self.append(self.value)
        self.append(label(caption, "triat-dim"))

    def set_value(self, text: str) -> None:
        self.value.set_text(text)


class _Ranking(Gtk.Box):
    """Classement : rang, nom, nombre d'écoutes."""

    def __init__(self, title: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         hexpand=True)
        self.append(SectionTitle(title))
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list-separate")
        self.append(self.list)

    def set_rows(self, rows: list[tuple[str, str, int]]) -> None:
        while (child := self.list.get_first_child()) is not None:
            self.list.remove(child)
        for rank, (name, sub, count) in enumerate(rows, 1):
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            for side in ("start", "end"):
                getattr(box, f"set_margin_{side}")(12)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.append(label(str(rank), "triat-mono", "triat-dim",
                             width_chars=2))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            text.append(label(name, "triat-body", ellipsize=3))
            if sub:
                text.append(label(sub, "triat-dim", ellipsize=3))
            box.append(text)
            box.append(label(nformat("{n} écoute", "{n} écoutes", count), "triat-dim"))
            row = Gtk.ListBoxRow(activatable=False)
            row.set_child(box)
            self.list.append(row)


class StatsPage(Gtk.Box):
    def __init__(self, engine) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self._engine = engine
        self._period = "30d"
        self._token = 0
        self.last_stats: dict | None = None

        self.chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.chips.add_css_class("triat-chips")
        self._chip_buttons: dict[str, Gtk.ToggleButton] = {}
        first = None
        for key, text in fmt.PERIODS:
            button = Gtk.ToggleButton(label=text)
            button.add_css_class("triat-chip")
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.set_active(key == self._period)
            button.connect("toggled", self._on_period, key)
            self._chip_buttons[key] = button
            self.chips.append(button)
        self.append(self.chips)

        self.empty = EmptyState(_("Rien ici pour l'instant"),
                                _("Les titres que tu écoutes apparaîtront ici, "
                                "sauf en session privée."))
        self.empty.set_visible(False)
        self.append(self.empty)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        grid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                       homogeneous=True)
        self.plays = _Figure(_("écoutes"))
        self.hours = _Figure(_("d'écoute"))
        self.days = _Figure(_("jours avec écoute"))
        self.streak = _Figure(_("jours d'affilée"))
        for figure in (self.plays, self.hours, self.days, self.streak):
            grid.append(figure)
        self.content.append(grid)
        self.note = label("", "triat-dim", wrap=True)
        self.content.append(self.note)
        self.top_tracks = _Ranking(_("Titres les plus écoutés"))
        self.top_artists = _Ranking(_("Artistes les plus écoutés"))
        self.content.append(self.top_tracks)
        self.content.append(self.top_artists)
        self.append(self.content)

    @property
    def period(self) -> str:
        return self._period

    def _on_period(self, button, key: str) -> None:
        if button.get_active():
            self._period = key
            self.refresh()

    def refresh(self) -> None:
        """Calcule dans un fil, applique dans la boucle GTK. Un résultat
        périmé (période changée entre-temps) est ignoré."""
        self._token += 1
        token, period = self._token, self._period
        library = self._engine.library

        def worker() -> None:
            try:
                data = library.stats_period(fmt.period_start(period, time.time()))
            except Exception:
                log.exception("Calcul des stats impossible")
                return
            GLib.idle_add(self._apply, token, data)

        threading.Thread(target=worker, name="triat-stats", daemon=True).start()

    def _apply(self, token: int, data: dict) -> bool:
        if token != self._token:
            return GLib.SOURCE_REMOVE
        self.show_stats(data)
        return GLib.SOURCE_REMOVE

    def show_stats(self, data: dict) -> None:
        self.last_stats = data
        has = bool(data["plays"])
        self.empty.set_visible(not has)
        self.content.set_visible(has)
        if not has:
            return
        self.plays.set_value(str(data["plays"]))
        self.hours.set_value(fmt.format_listening(data["seconds"]))
        self.days.set_value(str(data["days"]))
        self.streak.set_value(str(data["streak"]))
        self.note.set_text(fmt.streak_text(data["streak"]))
        self.top_tracks.set_rows([
            (ref.display_title, ref.display_artist, n)
            for ref, n in data["top_tracks"]])
        self.top_artists.set_rows([(name, "", n)
                                   for name, n in data["top_artists"]])

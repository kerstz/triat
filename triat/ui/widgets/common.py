"""Petits widgets partagés entre les écrans."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk


def animations_enabled(widget: Gtk.Widget | None = None) -> bool:
    """Vrai si l'utilisateur n'a pas demandé de réduire les animations
    (réglage `gtk-enable-animations`, relayé par les portails)."""
    settings = None
    if widget is not None:
        try:
            settings = widget.get_settings()
        except Exception:  # noqa: BLE001 - widget détruit
            settings = None
    if settings is None:
        settings = Gtk.Settings.get_default()
    return True if settings is None else bool(
        settings.get_property("gtk-enable-animations"))


def plural(count: int, noun: str) -> str:
    """« 1 titre », « 2 titres » (français, non traduisible).

    Conservé pour compatibilité : les écrans utilisent `nformat`, qui
    expose des msgid ngettext complets."""
    return f"{count} {noun}{'s' if count > 1 else ''}"


def label(text: str, *css: str, **kwargs) -> Gtk.Label:
    kwargs.setdefault("xalign", 0)
    widget = Gtk.Label(label=text, **kwargs)
    for name in css:
        widget.add_css_class(name)
    return widget


def accent_dot(size: int = 8) -> Gtk.Box:
    dot = Gtk.Box()
    dot.add_css_class("triat-accent-dot")
    dot.set_size_request(size, size)
    dot.set_valign(Gtk.Align.CENTER)
    return dot


class SectionTitle(Gtk.Box):
    """Titre de section : point-matrice en minuscules, action à droite.

    Pas de filet ni de puce : chez Nothing la typographie suffit à
    découper la page."""

    def __init__(self, text: str, action: str | None = None,
                 on_action=None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.title = label(text, "triat-display", "section", hexpand=True)
        self.append(self.title)
        self.action = None
        if action and on_action:
            self.action = Gtk.Button(label=action)
            self.action.add_css_class("flat")
            self.action.add_css_class("triat-label")
            self.action.set_valign(Gtk.Align.CENTER)
            self.action.connect("clicked", lambda *_: on_action())
            self.append(self.action)
        elif action:
            self.append(label(action, "triat-label", "triat-dim"))


class EmptyState(Gtk.Box):
    """État vide : un point, un titre, une explication. Jamais d'écran nu.

    Peut aussi montrer une activité en cours (pastille qui tourne) et un
    bouton d'action, par exemple « Réessayer ».
    """

    def __init__(self, title: str, detail: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("triat-card")
        self.set_margin_top(4)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.append(accent_dot(6))
        self._title = label(title, "triat-body")
        header.append(self._title)
        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        header.append(self.spinner)
        self.append(header)
        self._detail = label(detail, "triat-body", "triat-dim", wrap=True,
                             max_width_chars=64)
        self.append(self._detail)
        self.action_button = Gtk.Button(halign=Gtk.Align.START)
        self.action_button.add_css_class("pill")
        self.action_button.set_visible(False)
        self._action_handler = None
        self.append(self.action_button)

    def set_message(self, title: str, detail: str, busy: bool = False,
                    action: str | None = None, on_action=None) -> None:
        self._title.set_text(title)
        self._detail.set_text(detail)
        self._detail.set_visible(bool(detail))
        self.spinner.set_visible(busy)
        self.spinner.set_spinning(busy)
        if self._action_handler is not None:
            self.action_button.disconnect(self._action_handler)
            self._action_handler = None
        self.action_button.set_visible(bool(action and on_action))
        if action and on_action:
            self.action_button.set_label(action)
            self._action_handler = self.action_button.connect(
                "clicked", lambda *_: on_action())

"""Écran Bienvenue (screens.md §11).

Affiché au premier lancement. Trois étapes, toutes facultatives : Triat doit
rester utilisable sans compte.
"""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from ..core.auth import CookieError
from ..core.desktop import (browser_label, detect_browser_profiles,
                            inside_flatpak)
from ..core.downloads import FORMATS

log = logging.getLogger(__name__)

BROWSERS = ("firefox", "chromium", "brave", "vivaldi", "librewolf")
STEPS = 3
# En dessous, la colonne d'identité disparaît et la carte prend toute la
# largeur : la fenêtre descend jusqu'à 360 px.
NARROW_BELOW = 900


def _label(text: str, *css: str, **kwargs) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=0, **kwargs)
    for name in css:
        widget.add_css_class(name)
    return widget


class OnboardingView(Gtk.Box):
    """Accueil du premier lancement. Émet `finished` quand on peut entrer."""

    def __init__(self, engine, on_finish) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("triat-onboarding")
        self._engine = engine
        self._on_finish = on_finish
        self._step = 0
        self._signed_in = False

        self.content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.left = self._build_left()
        self.right = self._build_right()
        self.content.append(self.left)
        self.content.append(self.right)

        # Défilement : sur une fenêtre basse, la carte ne doit pas être
        # coupée. Pas de défilement horizontal : c'est le point de rupture
        # qui adapte la largeur.
        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.content)

        self.breakpoint_bin = Adw.BreakpointBin(hexpand=True, vexpand=True)
        self.breakpoint_bin.set_size_request(360, 360)
        narrow = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(f"max-width: {NARROW_BELOW}sp"))
        narrow.add_setter(self.content, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(self.left, "visible", False)
        self.breakpoint_bin.add_breakpoint(narrow)
        self.breakpoint_bin.set_child(self.scroller)
        self.append(self.breakpoint_bin)
        self._show_step(0)

    # ---- Colonne gauche : identité ---------------------------------------

    def _build_left(self) -> Gtk.Widget:
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                         valign=Gtk.Align.CENTER, hexpand=True)
        column.set_margin_start(56)
        column.set_margin_end(40)

        wordmark = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        name = _label("triat", "triat-display", "nav")
        name.add_css_class("hero")
        wordmark.append(name)
        dot = Gtk.Box(valign=Gtk.Align.CENTER)
        dot.add_css_class("triat-accent-dot")
        dot.set_size_request(10, 10)
        wordmark.append(dot)
        column.append(wordmark)

        column.append(_label(
            "Toute la musique de YouTube. Sans publicité, sans intro parlée.",
            "triat-body", wrap=True, max_width_chars=34))

        engagements = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        engagements.set_margin_top(20)
        for titre, detail in (
            ("Local d'abord",
             "Bibliothèque, playlists et historique restent sur votre machine."),
            ("Aucune télémétrie",
             "Rien n'est envoyé ailleurs que vers YouTube et SponsorBlock."),
            ("SponsorBlock intégré",
             "Les passages non musicaux sont sautés automatiquement."),
        ):
            ligne = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            marqueur = Gtk.Box(valign=Gtk.Align.START)
            marqueur.add_css_class("triat-chip-dot")
            marqueur.set_size_request(5, 5)
            marqueur.set_margin_top(7)
            ligne.append(marqueur)
            textes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            textes.append(_label(titre, "triat-body"))
            textes.append(_label(detail, "triat-body", "triat-dim", wrap=True,
                                 max_width_chars=38))
            ligne.append(textes)
            engagements.append(ligne)
        column.append(engagements)

        field = Gtk.Box()
        field.add_css_class("triat-dotfield")
        field.set_size_request(260, 200)
        field.set_can_target(False)
        field.set_halign(Gtk.Align.START)

        overlay = Gtk.Overlay()
        overlay.set_child(column)
        overlay.add_overlay(field)
        return overlay

    # ---- Colonne droite : étapes -----------------------------------------

    def _build_right(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        card.add_css_class("triat-card")

        self._step_label = _label("", "triat-label", "triat-dim")
        card.append(self._step_label)

        self._dots = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._step_dots = []
        for _ in range(STEPS):
            dot = Gtk.Box()
            dot.add_css_class("triat-chip-dot")
            dot.set_size_request(8, 8)
            self._step_dots.append(dot)
            self._dots.append(dot)
        card.append(self._dots)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._step_account(), "account")
        self._stack.add_named(self._step_sponsorblock(), "sponsorblock")
        self._stack.add_named(self._step_quality(), "quality")
        card.append(self._stack)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        buttons.set_margin_top(8)
        self._previous = Gtk.Button(label="Précédent")
        self._previous.add_css_class("pill")
        self._previous.set_size_request(-1, 44)
        self._previous.connect("clicked", lambda *_: self._back())
        buttons.append(self._previous)
        self._skip = Gtk.Button(label="Continuer sans compte")
        self._skip.add_css_class("pill")
        self._skip.set_size_request(-1, 44)
        self._skip.connect("clicked", lambda *_: self._next())
        buttons.append(self._skip)
        buttons.append(Gtk.Box(hexpand=True))
        self._next_button = Gtk.Button(label="Suivant")
        self._next_button.add_css_class("suggested-action")
        self._next_button.add_css_class("pill")
        self._next_button.set_size_request(-1, 44)
        self._next_button.connect("clicked", lambda *_: self._next())
        buttons.append(self._next_button)
        card.append(buttons)

        # La carte reste lisible sur grand écran (520 px au plus) et se
        # replie sur la largeur disponible sur petit écran.
        clamp = Adw.Clamp(maximum_size=520, tightening_threshold=480,
                          hexpand=True, valign=Gtk.Align.CENTER)
        clamp.set_margin_start(20)
        clamp.set_margin_end(20)
        clamp.set_margin_top(24)
        clamp.set_margin_bottom(24)
        clamp.set_child(card)
        return clamp

    def _step_account(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(_label("Importer vos cookies", "triat-display", "sm"))
        box.append(_label(
            "Sans compte, la recherche, la lecture et SponsorBlock "
            "fonctionnent. Les recommandations personnalisées, elles, "
            "demandent une session YouTube.",
            "triat-body", "triat-dim", wrap=True, max_width_chars=52))

        self._profiles = detect_browser_profiles(BROWSERS)
        detected = [name for name in BROWSERS if name in self._profiles]
        self._browser = Gtk.DropDown.new_from_strings(
            [browser_label(b) for b in detected]
            or ["Aucun navigateur accessible" if inside_flatpak()
                else "Aucun navigateur détecté"])
        self._browser.set_sensitive(bool(detected))
        self._browser.update_property([Gtk.AccessibleProperty.LABEL],
                                      ["Navigateur"])
        self._detected = detected
        box.append(self._browser)

        self._import = Gtk.Button(label="Importer depuis ce navigateur")
        self._import.add_css_class("suggested-action")
        self._import.add_css_class("pill")
        self._import.set_size_request(-1, 44)
        self._import.set_sensitive(bool(detected))
        self._import.connect("clicked", self._do_import)
        box.append(self._import)

        if detected:
            note = ("Les cookies sont chiffrés dans le trousseau du "
                    "système, jamais écrits en clair.")
        elif inside_flatpak():
            note = ("Triat tourne dans un bac à sable Flatpak : les profils "
                    "de vos navigateurs lui sont invisibles. Continuez, puis "
                    "importez un fichier cookies.txt depuis les réglages.")
        else:
            note = ("Aucun profil de navigateur trouvé. Continuez : vous "
                    "pourrez importer un fichier cookies.txt depuis les "
                    "réglages.")
        self._account_status = _label(
            note, "triat-body", "triat-dim", wrap=True, max_width_chars=52)
        box.append(self._account_status)
        return box

    def _step_sponsorblock(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(_label("Sauter le hors-sujet", "triat-display", "sm"))
        box.append(_label(
            "Triat saute les passages non musicaux : intros parlées, "
            "sponsors, rappels d'abonnement. Réglable catégorie par "
            "catégorie dans les réglages.",
            "triat-body", "triat-dim", wrap=True, max_width_chars=52))

        row = Adw.SwitchRow(title="Sauter les segments",
                            subtitle="Recommandé")
        row.set_active(bool(self._engine.settings.get("sponsorblock_enabled")))
        row.connect("notify::active", lambda r, _p: self._set(
            "sponsorblock_enabled", r.get_active()))
        group = Adw.PreferencesGroup()
        group.add(row)
        box.append(group)
        return box

    def _step_quality(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(_label("Qualité audio", "triat-display", "sm"))
        box.append(_label(
            "Opus offre le meilleur rapport qualité-débit. Ce choix concerne "
            "les fichiers téléchargés ; la qualité de lecture se règle "
            "ensuite dans les réglages.",
            "triat-body", "triat-dim", wrap=True, max_width_chars=52))

        group = Adw.PreferencesGroup()
        combo = Adw.ComboRow(title="Format téléchargé")
        model = Gtk.StringList()
        values = list(FORMATS)
        for name in values:
            model.append({"opus": "Opus", "m4a": "M4A (AAC)", "mp3": "MP3"}.get(name, name))
        combo.set_model(model)
        current = self._engine.settings.get("download_format")
        combo.set_selected(values.index(current) if current in values else 0)
        combo.connect("notify::selected", lambda r, _p: self._set(
            "download_format", values[r.get_selected()]))
        group.add(combo)
        box.append(group)
        return box

    # ---- Logique ---------------------------------------------------------

    def _set(self, key: str, value) -> None:
        self._engine.settings.set(key, value)
        self._engine.settings.save()

    @staticmethod
    def _detect_browsers() -> list[str]:
        """Navigateurs dont un profil existe réellement (natif, Flatpak
        ou Snap)."""
        profiles = detect_browser_profiles(BROWSERS)
        return [name for name in BROWSERS if name in profiles]

    def _do_import(self, button: Gtk.Button) -> None:
        if not self._detected:
            return
        name = self._detected[self._browser.get_selected()]
        profile = self._profiles.get(name)       # None : emplacement natif
        button.set_sensitive(False)
        self._account_status.set_text(
            f"Lecture du profil {browser_label(name)}…")

        def worker() -> None:
            try:
                if profile:
                    status = self._engine.cookies.import_from_browser(
                        name, profile=profile)
                else:
                    status = self._engine.cookies.import_from_browser(name)
            except CookieError as exc:
                GLib.idle_add(self._import_done, button, None, str(exc))
                return
            except Exception as exc:
                log.exception("Import impossible")
                GLib.idle_add(self._import_done, button, None, str(exc))
                return
            GLib.idle_add(self._import_done, button, status, None)

        threading.Thread(target=worker, daemon=True).start()

    def _import_done(self, button: Gtk.Button, status, error) -> bool:
        button.set_sensitive(True)
        if error:
            self._account_status.set_text(error.split("\n")[0][:110])
            return GLib.SOURCE_REMOVE
        self._engine.ytmusic.invalidate()
        self._engine.innertube.invalidate()
        if status is not None and status.authenticated:
            self._account_status.set_text(
                f"Session trouvée : {status.count} cookies. "
                "Les recommandations sont actives.")
            self._signed_in = True
            self._update_buttons()
        else:
            self._account_status.set_text(
                "Cookies importés, mais sans session connectée. "
                "Connectez-vous à YouTube dans le navigateur, puis réessayez.")
        return GLib.SOURCE_REMOVE

    def _show_step(self, index: int) -> None:
        self._step = index
        names = ("account", "sponsorblock", "quality")
        titres = ("compte, facultatif", "SponsorBlock", "qualité")
        self._stack.set_visible_child_name(names[index])
        self._step_label.set_text(f"Étape {index + 1} sur {STEPS} : {titres[index]}")
        for position, dot in enumerate(self._step_dots):
            if position == index:
                dot.add_css_class("triat-accent-dot")
                dot.remove_css_class("triat-chip-dot")
            else:
                dot.remove_css_class("triat-accent-dot")
                dot.add_css_class("triat-chip-dot")
        self._update_buttons()

    def _update_buttons(self) -> None:
        """Étape 1 : « Continuer sans compte » OU « Suivant » (une fois la
        session importée), jamais les deux pour la même action."""
        index = self._step
        self._previous.set_visible(index > 0)
        first = index == 0
        self._skip.set_visible(first and not self._signed_in)
        self._next_button.set_visible(not first or self._signed_in)
        self._next_button.set_label(
            "Commencer" if index == STEPS - 1 else "Suivant")

    def _back(self) -> None:
        if self._step > 0:
            self._show_step(self._step - 1)

    def _next(self) -> None:
        if self._step + 1 < STEPS:
            self._show_step(self._step + 1)
            return
        self._set("onboarding_done", True)
        self._on_finish()

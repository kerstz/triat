"""Réglages (screens.md §10).

`Adw.PreferencesDialog` restylé plutôt qu'une fenêtre reconstruite à la
main, comme l'impose GTK.md §1.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ..core.auth import CookieError
from ..core.desktop import (browser_label, desktop_accent,
                            detect_browser_profiles, inside_flatpak)
from ..core.scrobble import ScrobbleError
from ..core.sponsorblock import ASK, CATEGORIES, IGNORE, SKIP
from ..core.sync import PAIRING_SECONDS, pairing_qr_svg, pairing_uri
from ..core.sync import _local_ipv4 as local_addresses
from .equalizer_group import EqualizerGroup
from ..i18n import _, nformat

log = logging.getLogger(__name__)

# Libellés lisibles des catégories SponsorBlock.
CATEGORY_LABELS = {
    "music_offtopic": (_("Passage non musical"),
                       _("Intros parlées, outros, tout ce qui n'est pas la musique")),
    "sponsor": (_("Sponsor"), _("Publicité payée insérée dans la vidéo")),
    "selfpromo": (_("Auto-promotion"), _("L'auteur fait la promotion de ses propres contenus")),
    "interaction": (_("Rappel d'abonnement"), _("« Abonnez-vous, likez »")),
    "intro": (_("Intro"), _("Générique ou écran d'accueil")),
    "outro": (_("Outro"), _("Générique de fin, cartes de fin")),
    "preview": (_("Récapitulatif"), _("Résumé de ce qui va suivre")),
    "filler": (_("Digression"), _("Aparté sans rapport avec le sujet")),
}

POLICIES = ((SKIP, _("Auto")), (ASK, _("Notifier")), (IGNORE, _("Ignorer")))

# Navigateurs proposés à l'import, dans l'ordre de popularité sous Linux.
BROWSERS = ("firefox", "chromium", "brave", "vivaldi", "chrome", "edge", "opera")

# Période de rafraîchissement de « dernière synchro » (page visible).
DEVICES_REFRESH_SECONDS = 30

# Délai maximal d'attente de « Compléter les durées ».
DURATIONS_TIMEOUT = 60

QUALITIES = (("low", _("Basse")), ("normal", _("Normale")),
             ("high", _("Haute")), ("max", _("Maximale")))


def _plain_row(**kwargs) -> Adw.ActionRow:
    """Ligne dont le texte n'est jamais lu comme du balisage Pango : un
    chemin, un nom de téléphone ou une exception contiennent des « & » et
    des « < » qui, sinon, vident le libellé."""
    row = Adw.ActionRow(**kwargs)
    row.set_use_markup(False)
    return row


def format_pairing_code(code: str) -> str:
    """Code d'appairage groupé 4 + 4 : `ABCDEFGH` devient `ABCD-EFGH`."""
    return f"{code[:4]}-{code[4:]}"


def format_countdown(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return _("Expire dans {minutes}:{seconds:02d}").format(
        minutes=seconds // 60, seconds=seconds % 60)


def format_last_sync(when, now: float | None = None) -> str:
    """Dernière synchro en français, relative (« il y a 5 min »)."""
    if not when:
        return _("Jamais synchronisé")
    elapsed = max(0, int((now if now is not None else time.time()) - float(when)))
    if elapsed < 60:
        return _("Dernière synchro : à l'instant")
    if elapsed < 3600:
        return _("Dernière synchro : il y a {n} min").format(n=elapsed // 60)
    if elapsed < 86400:
        return _("Dernière synchro : il y a {n} h").format(n=elapsed // 3600)
    return _("Dernière synchro : il y a {n} j").format(n=elapsed // 86400)


def format_device_status(info: dict, now: float | None = None) -> str:
    """Sous-titre d'un téléphone : « Appairage inachevé » tant qu'aucune
    synchro n'a confirmé l'appairage (drapeau absent = ancien appareil, déjà
    confirmé), sinon la dernière synchro."""
    if info.get("confirmed") is False:
        return _("Appairage inachevé : en attente de la première synchro")
    return format_last_sync(info.get("last_sync"), now)


SC_WARNING = _("SoundCloud n'autorise pas officiellement cet usage ; le service "
              "peut cesser de fonctionner à tout moment ; aucun contenu n'est "
              "téléchargé.")
SC_NOTES = _("Pas de téléchargement, pas de paroles ni de SponsorBlock, pas de "
            "synchro avec le téléphone pour ces titres.")
SOURCES = (("youtube", "YouTube"), ("soundcloud", "SoundCloud"),
           ("both", _("Les deux")))
SC_QUALITIES = (("auto", _("Automatique")), ("high", _("Haute")), ("low", _("Basse")))


def soundcloud_alert() -> Adw.AlertDialog:
    """Avertissement affiché à la première activation de SoundCloud."""
    dialog = Adw.AlertDialog(heading=_("Activer SoundCloud ?"),
                             body=f"{SC_WARNING}\n\n{SC_NOTES}")
    dialog.add_response("cancel", _("Annuler"))
    dialog.add_response("enable", _("Activer"))
    dialog.set_response_appearance("enable", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    return dialog


def apply_soundcloud(engine, enabled: bool) -> None:
    """Active ou coupe SoundCloud via le moteur (repli : réglage seul)."""
    setter = getattr(engine, "set_soundcloud_enabled", None)
    if setter is not None:
        setter(enabled)
    else:
        engine.settings.set("soundcloud_enabled", bool(enabled))
    engine.settings.save()


class SoundCloudSwitch:
    """Interrupteur SoundCloud et son avertissement (réglages, accueil)."""

    def __init__(self, engine, parent, on_toggled=None) -> None:
        self._engine = engine
        self._parent = parent
        self._on_toggled = on_toggled
        self._resetting = False
        self.alert = None
        self.row = Adw.SwitchRow(
            title=_("SoundCloud (expérimental)"),
            subtitle=_("Recherche et lecture de titres SoundCloud"))
        self.row.set_active(bool(engine.settings.get("soundcloud_enabled")))
        self.row.connect("notify::active", self._on_active)

    def _on_active(self, row, _pspec) -> None:
        if self._resetting:
            return
        if row.get_active() and not self._engine.settings.get(
                "soundcloud_tos_ack"):
            self.alert = soundcloud_alert()
            self.alert.connect("response", self._on_response)
            root = self._parent.get_root() if self._parent else None
            if root is not None:
                self.alert.present(self._parent)
            return
        self._apply(row.get_active())

    def _on_response(self, _dialog, response: str) -> None:
        self.alert = None
        if response != "enable":
            self._resetting = True
            self.row.set_active(False)
            self._resetting = False
            return
        self._engine.settings.set("soundcloud_tos_ack", True)
        self._apply(True)

    def _apply(self, enabled: bool) -> None:
        apply_soundcloud(self._engine, enabled)
        if self._on_toggled is not None:
            self._on_toggled(enabled)


def client_id_age(now: float | None = None):
    """Âge en secondes du client_id mis en cache, ou None (jamais la valeur)."""
    try:
        from ..core.paths import cache_dir
        data = json.loads((cache_dir() / "soundcloud.json").read_text())
        found = float(data["found_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return max(0.0, (time.time() if now is None else now) - found)


def format_client_id_status(age) -> str:
    if age is None:
        return _("Identifiant client : introuvable")
    if age < 3600:
        return _("Identifiant client : trouvé il y a {n} min").format(
            n=max(1, int(age // 60)))
    if age < 86400:
        return _("Identifiant client : trouvé il y a {n} h").format(n=int(age // 3600))
    return _("Identifiant client : trouvé il y a {n} j").format(n=int(age // 86400))


class SettingsDialog(Adw.PreferencesDialog):
    """Réglages de Triat."""

    def __init__(self, engine, on_change=None) -> None:
        super().__init__(title=_("Réglages"))
        self._engine = engine
        self._on_change = on_change
        self.set_search_enabled(True)

        self.add(self._page_appearance())
        self.add(self._page_playback())
        self.add(self._page_sponsorblock())
        self.add(self._page_account())
        self.add(self._page_library())
        self.add(self._page_sync())
        self.add(self._page_system())

    # ---- Outils ----------------------------------------------------------

    def _save(self, key: str, value) -> None:
        self._engine.settings.set(key, value)
        self._engine.settings.save()
        if self._on_change is not None:
            self._on_change(key, value)

    def _switch_row(self, title: str, subtitle: str, key: str) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_active(bool(self._engine.settings.get(key)))
        row.connect("notify::active",
                    lambda r, _p: self._save(key, r.get_active()))
        return row

    # ---- Lecture ---------------------------------------------------------

    def _set_pause_fade(self, enabled: bool) -> None:
        setter = getattr(self._engine, "set_pause_fade", None)
        if setter is not None:
            setter(enabled)
        else:
            self._save("pause_fade", enabled)

    def _page_playback(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Lecture"),
                                   icon_name="triat-play-symbolic")
        group = Adw.PreferencesGroup(title=_("Lecture"))

        quality = Adw.ComboRow(title=_("Qualité audio"),
                               subtitle=_("Opus est préféré quand il est disponible"))
        model = Gtk.StringList()
        for _value, name in QUALITIES:
            model.append(name)
        quality.set_model(model)
        current = self._engine.settings.get("audio_quality")
        quality.set_selected(next((i for i, (v, _) in enumerate(QUALITIES)
                                   if v == current), 2))
        quality.connect("notify::selected", lambda r, _p: self._save(
            "audio_quality", QUALITIES[r.get_selected()][0]))
        group.add(quality)

        group.add(self._switch_row(
            _("Radio infinie"),
            _("Poursuit la lecture quand la file d'attente se vide"),
            "autoplay_radio"))

        fade = Adw.SwitchRow(
            title=_("Fondu à la pause"),
            subtitle=_("Adoucit la pause et la reprise (environ 200 ms)"))
        fade.set_active(bool(self._engine.settings.get("pause_fade")))
        fade.connect("notify::active", lambda r, _p: self._set_pause_fade(
            r.get_active()))
        self.pause_fade_row = fade
        group.add(fade)

        normalise = Adw.SwitchRow(
            title=_("Égaliser le volume"),
            subtitle=_("Rapproche le niveau sonore d'une piste à l'autre"))
        normalise.set_active(bool(self._engine.settings.get("normalise_volume")))
        normalise.connect("notify::active", lambda r, _p:
                          self._engine.set_normalisation(r.get_active()))
        group.add(normalise)
        page.add(group)
        page.add(self._sources_group())
        page.add(EqualizerGroup(self._engine))
        return page

    def _sources_group(self) -> Adw.PreferencesGroup:
        settings = self._engine.settings
        group = Adw.PreferencesGroup(
            title=_("Sources"),
            description=_("YouTube reste la source principale."))
        self.sc_switch = SoundCloudSwitch(self._engine, self,
                                          self._sync_sc_sensitivity)
        group.add(self.sc_switch.row)

        self.sc_source = Adw.ComboRow(title=_("Source de recherche par défaut"))
        model = Gtk.StringList()
        for _value, name in SOURCES:
            model.append(name)
        self.sc_source.set_model(model)
        current = settings.get("content_source")
        self.sc_source.set_selected(next(
            (i for i, (v, _) in enumerate(SOURCES) if v == current), 0))
        self.sc_source.connect("notify::selected", lambda r, _p: self._save(
            "content_source", SOURCES[r.get_selected()][0]))
        group.add(self.sc_source)

        self.sc_quality = Adw.ComboRow(title=_("Qualité SoundCloud"))
        model = Gtk.StringList()
        for _value, name in SC_QUALITIES:
            model.append(name)
        self.sc_quality.set_model(model)
        current = settings.get("soundcloud_quality")
        self.sc_quality.set_selected(next(
            (i for i, (v, _) in enumerate(SC_QUALITIES) if v == current), 0))
        self.sc_quality.connect("notify::selected", lambda r, _p: self._save(
            "soundcloud_quality", SC_QUALITIES[r.get_selected()][0]))
        group.add(self.sc_quality)

        self.sc_badge = self._switch_row(
            _("Badge de source"), _("Indique l'origine de chaque titre"),
            "source_badge")
        group.add(self.sc_badge)
        self.sc_hide = self._switch_row(
            _("Masquer les titres non lisibles"),
            _("Cache les titres Go+ ou protégés au lieu de les griser"),
            "soundcloud_hide_unplayable")
        group.add(self.sc_hide)

        self.sc_client = _plain_row(title=_("Identifiant client"))
        self.sc_client.set_subtitle(format_client_id_status(client_id_age()))
        group.add(self.sc_client)

        self.sc_purge = Gtk.Button(label=_("Effacer les données SoundCloud"),
                                   valign=Gtk.Align.CENTER)
        self.sc_purge.add_css_class("destructive-action")
        self.sc_purge.connect("clicked", self._confirm_purge_soundcloud)
        self.sc_purge_row = _plain_row(
            title=_("Effacer les données SoundCloud"),
            subtitle=_("Supprime les titres SoundCloud de la bibliothèque"))
        self.sc_purge_row.add_suffix(self.sc_purge)
        group.add(self.sc_purge_row)

        self._sc_dependents = (self.sc_source, self.sc_quality, self.sc_badge,
                               self.sc_hide, self.sc_client, self.sc_purge_row)
        self._sync_sc_sensitivity(self.sc_switch.row.get_active())
        return group

    def _sync_sc_sensitivity(self, enabled: bool) -> None:
        for widget in self._sc_dependents:
            widget.set_sensitive(bool(enabled))
        if enabled:
            self.sc_client.set_subtitle(format_client_id_status(client_id_age()))

    def _confirm_purge_soundcloud(self, _button=None) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Effacer les données SoundCloud ?"),
            body=_("Les titres SoundCloud sont retirés de la bibliothèque, des "
                 "playlists et de l'historique."))
        dialog.add_response("cancel", _("Annuler"))
        dialog.add_response("purge", _("Effacer"))
        dialog.set_response_appearance("purge", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_purge_response)
        self._purge_dialog = dialog
        dialog.present(self)

    def _on_purge_response(self, _dialog, response: str) -> None:
        if response != "purge":
            return
        count = self._engine.library.purge_source("soundcloud")
        if self._on_change is not None:
            self._on_change("soundcloud_purged", count)
        self.add_toast(Adw.Toast(
            title=nformat("{n} titre SoundCloud effacé",
                    "{n} titres SoundCloud effacés", count),
            use_markup=False))

    # ---- SponsorBlock ----------------------------------------------------

    def _page_sponsorblock(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("SponsorBlock"),
                                   icon_name="triat-next-symbolic")

        group = Adw.PreferencesGroup(
            title=_("SponsorBlock"),
            description=_("Les segments sont demandés par préfixe de hachage : "
                        "l'identifiant de la vidéo ne quitte jamais ta machine. "
                        "Données SponsorBlock (sponsor.ajay.app), "
                        "licence CC BY-NC-SA 4.0."))
        group.add(self._switch_row(
            _("Sauter les segments"), _("Active la détection et le saut"),
            "sponsorblock_enabled"))
        anonymous = _plain_row(
            title=_("Requêtes anonymes"),
            subtitle=_("Seuls les 4 premiers caractères du hachage partent"))
        anonymous.add_suffix(Gtk.Label(label=_("SHA-256 · 4"),
                                       css_classes=["triat-mono", "triat-dim"]))
        group.add(anonymous)
        page.add(self._session_group())
        page.add(group)

        categories = Adw.PreferencesGroup(
            title=_("Catégories"),
            description=_("Auto saute sans rien demander, Notifier propose un "
                        "bouton, Ignorer ne fait rien."))
        policies = dict(self._engine.settings.get("sponsorblock_categories") or {})
        for name in CATEGORIES:
            title, subtitle = CATEGORY_LABELS.get(name, (name, ""))
            row = _plain_row(title=title, subtitle=subtitle)
            row.add_suffix(self._policy_chips(name, policies))
            categories.add(row)
        page.add(categories)
        return page

    def _session_group(self) -> Adw.PreferencesGroup:
        """Bilan depuis le lancement : temps gagné en grand, en Doto."""
        stats = getattr(self._engine, "session_skips", None) or {}
        seconds = int(stats.get("seconds", 0))
        segments = stats.get("segments", 0)
        tracks = len(stats.get("tracks", ()))
        group = Adw.PreferencesGroup(title=_("Session"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("triat-card")
        big = Gtk.Label(label=f"{seconds // 60}:{seconds % 60:02d}", xalign=0,
                        css_classes=["triat-display", "md"])
        box.append(big)
        box.append(Gtk.Label(label=_("sautées depuis le lancement"), xalign=0,
                             css_classes=["triat-body", "triat-dim"]))
        box.append(Gtk.Label(
            label=_("{segments} dans {tracks}").format(
                segments=nformat("{n} segment", "{n} segments", segments),
                tracks=nformat("{n} piste", "{n} pistes", tracks)),
            xalign=0, css_classes=["triat-mono", "triat-dim"]))
        self.session_label = big
        group.add(box)
        return group

    def _policy_chips(self, category: str, policies: dict) -> Gtk.Box:
        """Groupe de puces à choix unique (ChipGroup de GTK.md §4)."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                      valign=Gtk.Align.CENTER)
        box.add_css_class("triat-chips")
        current = policies.get(category, IGNORE)
        first: Gtk.ToggleButton | None = None
        for value, label in POLICIES:
            button = Gtk.ToggleButton()
            button.add_css_class("triat-chip")
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            dot = Gtk.Box(css_classes=["triat-chip-dot", f"policy-{value}"],
                          valign=Gtk.Align.CENTER)
            inner.append(dot)
            inner.append(Gtk.Label(label=label))
            button.set_child(inner)
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.set_active(value == current)
            button.connect("toggled", self._on_policy, category, value)
            box.append(button)
        return box

    def _on_policy(self, button: Gtk.ToggleButton, category: str,
                   value: str) -> None:
        if not button.get_active():
            return
        policies = dict(self._engine.settings.get("sponsorblock_categories") or {})
        if policies.get(category) == value:
            return
        policies[category] = value
        self._save("sponsorblock_categories", policies)

    # ---- Compte ----------------------------------------------------------

    def _page_account(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Compte"),
                                   icon_name="triat-heart-symbolic")
        group = Adw.PreferencesGroup(
            title=_("Cookies YouTube"),
            description=_("Sans compte, la recherche et la radio fonctionnent, "
                        "mais pas les recommandations personnalisées."))

        self.status_row = _plain_row(title=_("Statut"))
        group.add(self.status_row)

        browser = Adw.ComboRow(
            title=_("Importer depuis un navigateur"),
            subtitle=(_("Triat tourne dans un bac à sable Flatpak : importe "
                      "plutôt un fichier cookies.txt") if inside_flatpak()
                      else _("Le navigateur doit être fermé")))
        model = Gtk.StringList()
        for name in BROWSERS:
            model.append(browser_label(name))
        browser.set_model(model)
        self._browser_row = browser
        group.add(browser)

        actions = _plain_row(title=_("Actions"))
        import_button = Gtk.Button(label=_("Importer"), valign=Gtk.Align.CENTER)
        import_button.add_css_class("suggested-action")
        import_button.add_css_class("pill")
        import_button.connect("clicked", self._import_browser)
        actions.add_suffix(import_button)

        file_button = Gtk.Button(label=_("Depuis un fichier"), valign=Gtk.Align.CENTER)
        file_button.add_css_class("pill")
        file_button.connect("clicked", self._import_file)
        actions.add_suffix(file_button)

        clear_button = Gtk.Button(label=_("Effacer"), valign=Gtk.Align.CENTER)
        clear_button.add_css_class("destructive-action")
        clear_button.add_css_class("pill")
        clear_button.connect("clicked", self._clear_cookies)
        actions.add_suffix(clear_button)
        group.add(actions)

        page.add(group)
        self._refresh_status()
        return page

    def _refresh_status(self) -> None:
        # Le trousseau peut bloquer (D-Bus, déverrouillage) : hors fil GTK.
        self.status_row.set_subtitle(_("Vérification…"))

        def worker() -> None:
            try:
                status = self._engine.cookies.status()
            except Exception:
                status = None
            GLib.idle_add(self._apply_status, status)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_status(self, status) -> bool:
        if status is None:
            self.status_row.set_subtitle(_("Trousseau illisible"))
        elif not status.present:
            self.status_row.set_subtitle(_("Aucun cookie enregistré"))
        elif status.expired:
            self.status_row.set_subtitle(_("Session expirée : réimporte-les"))
        elif status.authenticated:
            days = status.days_left
            if days:
                self.status_row.set_subtitle(
                    _("Session authentifiée, {count} cookies, expire dans {days} j")
                    .format(count=status.count, days=f"{days:.0f}"))
            else:
                self.status_row.set_subtitle(
                    _("Session authentifiée, {count} cookies").format(count=status.count))
        else:
            self.status_row.set_subtitle(
                _("{count} cookies, sans session : visiteur seulement").format(count=status.count))
        return GLib.SOURCE_REMOVE

    def _import_browser(self, button: Gtk.Button) -> None:
        name = BROWSERS[self._browser_row.get_selected()]
        # Profil Flatpak ou Snap : yt-dlp ne le trouve pas seul.
        profile = detect_browser_profiles([name]).get(name)
        button.set_sensitive(False)
        self.status_row.set_subtitle(
            _("Lecture du profil {browser_label}…").format(browser_label=browser_label(name)))

        def worker() -> None:
            try:
                if profile:
                    self._engine.cookies.import_from_browser(
                        name, profile=profile)
                else:
                    self._engine.cookies.import_from_browser(name)
            except CookieError as exc:
                GLib.idle_add(self._import_done, button, str(exc))
                return
            except Exception as exc:
                log.exception("Import des cookies impossible")
                GLib.idle_add(self._import_done, button, str(exc))
                return
            GLib.idle_add(self._import_done, button, None)

        threading.Thread(target=worker, daemon=True).start()

    def _import_file(self, _button) -> None:
        dialog = Gtk.FileDialog(title=_("Importer des cookies"))
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        cookie_filter = Gtk.FileFilter()
        cookie_filter.set_name(_("Cookies (.txt, .json)"))
        cookie_filter.add_pattern("*.txt")
        cookie_filter.add_pattern("*.json")
        filters.append(cookie_filter)
        dialog.set_filters(filters)
        dialog.open(self.get_root(), None, self._on_cookie_file)

    def _on_cookie_file(self, dialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        if file is None or not file.get_path():
            return
        path = file.get_path()
        self.status_row.set_subtitle(_("Vérification…"))

        def worker() -> None:
            try:
                self._engine.cookies.import_file(path)
            except CookieError as exc:
                GLib.idle_add(self._cookie_op_done, str(exc))
                return
            except Exception as exc:
                log.exception("Import du fichier de cookies impossible")
                GLib.idle_add(self._cookie_op_done, str(exc))
                return
            GLib.idle_add(self._cookie_op_done, None)

        threading.Thread(target=worker, daemon=True).start()

    def _cookie_op_done(self, error) -> bool:
        if error:
            self.status_row.set_subtitle(error.split("\n")[0][:90])
        else:
            self._after_cookie_change()
        return GLib.SOURCE_REMOVE

    def _import_done(self, button: Gtk.Button, error) -> bool:
        button.set_sensitive(True)
        if error:
            self.status_row.set_subtitle(error.split("\n")[0][:90])
            return GLib.SOURCE_REMOVE
        self._after_cookie_change()
        return GLib.SOURCE_REMOVE

    def _after_cookie_change(self) -> None:
        # Les services en cache portent les anciens cookies : on les rouvre.
        self._engine.ytmusic.invalidate()
        self._engine.innertube.invalidate()
        self._refresh_status()
        if self._on_change is not None:
            self._on_change("cookie_source", None)

    def _clear_cookies(self, _button) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Effacer les cookies YouTube ?"),
            body=_("La session est supprimée du trousseau : les "
                 "recommandations personnalisées s'arrêtent jusqu'au "
                 "prochain import."))
        dialog.add_response("cancel", _("Annuler"))
        dialog.add_response("clear", _("Effacer"))
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_clear_cookies_response)
        dialog.present(self)

    def _on_clear_cookies_response(self, _dialog, response: str) -> None:
        if response != "clear":
            return
        self.status_row.set_subtitle(_("Vérification…"))

        def worker() -> None:
            try:
                self._engine.cookies.clear()
            except Exception as exc:
                log.exception("Effacement des cookies impossible")
                GLib.idle_add(self._cookie_op_done, str(exc))
                return
            GLib.idle_add(self._cookie_op_done, None)

        threading.Thread(target=worker, daemon=True).start()

    # ---- Scrobbling ------------------------------------------------------

    def _page_library(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Bibliothèque"),
                                   icon_name="triat-library-symbolic")
        library = self._engine.library
        group = Adw.PreferencesGroup(title=_("Bibliothèque"))
        stats = library.stats()
        self.library_stats = _plain_row(title=_("Contenu"))
        minutes = int(stats.get("seconds", 0) // 60)
        listened = (_("{hours} h {minutes:02d}").format(hours=minutes // 60,
                                                       minutes=minutes % 60)
                    if minutes >= 60 else _("{minutes} min").format(minutes=minutes))
        self.library_stats.set_subtitle(
            _("{tracks}, {playlists}, {favorites}, {plays} ({listened})").format(
                tracks=nformat("{n} piste", "{n} pistes", stats["tracks"]),
                playlists=nformat("{n} playlist", "{n} playlists", stats["playlists"]),
                favorites=nformat("{n} favori", "{n} favoris", stats["favorites"]),
                plays=nformat("{n} écoute", "{n} écoutes", stats["plays"]),
                listened=listened))
        group.add(self.library_stats)

        missing = len(library.missing_duration())
        durations = _plain_row(
            title=_("Compléter les durées"),
            subtitle=(nformat("{n} piste sans durée", "{n} pistes sans durée", missing) if missing
                      else _("Toutes les durées sont connues")))
        fill = Gtk.Button(label=_("Compléter"), valign=Gtk.Align.CENTER)
        fill.add_css_class("pill")
        fill.set_sensitive(bool(missing))
        fill.connect("clicked", self._fill_durations, durations)
        durations.add_suffix(fill)
        group.add(durations)

        searches = _plain_row(title=_("Historique de recherche"),
                                 subtitle=_("Les 8 dernières requêtes, locales"))
        clear_search = Gtk.Button(label=_("Effacer"), valign=Gtk.Align.CENTER)
        clear_search.add_css_class("pill")
        clear_search.connect("clicked", self._clear_searches)
        searches.add_suffix(clear_search)
        group.add(searches)

        plays = _plain_row(title=_("Historique d'écoute"),
                              subtitle=_("Alimente les mixes et « En boucle »"))
        clear_plays = Gtk.Button(label=_("Effacer"), valign=Gtk.Align.CENTER)
        clear_plays.add_css_class("pill")
        clear_plays.add_css_class("destructive-action")
        clear_plays.connect("clicked", self._confirm_clear_history)
        plays.add_suffix(clear_plays)
        group.add(plays)
        page.add(group)
        page.add(self._scrobble_group())
        return page

    def _fill_durations(self, button: Gtk.Button, row: Adw.ActionRow) -> None:
        button.set_sensitive(False)
        row.set_subtitle(_("Interrogation de YouTube…"))
        self._engine.backfill_durations_async()

        state = {"handler": 0, "timeout": 0}

        def done(*_args) -> bool:
            # Une seule fois : « library-changed » peut venir d'ailleurs, et
            # le repli temporel arrive après un signal déjà traité.
            if state["handler"]:
                self._engine.disconnect(state["handler"])
                state["handler"] = 0
            if state["timeout"]:
                GLib.source_remove(state["timeout"])
                state["timeout"] = 0
            left = len(self._engine.library.missing_duration())
            row.set_subtitle(nformat("{n} piste sans durée", "{n} pistes sans durée", left) if left
                             else _("Toutes les durées sont connues"))
            button.set_sensitive(bool(left))
            return GLib.SOURCE_REMOVE

        def on_timeout() -> bool:
            state["timeout"] = 0
            return done()

        state["handler"] = self._engine.connect("library-changed", done)
        # Repli : si la complétion n'émet rien (hors ligne, rien à écrire),
        # le gestionnaire ne doit pas rester branché indéfiniment.
        state["timeout"] = GLib.timeout_add_seconds(DURATIONS_TIMEOUT, on_timeout)

    def _clear_searches(self, button: Gtk.Button) -> None:
        self._save("search_history", [])
        button.set_sensitive(False)

    def _confirm_clear_history(self, _button) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Effacer l'historique d'écoute ?"),
            body=_("Les playlists et favoris restent. Les mixes et « En boucle » "
                 "repartiront de zéro."))
        dialog.add_response("cancel", _("Annuler"))
        dialog.add_response("clear", _("Effacer"))
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response == "clear":
                self._engine.library.clear_history()
                if self._on_change is not None:
                    self._on_change("history_cleared", True)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _scrobble_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("ListenBrainz"),
            description=_("Déclare tes écoutes à un service ouvert. Le jeton "
                        "se trouve sur listenbrainz.org, dans tes réglages "
                        "de profil."))

        self.scrobble_status = _plain_row(title=_("État"))
        group.add(self.scrobble_status)

        self._token_entry = Adw.PasswordEntryRow(title=_("Jeton"))
        group.add(self._token_entry)

        actions = _plain_row(title=_("Actions"))
        connect = Gtk.Button(label=_("Connecter"), valign=Gtk.Align.CENTER)
        connect.add_css_class("suggested-action")
        connect.add_css_class("pill")
        connect.connect("clicked", self._connect_scrobble)
        actions.add_suffix(connect)

        forget = Gtk.Button(label=_("Oublier"), valign=Gtk.Align.CENTER)
        forget.add_css_class("destructive-action")
        forget.add_css_class("pill")
        forget.connect("clicked", self._forget_scrobble)
        actions.add_suffix(forget)
        group.add(actions)
        self._refresh_scrobble()
        return group

    def _refresh_scrobble(self) -> None:
        settings = self._engine.settings
        if self._engine.scrobbler.enabled:
            user = settings.get("scrobble_user") or _("compte inconnu")
            self.scrobble_status.set_subtitle(_("Connecté : {user}").format(user=user))
        else:
            self.scrobble_status.set_subtitle(_("Non connecté"))

    def _connect_scrobble(self, button: Gtk.Button) -> None:
        token = self._token_entry.get_text().strip()
        if not token:
            self.scrobble_status.set_subtitle(_("Colle d'abord un jeton"))
            return
        button.set_sensitive(False)
        self.scrobble_status.set_subtitle(_("Vérification…"))

        def worker() -> None:
            try:
                name = self._engine.scrobbler.set_token(token)
            except ScrobbleError as exc:
                GLib.idle_add(self._scrobble_done, button, None, str(exc))
                return
            GLib.idle_add(self._scrobble_done, button, name, None)

        threading.Thread(target=worker, daemon=True).start()

    def _scrobble_done(self, button: Gtk.Button, name, error) -> bool:
        button.set_sensitive(True)
        if error:
            self.scrobble_status.set_subtitle(error[:90])
        else:
            self._token_entry.set_text("")
            self.scrobble_status.set_subtitle(_("Connecté : {name}").format(name=name))
        return GLib.SOURCE_REMOVE

    def _forget_scrobble(self, _button) -> None:
        self._engine.scrobbler.clear()
        self._refresh_scrobble()

    # ---- Apparence -------------------------------------------------------

    def _page_appearance(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Apparence"),
                                   icon_name="triat-home-symbolic")
        group = Adw.PreferencesGroup(title=_("Apparence"))
        row = self._switch_row(
            _("Suivre l'accent du bureau"),
            _("Reprend la couleur de ~/.config/nothing/config.json "
            "(redémarrage nécessaire)"),
            "follow_desktop_accent")
        accent = desktop_accent()
        if accent:
            row.set_subtitle(_("Accent actuel : {accent} — lu dans "
                                  "~/.config/nothing/config.json (redémarrage "
                                  "nécessaire)").format(accent=accent))
        group.add(row)

        scene = Adw.ComboRow(title=_("Plein écran"),
                             subtitle=_("Scène ouverte par Ctrl+I ; V bascule ensuite"))
        scenes = (("clip", _("Clip vidéo")), ("glyph", _("Matrice de points")))
        model = Gtk.StringList()
        for _value, name in scenes:
            model.append(name)
        scene.set_model(model)
        current = self._engine.settings.get("immersion_mode") or "clip"
        scene.set_selected(next((i for i, (v, _) in enumerate(scenes)
                                 if v == current), 0))
        scene.connect("notify::selected", lambda r, _p: self._save(
            "immersion_mode", scenes[r.get_selected()][0]))
        group.add(scene)
        page.add(group)
        return page

    # ---- Synchronisation -------------------------------------------------

    def _page_sync(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Synchronisation"),
                                   icon_name="triat-radio-symbolic")
        self._pairing_dialog: Adw.AlertDialog | None = None
        self._pairing_timer = 0
        self._pairing_left = 0
        self._pairing_known: set[str] = set()
        self._device_rows: list[Adw.ActionRow] = []
        self._device_ids: dict[Adw.ActionRow, str] = {}
        self._devices_timer = 0
        self._addresses: list[str] = []

        group = Adw.PreferencesGroup(
            title=_("Téléphone"),
            description=_("Favoris, playlists et historique restent identiques "
                        "sur le bureau et sur le téléphone. Tout passe par le "
                        "Wi-Fi local, rien ne sort sur Internet."))

        self.sync_switch = Adw.SwitchRow(
            title=_("Synchroniser avec le téléphone (Wi-Fi)"),
            subtitle=_("Le bureau attend le téléphone tant que Triat est ouvert"))
        self.sync_switch.set_active(bool(self._engine.settings.get("sync_enabled")))
        self.sync_switch.connect("notify::active", self._on_sync_toggled)
        group.add(self.sync_switch)

        self.sync_address = _plain_row(title=_("Adresse du bureau"))
        self.sync_address.add_css_class("property")
        self.copy_address_button = Gtk.Button(
            label=_("Copier"), valign=Gtk.Align.CENTER,
            tooltip_text=_("Copier les adresses"))
        self.copy_address_button.add_css_class("pill")
        self.copy_address_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Copier les adresses")])
        self.copy_address_button.set_sensitive(False)
        self.copy_address_button.connect("clicked", self._copy_addresses)
        self.sync_address.add_suffix(self.copy_address_button)
        group.add(self.sync_address)

        pair = _plain_row(
            title=_("Nouveau téléphone"),
            subtitle=_("Affiche un code à saisir sur le téléphone"))
        self.pair_button = Gtk.Button(label=_("Associer un téléphone"),
                                      valign=Gtk.Align.CENTER)
        self.pair_button.add_css_class("suggested-action")
        self.pair_button.add_css_class("pill")
        self.pair_button.connect("clicked", self._begin_pairing)
        pair.add_suffix(self.pair_button)
        group.add(pair)

        directions = (("both", _("Bidirectionnel")),
                      ("from_phone", _("Téléphone vers PC")),
                      ("to_phone", _("PC vers téléphone")))
        direction = Adw.ComboRow(
            title=_("Sens de la synchro"),
            subtitle=_("Bidirectionnel : les deux côtés restent identiques. "
                     "Sinon, un seul côté reçoit les nouveautés de l'autre."))
        dir_model = Gtk.StringList()
        for _value, name in directions:
            dir_model.append(name)
        direction.set_model(dir_model)
        current_dir = self._engine.settings.get("sync_direction")
        direction.set_selected(next((i for i, (v, _) in enumerate(directions)
                                     if v == current_dir), 0))
        direction.connect("notify::selected", lambda r, _p: self._save(
            "sync_direction", directions[r.get_selected()][0]))
        group.add(direction)
        page.add(group)

        self.devices_group = Adw.PreferencesGroup(
            title=_("Téléphones associés"),
            description=_("Oublier un téléphone l'empêche de se synchroniser "
                        "jusqu'à un nouvel appairage."))
        page.add(self.devices_group)

        cookies = Adw.PreferencesGroup(title=_("Session YouTube"))
        cookies.add(self._switch_row(
            _("Partager la session YouTube avec le téléphone"),
            _("La session part chiffrée vers les téléphones associés, "
            "seulement sur le Wi-Fi local"),
            "sync_share_cookies"))
        page.add(cookies)

        security = Adw.PreferencesGroup(title=_("Sécurité"))
        security.add(self._switch_row(
            _("Refuser les anciennes versions de l'app mobile"),
            _("Exige le format de synchro protégé contre le rejeu. Les "
            "téléphones qui l'ont déjà utilisé n'ont plus le droit de "
            "revenir à l'ancien format"),
            "sync_refuse_legacy"))
        page.add(security)

        # La mention « dernière synchro » vieillit : on la rafraîchit tant
        # que la page est visible, et plus du tout ensuite.
        page.connect("map", lambda *_: self._start_devices_timer())
        page.connect("unmap", lambda *_: self._stop_devices_timer())
        self.connect("closed", lambda *_: self._stop_devices_timer())

        self._refresh_sync()
        self._refresh_devices()
        return page

    def _on_sync_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        on = row.get_active()
        listening = self._engine.set_sync_enabled(on)
        if on and not listening:
            # Aucun port libre : on remet l'interrupteur à sa place.
            self.add_toast(Adw.Toast(title=_("Synchro impossible : port indisponible")))
            row.set_active(False)
            return
        fallback = getattr(self._engine.sync, "fallback_from", None)
        if on and fallback:
            self.add_toast(Adw.Toast(
                title=_("Port {fallback} occupé : port {port} utilisé").format(fallback=fallback, port=self._engine.sync.port)))
        self._refresh_sync()

    def _refresh_sync(self) -> None:
        """Adresse affichée et bouton d'appairage selon l'état du serveur."""
        sync = self._engine.sync
        running = bool(sync.running)
        self.pair_button.set_sensitive(running)
        if not running:
            self._addresses = []
            self.copy_address_button.set_sensitive(False)
            self.sync_address.set_subtitle(
                _("Serveur non démarré") if self.sync_switch.get_active()
                else _("Active la synchro pour la voir"))
            return
        # Déjà filtrées par le moteur (ni Docker, ni VPN) ; la route par
        # défaut vient en premier.
        addresses = local_addresses()
        self._addresses = [f"{ip}:{sync.port}" for ip in addresses]
        self.copy_address_button.set_sensitive(bool(self._addresses))
        subtitle = ", ".join(self._addresses) or _("adresse inconnue")
        fallback = getattr(sync, "fallback_from", None)
        if fallback:
            subtitle += _(" (port {port} occupé)").format(port=fallback)
        self.sync_address.set_subtitle(subtitle)

    def _copy_addresses(self, _button=None) -> None:
        if not self._addresses:
            return
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(", ".join(self._addresses))
        self.add_toast(Adw.Toast(title=_("Adresses copiées"), use_markup=False))

    def _start_devices_timer(self) -> None:
        if not self._devices_timer:
            self._devices_timer = GLib.timeout_add_seconds(
                DEVICES_REFRESH_SECONDS, self._tick_devices)

    def _stop_devices_timer(self) -> None:
        if self._devices_timer:
            GLib.source_remove(self._devices_timer)
            self._devices_timer = 0

    def _tick_devices(self) -> bool:
        self._refresh_last_sync()
        return GLib.SOURCE_CONTINUE

    def _refresh_last_sync(self) -> None:
        """Remet à jour « dernière synchro » sans reconstruire les lignes
        (le focus clavier sur « Oublier » ne doit pas sauter)."""
        prune = getattr(self._engine.sync, "prune_unconfirmed", None)
        if prune is not None and prune():
            self._refresh_devices()         # un appairage inachevé a expiré
            return
        devices = self._engine.sync.devices()
        for row, device_id in self._device_ids.items():
            info = devices.get(device_id)
            if info is not None:
                row.set_subtitle(format_device_status(info))

    def _refresh_devices(self) -> None:
        for row in self._device_rows:
            self.devices_group.remove(row)
        self._device_rows = []
        self._device_ids = {}
        devices = self._engine.sync.devices()
        if not devices:
            row = _plain_row(title=_("Aucun téléphone associé"))
            row.add_css_class("dim-label")
            self.devices_group.add(row)
            self._device_rows.append(row)
            return
        for device_id, info in sorted(
                devices.items(), key=lambda item: item[1].get("paired_at") or 0):
            row = _plain_row(
                title=info.get("name") or _("Téléphone"),
                subtitle=format_device_status(info))
            forget = Gtk.Button(label=_("Oublier"), valign=Gtk.Align.CENTER)
            forget.add_css_class("destructive-action")
            forget.add_css_class("pill")
            forget.connect("clicked", self._forget_device, device_id)
            row.add_suffix(forget)
            self.devices_group.add(row)
            self._device_rows.append(row)
            self._device_ids[row] = device_id

    def _forget_device(self, _button, device_id: str) -> None:
        self._engine.sync.forget_device(device_id)
        self._refresh_devices()

    # -- Appairage --

    def _begin_pairing(self, _button=None) -> None:
        """Ouvre la fenêtre du code : 120 s, se ferme seule à 0 ou dès qu'un
        nouvel appareil apparaît."""
        sync = self._engine.sync
        if self._pairing_dialog is not None or not sync.running:
            return
        self._pairing_known = set(sync.devices())
        code = sync.begin_pairing()
        self._pairing_left = PAIRING_SECONDS

        dialog = Adw.AlertDialog(
            heading=_("Associer un téléphone"),
            body=_("Sur le téléphone, ouvre Triat > Ordinateur et scanne le QR "
                 "code, ou choisis cet ordinateur et saisis le code."))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      halign=Gtk.Align.CENTER)
        self.pairing_code_label = Gtk.Label(
            label=format_pairing_code(code),
            css_classes=["triat-pair-code"])
        self.pairing_code_label.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Code d'appairage : {code}").format(code=" ".join(code))])
        self.pairing_countdown = Gtk.Label(
            label=format_countdown(self._pairing_left),
            css_classes=["triat-mono", "triat-dim"])
        qr = self._pairing_qr(code)
        if qr is not None:
            box.append(qr)
        box.append(self.pairing_code_label)
        box.append(self.pairing_countdown)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Annuler"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("closed", self._on_pairing_closed)
        self._pairing_dialog = dialog
        self._pairing_timer = GLib.timeout_add_seconds(1, self._tick_pairing)
        dialog.present(self)

    def _pairing_qr(self, code: str):
        """QR d'appairage 200×200 sur fond blanc, ou None."""
        sync = self._engine.sync
        hosts = local_addresses()
        if not hosts:
            return None
        # Toutes les adresses utilisables : le téléphone essaie chacune,
        # celle de la route par défaut d'abord.
        svg = pairing_qr_svg(pairing_uri(
            hosts, sync.port, code, sync.settings.get("sync_server_id")))
        if svg is None:
            return None
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(svg))
        except GLib.Error:
            return None
        pic = Gtk.Picture.new_for_paintable(texture)
        pic.set_size_request(200, 200)
        pic.set_can_shrink(False)
        pic.add_css_class("triat-pair-qr")
        pic.update_property([Gtk.AccessibleProperty.LABEL],
                            [_("QR code d'appairage")])
        return pic

    def _tick_pairing(self) -> bool:
        """Une seconde de plus : appareil arrivé, ou temps écoulé."""
        if self._pairing_dialog is None:
            self._pairing_timer = 0
            return GLib.SOURCE_REMOVE
        devices = self._engine.sync.devices()
        if set(devices) - self._pairing_known:
            self._pairing_timer = 0
            self._refresh_devices()
            self.add_toast(Adw.Toast(title=_("Téléphone associé")))
            self._close_pairing()
            return GLib.SOURCE_REMOVE
        if not self._engine.sync.pairing_active:
            # Expiré ou annulé côté serveur : le compte à rebours mentirait.
            self._pairing_timer = 0
            self.pairing_countdown.set_label(_("Appairage fermé : relance-le"))
            return GLib.SOURCE_REMOVE
        self._pairing_left -= 1
        if self._pairing_left <= 0:
            self._pairing_timer = 0
            self._close_pairing()
            return GLib.SOURCE_REMOVE
        self.pairing_countdown.set_label(format_countdown(self._pairing_left))
        return GLib.SOURCE_CONTINUE

    def _close_pairing(self) -> None:
        dialog = self._pairing_dialog
        if dialog is not None:
            dialog.close()
        self._on_pairing_closed(dialog)

    def _on_pairing_closed(self, _dialog) -> None:
        """Fermeture (bouton, Échap, compte à rebours ou appareil arrivé) :
        le code ne doit plus valoir rien."""
        if self._pairing_timer:
            GLib.source_remove(self._pairing_timer)
            self._pairing_timer = 0
        if self._pairing_dialog is None:
            return
        self._pairing_dialog = None
        self._engine.sync.cancel_pairing()
        self._refresh_devices()

    # ---- Système ---------------------------------------------------------

    def _page_system(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Système"),
                                   icon_name="triat-downloads-symbolic")
        group = Adw.PreferencesGroup(title=_("Vie privée"))
        group.add(self._switch_row(
            _("Session privée"),
            _("N'enregistre rien dans l'historique d'écoute"),
            "private_session"))

        self.blocked_row = _plain_row(
            title=_("Recommandations écartées"),
            subtitle=self._blocked_summary())
        manage = Gtk.Button(label=_("Gérer"), valign=Gtk.Align.CENTER)
        manage.add_css_class("pill")
        manage.connect("clicked", self._manage_blocked)
        self.blocked_row.add_suffix(manage)
        group.add(self.blocked_row)

        wipe = _plain_row(
            title=_("Effacer toutes mes données"),
            subtitle=_("Bibliothèque, playlists, historique et cookies"))
        button = Gtk.Button(label=_("Tout effacer"), valign=Gtk.Align.CENTER)
        button.add_css_class("destructive-action")
        button.add_css_class("pill")
        button.connect("clicked", self._confirm_wipe)
        wipe.add_suffix(button)
        group.add(wipe)
        page.add(group)
        return page

    def _blocked_summary(self) -> str:
        blocked = self._engine.library.blocked()
        tracks, artists = len(blocked["track"]), len(blocked["artist"])
        if not tracks and not artists:
            return _("Rien n'est écarté des suggestions")
        return _("{tracks} et {artists}").format(
            tracks=nformat("{n} titre", "{n} titres", tracks),
            artists=nformat("{n} artiste", "{n} artistes", artists))

    def _manage_blocked(self, _button=None) -> None:
        """Liste des titres et artistes écartés, chacun avec « Retirer »."""
        library = self._engine.library
        dialog = Adw.AlertDialog(
            heading=_("Recommandations écartées"),
            body=_("Ces titres et artistes ne sont plus proposés dans les "
                 "mixes, la radio ni les suggestions. La lecture, la "
                 "recherche et tes playlists ne changent pas."))
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listing.add_css_class("boxed-list")
        empty = _plain_row(title=_("Rien n'est écarté"))
        scroller = Gtk.ScrolledWindow(min_content_height=60,
                                      max_content_height=320,
                                      propagate_natural_height=True)
        scroller.set_child(listing)
        rows: list = []

        def refresh_empty() -> None:
            empty.set_visible(not rows)
            self.blocked_row.set_subtitle(self._blocked_summary())

        def remove(_button, kind: str, key: str, row) -> None:
            library.unblock(kind, key)
            listing.remove(row)
            rows.remove(row)
            refresh_empty()
            if self._on_change is not None:
                self._on_change("blocklist", True)

        blocked = library.blocked()
        for kind, noun in (("artist", _("Artiste")), ("track", _("Titre"))):
            for key, name in blocked[kind].items():
                row = _plain_row(title=name or key, subtitle=noun)
                undo = Gtk.Button(label=_("Retirer"), valign=Gtk.Align.CENTER)
                undo.add_css_class("flat")
                undo.connect("clicked", remove, kind, key, row)
                row.add_suffix(undo)
                listing.append(row)
                rows.append(row)
        listing.append(empty)
        refresh_empty()
        dialog.set_extra_child(scroller)
        dialog.add_response("close", _("Fermer"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        self.blocked_dialog = dialog
        dialog.present(self)

    def _confirm_wipe(self, _button) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Effacer toutes tes données ?"),
            body=_("Seront supprimés : ta bibliothèque et tes playlists, "
                 "l'historique, les cookies, les paroles et pochettes en "
                 "cache, la file d'écoutes ListenBrainz en attente, les "
                 "journaux, les copies de base mises de côté et les "
                 "appareils appairés. Cette action est irréversible."))
        self._wipe_downloads = Gtk.CheckButton(
            label=_("Supprimer aussi les titres téléchargés"))
        self._wipe_downloads.set_active(False)
        dialog.set_extra_child(self._wipe_downloads)
        dialog.add_response("cancel", _("Annuler"))
        dialog.add_response("wipe", _("Tout effacer"))
        dialog.set_response_appearance("wipe", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_wipe_response)
        dialog.present(self)

    def _on_wipe_response(self, _dialog, response: str) -> None:
        if response != "wipe":
            return
        check = getattr(self, "_wipe_downloads", None)
        self._engine.wipe_user_data(
            delete_downloads=bool(check is not None and check.get_active()))
        # Le moteur a coupé la synchro et oublié les téléphones : l'écran
        # doit le refléter.
        self.sync_switch.handler_block_by_func(self._on_sync_toggled)
        try:
            self.sync_switch.set_active(False)
        finally:
            self.sync_switch.handler_unblock_by_func(self._on_sync_toggled)
        self._refresh_sync()
        self._refresh_devices()
        self._after_cookie_change()
        if self._on_change is not None:
            self._on_change("wiped", True)

"""Réglages (screens.md §10).

`Adw.PreferencesDialog` restylé plutôt qu'une fenêtre reconstruite à la
main, comme l'impose GTK.md §1.
"""

from __future__ import annotations

import logging
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk

from ..core.auth import CookieError
from ..core.desktop import desktop_accent
from ..core.scrobble import ScrobbleError
from ..core.sponsorblock import ASK, CATEGORIES, IGNORE, SKIP
from ..core.sync import PAIRING_SECONDS
from ..core.sync import _local_ipv4 as local_addresses
from .equalizer_group import EqualizerGroup

log = logging.getLogger(__name__)

# Libellés lisibles des catégories SponsorBlock.
CATEGORY_LABELS = {
    "music_offtopic": ("Passage non musical",
                       "Intros parlées, outros, tout ce qui n'est pas la musique"),
    "sponsor": ("Sponsor", "Publicité payée insérée dans la vidéo"),
    "selfpromo": ("Auto-promotion", "L'auteur fait la promotion de ses propres contenus"),
    "interaction": ("Rappel d'abonnement", "« Abonnez-vous, likez »"),
    "intro": ("Intro", "Générique ou écran d'accueil"),
    "outro": ("Outro", "Générique de fin, cartes de fin"),
    "preview": ("Récapitulatif", "Résumé de ce qui va suivre"),
    "filler": ("Digression", "Aparté sans rapport avec le sujet"),
}

POLICIES = ((SKIP, "Auto"), (ASK, "Notifier"), (IGNORE, "Ignorer"))

# Navigateurs proposés à l'import, dans l'ordre de popularité sous Linux.
BROWSERS = ("firefox", "chromium", "brave", "vivaldi", "chrome", "edge", "opera")

QUALITIES = (("low", "Basse"), ("normal", "Normale"),
             ("high", "Haute"), ("max", "Maximale"))


def format_pairing_code(code: str) -> str:
    """Code d'appairage groupé 4 + 4 : `ABCDEFGH` devient `ABCD-EFGH`."""
    return f"{code[:4]}-{code[4:]}"


def format_countdown(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"Expire dans {seconds // 60}:{seconds % 60:02d}"


def format_last_sync(when, now: float | None = None) -> str:
    """Dernière synchro en français, relative (« il y a 5 min »)."""
    if not when:
        return "Jamais synchronisé"
    elapsed = max(0, int((now if now is not None else time.time()) - float(when)))
    if elapsed < 60:
        return "Dernière synchro : à l'instant"
    if elapsed < 3600:
        return f"Dernière synchro : il y a {elapsed // 60} min"
    if elapsed < 86400:
        return f"Dernière synchro : il y a {elapsed // 3600} h"
    return f"Dernière synchro : il y a {elapsed // 86400} j"


class SettingsDialog(Adw.PreferencesDialog):
    """Réglages de Triat."""

    def __init__(self, engine, on_change=None) -> None:
        super().__init__(title="Réglages")
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

    def _page_playback(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Lecture",
                                   icon_name="triat-play-symbolic")
        group = Adw.PreferencesGroup(title="Lecture")

        quality = Adw.ComboRow(title="Qualité audio",
                               subtitle="Opus est préféré quand il est disponible")
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
            "Radio infinie",
            "Poursuit la lecture quand la file d'attente se vide",
            "autoplay_radio"))

        normalise = Adw.SwitchRow(
            title="Égaliser le volume",
            subtitle="Rapproche le niveau sonore d'une piste à l'autre")
        normalise.set_active(bool(self._engine.settings.get("normalise_volume")))
        normalise.connect("notify::active", lambda r, _p:
                          self._engine.set_normalisation(r.get_active()))
        group.add(normalise)
        page.add(group)
        page.add(EqualizerGroup(self._engine))
        return page

    # ---- SponsorBlock ----------------------------------------------------

    def _page_sponsorblock(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="SponsorBlock",
                                   icon_name="triat-next-symbolic")

        group = Adw.PreferencesGroup(
            title="SponsorBlock",
            description="Les segments sont demandés par préfixe de hachage : "
                        "l'identifiant de la vidéo ne quitte jamais votre machine.")
        group.add(self._switch_row(
            "Sauter les segments", "Active la détection et le saut",
            "sponsorblock_enabled"))
        anonymous = Adw.ActionRow(
            title="Requêtes anonymes",
            subtitle="Seuls les 4 premiers caractères du hachage partent")
        anonymous.add_suffix(Gtk.Label(label="SHA-256 · 4",
                                       css_classes=["triat-mono", "triat-dim"]))
        group.add(anonymous)
        page.add(self._session_group())
        page.add(group)

        categories = Adw.PreferencesGroup(
            title="Catégories",
            description="Auto saute sans rien demander, Notifier propose un "
                        "bouton, Ignorer ne fait rien.")
        policies = dict(self._engine.settings.get("sponsorblock_categories") or {})
        for name in CATEGORIES:
            title, subtitle = CATEGORY_LABELS.get(name, (name, ""))
            row = Adw.ActionRow(title=title, subtitle=subtitle)
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
        group = Adw.PreferencesGroup(title="Session")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("triat-card")
        big = Gtk.Label(label=f"{seconds // 60}:{seconds % 60:02d}", xalign=0,
                        css_classes=["triat-display", "md"])
        box.append(big)
        box.append(Gtk.Label(label="sautées depuis le lancement", xalign=0,
                             css_classes=["triat-body", "triat-dim"]))
        plural = lambda n, w: f"{n} {w}{'s' if n > 1 else ''}"
        box.append(Gtk.Label(
            label=f"{plural(segments, 'segment')} dans {plural(tracks, 'piste')}",
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
        page = Adw.PreferencesPage(title="Compte",
                                   icon_name="triat-heart-symbolic")
        group = Adw.PreferencesGroup(
            title="Cookies YouTube",
            description="Sans compte, la recherche et la radio fonctionnent, "
                        "mais pas les recommandations personnalisées.")

        self.status_row = Adw.ActionRow(title="Statut")
        group.add(self.status_row)

        browser = Adw.ComboRow(title="Importer depuis un navigateur",
                               subtitle="Le navigateur doit être fermé")
        model = Gtk.StringList()
        for name in BROWSERS:
            model.append(name.capitalize())
        browser.set_model(model)
        self._browser_row = browser
        group.add(browser)

        actions = Adw.ActionRow(title="Actions")
        import_button = Gtk.Button(label="Importer", valign=Gtk.Align.CENTER)
        import_button.add_css_class("suggested-action")
        import_button.add_css_class("pill")
        import_button.connect("clicked", self._import_browser)
        actions.add_suffix(import_button)

        file_button = Gtk.Button(label="Depuis un fichier", valign=Gtk.Align.CENTER)
        file_button.add_css_class("pill")
        file_button.connect("clicked", self._import_file)
        actions.add_suffix(file_button)

        clear_button = Gtk.Button(label="Effacer", valign=Gtk.Align.CENTER)
        clear_button.add_css_class("destructive-action")
        clear_button.add_css_class("pill")
        clear_button.connect("clicked", self._clear_cookies)
        actions.add_suffix(clear_button)
        group.add(actions)

        page.add(group)
        self._refresh_status()
        return page

    def _refresh_status(self) -> None:
        try:
            status = self._engine.cookies.status()
        except Exception:
            self.status_row.set_subtitle("Trousseau illisible")
            return
        if not status.present:
            self.status_row.set_subtitle("Aucun cookie enregistré")
        elif status.expired:
            self.status_row.set_subtitle("Session expirée — réimportez")
        elif status.authenticated:
            days = status.days_left
            reste = f", expire dans {days:.0f} j" if days else ""
            self.status_row.set_subtitle(
                f"Session authentifiée, {status.count} cookies{reste}")
        else:
            self.status_row.set_subtitle(
                f"{status.count} cookies, sans session — visiteur seulement")

    def _import_browser(self, button: Gtk.Button) -> None:
        name = BROWSERS[self._browser_row.get_selected()]
        button.set_sensitive(False)
        self.status_row.set_subtitle(f"Lecture du profil {name}…")

        def worker() -> None:
            try:
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
        dialog = Gtk.FileDialog(title="Importer des cookies")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        cookie_filter = Gtk.FileFilter()
        cookie_filter.set_name("Cookies (.txt, .json)")
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
        try:
            self._engine.cookies.import_file(file.get_path())
        except CookieError as exc:
            self.status_row.set_subtitle(str(exc))
            return
        self._after_cookie_change()

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
        self._engine.cookies.clear()
        self._after_cookie_change()

    # ---- Scrobbling ------------------------------------------------------

    def _page_library(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Bibliothèque",
                                   icon_name="triat-library-symbolic")
        library = self._engine.library
        group = Adw.PreferencesGroup(title="Bibliothèque")
        stats = library.stats()
        self.library_stats = Adw.ActionRow(title="Contenu")
        plural = lambda n, w: f"{n} {w}{'s' if n > 1 else ''}"
        minutes = int(stats.get("seconds", 0) // 60)
        listened = (f"{minutes // 60} h {minutes % 60:02d}" if minutes >= 60
                    else f"{minutes} min")
        self.library_stats.set_subtitle(
            f"{plural(stats['tracks'], 'piste')}, "
            f"{plural(stats['playlists'], 'playlist')}, "
            f"{plural(stats['favorites'], 'favori')}, "
            f"{plural(stats['plays'], 'écoute')} ({listened})")
        group.add(self.library_stats)

        missing = len(library.missing_duration())
        durations = Adw.ActionRow(
            title="Compléter les durées",
            subtitle=(f"{missing} pistes sans durée" if missing
                      else "Toutes les durées sont connues"))
        fill = Gtk.Button(label="Compléter", valign=Gtk.Align.CENTER)
        fill.add_css_class("pill")
        fill.set_sensitive(bool(missing))
        fill.connect("clicked", self._fill_durations, durations)
        durations.add_suffix(fill)
        group.add(durations)

        searches = Adw.ActionRow(title="Historique de recherche",
                                 subtitle="Les 8 dernières requêtes, locales")
        clear_search = Gtk.Button(label="Effacer", valign=Gtk.Align.CENTER)
        clear_search.add_css_class("pill")
        clear_search.connect("clicked", self._clear_searches)
        searches.add_suffix(clear_search)
        group.add(searches)

        plays = Adw.ActionRow(title="Historique d'écoute",
                              subtitle="Alimente les mixes et « En boucle »")
        clear_plays = Gtk.Button(label="Effacer", valign=Gtk.Align.CENTER)
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
        row.set_subtitle("Interrogation de YouTube…")
        self._engine.backfill_durations_async()

        def done(*_args) -> None:
            left = len(self._engine.library.missing_duration())
            row.set_subtitle(f"{left} pistes sans durée" if left
                             else "Toutes les durées sont connues")
            self._engine.disconnect(handler)

        handler = self._engine.connect("library-changed", done)

    def _clear_searches(self, button: Gtk.Button) -> None:
        self._save("search_history", [])
        button.set_sensitive(False)

    def _confirm_clear_history(self, _button) -> None:
        dialog = Adw.AlertDialog(
            heading="Effacer l'historique d'écoute ?",
            body="Les playlists et favoris restent. Les mixes et « En boucle » "
                 "repartiront de zéro.")
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("clear", "Effacer")
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
            title="ListenBrainz",
            description="Déclare vos écoutes à un service ouvert. Le jeton "
                        "se trouve sur listenbrainz.org, dans vos réglages "
                        "de profil.")

        self.scrobble_status = Adw.ActionRow(title="État")
        group.add(self.scrobble_status)

        self._token_entry = Adw.PasswordEntryRow(title="Jeton")
        group.add(self._token_entry)

        actions = Adw.ActionRow(title="Actions")
        connect = Gtk.Button(label="Connecter", valign=Gtk.Align.CENTER)
        connect.add_css_class("suggested-action")
        connect.add_css_class("pill")
        connect.connect("clicked", self._connect_scrobble)
        actions.add_suffix(connect)

        forget = Gtk.Button(label="Oublier", valign=Gtk.Align.CENTER)
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
            user = settings.get("scrobble_user") or "compte inconnu"
            self.scrobble_status.set_subtitle(f"Connecté : {user}")
        else:
            self.scrobble_status.set_subtitle("Non connecté")

    def _connect_scrobble(self, button: Gtk.Button) -> None:
        token = self._token_entry.get_text().strip()
        if not token:
            self.scrobble_status.set_subtitle("Collez d'abord un jeton")
            return
        button.set_sensitive(False)
        self.scrobble_status.set_subtitle("Vérification…")

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
            self.scrobble_status.set_subtitle(f"Connecté : {name}")
        return GLib.SOURCE_REMOVE

    def _forget_scrobble(self, _button) -> None:
        self._engine.scrobbler.clear()
        self._refresh_scrobble()

    # ---- Apparence -------------------------------------------------------

    def _page_appearance(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Apparence",
                                   icon_name="triat-home-symbolic")
        group = Adw.PreferencesGroup(title="Apparence")
        row = self._switch_row(
            "Suivre l'accent du bureau",
            "Reprend la couleur de ~/.config/nothing/config.json "
            "(redémarrage nécessaire)",
            "follow_desktop_accent")
        accent = desktop_accent()
        if accent:
            row.set_subtitle(f"Accent actuel : {accent} — lu dans "
                             "~/.config/nothing/config.json (redémarrage nécessaire)")
        group.add(row)

        scene = Adw.ComboRow(title="Plein écran",
                             subtitle="Scène ouverte par Ctrl+I ; V bascule ensuite")
        scenes = (("clip", "Clip vidéo"), ("glyph", "Matrice de points"))
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
        page = Adw.PreferencesPage(title="Synchronisation",
                                   icon_name="triat-radio-symbolic")
        self._pairing_dialog: Adw.AlertDialog | None = None
        self._pairing_timer = 0
        self._pairing_left = 0
        self._pairing_known: set[str] = set()
        self._device_rows: list[Adw.ActionRow] = []

        group = Adw.PreferencesGroup(
            title="Téléphone",
            description="Favoris, playlists et historique restent identiques "
                        "sur le bureau et sur le téléphone. Tout passe par le "
                        "Wi-Fi local, rien ne sort sur Internet.")

        self.sync_switch = Adw.SwitchRow(
            title="Synchroniser avec le téléphone (Wi-Fi)",
            subtitle="Le bureau attend le téléphone tant que Triat est ouvert")
        self.sync_switch.set_active(bool(self._engine.settings.get("sync_enabled")))
        self.sync_switch.connect("notify::active", self._on_sync_toggled)
        group.add(self.sync_switch)

        self.sync_address = Adw.ActionRow(title="Adresse du bureau")
        self.sync_address.add_css_class("property")
        group.add(self.sync_address)

        pair = Adw.ActionRow(
            title="Nouveau téléphone",
            subtitle="Affiche un code à saisir sur le téléphone")
        self.pair_button = Gtk.Button(label="Associer un téléphone",
                                      valign=Gtk.Align.CENTER)
        self.pair_button.add_css_class("suggested-action")
        self.pair_button.add_css_class("pill")
        self.pair_button.connect("clicked", self._begin_pairing)
        pair.add_suffix(self.pair_button)
        group.add(pair)
        page.add(group)

        self.devices_group = Adw.PreferencesGroup(
            title="Téléphones associés",
            description="Oublier un téléphone l'empêche de se synchroniser "
                        "jusqu'à un nouvel appairage.")
        page.add(self.devices_group)

        cookies = Adw.PreferencesGroup(title="Session YouTube")
        cookies.add(self._switch_row(
            "Partager la session YouTube avec le téléphone",
            "La session part chiffrée vers les téléphones associés, "
            "seulement sur le Wi-Fi local",
            "sync_share_cookies"))
        page.add(cookies)

        self._refresh_sync()
        self._refresh_devices()
        return page

    def _on_sync_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        on = row.get_active()
        listening = self._engine.set_sync_enabled(on)
        if on and not listening:
            # Aucun port libre : on remet l'interrupteur à sa place.
            self.add_toast(Adw.Toast(title="Synchro impossible : port indisponible"))
            row.set_active(False)
            return
        self._refresh_sync()

    def _refresh_sync(self) -> None:
        """Adresse affichée et bouton d'appairage selon l'état du serveur."""
        sync = self._engine.sync
        running = bool(sync.running)
        self.pair_button.set_sensitive(running)
        if not running:
            self.sync_address.set_subtitle(
                "Serveur non démarré" if self.sync_switch.get_active()
                else "Activez la synchro pour la voir")
            return
        addresses = local_addresses() or ["adresse inconnue"]
        self.sync_address.set_subtitle(
            " · ".join(f"{ip}:{sync.port}" for ip in addresses))

    def _refresh_devices(self) -> None:
        for row in self._device_rows:
            self.devices_group.remove(row)
        self._device_rows = []
        devices = self._engine.sync.devices()
        if not devices:
            row = Adw.ActionRow(title="Aucun téléphone associé")
            row.add_css_class("dim-label")
            self.devices_group.add(row)
            self._device_rows.append(row)
            return
        for device_id, info in sorted(
                devices.items(), key=lambda item: item[1].get("paired_at") or 0):
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(info.get("name") or "Téléphone"),
                subtitle=format_last_sync(info.get("last_sync")))
            forget = Gtk.Button(label="Oublier", valign=Gtk.Align.CENTER)
            forget.add_css_class("destructive-action")
            forget.add_css_class("pill")
            forget.connect("clicked", self._forget_device, device_id)
            row.add_suffix(forget)
            self.devices_group.add(row)
            self._device_rows.append(row)

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
            heading="Associer un téléphone",
            body="Sur le téléphone, ouvrez Triat, choisissez cet ordinateur "
                 "puis saisissez ce code.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      halign=Gtk.Align.CENTER)
        self.pairing_code_label = Gtk.Label(
            label=format_pairing_code(code),
            css_classes=["triat-pair-code"])
        self.pairing_code_label.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Code d'appairage : " + " ".join(code)])
        self.pairing_countdown = Gtk.Label(
            label=format_countdown(self._pairing_left),
            css_classes=["triat-mono", "triat-dim"])
        box.append(self.pairing_code_label)
        box.append(self.pairing_countdown)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Annuler")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("closed", self._on_pairing_closed)
        self._pairing_dialog = dialog
        self._pairing_timer = GLib.timeout_add_seconds(1, self._tick_pairing)
        dialog.present(self)

    def _tick_pairing(self) -> bool:
        """Une seconde de plus : appareil arrivé, ou temps écoulé."""
        if self._pairing_dialog is None:
            self._pairing_timer = 0
            return GLib.SOURCE_REMOVE
        devices = self._engine.sync.devices()
        if set(devices) - self._pairing_known:
            self._pairing_timer = 0
            self._refresh_devices()
            self.add_toast(Adw.Toast(title="Téléphone associé"))
            self._close_pairing()
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
        page = Adw.PreferencesPage(title="Système",
                                   icon_name="triat-downloads-symbolic")
        group = Adw.PreferencesGroup(title="Vie privée")
        group.add(self._switch_row(
            "Session privée",
            "N'enregistre rien dans l'historique d'écoute",
            "private_session"))

        wipe = Adw.ActionRow(
            title="Effacer toutes mes données",
            subtitle="Bibliothèque, playlists, historique et cookies")
        button = Gtk.Button(label="Tout effacer", valign=Gtk.Align.CENTER)
        button.add_css_class("destructive-action")
        button.add_css_class("pill")
        button.connect("clicked", self._confirm_wipe)
        wipe.add_suffix(button)
        group.add(wipe)
        page.add(group)
        return page

    def _confirm_wipe(self, _button) -> None:
        dialog = Adw.AlertDialog(
            heading="Effacer toutes vos données ?",
            body="Bibliothèque, playlists, historique et cookies seront "
                 "supprimés. Cette action est irréversible.")
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("wipe", "Tout effacer")
        dialog.set_response_appearance("wipe", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_wipe_response)
        dialog.present(self)

    def _on_wipe_response(self, _dialog, response: str) -> None:
        if response != "wipe":
            return
        self._engine.library.wipe()
        self._engine.cookies.clear()
        self._after_cookie_change()
        if self._on_change is not None:
            self._on_change("wiped", True)

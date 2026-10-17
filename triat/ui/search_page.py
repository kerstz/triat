"""Page Recherche (screens.md §2, maquette `Search.dc.html`).

Puces Tout / Titres / Artistes / Playlists, carte « Meilleur résultat »,
cinq premiers titres, artistes et playlists locales. Albums et Clips de la
maquette sont absents : YouTube Music ne répond plus aux recherches
filtrées (voir `core/search_results.py`).
"""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk

from ..core import search_results
from .track_list import TrackList
from .widgets.cards import ArtistCard, PlaylistCard, seed_for
from .widgets.common import EmptyState, SectionTitle, label
from .widgets.glyph import GlyphArt
from ..i18n import _, nformat

log = logging.getLogger(__name__)

SOURCES = (("youtube", "YouTube"), ("soundcloud", "SoundCloud"),
           ("both", _("Les deux")))
SC_TOP = 5
SC_UNREACHABLE = _("SoundCloud injoignable")

# Fragments d'erreurs réseau (souvent en anglais : yt-dlp, urllib, socket).
_NETWORK_HINTS = ("urlopen", "network", "resolution", "resolve", "connection",
                  "connect", "timed out", "timeout", "unreachable", "ssl",
                  "temporary failure", "no route", "offline", "http error",
                  "getaddrinfo", "name or service")


def friendly_error(error: str) -> tuple[str, str]:
    """Titre et détail français pour une erreur de recherche brute."""
    low = (error or "").lower()
    if any(hint in low for hint in _NETWORK_HINTS):
        return (_("Pas de connexion"),
                _("YouTube est injoignable. Vérifie ta connexion, puis réessaie."))
    return (_("Recherche impossible"),
            _("YouTube n'a pas répondu comme prévu. Réessaie dans un instant."))


FILTERS = (("all", _("Tout")), ("tracks", _("Titres")),
           ("artists", _("Artistes")), ("playlists", _("Playlists")))
TOP_TRACKS = 5
BADGES = {"artist": _("Artiste"), "playlist": _("Playlist"), "track": _("Titre")}


def _flow() -> Gtk.FlowBox:
    flow = Gtk.FlowBox(homogeneous=False, column_spacing=8, row_spacing=8)
    flow.set_selection_mode(Gtk.SelectionMode.NONE)
    flow.set_max_children_per_line(8)
    flow.set_valign(Gtk.Align.START)
    return flow


class BestCard(Gtk.Button):
    """Carte « Meilleur résultat » : trame ronde, nom en Doto, badge."""

    def __init__(self) -> None:
        super().__init__()
        self.add_css_class("triat-tile")
        self.add_css_class("triat-best")
        self.set_size_request(400, 280)
        self.set_valign(Gtk.Align.START)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.art = GlyphArt(grid=15, size=128, radius=64.0)
        self.art.add_css_class("round")
        self.art.set_size_request(128, 128)
        self.art.set_halign(Gtk.Align.START)
        box.append(self.art)
        self.name = label("", "triat-display", "sm", ellipsize=3,
                          max_width_chars=16)
        box.append(self.name)
        meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.badge = label("", "triat-badge")
        meta.append(self.badge)
        self.detail = label("", "tile-detail", ellipsize=3, max_width_chars=28)
        meta.append(self.detail)
        box.append(meta)
        self.set_child(box)
        self.best = None

    def show(self, best) -> None:
        self.best = best
        if best.kind == "track":
            self.art.set_track(best.ref)
        else:
            self.art.set_seed(seed_for(best.title))
        self.name.set_text(best.title)
        self.badge.set_text(BADGES.get(best.kind, ""))
        self.detail.set_text(best.detail)
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             [_("Meilleur résultat : {title}, {badge}").format(
                                 title=best.title, badge=BADGES.get(best.kind, ""))])


class SearchPage(Gtk.Box):
    """Recherche et résultats (screens.md §2)."""

    def __init__(self, engine, on_play, on_action=None, on_artist=None,
                 on_playlist=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("triat-content")
        self._engine = engine
        self._on_play = on_play
        self._on_action = on_action
        self._on_artist = on_artist
        self._on_playlist = on_playlist
        self.results_data = None
        # Numéro de la dernière recherche : une réponse tardive d'une
        # requête précédente ne doit pas écraser la plus récente.
        self._seq = 0
        self._last_query = ""
        # Recherche SoundCloud : jeton propre, résultats et état à part.
        self._sc_seq = 0
        self._yt_pending = False
        self._yt_error: str | None = None
        self._sc_pending = False
        self._sc_error: str | None = None
        self._sc_all: list = []              # ScTrack, avant filtrage
        self._sc_shown: list = []            # ScTrack affichés

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        column.set_margin_bottom(24)

        self.entry = Gtk.SearchEntry(
            placeholder_text=_("Titre, artiste, ou URL YouTube collée…"),
            hexpand=True)
        # Au plus 560 px, mais sans minimum imposé : la fenêtre tuilée
        # peut descendre sous 500 px.
        self.entry.set_size_request(200, -1)
        self.entry.set_halign(Gtk.Align.FILL)
        self.entry.connect("activate", lambda *_: self.run_search())
        self.entry.connect("stop-search", self._on_escape)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(keys)
        clamp = Adw.Clamp(maximum_size=560, tightening_threshold=560,
                          child=self.entry)
        clamp.set_halign(Gtk.Align.START)
        clamp.set_hexpand(True)
        column.append(clamp)

        # --- Puces de filtre ---
        self.chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.chips.update_property([Gtk.AccessibleProperty.LABEL], [_("Filtres")])
        self._chip_buttons: dict[str, Gtk.ToggleButton] = {}
        first = None
        for key, text in FILTERS:
            button = Gtk.ToggleButton()
            button.add_css_class("triat-chip")
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            dot = Gtk.Box()
            dot.add_css_class("triat-chip-dot")
            dot.set_valign(Gtk.Align.CENTER)
            inner.append(dot)
            inner.append(Gtk.Label(label=text))
            button.set_child(inner)
            button.update_property([Gtk.AccessibleProperty.LABEL], [text])
            if first is None:
                first = button
                button.set_active(True)
            else:
                button.set_group(first)
            button.connect("toggled", self._on_chip, key)
            self._chip_buttons[key] = button
            self.chips.append(button)
        self.chips.set_visible(False)
        column.append(self.chips)

        # --- Source : visible seulement si SoundCloud est activé ---
        self.source_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                  spacing=8)
        self.source_row.update_property([Gtk.AccessibleProperty.LABEL],
                                        [_("Source de recherche")])
        self.source_row.append(label(_("Source :"), "triat-label", "triat-dim"))
        self._source_buttons: dict[str, Gtk.ToggleButton] = {}
        self._source: str | None = None        # choix de session, non persisté
        first = None
        for key, text in SOURCES:
            button = Gtk.ToggleButton(label=text)
            button.add_css_class("triat-chip")
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect("toggled", self._on_source, key)
            self._source_buttons[key] = button
            self.source_row.append(button)
        self.source_row.set_visible(False)
        column.append(self.source_row)
        self.connect("map", lambda *_: self.refresh_sources())

        # --- État vide : historique, puis messages ---
        self.empty = EmptyState(
            _("Aucune recherche"),
            _("Tape un titre, un artiste, ou colle une URL youtube.com."))
        column.append(self.empty)
        self.history_title = SectionTitle(_("Recherches récentes"))
        self.history = _flow()
        column.append(self.history_title)
        column.append(self.history)

        # --- Résultats, une page par filtre ---
        self.views = Gtk.Stack(vexpand=True)
        self.views.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.views.set_visible(False)
        column.append(self.views)

        # Tout
        overview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        best_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        best_column.append(SectionTitle(_("Meilleur résultat")))
        self.best_card = BestCard()
        self.best_card.connect("clicked", self._open_best)
        best_column.append(self.best_card)
        self.best_column = best_column
        top.append(best_column)
        tracks_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                                hexpand=True)
        tracks_column.append(SectionTitle(
            _("Titres"), _("Tout voir"), lambda: self.select_filter("tracks")))
        self.top_tracks = TrackList()
        self.top_tracks.set_vexpand(False)
        self.top_tracks.connect("track-activated", self._on_activated)
        self.top_tracks.connect("track-action", self._relay_action)
        tracks_column.append(self.top_tracks)
        top.append(tracks_column)
        self.tracks_column = tracks_column
        self.top = top
        overview.append(top)

        self.sc_overview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                   spacing=14)
        self.sc_overview.append(SectionTitle(
            _("SoundCloud"), _("Tout voir"), lambda: self.select_filter("tracks")))
        self.sc_overview_status = label("", "triat-dim")
        self.sc_overview.append(self.sc_overview_status)
        self.sc_top = self._sc_list()
        self.sc_overview.append(self.sc_top)
        self.sc_overview.set_visible(False)
        overview.append(self.sc_overview)

        self.artists_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                       spacing=14)
        self.artists_section.append(SectionTitle(
            _("Artistes"), _("Tout voir"), lambda: self.select_filter("artists")))
        self.overview_artists = _flow()
        self.artists_section.append(self.overview_artists)
        overview.append(self.artists_section)

        self.playlists_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                         spacing=14)
        self.playlists_section.append(SectionTitle(_("Tes playlists")))
        self.overview_playlists = _flow()
        self.playlists_section.append(self.overview_playlists)
        overview.append(self.playlists_section)
        self.views.add_named(self._scrolled(overview), "all")

        # Titres
        self.results = TrackList()
        self.results.connect("track-activated", self._on_activated)
        self.results.connect("track-action", self._relay_action)
        tracks_view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        tracks_view.append(self.results)
        self.sc_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                  spacing=14)
        self.sc_section.append(SectionTitle(_("SoundCloud")))
        self.sc_status = label("", "triat-dim")
        self.sc_section.append(self.sc_status)
        self.sc_results = self._sc_list()
        self.sc_section.append(self.sc_results)
        self.sc_section.set_visible(False)
        tracks_view.append(self.sc_section)
        self.tracks_view = tracks_view
        self.views.add_named(self._scrolled(tracks_view), "tracks")

        # Artistes, playlists
        self.all_artists = _flow()
        self.views.add_named(self._scrolled(self.all_artists), "artists")
        self.all_playlists = _flow()
        self.views.add_named(self._scrolled(self.all_playlists), "playlists")

        self.append(column)
        self._show_history()

    def _sc_list(self) -> TrackList:
        view = TrackList()
        view.set_vexpand(False)
        view.connect("track-activated", self._on_sc_activated)
        view.connect("track-action", self._relay_sc_action)
        return view

    @staticmethod
    def _scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(child)
        return scroller

    # ---- Saisie ----------------------------------------------------------

    def focus_entry(self) -> None:
        self.entry.grab_focus()

    def _on_escape(self, _entry) -> None:
        self._seq += 1            # une réponse en vol n'a plus d'objet
        self._sc_seq += 1
        self.entry.set_text("")
        self.results_data = None
        self._show_history()

    def _on_entry_key(self, _ctrl, keyval, _code, _state) -> bool:
        if keyval != Gdk.KEY_Down or not self.views.get_visible():
            return False
        target = (self.top_tracks if self.views.get_visible_child_name() == "all"
                  else self.views.get_visible_child())
        target.child_focus(Gtk.DirectionType.TAB_FORWARD)
        return True

    # ---- Source ----------------------------------------------------------

    def _sc_client(self):
        """Client SoundCloud du moteur, ou None s'il est désactivé."""
        settings = getattr(self._engine, "settings", None)
        if settings is None or not settings.get("soundcloud_enabled"):
            return None
        return getattr(self._engine, "soundcloud", None)

    def refresh_sources(self) -> None:
        """Montre la rangée « Source » seulement si SoundCloud est actif."""
        enabled = self._sc_client() is not None
        self.source_row.set_visible(enabled)
        if enabled:
            key = self.current_source()
            button = self._source_buttons[key]
            if not button.get_active():
                self._set_source_button(key)

    def current_source(self) -> str:
        """youtube | soundcloud | both : choix de session, sinon réglage."""
        if self._sc_client() is None:
            return "youtube"
        if self._source in self._source_buttons:
            return self._source
        settings = getattr(self._engine, "settings", None)
        effective = settings.effective_content_source() if settings else "youtube"
        return effective if effective in self._source_buttons else "youtube"

    def _set_source_button(self, key: str) -> None:
        # Sans rejouer la recherche : seul `_on_source` en décide.
        self._syncing_source = True
        try:
            self._source_buttons[key].set_active(True)
        finally:
            self._syncing_source = False

    _syncing_source = False

    def _on_source(self, button, key: str) -> None:
        if not button.get_active() or self._syncing_source:
            return
        if key == self._source:
            return
        self._source = key
        if self.results_data is not None and self._last_query:
            self.run_search(self._last_query)

    # ---- Recherche -------------------------------------------------------

    def run_search(self, query: str | None = None) -> None:
        if query is not None:
            self.entry.set_text(query)
        query = self.entry.get_text().strip()
        if not query:
            return
        self.refresh_sources()
        self._remember(query)
        self._seq += 1
        self._sc_seq += 1
        seq, sc_seq = self._seq, self._sc_seq
        self._last_query = query
        source = self.current_source()
        self._yt_pending = source != "soundcloud"
        self._sc_pending = source != "youtube"
        self._yt_error = self._sc_error = None
        self._sc_all = []
        self.results_data = None
        self._show_message(_("Recherche en cours…"), "", busy=True)

        if self._sc_pending:
            self._search_soundcloud(query, sc_seq)
        if self._yt_pending:
            def deliver(results, error) -> None:
                if seq == self._seq:
                    self._on_results(results, error)

            self._engine.search_all_async(query, deliver)

    def _search_soundcloud(self, query: str, token: int) -> None:
        """Recherche SoundCloud dans un fil, avec son propre jeton."""
        client = self._sc_client()

        def worker() -> None:
            tracks, error = [], None
            try:
                found = client.search_tracks(query, limit=20)
                tracks = list(found[0] if isinstance(found, tuple) else found)
            except Exception as exc:
                log.debug("Recherche SoundCloud : %s", exc)
                error = str(exc) or SC_UNREACHABLE
            GLib.idle_add(self._sc_done, token, tracks, error)

        if client is None:
            self._sc_done(token, [], SC_UNREACHABLE)
            return
        threading.Thread(target=worker, name="recherche-sc", daemon=True).start()

    def _sc_done(self, token: int, tracks: list, error: str | None) -> bool:
        if token == self._sc_seq:
            self._sc_pending = False
            self._sc_error = error
            self._sc_all = tracks
            self._render()
        return GLib.SOURCE_REMOVE

    def _remember(self, query: str) -> None:
        settings = getattr(self._engine, "settings", None)
        if settings is None or settings.get("private_session"):
            return
        settings.set("search_history", search_results.remember_query(
            settings.get("search_history"), query))

    # ---- Résultats -------------------------------------------------------

    def _on_results(self, results, error: str | None) -> None:
        self._yt_pending = False
        self._yt_error = error
        self._yt_results = None if error else results
        self._render()

    _yt_results = None

    def _hide_unplayable(self) -> bool:
        settings = getattr(self._engine, "settings", None)
        return bool(settings and settings.get("soundcloud_hide_unplayable"))

    def _render(self) -> None:
        """Affiche ce qui est arrivé ; YouTube ne dépend jamais de SoundCloud."""
        source = self.current_source()
        yt = self._yt_results if source != "soundcloud" else None
        sc_wanted = source != "youtube"
        sc_visible = [t for t in self._sc_all
                      if t.playable or not self._hide_unplayable()] \
            if sc_wanted else []
        have_yt = yt is not None and not yt.empty
        if not (have_yt or sc_visible):
            if self._yt_pending or self._sc_pending:
                return                        # encore en cours : pas de verdict
            if self._yt_error and source != "soundcloud":
                title, detail = friendly_error(self._yt_error)
                self._show_message(title, detail, action=_("Réessayer"),
                                   on_action=lambda: self.run_search(
                                       self._last_query))
            elif self._sc_error and sc_wanted:
                self._show_message(SC_UNREACHABLE,
                                   _("Réessaie dans un instant."),
                                   action=_("Réessayer"),
                                   on_action=lambda: self.run_search(
                                       self._last_query))
            else:
                self._show_message(_("Aucun résultat"), _("Essaie d'autres mots."))
            return
        if not have_yt:
            yt = search_results.build(self._last_query, [])
        yt.sc_tracks = sc_visible
        self.results_data = yt
        self._sc_shown = sc_visible
        tracks = yt.tracks
        self.results.set_tracks(tracks)
        self.results.set_visible(bool(tracks))
        self.top_tracks.set_tracks(tracks[:TOP_TRACKS])
        self.tracks_column.set_visible(bool(tracks))

        self.best_column.set_visible(yt.best is not None)
        if yt.best is not None:
            self.best_card.show(yt.best)
        self.top.set_visible(bool(tracks) or yt.best is not None)

        self._fill(self.overview_artists,
                   [ArtistCard(n, c) for n, c in yt.artists[:6]])
        self._fill(self.all_artists,
                   [ArtistCard(n, c) for n, c in yt.artists])
        self.artists_section.set_visible(bool(yt.artists))
        self._fill(self.overview_playlists,
                   [PlaylistCard(p) for p in yt.playlists[:6]])
        self._fill(self.all_playlists,
                   [PlaylistCard(p) for p in yt.playlists])
        self.playlists_section.set_visible(bool(yt.playlists))
        self._chip_buttons["playlists"].set_sensitive(bool(yt.playlists))
        self._chip_buttons["artists"].set_sensitive(bool(yt.artists))

        self._render_soundcloud(sc_wanted)

        self.empty.set_visible(False)
        self.history_title.set_visible(False)
        self.history.set_visible(False)
        self.chips.set_visible(True)
        self.views.set_visible(True)
        self.select_filter("all")

    def _render_soundcloud(self, wanted: bool) -> None:
        """Sections « SoundCloud » : titres, état de chargement ou d'échec."""
        for box, status, view, limit in (
                (self.sc_section, self.sc_status, self.sc_results, None),
                (self.sc_overview, self.sc_overview_status, self.sc_top,
                 SC_TOP)):
            box.set_visible(wanted)
            shown = self._sc_shown[:limit]
            view.set_tracks([t.ref for t in shown])
            view.set_unplayable({t.ref.video_id: t.reason or "blocked"
                                 for t in shown if not t.playable})
            view.set_visible(bool(shown))
            if self._sc_pending:
                text = _("Recherche SoundCloud…")
            elif self._sc_error:
                text = SC_UNREACHABLE
            elif not shown:
                text = _("Aucun titre sur SoundCloud.")
            else:
                text = ""
            status.set_text(text)
            status.set_visible(bool(text))

    def _fill(self, flow: Gtk.FlowBox, cards) -> None:
        flow.remove_all()
        for card in cards:
            if isinstance(card, ArtistCard):
                card.connect("clicked", self._open_artist)
            else:
                card.connect("clicked", self._open_playlist)
            flow.append(card)

    def select_filter(self, key: str) -> None:
        button = self._chip_buttons.get(key)
        if button is None:
            return
        if not button.get_active():
            button.set_active(True)          # déclenche _on_chip
        else:
            self.views.set_visible_child_name(key)

    def _on_chip(self, button, key: str) -> None:
        if button.get_active():
            self.views.set_visible_child_name(key)

    def _show_message(self, title: str, detail: str, busy: bool = False,
                      action: str | None = None, on_action=None) -> None:
        self.views.set_visible(False)
        self.chips.set_visible(False)
        self.history_title.set_visible(False)
        self.history.set_visible(False)
        self.empty.set_visible(True)
        self.empty.set_message(title, detail, busy=busy, action=action,
                               on_action=on_action)

    def _show_history(self) -> None:
        self._show_message(
            _("Aucune recherche"),
            _("Tape un titre, un artiste, ou colle une URL youtube.com."))
        settings = getattr(self._engine, "settings", None)
        history = search_results.clean_history(
            settings.get("search_history") if settings else [])
        self.history.remove_all()
        for query in history:
            chip = Gtk.Button(label=query)
            chip.add_css_class("triat-chip")
            chip.connect("clicked", lambda _b, q=query: self.run_search(q))
            self.history.append(chip)
        self.history_title.set_visible(bool(history))
        self.history.set_visible(bool(history))

    # ---- Actions ---------------------------------------------------------

    def _open_best(self, card) -> None:
        best = card.best
        if best is None:
            return
        if best.kind == "artist" and self._on_artist:
            self._on_artist(best.title)
        elif best.kind == "playlist" and self._on_playlist:
            self._on_playlist(best.playlist_id)
        elif best.kind == "track" and self.results_data:
            self._on_play(self.results_data.tracks, 0)

    def _open_artist(self, card) -> None:
        if self._on_artist:
            self._on_artist(card.artist)

    def _open_playlist(self, card) -> None:
        if self._on_playlist and card.playlist_id is not None:
            self._on_playlist(card.playlist_id)

    def _on_activated(self, _list, position: int) -> None:
        refs = self.results_data.tracks if self.results_data else []
        if 0 <= position < len(refs):
            self._on_play(refs, position)

    def _on_sc_activated(self, _list, position: int) -> None:
        """Clic sur un titre SoundCloud : file des titres lisibles, sans radio."""
        shown = list(self._sc_shown)
        if not (0 <= position < len(shown)) or not shown[position].playable:
            return
        playable = [t for t in shown if t.playable]
        start = playable.index(shown[position])
        skipped = len(shown) - len(playable)
        if skipped:
            self._announce(nformat("{n} titre ignoré", "{n} titres ignorés", skipped))
        self._on_play([t.ref for t in playable], start)

    def _announce(self, message: str) -> None:
        """Note visible et annoncée aux lecteurs d'écran."""
        self.sc_status.set_text(message)
        self.sc_status.set_visible(True)
        announce = getattr(self, "announce", None)
        if callable(announce):
            try:
                announce(message, Gtk.AccessibleAnnouncementPriority.MEDIUM)
            except Exception as exc:        # GTK < 4.14 ou pas de racine
                log.debug("Annonce impossible : %s", exc)

    def _relay_sc_action(self, _list, action: str, position: int) -> None:
        shown = self._sc_shown
        if self._on_action and 0 <= position < len(shown):
            self._on_action(action, shown[position].ref)

    def _relay_action(self, _list, action: str, position: int) -> None:
        refs = self.results_data.tracks if self.results_data else []
        if self._on_action and 0 <= position < len(refs):
            self._on_action(action, refs[position])


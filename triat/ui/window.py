"""Fenêtre principale de Triat.

Structure conforme à design/GTK.md §1 : navigation (rail ou libellés) +
pages, bandeau de lecture persistant, « Lecture en cours » et Immersion en
plein écran par-dessus. Tout le contenu vient de l'Engine.
"""

from __future__ import annotations

import datetime
import os
import threading
import time

from gi.repository import Adw, GLib, GObject, Gtk

from .artist_page import ArtistPage
from .downloads_page import DownloadsPage
from .library_page import LibraryPage
from .now_playing import NowPlayingView
from .onboarding import OnboardingView
from .immersion import ImmersionView
from .search_page import SearchPage
from .settings_dialog import CATEGORY_LABELS, SettingsDialog
from .track_actions import TrackActions
from ..core.display import canonical_key
from ..core.models import TrackRef
from .track_grid import TrackGrid
from .track_list import format_time
from .widgets.common import EmptyState, SectionTitle, accent_dot, label
from .widgets.cover import Cover
from .widgets.glyph import GlyphArt
from ..core.home_feed import HomeFeed
from ..core.mixes import MixStore, filter_blocked
from ..core.reco import is_blocked
from .widgets.dot_progress import DotProgressBar
from .widgets.dot_volume import DotVolume
from ..i18n import _, nformat


# Triat affiche ses dates en français sans dépendre de la locale du système.
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
         "dimanche")
MOIS = ("janvier", _("février"), "mars", "avril", "mai", "juin", "juillet",
        _("août"), "septembre", "octobre", "novembre", _("décembre"))

# Saut des flèches gauche / droite, en secondes.
SEEK_STEP = 5.0

# Aide des raccourcis (Ctrl+?) : titre, accélérateur.
SHORTCUTS = (
    (_("Lecture / pause"), "space"),
    (_("Reculer de 5 s"), "Left"),
    (_("Avancer de 5 s"), "Right"),
    (_("Piste précédente"), "<Shift>Left"),
    (_("Piste suivante"), "<Shift>Right"),
    (_("Rechercher"), "<Primary>f"),
    (_("Lecture en cours"), "<Primary>l"),
    (_("Plein écran (Immersion)"), "<Primary>i"),
    (_("Mini-lecteur"), "<Primary>m"),
    (_("Réglages"), "<Primary>comma"),
    (_("Fermer la vue ouverte"), "Escape"),
    (_("Raccourcis clavier"), "<Primary>question"),
    (_("Quitter"), "<Primary>q"),
)

# « Radio » n'est plus une page : c'est une action (tuile de l'accueil).
NAV_ITEMS = (
    ("home", _("Accueil"), "triat-home-symbolic"),
    ("search", _("Recherche"), "triat-search-symbolic"),
    ("library", _("Bibliothèque"), "triat-library-symbolic"),
    ("downloads", _("Hors-ligne"), "triat-downloads-symbolic"),
)


def make_toast(message: str, timeout: int = 5) -> Adw.Toast:
    """Toast au texte littéral.

    `use-markup` vaut vrai par défaut : un titre contenant « & » ou « < »
    (« Simon & Garfunkel », une URL) donnait un toast vide."""
    return Adw.Toast(title=message, timeout=timeout, use_markup=False)


def is_hyprland() -> bool:
    """Sous Hyprland la fenêtre reste nue : le gestionnaire la tuile et la
    déplace. Ailleurs, on fournit une poignée et les boutons de fenêtre."""
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return True
    return "hyprland" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()


def category_label(category: str) -> str:
    """Nom français d'une catégorie SponsorBlock."""
    return CATEGORY_LABELS.get(category, (category,))[0]


def overline(text: str) -> Gtk.Label:
    """Sur-titre espacé (DESIGN.md §3).

    L'espacement vient du CSS (`.spaced`, letter-spacing .5em) et non
    d'espaces insérés dans la chaîne : un lecteur d'écran épellerait
    « L E C T E U R » lettre par lettre.
    """
    return label(text, "triat-label", "triat-dim")


def greeting(now: datetime.datetime | None = None) -> str:
    hour = (now or datetime.datetime.now()).hour
    if hour < 12:
        return _("Bonjour")
    if hour < 18:
        return _("Bon après-midi")
    return _("Bonsoir")


def date_line(now: datetime.datetime | None = None) -> str:
    today = now or datetime.datetime.now()
    day = JOURS[today.weekday()]
    return _("{day} {number} {month}").format(
        day=day.capitalize(), number=today.day, month=MOIS[today.month - 1])


class Sidebar(Gtk.Box):
    """Navigation : rail d'icônes sous 1 100 px, libellés au-dessus.

    La fenêtre est tuilée par Hyprland (950 px sur la moitié d'un
    ultrawide) : replier la navigation par-dessus le contenu, comme le
    faisait Adw.NavigationSplitView, masquait tout l'écran. Le rail reste.
    """

    WIDE = 208
    COMPACT = 60

    def __init__(self, on_navigate, on_settings=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("triat-sidebar")
        self._on_settings = on_settings or (lambda: None)
        self._on_navigate = on_navigate
        self._buttons: dict[str, Gtk.Button] = {}
        self._labels: list[Gtk.Widget] = []
        self._nav_contents: list[Gtk.Widget] = []
        self.compact = False
        self.set_size_request(self.WIDE, -1)

        # Logo : Doto, comme le Ndot de la barre, et le point « en lecture ».
        self.wordmark = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.wordmark.set_margin_start(12)
        self.wordmark.set_margin_top(4)
        self.wordmark.set_margin_bottom(22)
        self._logo = label(_("triat"), "triat-display", "nav")
        self.wordmark.append(self._logo)
        # En rail, le mot ne tient pas : le triangle en points de l'icône.
        self._mark = Gtk.Image.new_from_icon_name("triat-logo-symbolic")
        self._mark.set_pixel_size(20)
        self._mark.set_visible(False)
        self.wordmark.append(self._mark)
        self.live = accent_dot(6)
        self.live.set_valign(Gtk.Align.END)
        self.live.set_margin_bottom(6)
        self.wordmark.append(self.live)
        self.append(self.wordmark)

        nav = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for key, text, icon in NAV_ITEMS:
            button = self._nav_button(key, text, icon,
                                      lambda _b, k=key: self._on_navigate(k))
            self._buttons[key] = button
            nav.append(button)
        self.append(nav)

        self._playlists = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self._playlists.set_margin_top(22)
        heading = label(_("Playlists"), "triat-side-heading")
        heading.set_margin_start(12)
        heading.set_margin_bottom(6)
        self._playlists.append(heading)
        self._playlist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                     spacing=1)
        self._playlists.append(self._playlist_box)
        self.append(self._playlists)
        self.set_playlists([], None)

        spacer = Gtk.Box(vexpand=True)
        self.append(spacer)
        self.settings_button = self._nav_button(
            "settings", _("Réglages"), "triat-settings-symbolic",
            lambda *_: self._on_settings())
        self.append(self.settings_button)
        self._data_path = ""

        self.select("home")

    def set_compact(self, compact: bool) -> None:
        """Rail d'icônes (fenêtre étroite) ou navigation avec libellés."""
        self.compact = compact
        if compact:
            self.add_css_class("compact")
        else:
            self.remove_css_class("compact")
        self.set_size_request(self.COMPACT if compact else self.WIDE, -1)
        for widget in self._labels:
            widget.set_visible(not compact)
        # Rail : l'icône seule se centre dans le cercle actif, sinon elle
        # reste collée à gauche du bouton de 40 px.
        for content in self._nav_contents:
            content.set_halign(Gtk.Align.CENTER if compact else Gtk.Align.FILL)
        self._logo.set_visible(not compact)
        self._mark.set_visible(compact)
        self.wordmark.set_halign(Gtk.Align.CENTER if compact else Gtk.Align.FILL)
        self.wordmark.set_margin_start(0 if compact else 12)
        self.wordmark.set_margin_top(10 if compact else 4)
        self._playlists.set_visible(not compact)
        for button in (*self._buttons.values(), self.settings_button):
            button.set_halign(Gtk.Align.CENTER if compact else Gtk.Align.FILL)

    def set_data_path(self, path: str) -> None:
        """Le dossier de données, en infobulle de Réglages : utile au
        diagnostic, bruit au quotidien."""
        home = GLib.get_home_dir()
        shown = "~" + path[len(home):] if path.startswith(home) else path
        self._data_path = shown
        self.settings_button.set_tooltip_text(_("Réglages\nDonnées : {shown}").format(shown=shown))

    def set_live(self, playing: bool) -> None:
        self.live.set_opacity(1.0 if playing else 0.0)

    def set_playlists(self, items: list[dict], on_open) -> None:
        """Raccourcis vers les playlists, sous la navigation."""
        while (child := self._playlist_box.get_first_child()) is not None:
            self._playlist_box.remove(child)
        self._playlists.set_visible(bool(items) and not self.compact)
        for item in items[:8]:
            button = Gtk.Button(halign=Gtk.Align.FILL)
            button.add_css_class("triat-side-playlist")
            row = Gtk.Box(spacing=8)
            row.append(label(item["name"], hexpand=True, ellipsize=3))
            row.append(label(str(item["item_count"]), "triat-mono"))
            button.set_child(row)
            button.set_tooltip_text(item["name"])
            button.connect("clicked", lambda _b, pid=item["id"]: on_open(pid))
            self._playlist_box.append(button)

    def _nav_button(self, key: str, text: str, icon: str,
                    callback) -> Gtk.Button:
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(18)
        content.append(image)
        name = label(text)
        content.append(name)
        self._labels.append(name)
        self._nav_contents.append(content)

        button = Gtk.Button(child=content)
        button.add_css_class("triat-nav-item")
        button.set_tooltip_text(text)
        button.update_property([Gtk.AccessibleProperty.LABEL], [text])
        button.connect("clicked", callback)
        return button

    def select(self, key: str) -> None:
        for name, button in self._buttons.items():
            if name == key:
                button.add_css_class("active")
            else:
                button.remove_css_class("active")
            button.update_state([Gtk.AccessibleState.SELECTED], [int(name == key)])


RECENT_SHOWN = 18


def dedupe_recent(tracks, limit: int = RECENT_SHOWN) -> list:
    """« Écouter à nouveau » : une seule fois par titre affiché (versions
    « Official Video », « Lyrics »… regroupées), dans l'ordre d'écoute."""
    seen, kept = set(), []
    for ref in tracks:
        key = canonical_key(ref.title, ref.artist)
        if key in seen:
            continue
        seen.add(key)
        kept.append(ref)
        if len(kept) >= limit:
            break
    return kept


class ResumeCard(Gtk.Box):
    """Tête de l'accueil : la piste en cours ou la dernière écoutée, en
    grande trame. C'est l'élément fort de l'écran ; le reste se tait."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
        self.add_css_class("triat-resume")
        self.art = GlyphArt(grid=19, size=148, radius=6.0)
        self.art.set_size_request(148, 148)
        self.art.set_valign(Gtk.Align.CENTER)
        self.append(self.art)

        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                        valign=Gtk.Align.CENTER, hexpand=True)
        self.kicker = label("", "triat-dim")
        words.append(self.kicker)
        self.title = label("", "triat-display", "md", ellipsize=3,
                           max_width_chars=34)
        words.append(self.title)
        self.artist = label("", "triat-body", "triat-dim", ellipsize=3)
        words.append(self.artist)
        # Les boutons passent à la ligne dans une fenêtre étroite.
        self.actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                   column_spacing=8, row_spacing=8,
                                   max_children_per_line=3)
        self.actions.set_halign(Gtk.Align.START)
        self.actions.set_margin_top(12)
        self.primary = Gtk.Button(label=_("Reprendre"))
        self.primary.add_css_class("suggested-action")
        self.radio = Gtk.Button(label=_("Ma radio"))
        self.radio.add_css_class("pill")
        self.radio.set_tooltip_text(_("Enchaîne sur ce que tu écoutes"))
        self.fullscreen = Gtk.Button(label=_("Plein écran"))
        self.fullscreen.add_css_class("pill")
        self.fullscreen.set_tooltip_text(_("Le clip, ou la matrice de points (Ctrl+I)"))
        for button in (self.primary, self.radio, self.fullscreen):
            self.actions.append(button)
        words.append(self.actions)
        self.append(words)
        self.ref = None

    def show(self, ref, playing: bool, is_current: bool) -> None:
        self.ref = ref
        if ref is None:
            self.art.set_seed(7)
            self.kicker.set_text(_("Rien n'a encore été écouté"))
            self.title.set_text(_("Cherche un titre pour commencer"))
            self.artist.set_text(_("Ou importe une playlist depuis la Bibliothèque."))
            self.primary.set_label(_("Rechercher"))
            self.radio.set_visible(False)
            self.fullscreen.set_visible(False)
            return
        self.art.set_track(ref)
        self.kicker.set_text(_("En lecture") if playing and is_current
                             else _("Reprendre"))
        self.title.set_text(ref.display_title)
        self.title.set_tooltip_text(ref.title)
        self.artist.set_text(ref.display_artist or "")
        self.primary.set_label(_("Pause") if playing and is_current
                               else _("Reprendre"))
        self.radio.set_visible(True)
        self.fullscreen.set_visible(True)


class MixCard(Gtk.Button):
    """Vignette de mix : matrice de points propre au mix, nom, artistes."""

    def __init__(self, mix, size: int = 148) -> None:
        super().__init__()
        self.add_css_class("triat-mix")
        self.mix = mix
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.art = GlyphArt(grid=13, size=size, radius=6.0)
        self.art.set_size_request(size, size)
        self.art.set_halign(Gtk.Align.START)
        box.set_size_request(size, -1)
        self.set_halign(Gtk.Align.START)
        self.art.set_seed(mix.seed)
        box.append(self.art)
        name = label(mix.title, "mix-name", ellipsize=3, max_width_chars=18)
        name.set_margin_top(8)
        box.append(name)
        box.append(label(mix.subtitle, "mix-detail", wrap=True, lines=2,
                         ellipsize=3, max_width_chars=20))
        self.set_child(box)
        # « Parce que… » : la raison d'abord, puis le détail.
        reason = getattr(mix, "reason", "")
        self.set_tooltip_text(_("{reason}\n{subtitle}").format(reason=reason, subtitle=mix.subtitle) if reason
                              else mix.subtitle)
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             [_("{title}, {subtitle}").format(title=mix.title, subtitle=mix.subtitle)])


class HomePage(Gtk.Box):
    """Accueil : reprendre, mixes, réécoute, playlists (screens.md §1)."""

    def __init__(self, engine=None, on_play=None, on_immersion=None,
                 on_search=None, feed=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("triat-content")
        self._engine = engine
        self._on_play = on_play
        self._on_immersion = on_immersion
        self._on_search = on_search
        self.mixes = []
        # Mixes figés pour la journée et accueil distant en cache : l'accueil
        # s'ouvre sur le local, le reste arrive ensuite (jamais sur le fil
        # GTK pour le réseau ni pour une grosse bibliothèque).
        self._store = MixStore(engine.library) if engine is not None else None
        if feed is None and engine is not None and hasattr(
                engine.recommender, "remote_home"):
            feed = HomeFeed(engine.library, engine.recommender)
        self._feed = feed
        self._building = False       # construction des mixes en arrière-plan
        self._build_dirty = False    # un refresh est arrivé pendant ce temps
        self._fetching = False       # requête distante en cours
        self._has_recent = False
        self._refresh_seq = 0        # jeton : un résultat périmé est ignoré

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        column.set_margin_bottom(28)

        self.resume = ResumeCard()
        self.resume.set_margin_top(6)
        self.resume.set_margin_bottom(18)
        column.append(self.resume)
        self.resume.primary.connect("clicked", self._on_primary)
        self.start_button = self.resume.radio
        if engine is not None:
            self.resume.radio.connect("clicked", lambda *_: engine.start_radio())
            self.resume.fullscreen.connect("clicked", self._start_immersion)
            engine.connect("track-changed", lambda *_: self._show_resume())
            engine.connect("state-changed", lambda *_: self._show_resume())
            # Synchro avec le téléphone, métadonnées complétées : l'accueil
            # se remet à jour s'il est à l'écran (sinon à la prochaine visite).
            engine.connect("library-changed", lambda *_: self._on_library_changed())
            # Signal absent d'un moteur factice : on s'en passe.
            if GObject.signal_lookup("data-wiped", type(engine)):
                engine.connect("data-wiped", lambda *_: self._on_data_wiped())

        # --- Mixes ---
        self.mixes_section = SectionTitle(_("Mixes pour toi"))
        column.append(self.mixes_section)
        self.mix_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                   homogeneous=True, column_spacing=4,
                                   row_spacing=10, min_children_per_line=2,
                                   max_children_per_line=8)
        self.mix_box.set_valign(Gtk.Align.START)
        self.mix_box.set_margin_bottom(18)
        column.append(self.mix_box)

        # --- Suggestions distantes (cache, puis réseau en arrière-plan) ---
        self.remote_title = SectionTitle(_("Suggestions"))
        self.remote_note = label("", "triat-dim", wrap=True)
        self.remote_grid = TrackGrid(cover_size=120)
        self.remote_grid.connect("item-activated", self._play_remote)
        self.remote_grid.set_margin_bottom(18)
        for widget in (self.remote_title, self.remote_note, self.remote_grid):
            widget.set_visible(False)
            column.append(widget)

        # --- Écouter à nouveau ---
        self.recent_title = SectionTitle(_("Écouter à nouveau"))
        column.append(self.recent_title)
        self.recent_grid = TrackGrid(cover_size=120)
        self.recent_grid.connect("item-activated", self._play_recent)
        self.recent_grid.set_margin_bottom(18)
        self.recent_empty = EmptyState(
            _("Rien pour l'instant"),
            _("Ton historique d'écoute apparaîtra ici. Il reste sur cet "
            "ordinateur."))
        column.append(self.recent_grid)
        column.append(self.recent_empty)

        # --- Playlists ---
        self.mixes_title = SectionTitle(_("Tes playlists"))
        column.append(self.mixes_title)
        self.mixes_grid = TrackGrid(cover_size=120)
        self.mixes_grid.connect("item-activated", self._play_mix)
        self.mixes_empty = EmptyState(
            _("Aucune playlist"),
            _("Importe un fichier de liens YouTube depuis la Bibliothèque "
            "pour créer ta première playlist."))
        column.append(self.mixes_grid)
        column.append(self.mixes_empty)

        scroller.set_child(column)
        self.append(scroller)

        self._playlist_ids: list[int] = []
        self.refresh()

    def _on_library_changed(self) -> None:
        if self.get_mapped():
            self.refresh()

    def _on_data_wiped(self) -> None:
        """Données effacées : caches d'accueil et mixes figés jetés."""
        if self._feed is not None:
            self._feed.reset()
        if self._store is not None:
            self._store.reset()
        self.refresh()

    def set_narrow(self, narrow: bool) -> None:
        self.resume.art.set_size_request(112 if narrow else 148,
                                         112 if narrow else 148)

    # ---- Contenu ---------------------------------------------------------

    def _show_resume(self) -> None:
        if self._engine is None:
            self.resume.show(None, False, False)
            return
        current = self._engine.current
        ref = current
        if ref is None:
            recent = self._engine.library.recent(limit=1)
            ref = recent[0] if recent else None
        playing = self._engine.state == "playing"
        is_current = (ref is not None and current is not None
                      and ref.video_id == current.video_id)
        self.resume.show(ref, playing, is_current)

    def refresh(self) -> None:
        """Recharge toutes les sections depuis la bibliothèque.

        Les requêtes lourdes (historique, pochettes de playlists, mixes) ne
        passent jamais par le fil GTK : un fil les calcule, le résultat est
        appliqué via idle_add, et ignoré s'il est périmé."""
        self._show_resume()
        if self._engine is None:
            self.recent_grid.set_visible(False)
            self.mixes_grid.set_visible(False)
            self.mixes_section.set_visible(False)
            return
        self._refresh_seq += 1
        if self._building:
            # Un calcul est en vol : il sera relancé à son retour.
            self._build_dirty = True
            return
        self._building = True
        seq = self._refresh_seq
        library, store = self._engine.library, self._store
        today = datetime.date.today()

        def worker() -> None:
            try:
                data = self._collect(library, store, today)
            except Exception:
                data = None
            GLib.idle_add(self._refresh_done, seq, data)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _collect(library, store, today) -> dict:
        recent = dedupe_recent(library.recent(limit=60), RECENT_SHOWN)
        covers = list(library.playlist_covers())
        mixes = None
        if store is not None:
            try:
                mixes = store.get_or_build(today, time.time())
            except Exception:
                mixes = None
        return {"recent": recent, "covers": covers, "mixes": mixes}

    def _refresh_done(self, seq: int, data) -> bool:
        self._building = False
        if self._build_dirty or seq != self._refresh_seq:
            self._build_dirty = False
            self.refresh()
            return GLib.SOURCE_REMOVE
        if data is not None and self._engine is not None:
            self._apply(data)
        return GLib.SOURCE_REMOVE

    def _apply(self, data: dict) -> None:
        library = self._engine.library
        if data["mixes"] is not None:
            self._render_mixes(data["mixes"])
        recent = data["recent"]
        self.recent_grid.set_items(recent)
        self.recent_grid.set_visible(bool(recent))
        self._has_recent = bool(recent)
        self._update_empty_states()

        # Chaque playlist est représentée par la pochette de sa première
        # piste : c'est ce que l'utilisateur reconnaît. Une seule requête.
        covers, badges, self._playlist_ids = [], {}, []
        for item in data["covers"]:
            if not item["first_id"] or not item["item_count"]:
                continue
            cover = TrackRef(video_id=item["first_id"], title=item["name"],
                             artist="", thumbnail=item["first_thumbnail"])
            covers.append(cover)
            badges[cover.video_id] = nformat(
                "{n} titre", "{n} titres", item["item_count"])
            self._playlist_ids.append(item["id"])

        self.mixes_grid.set_items(covers, badges)
        self.mixes_grid.set_visible(bool(covers))
        self.mixes_empty.set_visible(not covers)

        self._refresh_remote(library)

    def _update_empty_states(self) -> None:
        # Sans historique mais avec des mixes, la section vide n'apprend rien.
        self.recent_title.set_visible(self._has_recent or not self.mixes)
        self.recent_empty.set_visible(not self._has_recent and not self.mixes)

    # ---- Mixes -------------------------------------------------------------

    def _render_mixes(self, mixes) -> None:
        # Le filtre est appliqué à l'affichage : écarter un titre ne demande
        # pas de reconstruire les mixes figés.
        blocked = self._engine.library.blocked()
        self.mixes = filter_blocked(mixes, blocked)
        child = self.mix_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.mix_box.remove(child)
            child = following
        for mix in self.mixes:
            card = MixCard(mix)
            card.connect("clicked", self._play_generated, mix)
            self.mix_box.append(card)
        self.mixes_section.set_visible(bool(self.mixes))
        self.mix_box.set_visible(bool(self.mixes))

    # ---- Suggestions distantes ---------------------------------------------

    def reset_remote(self) -> None:
        """Cookies changés ou données effacées : cache jeté, rechargement."""
        if self._feed is not None:
            self._feed.reset()

    def _refresh_remote(self, library) -> None:
        feed = self._feed
        if feed is None:
            return
        self._render_remote(feed.cached(), library)
        if not self._fetching and feed.needs_refresh():
            self._fetching = True

            def worker() -> None:
                try:
                    snapshot = feed.refresh()
                except Exception:
                    snapshot = None
                GLib.idle_add(self._remote_done, snapshot)

            threading.Thread(target=worker, daemon=True).start()

    def _remote_done(self, snapshot) -> bool:
        self._fetching = False
        if snapshot is not None and self._engine is not None:
            self._render_remote(snapshot, self._engine.library)
        return GLib.SOURCE_REMOVE

    def _render_remote(self, snapshot, library) -> None:
        tracks, title = [], ""
        if snapshot is not None:
            blocked = library.blocked()
            for section in snapshot.sections:
                tracks = [t for t in section["tracks"]
                          if not is_blocked(t, blocked)][:18]
                if tracks:
                    title = section.get("title") or _("Suggestions")
                    break
        shown = bool(tracks)
        if shown:
            self.remote_title.title.set_text(title)
            self.remote_grid.set_items(tracks)
            note = (_("Selon ton compte YouTube Music")
                    if snapshot.source == "ytmusic"
                    else _("Suggestions YouTube, sans compte"))
            if snapshot.stale:
                age = max(0.0, time.time() - snapshot.fetched_at)
                ago = (_("{n} h").format(n=int(age // 3600)) if age < 48 * 3600
                       else _("{n} j").format(n=int(age // 86400)))
                note += _(" · hors ligne, mis à jour il y a {ago}").format(ago=ago)
            self.remote_note.set_text(note)
        for widget in (self.remote_title, self.remote_note, self.remote_grid):
            widget.set_visible(shown)

    def _play_remote(self, _grid, position: int) -> None:
        refs = self.remote_grid.items
        if self._on_play and 0 <= position < len(refs):
            self._on_play(refs, position)

    # ---- Lecture ---------------------------------------------------------

    def _on_primary(self, _button) -> None:
        engine, ref = self._engine, self.resume.ref
        if engine is None or ref is None:
            if self._on_search:
                self._on_search()
            return
        if engine.current is not None and engine.current.video_id == ref.video_id:
            engine.toggle_pause()
        elif self._on_play:
            self._on_play([ref], 0)

    def _play_generated(self, _button, mix) -> None:
        if self._on_play and mix.refs:
            self._on_play(mix.refs, 0)

    def _start_immersion(self, _button) -> None:
        """Plein écran tout de suite ; sans lecture, lance le mix du jour."""
        if self._engine.current is None and self.mixes and self._on_play:
            self._on_play(self.mixes[0].refs, 0)
        if self._on_immersion is not None:
            self._on_immersion()

    def _play_recent(self, _grid, position: int) -> None:
        refs = self.recent_grid.items
        if self._on_play and 0 <= position < len(refs):
            self._on_play(refs, position)

    def _play_mix(self, _grid, position: int) -> None:
        if not (self._on_play and 0 <= position < len(self._playlist_ids)):
            return
        refs = self._engine.library.playlist_items(self._playlist_ids[position])
        if refs:
            self._on_play(refs, 0)


class PlayerBar(Gtk.Box):
    """Bandeau de lecture : à plat, collé au bas de la fenêtre.

    La progression court sur toute la largeur, au-dessus. Dessous : la
    piste à gauche (cliquable, ouvre Lecture en cours), le transport au
    centre, le temps, le volume et le plein écran à droite.
    """

    def __init__(self, engine=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("triat-player-bar")
        self._engine = engine

        self.progress = DotProgressBar(step=5.0, radius=1.3, height=10,
                                       show_head=True)
        self.progress.set_hexpand(True)
        self.progress.set_margin_top(6)
        self.append(self.progress)

        row = Gtk.CenterBox()
        row.set_size_request(-1, 58)
        self.append(row)

        # --- gauche : pochette + titres ---
        self.cover = Cover(size=40, radius=4.0)
        self.cover.set_valign(Gtk.Align.CENTER)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                         valign=Gtk.Align.CENTER)
        self._title = label(_("Rien en lecture"), "now-title", ellipsize=3,
                            max_width_chars=30)
        self._artist = label(_("Choisis une piste"), "now-artist", ellipsize=3,
                             max_width_chars=30)
        titles.append(self._title)
        titles.append(self._artist)
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                          valign=Gtk.Align.CENTER)
        content.append(self.cover)
        content.append(titles)
        # Zone « ouvrir Lecture en cours » : un vrai bouton, focalisable au
        # clavier et annoncé par les lecteurs d'écran.
        self.expand_button = Gtk.Button(child=content, valign=Gtk.Align.CENTER)
        self.expand_button.add_css_class("flat")
        self.expand_button.add_css_class("triat-expand")
        self.expand_button.set_tooltip_text(_("Lecture en cours (Ctrl+L)"))
        self.expand_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Ouvrir Lecture en cours")])
        # Chargement : le moteur met 1 à 3 s à démarrer une piste.
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.spinner.set_visible(False)
        self.spinner.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Chargement")])
        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                       valign=Gtk.Align.CENTER)
        left.append(self.expand_button)
        left.append(self.spinner)
        row.set_start_widget(left)
        self._loading = False
        self._busy = False

        # --- centre : transport ---
        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.prev_button = self._icon_button("triat-previous-symbolic", _("Précédent"))
        self.play_button = self._icon_button("triat-play-symbolic", _("Lecture"),
                                             primary=True)
        self.next_button = self._icon_button("triat-next-symbolic", _("Suivant"))
        for button in (self.prev_button, self.play_button, self.next_button):
            transport.append(button)
        self.centre = transport
        row.set_center_widget(transport)

        # --- droite : temps, volume, plein écran ---
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                        valign=Gtk.Align.CENTER)
        self._elapsed = label("0:00", "triat-mono")
        self._total = label("0:00", "triat-mono")
        times = Gtk.Box(spacing=4)
        times.append(self._elapsed)
        times.append(label("/", "triat-mono"))
        times.append(self._total)
        right.append(times)
        self.volume_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        volume_icon = Gtk.Image.new_from_icon_name("triat-volume-symbolic")
        volume_icon.add_css_class("triat-dim")
        self.volume_box.append(volume_icon)
        self.volume = DotVolume()
        self.volume_box.append(self.volume)
        right.append(self.volume_box)
        self.fullscreen_button = Gtk.Button(icon_name="triat-fullscreen-symbolic")
        self.fullscreen_button.add_css_class("flat")
        self.fullscreen_button.add_css_class("circular")
        self.fullscreen_button.set_tooltip_text(_("Plein écran (Ctrl+I)"))
        self.fullscreen_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Plein écran")])
        right.append(self.fullscreen_button)
        row.set_end_widget(right)
        self.right = right
        self._times = times

        if engine is not None:
            self._connect(engine)
        else:
            for button in (self.prev_button, self.play_button, self.next_button):
                button.set_sensitive(False)

    # ---- Câblage ---------------------------------------------------------

    def _connect(self, engine) -> None:
        self.play_button.connect("clicked", lambda *_: engine.toggle_pause())
        self.next_button.connect("clicked", lambda *_: engine.next())
        self.prev_button.connect("clicked", lambda *_: engine.previous())
        self.progress.connect("seek-requested",
                              lambda _w, value: engine.seek(value))
        self._volume_handler = self.volume.connect(
            "volume-changed", lambda _w, value: engine.set_volume(value))
        self.volume.props.volume = float(engine.settings.get("volume"))
        engine.connect("options-changed", self._on_options)
        engine.connect("busy", self._on_busy)

        engine.connect("track-changed", self._on_track)
        engine.connect("state-changed", self._on_state)
        engine.connect("position-changed", self._on_position)
        engine.connect("duration-changed", self._on_duration)
        engine.connect("queue-changed", lambda *_: self._refresh_buttons())
        self._refresh_buttons()

    def _icon_button(self, icon: str, name: str,
                     primary: bool = False) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon)
        button.add_css_class("circular")
        button.add_css_class("suggested-action" if primary else "flat")
        button.set_tooltip_text(name)
        button.update_property([Gtk.AccessibleProperty.LABEL], [name])
        size = 40 if primary else 34
        button.set_size_request(size, size)
        button.set_valign(Gtk.Align.CENTER)
        return button

    # ---- Réactions -------------------------------------------------------

    def _on_track(self, _engine, ref) -> None:
        if ref is None:
            self._title.set_text(_("Rien en lecture"))
            self._artist.set_text(_("Choisis une piste"))
            self.cover.clear()
            self.progress.clear()
            self._elapsed.set_text("0:00")
            self._total.set_text("0:00")
            return
        self._title.set_text(ref.display_title)
        self._title.set_tooltip_text(ref.title)
        self._artist.set_text(ref.display_artist or "—")
        self.cover.set_track(ref)
        self.progress.props.position = 0.0
        self.progress.props.duration = ref.duration or 0.0
        self._total.set_text(format_time(ref.duration))
        self._refresh_buttons()

    def _on_state(self, _engine, state: str) -> None:
        playing = state == "playing"
        self.play_button.set_icon_name(
            "triat-pause-symbolic" if playing
            else "triat-play-symbolic")
        self.play_button.set_sensitive(state != "idle")
        name = _("Pause") if playing else _("Lecture")
        self.play_button.set_tooltip_text(name)
        self.play_button.update_property([Gtk.AccessibleProperty.LABEL], [name])
        self._loading = state == "loading"
        self._update_spinner()

    def _on_busy(self, _engine, busy: bool) -> None:
        self._busy = busy
        self._update_spinner()

    def _update_spinner(self) -> None:
        active = self._loading or self._busy
        self.spinner.set_visible(active)
        self.spinner.set_spinning(active)

    def _on_options(self, engine) -> None:
        """Volume changé ailleurs (Lecture en cours, MPRIS) : la barre suit,
        sans rappeler le moteur."""
        value = float(engine.settings.get("volume"))
        self.volume.handler_block(self._volume_handler)
        try:
            self.volume.props.volume = value
        finally:
            self.volume.handler_unblock(self._volume_handler)

    def _on_position(self, _engine, value: float) -> None:
        self.progress.props.position = value
        self._elapsed.set_text(format_time(value))

    def _on_duration(self, _engine, value: float) -> None:
        if value > 0:
            self.progress.props.duration = value
            self._total.set_text(format_time(value))

    def _refresh_buttons(self) -> None:
        if self._engine is None:
            return
        self.next_button.set_sensitive(self._engine.queue.has_next)
        self.prev_button.set_sensitive(self._engine.queue.has_previous
                                       or self._engine.current is not None)

    def set_segments(self, segments) -> None:
        self.progress.set_segments(
            [(s.start, s.end, 1.0) for s in segments])

    def set_compact(self, compact: bool) -> None:
        """Sous 760 px : piste, transport et plein écran seulement."""
        self.volume_box.set_visible(not compact)
        self._times.set_visible(not compact)


class TriatWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, engine=None,
                 data_path: str = "") -> None:
        super().__init__(application=app, title="Triat")
        self.set_default_size(1440, 900)
        self.set_size_request(360, 480)
        self.engine = engine

        self.add_css_class("triat-window")
        self.sidebar = Sidebar(self._on_navigate, self.open_settings)
        # Sans défilement, « Réglages » se fait rogner dès que la fenêtre
        # manque de hauteur.
        sidebar_scroll = Gtk.ScrolledWindow()
        sidebar_scroll.add_css_class("triat-sidebar-scroll")
        sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sidebar_scroll.set_child(self.sidebar)
        sidebar_scroll.set_propagate_natural_width(True)
        # Un libellé extensible dans une ligne de playlist propagerait son
        # hexpand jusqu'ici : la navigation mangeait la moitié de la fenêtre.
        sidebar_scroll.set_hexpand(False)
        self.sidebar.set_vexpand(True)
        if data_path:
            self.sidebar.set_data_path(data_path)

        self.home = HomePage(engine, self._play_from_list,
                             on_immersion=self.open_immersion,
                             on_search=lambda: self._on_navigate("search"))
        self.actions = TrackActions(engine, self._toast, self) if engine else None
        self.search = SearchPage(engine, self._play_from_list,
                                 self._on_track_action,
                                 on_artist=self.show_artist,
                                 on_playlist=self.show_playlist) if engine else None
        self.library = (LibraryPage(engine, self._play_from_list, self._toast,
                                    self._on_track_action,
                                    on_artist=self.show_artist)
                        if engine else None)

        # Pas de fondu : les pages sont translucides, un fondu les
        # superposerait. Changement sec, comme le shell (« dry, fast »).
        self.pages = Gtk.Stack(hexpand=True, vexpand=True)
        self.pages.set_transition_type(Gtk.StackTransitionType.NONE)
        self.pages.add_named(self.home, "home")
        if self.search is not None:
            self.pages.add_named(self.search, "search")
        if self.library is not None:
            self.pages.add_named(self.library, "library")
        self.downloads = DownloadsPage(engine) if engine else None
        if self.downloads is not None:
            self.pages.add_named(self.downloads, "downloads")
        self.artist = (ArtistPage(engine, self._play_from_list,
                                  self._on_track_action) if engine else None)
        if self.artist is not None:
            self.pages.add_named(self.artist, "artist")

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.append(sidebar_scroll)
        body.append(self.pages)
        self.body = body

        self.player_bar = PlayerBar(engine)

        # Coquille : navigation + pages, bandeau de lecture dessous.
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Hors Hyprland, rien d'autre ne permet de déplacer, réduire ou
        # fermer la fenêtre : une poignée fine et les boutons de fenêtre.
        self.window_bar = Gtk.WindowHandle()
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.append(Gtk.Box(hexpand=True))
        bar.append(Gtk.WindowControls(side=Gtk.PackType.END))
        self.window_bar.set_child(bar)
        self.window_bar.set_visible(not is_hyprland())
        shell.append(self.window_bar)
        shell.append(self.body)
        shell.append(self.player_bar)
        self.shell = shell
        if engine is not None:
            self.now_playing = NowPlayingView(engine, on_close=self.close_sheet,
                                           on_artist=self.show_artist,
                                           on_immersion=self.open_immersion,
                                           on_action=self._on_track_action)
        else:
            self.now_playing = None

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(320)
        self.stack.add_named(self.shell, "main")
        # « Lecture en cours » occupe toute la fenêtre et glisse depuis le
        # bandeau : Adw.BottomSheet la plafonnait à mi-hauteur.
        if self.now_playing is not None:
            self.stack.add_named(self.now_playing, "nowplaying")

        # Immersion : construite une fois, sa surface GL n'existe que pendant
        # l'affichage (voir ImmersionView.activate).
        self.immersion = None
        self._fullscreen_by_immersion = False
        if engine is not None:
            self.immersion = ImmersionView(engine, self.close_immersion,
                                           self._toggle_fullscreen)
            self.stack.add_named(self.immersion, "immersion")
            # Le rendu mpv doit être libéré avant l'arrêt du moteur, avec le
            # contexte GL encore vivant : on quitte l'écran avant la fermeture.
            self.connect("close-request", lambda *_: (self.close_immersion(),
                                                      False)[1])

        # Premier lancement : on guide plutôt que d'ouvrir sur une
        # bibliothèque vide (screens.md §11).
        self.onboarding = None
        if engine is not None and not engine.settings.get("onboarding_done"):
            self.onboarding = OnboardingView(engine, self._finish_onboarding)
            self.stack.add_named(self.onboarding, "onboarding")
            self.stack.set_visible_child_name("onboarding")

        self.toasts = Adw.ToastOverlay(child=self.stack)
        self.toasts.add_css_class("triat-root")
        self.stack.add_css_class("triat-root")
        self.set_content(self.toasts)

        self._install_breakpoints()
        self._install_shortcuts()
        self.refresh_sidebar()
        if engine is not None:
            self._connect_engine(engine)
            self.player_bar.expand_button.connect(
                "clicked", lambda *_: self.open_sheet())
            self.player_bar.fullscreen_button.connect(
                "clicked", lambda *_: self.home._start_immersion(None))

    def refresh_sidebar(self) -> None:
        if self.engine is None:
            return
        self.sidebar.set_playlists(self.engine.library.list_playlists(),
                                   self.show_playlist)

    def _finish_onboarding(self) -> None:
        self.stack.set_visible_child_name("main")
        self.home.refresh()
        if self.library is not None:
            self.library.refresh()

    # ---- Réglages --------------------------------------------------------

    def open_settings(self) -> None:
        if self.engine is None:
            return
        SettingsDialog(self.engine, self._on_setting_changed).present(self)

    def _on_setting_changed(self, key: str, _value) -> None:
        """Répercute un réglage sans attendre un redémarrage."""
        if key == "immersion_mode" and self.immersion is not None:
            self.immersion.set_mode(_value, remember=False)
            return
        if key in ("cookie_source", "wiped", "history_cleared",
                   "private_session", "blocklist"):
            if key in ("cookie_source", "wiped"):
                # Les suggestions distantes dépendent du compte : cache jeté.
                try:
                    self.engine.library.reco_cache_clear("home:")
                except Exception:
                    pass
                self.home.reset_remote()
            self.home.refresh()
            if self.library is not None:
                self.library.refresh()
            if key in ("cookie_source", "wiped"):
                self._toast(_("Réglages appliqués"))

    # ---- Vue plein écran -------------------------------------------------

    @property
    def sheet_open(self) -> bool:
        return self.stack.get_visible_child_name() == "nowplaying"

    def open_sheet(self) -> None:
        if self.now_playing is not None and not self.sheet_open:
            self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_UP)
            self.stack.set_transition_duration(240)
            self.stack.set_visible_child_name("nowplaying")

    # ---- Immersion ---------------------------------------------------------

    def open_immersion(self) -> None:
        if self.immersion is None or self.immersion.active:
            return
        if self.engine.current is None:
            self._toast(_("Rien en lecture"))
            return
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(320)
        self.stack.set_visible_child_name("immersion")
        self._fullscreen_by_immersion = not self.is_fullscreen()
        if self._fullscreen_by_immersion:
            self.fullscreen()
        self.immersion.activate()

    def close_immersion(self) -> None:
        if self.immersion is None or not self.immersion.active:
            return
        self.immersion.deactivate()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_visible_child_name("main")
        if self._fullscreen_by_immersion:
            self.unfullscreen()
        self._fullscreen_by_immersion = False

    def _toggle_fullscreen(self) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def close_sheet(self) -> None:
        if self.sheet_open:
            self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_DOWN)
            self.stack.set_transition_duration(200)
            self.stack.set_visible_child_name("main")

    def toggle_sheet(self) -> None:
        if self.sheet_open:
            self.close_sheet()
        else:
            self.open_sheet()

    # ---- Moteur ----------------------------------------------------------

    def _connect_engine(self, engine) -> None:
        engine.connect("state-changed",
                       lambda _e, state: self.sidebar.set_live(state == "playing"))
        self.sidebar.set_live(False)
        engine.connect("error", self._on_error)
        if GObject.signal_lookup("segments-changed", type(engine)):
            engine.connect("segments-changed", self._on_segments_changed)
        engine.connect("track-changed", self._on_track_marks)
        engine.connect("segment-skipped", self._on_segment_skipped)
        engine.connect("segment-pending", self._on_segment_pending)
        # Les playlists de la barre latérale suivent la bibliothèque
        # (synchro du téléphone : playlists créées ou supprimées là-bas).
        engine.connect("library-changed", lambda *_: self.refresh_sidebar())

    def _on_track_marks(self, _engine, _ref) -> None:
        if self.library is not None:
            self.library._mark_current()

    def _on_segments_changed(self, _engine, segments) -> None:
        """Reporte les segments connus sur les deux barres de progression."""
        segments = segments or []
        self.player_bar.set_segments(segments)
        if self.now_playing is not None:
            self.now_playing.set_segments(segments)

    def _toast(self, message: str, timeout: int = 5) -> None:
        self.toasts.add_toast(make_toast(message, timeout))

    def _on_error(self, _engine, message: str) -> None:
        self._toast(message, timeout=6)

    def _on_segment_skipped(self, _engine, segment, gained: float) -> None:
        # Toast SponsorBlock avec possibilité d'annuler (DESIGN.md §6).
        libelle = category_label(segment.category)
        toast = make_toast(_("{libelle} sauté · {format_time}").format(libelle=libelle, format_time=format_time(gained)))
        toast.set_button_label(_("Annuler"))
        toast.connect("button-clicked",
                      lambda *_: self.engine.cancel_segment(segment))
        self.toasts.add_toast(toast)

    def _on_segment_pending(self, _engine, segment) -> None:
        toast = make_toast(
            _("{category_label} détecté").format(category_label=category_label(segment.category)), timeout=6)
        toast.set_button_label(_("Sauter"))
        toast.connect("button-clicked",
                      lambda *_: self.engine.skip_segment(segment))
        self.toasts.add_toast(toast)

    def _on_track_action(self, action: str, ref) -> None:
        if action == "artist":
            # Depuis la file de « Lecture en cours » : on quitte la feuille
            # pour montrer la page de l'artiste.
            self.close_sheet()
            self.show_artist(ref.artist if ref else "")
            return
        if self.actions is not None:
            self.actions.handle(action, ref)

    def show_playlist(self, playlist_id: int) -> None:
        """Ouvre une playlist dans la Bibliothèque."""
        if self.library is None:
            return
        self._on_navigate("library")
        self.library.show_playlist(playlist_id)

    def show_artist(self, artist: str) -> None:
        """Ouvre la page d'un artiste."""
        if self.artist is None or not (artist or "").strip():
            self._toast(_("Artiste inconnu pour cette piste"))
            return
        self.artist.show_artist(artist)
        self.sidebar.select("library")
        self.pages.set_visible_child_name("artist")
        self.close_sheet()

    def _play_from_list(self, refs, position: int) -> None:
        if self.engine is not None:
            self.engine.play_refs(refs, start=position)

    # ---- Raccourcis ------------------------------------------------------

    def _typing(self) -> bool:
        """Vrai si le clavier appartient à autre chose que la lecture : un
        champ de saisie, ou un dialogue ouvert par-dessus la fenêtre."""
        if self.get_visible_dialog() is not None:
            return True
        focus = self.get_focus()
        return isinstance(focus, (Gtk.Editable, Gtk.TextView))

    def _focus_wants_space(self) -> bool:
        """Vrai si l'utilisateur a mis, au clavier, le focus sur un bouton,
        une case ou une ligne de liste : Espace doit alors l'activer. Après
        un simple clic souris, le focus n'est pas « visible » et Espace
        reste la touche lecture/pause."""
        try:
            if not self.get_focus_visible():
                return False
        except Exception:
            return False
        widget = self.get_focus()
        while widget is not None:
            if isinstance(widget, (Gtk.Button, Gtk.CheckButton, Gtk.Switch,
                                   Gtk.ListBoxRow, Gtk.ListView,
                                   Gtk.GridView, Gtk.ColumnView)):
                return True
            widget = widget.get_parent()
        return False

    def _sc_toggle(self, *_args) -> bool:
        if self._typing() or self.engine is None:
            return False
        if self._focus_wants_space():
            return False                # le widget focalisé s'en charge
        self.engine.toggle_pause()
        return True

    def _sc_seek(self, delta: float):
        def seek(*_args) -> bool:
            engine = self.engine
            if self._typing() or engine is None or engine.current is None:
                return False
            target = max(0.0, engine.player.position_hint + delta)
            duration = engine.player.duration or 0.0
            if duration > 0:
                target = min(target, duration)
            engine.seek(target)
            return True
        return seek

    def _sc_skip(self, forward: bool):
        def skip(*_args) -> bool:
            if self._typing() or self.engine is None:
                return False
            if forward:
                self.engine.next()
            else:
                self.engine.previous()
            return True
        return skip

    def _sc_escape(self, *_args) -> bool:
        """Échap : referme ce qui a été ouvert par-dessus, puis la page
        d'artiste. Un champ de saisie garde sa propre touche Échap."""
        if self._typing():
            return False
        if self.immersion is not None and self.immersion.active:
            self.close_immersion()
            return True
        if self.sheet_open:
            self.close_sheet()
            return True
        if (self.stack.get_visible_child_name() == "main"
                and self.pages.get_visible_child_name() == "artist"):
            self._on_navigate("library")
            return True
        return False

    def _install_shortcuts(self) -> None:
        # Espace en phase de capture : un bouton cliqué garde le focus et
        # consommerait Espace (« Suivant » saute alors encore). Les autres
        # touches restent en bulle : le widget focalisé passe d'abord.
        capture = Gtk.ShortcutController()
        capture.set_scope(Gtk.ShortcutScope.GLOBAL)
        capture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._space_controller = capture
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.GLOBAL)
        self._shortcut_controller = controller

        def add(target, accel: str, callback) -> None:
            target.add_shortcut(Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string(accel),
                action=Gtk.CallbackAction.new(callback)))

        def focus_search(*_args) -> bool:
            self._on_navigate("search")
            return True

        def open_now_playing(*_args) -> bool:
            self.toggle_sheet()
            return True

        def open_prefs(*_args) -> bool:
            self.open_settings()
            return True

        def toggle_immersion(*_args) -> bool:
            if self.immersion is not None and self.immersion.active:
                self.close_immersion()
            else:
                self.open_immersion()
            return True

        def help_(*_args) -> bool:
            self.show_shortcuts()
            return True

        add(capture, "space", self._sc_toggle)
        add(controller, "<Primary>f", focus_search)
        add(controller, "<Primary>l", open_now_playing)
        add(controller, "<Primary>comma", open_prefs)
        add(controller, "<Primary>i", toggle_immersion)
        add(controller, "<Primary>question", help_)
        add(controller, "<Primary><Shift>slash", help_)
        add(controller, "Left", self._sc_seek(-SEEK_STEP))
        add(controller, "Right", self._sc_seek(SEEK_STEP))
        add(controller, "<Shift>Left", self._sc_skip(False))
        add(controller, "<Shift>Right", self._sc_skip(True))
        add(controller, "Escape", self._sc_escape)
        self.add_controller(capture)
        self.add_controller(controller)

    def show_shortcuts(self) -> None:
        """Fenêtre d'aide des raccourcis (Ctrl+?)."""
        if getattr(Adw, "ShortcutsDialog", None) is not None:
            dialog = Adw.ShortcutsDialog()
            section = Adw.ShortcutsSection(title=_("Triat"))
            for title, accel in SHORTCUTS:
                section.add(Adw.ShortcutsItem.new(title, accel))
            dialog.add(section)
            dialog.present(self)
            return
        win = Gtk.ShortcutsWindow(modal=True, transient_for=self)
        section = Gtk.ShortcutsSection(section_name="shortcuts",
                                       title=_("Raccourcis"))
        group = Gtk.ShortcutsGroup(title=_("Triat"))
        for title, accel in SHORTCUTS:
            group.add_shortcut(Gtk.ShortcutsShortcut(title=title,
                                                     accelerator=accel))
        section.add_group(group)
        win.add_section(section)
        win.present()

    # ---- Navigation ------------------------------------------------------

    def _install_breakpoints(self) -> None:
        # Hyprland tuile Triat à ~950 px sur la moitié d'un ultrawide :
        # rail d'icônes en dessous de 1 100 px, libellés au-dessus.
        wide = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 1100px"))
        wide.connect("apply", lambda *_: self._set_narrow(True))
        wide.connect("unapply", lambda *_: self._set_narrow(False))
        self.add_breakpoint(wide)

        narrow = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 760px"))
        narrow.connect("apply", lambda *_: self.player_bar.set_compact(True))
        narrow.connect("unapply", lambda *_: self.player_bar.set_compact(False))
        self.add_breakpoint(narrow)

    def _set_narrow(self, narrow: bool) -> None:
        self.sidebar.set_compact(narrow)
        for page in (self.home, self.search, self.library, self.downloads,
                     self.artist):
            if page is None:
                continue
            if narrow:
                page.add_css_class("narrow")
            else:
                page.remove_css_class("narrow")
        self.home.set_narrow(narrow)
        if self.now_playing is not None:
            self.now_playing.set_narrow(narrow)

    def _on_navigate(self, key: str) -> None:
        # Ctrl+F, « triat --search » ou la barre latérale ne doivent pas
        # agir en coulisses derrière Lecture en cours ou Immersion. Pendant
        # l'accueil guidé, on ne quitte rien.
        if self.stack.get_visible_child_name() != "onboarding":
            self.close_immersion()
            self.close_sheet()
        if key not in ("home", "search", "library", "downloads"):
            return
        self.sidebar.select(key)
        if key == "home":
            self.home.refresh()
            self.refresh_sidebar()
            self.pages.set_visible_child_name("home")
        elif key == "search" and self.search is not None:
            self.pages.set_visible_child_name("search")
            GLib.idle_add(self.search.focus_entry)
        elif key == "library" and self.library is not None:
            self.pages.set_visible_child_name("library")
            self.library.refresh()
            self.refresh_sidebar()
        elif key == "downloads" and self.downloads is not None:
            self.pages.set_visible_child_name("downloads")
            self.downloads.refresh()

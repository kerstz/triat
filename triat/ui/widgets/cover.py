"""Cover — pochette réelle, ou motif de points à défaut.

DESIGN.md §5 : « Pochette absente : motif de points généré à partir du hash
de l'ID (disque, anneau, bandes, demi, coin, diagonale). » Le motif est donc
déterministe — une piste donnée garde toujours la même pochette de repli.

Le téléchargement ET le décodage se font dans un petit pool de threads,
à la taille affichée ; les textures sont gardées en mémoire (LRU) et
partagées entre toutes les pochettes. `do_snapshot` ne fait jamais
d'entrée-sortie ni de calcul : le motif de repli est un nœud de rendu
construit une fois par motif et par taille.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Graphene, Gsk, Gtk, Pango

from ...core import artwork
from ...core.models import SOUNDCLOUD
from .dot_progress import rgba
from ...i18n import _

log = logging.getLogger(__name__)

DOT_COLOUR = "#f1f1f1"
BACKGROUND = "#111111"
PATTERNS = ("disque", "anneau", "bandes", "demi", "coin", "diagonale")
GRID = 11          # points par côté


def square_crop_rect(width: int, height: int) -> tuple[int, int, int]:
    """Carré centré dans une image `width`×`height` : (x, y, côté).

    Une vignette vidéo 16:9 est recadrée (pas étirée, pas de bandes).
    """
    side = max(0, min(int(width), int(height)))
    return (int(width) - side) // 2, (int(height) - side) // 2, side


def is_youtube_thumbnail(url: str | None) -> bool:
    """Vrai pour une vignette `i.ytimg.com/vi/<id>/…`."""
    return bool(url) and "ytimg.com/vi" in url


def content_rect(width: int, height: int, url: str | None = None
                 ) -> tuple[int, int, int, int]:
    """Zone utile (x, y, l, h) : ôte les bandes noires des vignettes 4:3.

    `hqdefault`/`sddefault` de YouTube sont en 4:3 avec des bandes noires
    haut et bas autour d'une image 16:9 ; `maxresdefault` et `mqdefault`
    sont déjà en 16:9 pur et ne sont pas touchées.
    """
    if is_youtube_thumbnail(url) and width > 0 and height > 0 \
            and abs(width / height - 4 / 3) < 0.02:
        inner = round(width * 9 / 16)
        return 0, (height - inner) // 2, width, inner
    return 0, 0, width, height


def pattern_for(video_id: str) -> str:
    """Motif déterminé par l'identifiant : stable d'une session à l'autre."""
    if not video_id:
        return PATTERNS[0]
    digest = hashlib.sha256(video_id.encode("utf-8")).digest()
    return PATTERNS[digest[0] % len(PATTERNS)]


def pattern_points(name: str, grid: int = GRID) -> list[tuple[float, float, float]]:
    """Points allumés d'un motif, en coordonnées relatives (0-1) + intensité.

    Calculé une fois par piste, jamais pendant le rendu.
    """
    points: list[tuple[float, float, float]] = []
    centre = (grid - 1) / 2
    for row in range(grid):
        for col in range(grid):
            # Coordonnées centrées, normalisées entre -1 et 1.
            dx = (col - centre) / centre
            dy = (row - centre) / centre
            distance = math.hypot(dx, dy)
            weight = 0.0

            if name == "disque":
                weight = 1.0 if distance <= 0.82 else 0.0
            elif name == "anneau":
                weight = 1.0 if 0.45 <= distance <= 0.85 else 0.0
            elif name == "bandes":
                weight = 1.0 if row % 3 != 2 else 0.0
            elif name == "demi":
                weight = 1.0 if dy <= 0.05 else 0.0
            elif name == "coin":
                weight = 1.0 if (dx + dy) <= -0.1 else 0.0
            elif name == "diagonale":
                weight = 1.0 if abs(dx - dy) <= 0.45 else 0.0

            if weight > 0 and distance <= 1.05:
                # Les points s'estompent vers le bord : le motif respire.
                fade = max(0.35, 1.0 - 0.45 * distance)
                points.append(((col + 0.5) / grid, (row + 0.5) / grid, fade))
    return points


# Badge de source : libellé texte « SC », jamais le logo (SPEC-SOUNDCLOUD §7.3).
BADGE_TEXT = "SC"
BADGE_ACCESSIBLE = _("Source : SoundCloud")
_badge_default = True


def set_badge_default(enabled: bool) -> None:
    """Réglage `source_badge` pour les pochettes sans choix explicite."""
    global _badge_default
    _badge_default = bool(enabled)


def badge_rect(side: float) -> tuple[float, float, float, float]:
    """Cadre (x, y, l, h) du badge, collé au coin bas-droit d'un carré."""
    width = max(10.0, side * 0.34)
    height = max(8.0, side * 0.24)
    return side - width - 1, side - height - 1, width, height


class _TextureCache:
    """Textures de pochettes, réduites à la taille affichée, partagées.

    Avant : un thread par ligne affichée, et un JPEG 480×360 décodé dans le
    fil de l'interface à chaque réaffichage — d'où les saccades au
    défilement. Maintenant : 4 threads, décodage réduit hors du fil GTK,
    un seul chargement par (image, taille) même demandée dix fois.
    """

    LIMIT = 600
    # Un échec (hors ligne, 404) n'est pas retenté avant ce délai : sans
    # cela, chaque défilement hors ligne relançait toutes les requêtes.
    FAILURE_TTL = 120.0

    def __init__(self) -> None:
        self._textures: OrderedDict = OrderedDict()
        self._failed: dict = {}
        self._waiting: dict = {}
        self._pool = ThreadPoolExecutor(max_workers=4,
                                        thread_name_prefix="pochettes")

    def get(self, url: str, pixels: int):
        key = (url, pixels)
        texture = self._textures.get(key)
        if texture is not None:
            self._textures.move_to_end(key)
        return texture

    def request(self, url: str, pixels: int, callback) -> None:
        key = (url, pixels)
        failed_at = self._failed.get(key)
        if failed_at is not None:
            if time.monotonic() - failed_at < self.FAILURE_TTL:
                GLib.idle_add(self._answer_failure, callback)
                return
            del self._failed[key]
        if key in self._waiting:
            self._waiting[key].append(callback)
            return
        self._waiting[key] = [callback]
        self._pool.submit(self._load, key)

    def submit(self, function, *args) -> None:
        """Exécute une tâche de chargement dans le pool partagé."""
        self._pool.submit(function, *args)

    @staticmethod
    def _answer_failure(callback) -> bool:
        callback(None)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _texture_from(pixbuf):
        """Texture mémoire : `Gdk.Texture.new_for_pixbuf` est dépréciée."""
        has_alpha = pixbuf.get_has_alpha()
        return Gdk.MemoryTexture.new(
            pixbuf.get_width(), pixbuf.get_height(),
            Gdk.MemoryFormat.R8G8B8A8 if has_alpha else Gdk.MemoryFormat.R8G8B8,
            pixbuf.read_pixel_bytes(), pixbuf.get_rowstride())

    @staticmethod
    def _square_pixbuf(path: str, url: str, pixels: int):
        """Charge l'image réduite, recadrée en carré centré (sans étirer)."""
        _info, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
        if width <= 0 or height <= 0:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, pixels, pixels, True)
        # Petit côté de la zone utile ramené à `pixels`.
        _x, _y, cw, ch = content_rect(width, height, url)
        ratio = pixels / max(1, min(cw, ch))
        full = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            path, max(1, round(width * ratio)), max(1, round(height * ratio)),
            True)
        sx, sy, sw, sh = content_rect(full.get_width(), full.get_height(), url)
        cx, cy, side = square_crop_rect(sw, sh)
        if side <= 0:
            return full
        # `copy` : le sous-pixbuf partage la mémoire et le rowstride du parent.
        return full.new_subpixbuf(sx + cx, sy + cy, side, side).copy()

    def _load(self, key) -> None:
        url, pixels = key
        pixbuf = None
        path = artwork.fetch(url)
        if path is not None:
            try:
                pixbuf = self._square_pixbuf(str(path), url, pixels)
            except GLib.Error as exc:
                log.debug("Pochette illisible : %s", exc)
        texture = None
        if pixbuf is not None:
            try:
                texture = self._texture_from(pixbuf)
            except Exception as exc:                  # pixbuf inattendu
                log.debug("Texture impossible : %s", exc)
        GLib.idle_add(self._deliver, key, texture)

    def _deliver(self, key, texture) -> bool:
        if texture is not None:
            self._failed.pop(key, None)
            self._textures[key] = texture
            while len(self._textures) > self.LIMIT:
                self._textures.popitem(last=False)
        else:
            self._failed[key] = time.monotonic()
        for callback in self._waiting.pop(key, []):
            callback(texture)
        return GLib.SOURCE_REMOVE


TEXTURES = _TextureCache()
_PLACEHOLDERS: dict = {}


def _placeholder_node(pattern: str, side: float, radius: float):
    """Nœud de rendu du motif de repli, construit une seule fois."""
    key = (pattern, round(side), round(radius))
    node = _PLACEHOLDERS.get(key)
    if node is None:
        snapshot = Gtk.Snapshot()
        rect = Graphene.Rect().init(0, 0, side, side)
        snapshot.append_color(rgba(BACKGROUND), rect)
        dot = side / GRID * 0.3
        for rel_x, rel_y, fade in pattern_points(pattern):
            x, y = rel_x * side, rel_y * side
            dot_rect = Graphene.Rect().init(x - dot, y - dot, dot * 2, dot * 2)
            clip = Gsk.RoundedRect()
            clip.init_from_rect(dot_rect, dot)
            snapshot.push_rounded_clip(clip)
            snapshot.append_color(rgba(DOT_COLOUR, fade * 0.9), dot_rect)
            snapshot.pop()
        node = snapshot.to_node()
        _PLACEHOLDERS[key] = node
    return node


class Cover(Gtk.Widget):
    """Pochette carrée. Affiche l'image réelle dès qu'elle est disponible."""

    __gtype_name__ = "TriatCover"

    def __init__(self, size: int = 60, radius: float = 6.0) -> None:
        # IMG : la pochette est une image porteuse de sens quand elle illustre
        # une piste (libellé posé par `_refresh_a11y`), masquée sinon.
        super().__init__(accessible_role=Gtk.AccessibleRole.IMG)
        self.add_css_class("triat-cover")
        self._title: str | None = None
        self._size = size
        self._radius = radius
        self._texture: Gdk.Texture | None = None
        self._pattern = PATTERNS[0]
        self._url: str | None = None
        self._token = 0
        self._badge = False
        self._badge_layout = None
        self.set_size_request(size, size)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self._refresh_a11y()

    # ---- API -------------------------------------------------------------

    def set_size(self, size: int) -> None:
        self._size = size
        self.set_size_request(size, size)
        self.queue_resize()

    def _pixels(self) -> int:
        # Deux fois la taille logique : net sur un écran HiDPI, et toujours
        # bien plus léger que l'image source.
        return max(self._size, 16) * 2

    @property
    def badge_visible(self) -> bool:
        return self._badge

    def _set_badge(self, ref, badge: bool | None) -> None:
        wanted = _badge_default if badge is None else bool(badge)
        show = wanted and ref is not None \
            and getattr(ref, "source", None) == SOUNDCLOUD
        if show == self._badge:
            return
        self._badge = show
        self._refresh_a11y()
        self.queue_draw()

    def accessible_text(self) -> str | None:
        """Libellé lu par les lecteurs d'écran ; None si la pochette est
        purement décorative (aucune piste, pas de badge)."""
        parts = []
        if self._title:
            parts.append(_("Pochette de {title}").format(title=self._title))
        if self._badge:
            parts.append(BADGE_ACCESSIBLE)
        return ". ".join(parts) or None

    def _refresh_a11y(self) -> None:
        text = self.accessible_text()
        if text is None:
            self.reset_property(Gtk.AccessibleProperty.LABEL)
            self.update_state([Gtk.AccessibleState.HIDDEN], [True])
        else:
            self.update_state([Gtk.AccessibleState.HIDDEN], [False])
            self.update_property([Gtk.AccessibleProperty.LABEL], [text])

    def set_track(self, ref, badge: bool | None = None) -> None:
        """Affiche la pochette d'une piste, ou son motif de repli.

        `badge` : None suit le réglage global ; sinon force l'affichage du
        badge de source (piste SoundCloud seulement)."""
        self._title = (getattr(ref, "title", None) or None) if ref else None
        self._set_badge(ref, badge)
        self._refresh_a11y()
        url = getattr(ref, "thumbnail", None) if ref else None
        pattern = pattern_for(getattr(ref, "video_id", "") if ref else "")
        if url and url == self._url and self._texture is not None:
            return                      # déjà affichée : rien à refaire
        self._token += 1
        token = self._token
        self._url = url
        self._pattern = pattern
        self._texture = TEXTURES.get(url, self._pixels()) if url else None
        self.queue_draw()
        if url and self._texture is None:
            TEXTURES.request(url, self._pixels(),
                             lambda texture: self._apply(texture, token))

    def clear(self) -> None:
        self._title = None
        self._set_badge(None, False)
        self._refresh_a11y()
        self._token += 1
        self._texture = None
        self._url = None
        self._pattern = PATTERNS[0]
        self.queue_draw()

    def _apply(self, texture, token: int) -> None:
        # Une autre piste a pu être demandée entre-temps.
        if token != self._token or texture is None:
            return
        self._texture = texture
        self.queue_draw()

    # ---- Rendu -----------------------------------------------------------

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width = self.get_width()
        height = self.get_height()
        if width <= 0 or height <= 0:
            return
        side = min(width, height)
        offset_x = (width - side) / 2
        offset_y = (height - side) / 2

        rect = Graphene.Rect().init(offset_x, offset_y, side, side)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(rect, self._radius)
        snapshot.push_rounded_clip(rounded)
        if self._texture is not None:
            snapshot.append_texture(self._texture, rect)
        else:
            snapshot.save()
            snapshot.translate(Graphene.Point().init(offset_x, offset_y))
            snapshot.append_node(_placeholder_node(self._pattern, side, self._radius))
            snapshot.restore()
        snapshot.pop()
        if self._badge and side >= 28:
            self._draw_badge(snapshot, offset_x, offset_y, side)

    def _draw_badge(self, snapshot: Gtk.Snapshot, ox: float, oy: float,
                    side: float) -> None:
        x, y, w, h = badge_rect(side)
        rect = Graphene.Rect().init(ox + x, oy + y, w, h)
        pill = Gsk.RoundedRect()
        pill.init_from_rect(rect, h / 2)
        snapshot.push_rounded_clip(pill)
        snapshot.append_color(rgba(BACKGROUND, 0.85), rect)
        snapshot.pop()
        layout = self._badge_layout
        if layout is None:
            layout = self._badge_layout = self.create_pango_layout(BADGE_TEXT)
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_scale_new(0.7 if side < 48 else 0.85))
            layout.set_attributes(attrs)
        text_w, text_h = layout.get_pixel_size()
        snapshot.save()
        snapshot.translate(Graphene.Point().init(
            ox + x + (w - text_w) / 2, oy + y + (h - text_h) / 2))
        snapshot.append_layout(layout, rgba(DOT_COLOUR))
        snapshot.restore()

    def do_measure(self, _orientation, _for_size):
        return (self._size, self._size, -1, -1)

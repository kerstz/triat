"""Recadrage carré des vignettes (fonctions pures, sans GTK)."""

import pytest

pytest.importorskip("gi")

from triat.ui.widgets.cover import (  # noqa: E402
    content_rect, is_youtube_thumbnail, square_crop_rect)


@pytest.mark.parametrize("w,h,expected", [
    (1280, 720, (280, 0, 720)),      # 16:9 : on coupe les côtés
    (480, 360, (60, 0, 360)),        # 4:3
    (500, 500, (0, 0, 500)),         # déjà carré
    (300, 600, (0, 150, 300)),       # portrait : on coupe haut et bas
    (0, 0, (0, 0, 0)),
])
def test_square_crop_rect(w, h, expected):
    assert square_crop_rect(w, h) == expected


def test_crop_stays_inside_image():
    for w, h in ((1920, 1080), (321, 181), (181, 321), (1, 7)):
        x, y, side = square_crop_rect(w, h)
        assert x >= 0 and y >= 0 and x + side <= w and y + side <= h
        assert side == min(w, h)


def test_youtube_thumbnail_detection():
    assert is_youtube_thumbnail("https://i.ytimg.com/vi/abc123/hqdefault.jpg")
    assert not is_youtube_thumbnail("https://example.com/cover.jpg")
    assert not is_youtube_thumbnail(None)


def test_hqdefault_letterbox_removed():
    url = "https://i.ytimg.com/vi/abc/hqdefault.jpg"
    assert content_rect(480, 360, url) == (0, 45, 480, 270)


def test_16_9_and_other_urls_untouched():
    url = "https://i.ytimg.com/vi/abc/maxresdefault.jpg"
    assert content_rect(1280, 720, url) == (0, 0, 1280, 720)
    assert content_rect(480, 360, "https://example.com/a.jpg") == (0, 0, 480, 360)

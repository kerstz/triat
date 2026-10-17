"""Une saisie « ytsearch… » n'atteint jamais yt-dlp telle quelle."""

from __future__ import annotations

import pytest

from triat.core.extractor import Extractor


@pytest.mark.parametrize("typed", [
    "ytsearch50:musique", "ytsearchall:x", "YTSEARCH5:y", "ytsearchdate:z"])
def test_user_ytsearch_is_wrapped_as_plain_text(typed):
    seen = {}
    ex = Extractor()

    def fake(query, extra=None, **kw):
        seen["query"] = query
        return None

    ex._extract = fake
    ex.resolve(typed)
    assert seen["query"] == f"ytsearch1:{typed}"

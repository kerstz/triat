"""Tokens Signal : JSON complet, génération idempotente, contrastes, CSS propre."""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gen_tokens", ROOT / "tools/gen_tokens.py")
gen = importlib.util.module_from_spec(_spec)
sys.modules["gen_tokens"] = gen
_spec.loader.exec_module(gen)

CSS = ROOT / "data/style.css"
COLORS = ["ground", "veil-1", "veil-2", "veil-3", "veil-4", "hair", "ink", "ink-2",
          "ink-3", "ink-4", "dot-off", "disabled", "signal", "signal-ink",
          "select-bg", "select-fg", "danger", "ok", "focus"]
ROLES = ["display-xl", "display-l", "display-m", "title-l", "title-m", "title-s",
         "body", "body-s", "meta", "mono", "mono-s"]


@pytest.fixture(scope="module")
def tokens():
    return gen.load_tokens()


def test_tokens_complete(tokens):
    for name in COLORS:
        assert name in tokens["color"], name
    assert list(tokens["font"]["role"]) == ROLES
    for r in tokens["font"]["role"].values():
        assert {"family", "weight", "px", "sp", "line"} <= set(r)
        assert r["family"] in tokens["font"]["family"]
    assert list(tokens["space"].values()) == [4, 8, 12, 16, 24, 32, 48, 64]
    assert {"r-panel", "r-art-s", "r-art-l", "r-sheet", "r-pill"} <= set(tokens["radius"])
    assert list(tokens["motion"]) == ["t-fast", "t-base", "t-page", "t-art"]
    for entry in tokens["color"].values():
        gen.parse_color(entry["value"])


def test_generation_is_idempotent(tokens):
    block = gen.render_css(tokens)
    once = gen.replace_block(CSS.read_text(), block)
    assert gen.replace_block(once, block) == once
    assert block.startswith(gen.BEGIN) and block.endswith(gen.END)


def test_replace_block_needs_markers():
    with pytest.raises(ValueError):
        gen.replace_block("body {}", "x")


def test_check_passes_on_repo():
    r = subprocess.run([sys.executable, str(ROOT / "tools/gen_tokens.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_check_fails_when_stale(tmp_path):
    stale = tmp_path / "style.css"
    stale.write_text(CSS.read_text().replace("--triat-ink: #F2F2F2", "--triat-ink: #000000"))
    assert gen.main(["--check", "--css", str(stale)]) == 1
    assert gen.main(["--css", str(stale)]) == 0
    assert gen.main(["--check", "--css", str(stale)]) == 0


def test_contrast_formula():
    assert gen.contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert gen.contrast_ratio("#777777", "#777777") == pytest.approx(1.0)


@pytest.mark.parametrize("fg", ["ink", "ink-2", "ink-3"])
@pytest.mark.parametrize("bg", ["ground", "veil-2"])
def test_text_contrast_aa(tokens, fg, bg):
    c = tokens["color"]
    ratio = gen.contrast_ratio(gen.flat_hex(c[fg]), gen.flat_hex(c[bg]))
    assert ratio >= 4.5, f"{fg} sur {bg} : {ratio:.2f}"


def test_disabled_and_ui_contrast(tokens):
    c = tokens["color"]
    ground = gen.flat_hex(c["ground"])
    assert gen.contrast_ratio(gen.flat_hex(c["disabled"]), ground) >= 3.0
    assert gen.contrast_ratio(gen.flat_hex(c["signal-ink"]), ground) >= 4.5
    assert gen.contrast_ratio(gen.flat_hex(c["select-fg"]), gen.flat_hex(c["select-bg"])) >= 4.5


def _outside_generated(css: str) -> str:
    css = re.sub(re.escape(gen.BEGIN) + r".*?" + re.escape(gen.END), "", css, flags=re.S)
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


# Liste blanche : valeurs inévitables hors tokens (vide aujourd'hui).
HARDCODED_ALLOWED: list[str] = []


def test_no_hardcoded_colors_in_stylesheet():
    css = _outside_generated(CSS.read_text())
    for allowed in HARDCODED_ALLOWED:
        css = css.replace(allowed, "")
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not re.findall(r"\brgba?\(", css)


def test_focus_ring_present():
    css = CSS.read_text()
    assert re.search(r"\*:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--triat-focus\)"
                     r"[^}]*outline-offset:\s*2px", css)


def test_every_used_variable_is_defined():
    css = CSS.read_text()
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    used = set(re.findall(r"var\((--[\w-]+)\)", css))
    assert used <= defined, used - defined


def test_stylesheet_parses_in_gtk():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
    except (ImportError, ValueError):
        pytest.skip("GTK4 indisponible")
    errors = []
    provider = Gtk.CssProvider()
    provider.connect("parsing-error", lambda _p, _s, err: errors.append(err.message))
    provider.load_from_path(str(CSS))
    assert not errors, errors

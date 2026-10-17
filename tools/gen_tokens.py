#!/usr/bin/env python3
"""Genere les tokens Signal depuis design/tokens.json (stdlib uniquement).

    python tools/gen_tokens.py            # reecrit le bloc genere de data/style.css
    python tools/gen_tokens.py --check    # echoue (code 1) si le fichier n'est pas a jour

Le bloc est un `:root { --triat-... }` place entre deux marqueurs. Les
custom properties exigent GTK >= 4.16 ; le CSS existant les utilisait deja.
`render_kotlin()` prepare la sortie Android (non ecrite ici).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKENS = ROOT / "design" / "tokens.json"
CSS = ROOT / "data" / "style.css"
BEGIN = "/* BEGIN GENERATED TOKENS */"
END = "/* END GENERATED TOKENS */"
PREFIX = "--triat-"

_HEX = re.compile(r"^#([0-9a-fA-F]{6})$")
_RGBA = re.compile(
    r"^rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)$")


def load_tokens(path: Path = TOKENS) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_color(value: str) -> tuple[int, int, int, float]:
    """`#rrggbb` ou `rgba(r, g, b, a)` -> (r, g, b, alpha 0..1)."""
    m = _HEX.match(value)
    if m:
        n = int(m.group(1), 16)
        return (n >> 16) & 255, (n >> 8) & 255, n & 255, 1.0
    m = _RGBA.match(value)
    if m:
        r, g, b = (int(m.group(i)) for i in (1, 2, 3))
        return r, g, b, float(m.group(4))
    raise ValueError(f"couleur illisible : {value!r}")


def flat_hex(entry: dict) -> str:
    """Couleur opaque equivalente sur `ground` (pour le contraste)."""
    return entry.get("flat", entry["value"])


def relative_luminance(hex_color: str) -> float:
    r, g, b, _ = parse_color(hex_color)

    def lin(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast_ratio(fg: str, bg: str) -> float:
    """Contraste WCAG 2.x entre deux couleurs opaques."""
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _px(n: float) -> str:
    return f"{n:g}px"


def css_variables(tokens: dict) -> list[tuple[str, str]]:
    """Paires (nom `--triat-x`, valeur) dans un ordre stable."""
    out: list[tuple[str, str]] = []
    for name, entry in tokens["color"].items():
        out.append((f"{PREFIX}{name}", entry["value"]))
        if "flat" in entry:
            out.append((f"{PREFIX}{name}-solid", entry["flat"]))
    fam = tokens["font"]["family"]
    for name, f in fam.items():
        out.append((f"{PREFIX}font-{name}", f'"{f}"'))
    for name, r in tokens["font"]["role"].items():
        out.append((f"{PREFIX}{name}-size", _px(r["px"])))
        out.append((f"{PREFIX}{name}-weight", str(r["weight"])))
        out.append((f"{PREFIX}{name}-line", f"{r['line']:g}"))
    for name, n in tokens["space"].items():
        out.append((f"{PREFIX}{name}", _px(n)))
    for name, r in tokens["radius"].items():
        out.append((f"{PREFIX}{name}", _px(r["desktop"])))
    for name, m in tokens["motion"].items():
        out.append((f"{PREFIX}{name}", f"{m['ms']}ms"))
        out.append((f"{PREFIX}{name}-curve", m["curve"]))
    return out


def render_css(tokens: dict) -> str:
    """Le bloc complet, marqueurs compris, sans saut de ligne final."""
    lines = [BEGIN,
             "/* Genere par tools/gen_tokens.py depuis design/tokens.json : ne pas editer. */",
             ":root {"]
    lines += [f"  {n}: {v};" for n, v in css_variables(tokens)]
    lines += ["}", END]
    return "\n".join(lines)


def replace_block(css: str, block: str) -> str:
    """Remplace le bloc entre marqueurs (idempotent). Erreur si absent."""
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    if not pattern.search(css):
        raise ValueError(f"marqueurs {BEGIN} / {END} introuvables")
    return pattern.sub(lambda _m: block, css, count=1)


def _camel(name: str) -> str:
    head, *rest = re.split(r"[-_]", name)
    return head + "".join(p.capitalize() for p in rest)


def render_kotlin(tokens: dict, package: str = "com.ivor.ivormusic.ui.theme") -> str:
    """Source Kotlin `TriatTokens` (non ecrite sur disque par ce script)."""
    L = [f"package {package}", "",
         "import androidx.compose.ui.graphics.Color", "",
         "// Genere par tools/gen_tokens.py depuis design/tokens.json : ne pas editer.",
         "object TriatTokens {"]
    for name, entry in tokens["color"].items():
        r, g, b, a = parse_color(entry["value"])
        argb = (round(a * 255) << 24) | (r << 16) | (g << 8) | b
        L.append(f"    val {_camel(name)} = Color(0x{argb:08X})")
    L.append("")
    for name, n in tokens["space"].items():
        L.append(f"    const val {_camel(name)}Dp = {n}")
    for name, r in tokens["radius"].items():
        L.append(f"    const val {_camel(name)}Dp = {r['android']}")
    for name, r in tokens["font"]["role"].items():
        L.append(f"    const val {_camel(name)}Sp = {r['sp']}")
    for name, m in tokens["motion"].items():
        L.append(f"    const val {_camel(name)}Ms = {m['ms']}")
    L.append("}")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="echoue si le CSS n'est pas a jour")
    ap.add_argument("--tokens", type=Path, default=TOKENS)
    ap.add_argument("--css", type=Path, default=CSS)
    args = ap.parse_args(argv)
    current = args.css.read_text(encoding="utf-8")
    new = replace_block(current, render_css(load_tokens(args.tokens)))
    if args.check:
        if new != current:
            print(f"{args.css} n'est pas a jour : lancer python tools/gen_tokens.py",
                  file=sys.stderr)
            return 1
        print("tokens a jour")
        return 0
    if new != current:
        args.css.write_text(new, encoding="utf-8")
        print(f"{args.css} mis a jour")
    else:
        print("deja a jour")
    return 0


if __name__ == "__main__":
    sys.exit(main())

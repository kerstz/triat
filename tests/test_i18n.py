"""Internationalisation : repli français, pluriels, catalogue à jour et
chaînes de l'interface toutes marquées pour la traduction."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from triat import i18n

ROOT = Path(__file__).resolve().parent.parent
POT = ROOT / "po" / "triat.pot"
POTFILES = ROOT / "po" / "POTFILES"
KEYWORDS = ["--keyword=_", "--keyword=ngettext:1,2", "--keyword=nformat:1,2",
            "--keyword=pgettext:1c,2", "--keyword=N_"]


@pytest.fixture(autouse=True)
def _restore_catalog():
    yield
    i18n.setup()


def test_identity_without_catalog(tmp_path):
    i18n.setup(str(tmp_path))
    assert i18n._("Aucune playlist") == "Aucune playlist"
    assert i18n._("« {name} » créée") == "« {name} » créée"


def test_ngettext_french_rule_without_catalog(tmp_path):
    i18n.setup(str(tmp_path))
    assert i18n.ngettext("{n} titre", "{n} titres", 0) == "{n} titre"
    assert i18n.ngettext("{n} titre", "{n} titres", 1) == "{n} titre"
    assert i18n.ngettext("{n} titre", "{n} titres", 2) == "{n} titres"
    assert i18n.nformat("{n} titre", "{n} titres", 1) == "1 titre"
    assert i18n.nformat("{n} titre", "{n} titres", 3) == "3 titres"
    assert i18n.nformat("{n} piste vers {name}", "{n} pistes vers {name}",
                        2, name="x") == "2 pistes vers x"


@pytest.mark.skipif(shutil.which("msgfmt") is None, reason="msgfmt absent")
def test_compiled_catalog_is_used(tmp_path):
    po = tmp_path / "en.po"
    po.write_text(
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
        'msgid "Annuler"\nmsgstr "Cancel"\n\n'
        'msgid "{n} titre"\nmsgid_plural "{n} titres"\n'
        'msgstr[0] "{n} track"\nmsgstr[1] "{n} tracks"\n',
        encoding="utf-8")
    mo = tmp_path / "en" / "LC_MESSAGES" / "triat.mo"
    mo.parent.mkdir(parents=True)
    subprocess.run(["msgfmt", "-o", str(mo), str(po)], check=True)
    i18n.setup(str(tmp_path), languages=["en"])
    assert i18n._("Annuler") == "Cancel"
    assert i18n._("Inconnu") == "Inconnu"          # repli sur le msgid
    assert i18n.nformat("{n} titre", "{n} titres", 1) == "1 track"
    assert i18n.nformat("{n} titre", "{n} titres", 0) == "0 tracks"


# ---- Catalogue ------------------------------------------------------------

def _parse_po(text: str) -> set[tuple[str, str]]:
    """Ensemble (msgid, msgid_plural) d'un .po/.pot, sans l'en-tête."""
    entries: set[tuple[str, str]] = set()
    current: dict[str, str] = {}
    field = None

    def flush() -> None:
        if current.get("msgid"):
            entries.add((current["msgid"], current.get("msgid_plural", "")))
        current.clear()

    for line in text.splitlines():
        if line.startswith("msgid_plural "):
            field = "msgid_plural"
            current[field] = ast.literal_eval(line[len("msgid_plural "):])
        elif line.startswith("msgid "):
            flush()
            field = "msgid"
            current[field] = ast.literal_eval(line[len("msgid "):])
        elif line.startswith("msgstr"):
            field = None
        elif line.startswith('"') and field:
            current[field] += ast.literal_eval(line)
    flush()
    return entries


@pytest.mark.skipif(shutil.which("xgettext") is None, reason="xgettext absent")
def test_pot_is_up_to_date(tmp_path):
    files = POTFILES.read_text(encoding="utf-8").split()
    out = tmp_path / "triat.pot"
    subprocess.run(
        ["xgettext", "--language=Python", *KEYWORDS, "--from-code=UTF-8",
         "--no-location", "-o", str(out), *files],
        cwd=ROOT, check=True, capture_output=True)
    fresh = _parse_po(out.read_text(encoding="utf-8"))
    committed = _parse_po(POT.read_text(encoding="utf-8"))
    assert fresh == committed, (
        "po/triat.pot périmé : lance tools/i18n.sh. "
        f"Manquants : {sorted(fresh - committed)[:3]} ; "
        f"en trop : {sorted(committed - fresh)[:3]}")


def test_potfiles_cover_every_marked_module():
    listed = set(POTFILES.read_text(encoding="utf-8").split())
    for path in listed:
        assert (ROOT / path).is_file(), path
    marked = set()
    for path in [*(ROOT / "triat" / "ui").rglob("*.py"), ROOT / "triat" / "app.py"]:
        if re.search(r"(?<![\w.])(_|nformat|ngettext)\(", path.read_text(encoding="utf-8")):
            marked.add(str(path.relative_to(ROOT)))
    assert marked <= listed, sorted(marked - listed)


# ---- Chaînes d'interface non marquées -----------------------------------------

LETTERS = re.compile(r"[A-Za-zÀ-ÿ]")
# Appels dont l'argument texte est affiché : (nom, positions, mots-clés).
DISPLAY_CALLS = {
    "set_title": ((0,), ()),
    "set_label": ((0,), ()),
    "set_tooltip_text": ((0,), ()),
    "EmptyState": ((0, 1), ()),
    "Label": ((), ("label",)),
}
# Littéraux volontairement non traduits : (fichier, texte). Liste vide
# aujourd'hui ; on n'y ajoute que des noms propres ou des symboles.
WHITELIST: set[tuple[str, str]] = set()


def _unwrapped(node: ast.expr) -> list[ast.expr]:
    """Littéraux texte d'une expression qui ne passent pas par `_()`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node] if LETTERS.search(node.value) else []
    if isinstance(node, ast.JoinedStr):
        # Une f-string purement numérique (« {m}:{s:02d} ») n'a rien à traduire.
        fixed = [p.value for p in node.values if isinstance(p, ast.Constant)]
        return [node] if any(LETTERS.search(t) for t in fixed) else []
    if isinstance(node, ast.IfExp):
        return _unwrapped(node.body) + _unwrapped(node.orelse)
    return []


def _display_violations() -> list[str]:
    bad = []
    files = [*(ROOT / "triat" / "ui").rglob("*.py"), ROOT / "triat" / "app.py"]
    for path in sorted(files):
        rel = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name not in DISPLAY_CALLS:
                continue
            positions, keywords = DISPLAY_CALLS[name]
            nodes = [call.args[i] for i in positions if i < len(call.args)]
            nodes += [k.value for k in call.keywords if k.arg in keywords]
            for node in nodes:
                for lit in _unwrapped(node):
                    text = lit.value if isinstance(lit, ast.Constant) else ast.unparse(lit)
                    if (rel, text) not in WHITELIST:
                        bad.append(f"{rel}:{lit.lineno}: {text!r}")
    return bad


def test_ui_strings_are_marked_for_translation():
    assert _display_violations() == []

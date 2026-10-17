"""Métadonnées propres à l'affichage (jamais écrites en base).

Fonctions pures : elles nettoient les titres et noms d'artistes bruités des
téléversements YouTube (« Artiste - Titre (Official Video) », « X - Topic »…)
uniquement pour ce que l'on montre. La donnée stockée n'est jamais modifiée.

NB : les règles de bruit entre parenthèses reprennent celles de
`importer._NOISE` (utilisées à l'import) ; `downloads.py`/`importer.py`
pourront plus tard importer ce module pour n'avoir qu'une seule source.
"""

from __future__ import annotations

import re
import unicodedata

# ---- Normalisation ---------------------------------------------------------


def _fold(text: str) -> str:
    """Minuscules, sans accents, espaces compactés."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(bare.casefold().split())


def _alnum(text: str) -> str:
    """Forme de comparaison : minuscules, sans accents ni ponctuation."""
    return re.sub(r"[\W_]+", "", _fold(text))


# ---- Bruit des titres ------------------------------------------------------

# Mentions entre () ou [] qui n'apportent rien. « Live », « Remix »,
# « feat. », « Acoustic »… n'y figurent volontairement pas.
_BRACKET_NOISE = re.compile(
    r"""\s*[\(\[]\s*(?:
        official\s*(?:hd\s*)?(?:music\s*)?(?:video|audio|visuali[sz]er|lyric\s*video|clip)?
      | (?:official\s*)?(?:lyrics?|lyric)(?:\s*video)?
      | music\s*video
      | clip\s*officiel | vid[ée]o\s*officielle? | audio\s*officiel
      | paroles | audio | visuali[sz]er
      | hd | hq | 4k | full\s*hd | mv | m/v
      | remaster(?:ed)?(?:\s*\d{4})?
    )\s*[\)\]]""",
    re.IGNORECASE | re.VERBOSE,
)
# « Titre - Official Video » / « Titre – Clip officiel » en fin.
_DASH_TAIL = re.compile(
    r"\s+[-–—]\s*(?:official\s*(?:music\s*)?(?:video|audio)|clip\s*officiel)\s*$",
    re.IGNORECASE,
)
# « // LETRAS4K », « // Paroles » en fin.
_SLASH_TAIL = re.compile(r"\s*//.*$")
# « | Nom de chaîne » en fin.
_PIPE_TAIL = re.compile(r"\s*\|\s*([^|]*)$")
_CHANNEL_WORDS = re.compile(
    r"\b(?:official|officiel|records?|recordings|music|musique|vevo|tv|"
    r"channel|chaine|chaîne|topic|entertainment|label|studios?|live\s*session)\b",
    re.IGNORECASE,
)
# Qualité collée en fin de titre (majuscules uniquement : « HD », « 4K »).
_QUALITY_TAIL = re.compile(r"\s+(?:HD|HQ|4K|8K|1080p|720p)\s*$")
_HASHTAGS = re.compile(r"(?:\s+#\w+)+\s*$", re.UNICODE)
_SEPARATOR = re.compile(r"\s[-–—]\s")


def _strip_noise(text: str) -> str:
    """Retire hashtags, // ..., | chaîne, mentions et qualité en fin."""
    previous = None
    # Quelques passes : « Titre (Official Video) HD // X » s'imbrique.
    for _ in range(4):
        if text == previous:
            break
        previous = text
        text = _HASHTAGS.sub("", text)
        text = _SLASH_TAIL.sub("", text)
        text = _BRACKET_NOISE.sub("", text)
        text = _DASH_TAIL.sub("", text)
        text = _QUALITY_TAIL.sub("", text)
        text = text.strip()
    return " ".join(text.split())


def _strip_pipe_tail(text: str, artist: str) -> str:
    """« Titre | Chaîne » : n'ôte la fin que si elle ressemble à une chaîne."""
    found = _PIPE_TAIL.search(text)
    if not found or found.start() == 0:
        return text
    tail = found.group(1).strip()
    if not tail:
        return text[: found.start()].strip()
    key = _alnum(artist)
    if _CHANNEL_WORDS.search(tail) or (key and key in _alnum(tail)):
        return text[: found.start()].strip()
    return text


# ---- Artiste ---------------------------------------------------------------

_TOPIC = re.compile(r"\s*[-–—]\s*Topic\s*$", re.IGNORECASE)
_VEVO_SPACED = re.compile(r"\s+VEVO\s*$", re.IGNORECASE)
_OFFICIAL_TAIL = re.compile(r"\s+Official(?:\s+(?:Channel|Music|Artist))?\s*$",
                            re.IGNORECASE)


def _clean_artist(name: str) -> tuple[str, bool]:
    """Nom nettoyé + vrai s'il vient d'une chaîne automatique « - Topic »."""
    text = " ".join((name or "").split())
    topic = False
    if _TOPIC.search(text) and _TOPIC.sub("", text).strip():
        text = _TOPIC.sub("", text).strip()
        topic = True
    for pattern in (_VEVO_SPACED, _OFFICIAL_TAIL):
        reduced = pattern.sub("", text).strip()
        if reduced:
            text = reduced
    # « DaftPunkVEVO » : on ne sait pas re-espacer sûrement, on laisse.
    return text, topic


# Fin de titre qui est une version et non un artiste : « Halo - Live ».
_VERSION_WORDS = re.compile(
    r"^(?:live|remix|remaster(?:ed)?|acoustic|version|edit|radio edit|"
    r"instrumental|demo|reprise|cover|mix|extended|slowed|sped up|"
    r"\d{4}|remastered \d{4})\b",
    re.IGNORECASE,
)


def _split_once(title: str):
    """(gauche, droite) si le titre a exactement un séparateur « - »."""
    if len(_SEPARATOR.findall(title)) != 1:
        return None
    left, right = _SEPARATOR.split(title, 1)
    left, right = left.strip(), right.strip()
    if not (left and right):
        return None
    return left, right


def _guess_artist(artist: str, topic: bool, title: str) -> str | None:
    """Heuristique prudente : « X - Y » téléversé par une chaîne quelconque."""
    if topic or not artist:
        return None           # « Topic » : fiable ; vide : géré par l'appelant
    parts = _split_once(title)
    if parts is None:
        return None
    left, right = parts
    if _alnum(artist) in _alnum(title):
        return None           # l'artiste apparaît déjà dans le titre
    if len(left) > 40 or len(left.split()) > 5:
        return None           # trop long pour un nom d'artiste
    if _VERSION_WORDS.match(_fold(right)) or _VERSION_WORDS.match(_fold(left)):
        return None           # « Halo - Live » : version, pas artiste
    return left


def _prepared(title: str, artist: str) -> tuple[str, str, bool]:
    """Titre sans bruit, artiste nettoyé, drapeau Topic."""
    name, topic = _clean_artist(artist)
    text = _strip_noise(title or "")
    text = _strip_pipe_tail(text, name)
    text = _strip_noise(text)
    return text, name, topic


# ---- API publique ----------------------------------------------------------


def display_artist(artist: str, title: str = "") -> str:
    """Artiste à afficher : nettoyé, éventuellement déduit du titre."""
    text, name, topic = _prepared(title, artist)
    if not name:
        parts = _split_once(text)
        return parts[0] if parts else ""
    guessed = _guess_artist(name, topic, text)
    return guessed or name


def display_title(title: str, artist: str = "") -> str:
    """Titre à afficher, sans « Artiste - » répété ni mentions parasites."""
    original = (title or "").strip()
    text, name, topic = _prepared(original, artist)
    effective = _guess_artist(name, topic, text) or name
    if effective:
        parts = _split_once(text)
        # Un seul séparateur est requis pour le cas déduit ; pour l'artiste
        # connu on accepte aussi « Artiste - Titre - Version ».
        match = re.split(r"\s[-–—]\s", text, maxsplit=1)
        if len(match) == 2 and match[1].strip() and \
                _alnum(match[0]) == _alnum(effective):
            text = match[1].strip()
        elif parts is not None and _alnum(parts[0]) == _alnum(effective):
            text = parts[1]
    return text or original


def canonical_key(title: str, artist: str = "") -> str:
    """Clé de dédoublonnage : titre et artiste affichés, sans casse/accents."""
    shown_artist = display_artist(artist, title)
    shown_title = display_title(title, artist)
    return f"{_alnum(shown_title)}|{_alnum(shown_artist)}"

"""Statistiques d'écoute : périodes et mise en forme (fonctions pures)."""

from __future__ import annotations

from datetime import datetime

DAY = 86400.0

PERIODS = (("7d", "7 jours"), ("30d", "30 jours"),
           ("year", "Cette année"), ("all", "Tout"))


def period_start(key: str, now: float) -> float | None:
    """Début (timestamp) de la période `key`, None pour « tout »."""
    if key == "7d":
        return now - 7 * DAY
    if key == "30d":
        return now - 30 * DAY
    if key == "year":
        return datetime(datetime.fromtimestamp(now).year, 1, 1).timestamp()
    if key == "all":
        return None
    raise ValueError(f"Période inconnue : {key!r}")


def format_listening(seconds: float) -> str:
    """« 12 h 05 », « 47 min », « moins d'une minute »."""
    minutes = int(max(seconds, 0) // 60)
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60:02d}"
    if minutes == 0:
        return "moins d'une minute"
    return f"{minutes} min"


def format_days(count: int) -> str:
    return f"{count} jour{'s' if count > 1 else ''}"


def streak_text(streak: int) -> str:
    """Phrase au tutoiement sous le chiffre de la série."""
    if streak <= 0:
        return "Écoute quelque chose aujourd'hui pour lancer une série."
    if streak == 1:
        return "Tu as écouté aujourd'hui ou hier."
    return f"Tu écoutes depuis {streak} jours d'affilée."


def summary_line(stats: dict) -> str:
    """Résumé d'une phrase, au tutoiement."""
    plays = stats.get("plays", 0)
    if not plays:
        return "Rien ici pour l'instant"
    return (f"Tu as écouté {plays} titre{'s' if plays > 1 else ''}, "
            f"soit {format_listening(stats.get('seconds', 0))}.")

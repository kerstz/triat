#!/bin/sh
# Lanceur Flatpak de Triat.
set -eu
export PYTHONPATH="/app/share/triat:${PYTHONPATH:-}"
# Pas d'écriture de .pyc dans un préfixe en lecture seule.
export PYTHONDONTWRITEBYTECODE=1
# Le dossier Musique de l'hôte est résolu par l'application elle-même
# (core/paths.py, GLib.get_user_special_dir) : aucun xdg-user-dir ici.
exec python3 -m triat "$@"

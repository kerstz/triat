#!/usr/bin/env bash
# Génère python3-requirements.json, référencé par org.triat.Triat.yml.
#
# Demande du réseau (PyPI) : ce fichier ne peut pas être produit hors
# ligne ni écrit à la main, les empreintes doivent venir de PyPI. À relancer
# à chaque changement de requirements.lock, puis à commiter le résultat.
#
# Prérequis : flatpak-pip-generator
#   https://github.com/flatpak/flatpak-builder-tools/tree/master/pip
#   (paquet « flatpak-builder-tools » ou script téléchargé à la main)
#
#   packaging/flatpak/gen-requirements.sh [runtime-sdk]
#
# Le SDK doit correspondre au runtime-version du manifeste.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SDK="${1:-org.gnome.Sdk//51}"

if ! command -v flatpak-pip-generator >/dev/null 2>&1; then
    echo "flatpak-pip-generator introuvable (voir l'en-tête de ce script)" >&2
    exit 1
fi

reqs="$ROOT/requirements.lock"
[ -f "$reqs" ] || reqs="$ROOT/requirements.txt"

# PyGObject vient du runtime GNOME : on ne le demande pas à pip.
# Le verrou porte des empreintes et des marqueurs de plateforme : on ne
# garde que les lignes « nom==version » qui concernent Linux/CPython
# (flatpak-pip-generator recalcule les sha256 depuis PyPI).
filtered="$(mktemp)"
trap 'rm -f "$filtered"' EXIT
grep -E '^[A-Za-z]' "$reqs" \
    | grep -viE '^(pygobject|pycairo)([=<>~ ]|$)' \
    | grep -vE "sys_platform == 'win32'|implementation_name != 'cpython' *\\\\?$|^brotlicffi" \
    | sed -E 's/ *;.*//; s/ *\\$//' > "$filtered"

# Wheels de préférence : évite de compiler cryptography (Rust) et cffi en sdist.
# --prefer-wheels prend une liste de paquets séparés par des virgules
# (surcharge possible : PREFER_WHEELS=a,b ./gen-requirements.sh) et exige --runtime.
flatpak-pip-generator --runtime "$SDK" \
    --prefer-wheels="${PREFER_WHEELS:-cryptography,cffi,brotli,pycryptodomex,zeroconf}" \
    --requirements-file "$filtered" \
    --output "$HERE/python3-requirements"
echo "écrit : $HERE/python3-requirements.json"

#!/usr/bin/env bash
# Installe Triat dans une session Nothing OS.
#
# Conventions reprises du script ./install des dotfiles : copie (pas de
# lien symbolique), sauvegarde des fichiers existants en .bak-<date>,
# et un drapeau --uninstall.
#
#   ./install.sh              installe l'application et l'intégration
#   ./install.sh --files      intégration Hyprland et barre Quickshell seulement
#   ./install.sh --uninstall  retire ce qui a été posé

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
PREFIX="${PREFIX:-$HOME/.local}"
# Le code vit dans $APP_DIR, JAMAIS dans $PREFIX/share/triat : ce dossier-là
# est celui des données de l'utilisateur (library.db, réglages, cookies),
# que l'installation et la désinstallation ne doivent pas toucher.
APP_DIR="$PREFIX/lib/triat"
LEGACY_DIR="$PREFIX/share/triat"
HYPR_DIR="$HOME/.config/hypr"
CUSTOM="$HYPR_DIR/custom.lua"
MARKER='require("triat")'

info() { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }

backup() {
    [ -e "$1" ] || return 0
    cp -a "$1" "$1.bak-$STAMP"
    info "sauvegarde : $(basename "$1").bak-$STAMP"
}

# Anciennes installations : le code était posé dans $PREFIX/share/triat, à
# côté des données. On n'en retire que ce que ce script y avait mis (triat/,
# data/, venv/), jamais le dossier lui-même ni le reste de son contenu.
remove_legacy_code() {
    local sub removed=0
    for sub in triat data venv; do
        if [ -e "$LEGACY_DIR/$sub" ]; then
            rm -rf "${LEGACY_DIR:?}/$sub"
            removed=1
        fi
    done
    [ "$removed" -eq 0 ] || info "ancien code retiré de $LEGACY_DIR (données conservées)"
}

install_app() {
    info "installation de Triat dans $APP_DIR"
    mkdir -p "$APP_DIR" "$PREFIX/bin" \
             "$PREFIX/share/applications" "$PREFIX/share/icons"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
              --exclude '__pycache__' --exclude '*.pyc' \
              "$ROOT/triat" "$ROOT/data" "$APP_DIR/"
    else
        rm -rf "$APP_DIR/triat" "$APP_DIR/data"
        cp -a "$ROOT/triat" "$ROOT/data" "$APP_DIR/"
        find "$APP_DIR" -name '__pycache__' -prune -exec rm -rf {} +
    fi

    # Environnement Python dédié. `--system-site-packages` pour hériter de
    # PyGObject, qui vient de la distribution et ne s'installe pas par pip.
    local venv="$APP_DIR/venv"
    if [ ! -x "$venv/bin/python" ]; then
        info "création de l'environnement Python"
        python3 -m venv --system-site-packages "$venv"
    fi
    info "installation des dépendances"
    "$venv/bin/pip" install -q --upgrade pip
    # Versions figées quand le verrou existe : deux installations donnent
    # alors exactement le même environnement.
    local reqs="$ROOT/requirements.lock"
    [ -f "$reqs" ] || reqs="$ROOT/requirements.txt"
    "$venv/bin/pip" install -q -r "$reqs"

    cat > "$PREFIX/bin/triat" <<LAUNCHER
#!/bin/sh
# Lanceur de Triat (généré par install.sh).
export PYTHONPATH="$APP_DIR:\${PYTHONPATH:-}"
export PYTHONDONTWRITEBYTECODE=1
exec "$venv/bin/python" -m triat "\$@"
LAUNCHER
    chmod +x "$PREFIX/bin/triat"

    install -Dm644 "$ROOT/packaging/flatpak/org.triat.Triat.desktop" \
        "$PREFIX/share/applications/org.triat.Triat.desktop"
    install -Dm644 "$ROOT/data/icons/hicolor/scalable/apps/org.triat.Triat.svg" \
        "$PREFIX/share/icons/hicolor/scalable/apps/org.triat.Triat.svg"
    command -v gtk-update-icon-cache >/dev/null 2>&1 \
        && gtk-update-icon-cache -qtf "$PREFIX/share/icons/hicolor" 2>/dev/null || true
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true
    # Les lanceurs (fuzzel, rofi, Quickshell) relisent le .desktop : il doit
    # désigner le lanceur réel, pas un nom d'avant renommage.
    desktop-file-validate "$PREFIX/share/applications/org.triat.Triat.desktop" \
        2>/dev/null || true
    # Contrôle réel : importer l'interface (GTK, libadwaita, libmpv), pas
    # seulement afficher l'aide, qui réussirait avec une GTK cassée.
    if PYTHONPATH="$APP_DIR" "$venv/bin/python" -c \
            "import triat.app, triat.ui.immersion, mpv" >/dev/null 2>&1 \
       && "$PREFIX/bin/triat" --help >/dev/null 2>&1; then
        info "lanceur : $PREFIX/bin/triat"
    else
        warn "le lanceur ne démarre pas — vérifiez $APP_DIR/venv"
    fi

    # Migration : l'ancien code (même dossier que les données) devient
    # inutile une fois le nouveau en place.
    remove_legacy_code

    case ":$PATH:" in
        *":$PREFIX/bin:"*) ;;
        *) warn "$PREFIX/bin n'est pas dans le PATH" ;;
    esac
}

install_files() {
    mkdir -p "$HYPR_DIR"
    backup "$HYPR_DIR/triat.lua"
    cp "$ROOT/packaging/nothing-os/triat.lua" "$HYPR_DIR/triat.lua"
    info "règles Hyprland : $HYPR_DIR/triat.lua"

    if [ -f "$CUSTOM" ] && grep -qF "$MARKER" "$CUSTOM"; then
        info "custom.lua charge déjà triat.lua"
    else
        backup "$CUSTOM"
        {
            printf '\n-- Triat : lecteur de musique (ajouté le %s)\n' "$STAMP"
            printf '%s\n' "$MARKER"
        } >> "$CUSTOM"
        info "custom.lua : $MARKER ajouté"
    fi

    # Barre Quickshell : module Triat (logo, titre, contrôles).
    python3 "$ROOT/packaging/nothing-os/quickshell/install_bar.py"

    if command -v hyprctl >/dev/null 2>&1; then
        hyprctl reload >/dev/null 2>&1 && info "Hyprland rechargé"
    fi
}

uninstall() {
    # Jamais `rm -rf $PREFIX/share/triat` : c'est le dossier des données.
    rm -rf "$APP_DIR" "$PREFIX/bin/triat" \
           "$PREFIX/share/applications/org.triat.Triat.desktop" \
           "$PREFIX/share/icons/hicolor/scalable/apps/org.triat.Triat.svg" \
           "$HYPR_DIR/triat.lua"
    remove_legacy_code
    if [ -f "$CUSTOM" ]; then
        backup "$CUSTOM"
        grep -vF "$MARKER" "$CUSTOM" > "$CUSTOM.tmp" && mv "$CUSTOM.tmp" "$CUSTOM"
    fi
    python3 "$ROOT/packaging/nothing-os/quickshell/install_bar.py" --remove
    info "Triat retiré. Vos données restent dans ~/.local/share/triat."
}

case "${1:-all}" in
    --uninstall) uninstall ;;
    --files)     install_files ;;
    --app)       install_app ;;
    all|"")      install_app; install_files ;;
    *)           warn "option inconnue : $1"; exit 2 ;;
esac

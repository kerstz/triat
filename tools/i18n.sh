#!/bin/sh
# Internationalisation : extraction (xgettext) et compilation (msgfmt).
#
#   tools/i18n.sh            extrait po/triat.pot puis compile po/*.po
#   tools/i18n.sh extract    extrait seulement
#   tools/i18n.sh compile    compile seulement (triat/locale/<lang>/LC_MESSAGES/triat.mo)
#
# Langue source : français (les msgid sont les chaînes françaises). Les .mo
# sont des artefacts de build (triat/locale/ est ignoré par git).
set -eu
cd "$(dirname "$0")/.."

extract() {
    # shellcheck disable=SC2046
    xgettext --language=Python --keyword=_ --keyword=ngettext:1,2 \
        --keyword=nformat:1,2 --keyword=pgettext:1c,2 --keyword=N_ \
        --from-code=UTF-8 --no-location \
        --package-name=triat --copyright-holder=Triat \
        -o po/triat.pot $(cat po/POTFILES)
}

compile() {
    for po in po/*.po; do
        [ -e "$po" ] || continue
        lang=$(basename "$po" .po)
        mkdir -p "triat/locale/$lang/LC_MESSAGES"
        msgfmt --check -o "triat/locale/$lang/LC_MESSAGES/triat.mo" "$po"
    done
}

case "${1:-all}" in
    extract) extract ;;
    compile) compile ;;
    all) extract; compile ;;
    *) echo "usage : $0 [extract|compile]" >&2; exit 2 ;;
esac

#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Regenerates the translation template from the sources and merges it into
# every existing translation. Add a language with: msginit -i razer-blade-control.pot -l <lang>
set -euo pipefail

PO="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="razer-blade-control"

cd "$PO/../extension/razer-blade-control@SymonRod"
xgettext --from-code=UTF-8 --language=JavaScript \
    --keyword=_ --keyword=N_ --add-comments=Translators \
    --package-name="$DOMAIN" --msgid-bugs-address=https://github.com/SymonRod/razer-blade-control/issues \
    --output="$PO/$DOMAIN.pot" \
    extension.js prefs.js daemon.js

for po in "$PO"/*.po; do
    [[ -e $po ]] || continue
    msgmerge --quiet --update --backup=none "$po" "$PO/$DOMAIN.pot"
done

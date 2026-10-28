#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Builds the extension zip, with compiled translations, for extensions.gnome.org
# and install.sh. Output: dist/<uuid>.shell-extension.zip
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
UUID="razer-blade-control@SymonRod"

mkdir -p "$SRC/dist"
gnome-extensions pack "$SRC/extension/$UUID" \
    --force \
    --podir="$SRC/po" \
    --extra-source=daemon.js \
    --extra-source=icons \
    --out-dir="$SRC/dist"
echo "$SRC/dist/$UUID.shell-extension.zip"

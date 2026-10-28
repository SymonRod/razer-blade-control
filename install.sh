#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Installs razerbladed (user service), the udev rule and the GNOME extension.
# Run as your normal user: sudo is used only for the udev rule and the GPU helper.
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
    echo "Run this script as your normal user, not with sudo." >&2
    exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
UUID="razer-blade-control@SymonRod"
BINDIR="$HOME/.local/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DBUS_DIR="$DATA_HOME/dbus-1/services"

echo "==> udev rule (needs sudo)"
sudo install -Dm644 "$SRC/data/70-razerblade.rules" /etc/udev/rules.d/70-razerblade.rules
sudo udevadm control --reload
sudo udevadm trigger --action=change --subsystem-match=hidraw
udevadm settle

echo "==> GPU helper and polkit action (needs sudo)"
sudo install -Dm755 "$SRC/daemon/razerblade-gpu" /usr/local/libexec/razerblade-gpu
sudo install -Dm644 "$SRC/data/io.github.symonrod.razerbladecontrol.gpu.policy" \
    /usr/share/polkit-1/actions/io.github.symonrod.razerbladecontrol.gpu.policy

echo "==> Daemon"
install -Dm755 "$SRC/daemon/razerbladed.py" "$BINDIR/razerbladed"
mkdir -p "$UNIT_DIR" "$DBUS_DIR"
sed "s|@BINDIR@|$BINDIR|g" "$SRC/data/razerbladed.service" > "$UNIT_DIR/razerbladed.service"
sed "s|@BINDIR@|$BINDIR|g" "$SRC/data/io.github.SymonRod.RazerBladeControl.service" > "$DBUS_DIR/io.github.SymonRod.RazerBladeControl.service"
systemctl --user daemon-reload
systemctl --user enable razerbladed.service
systemctl --user restart razerbladed.service

echo "==> GNOME extension"
gnome-extensions install --force "$("$SRC/pack.sh" | tail -n 1)"
gnome-extensions enable "$UUID" 2>/dev/null || true

echo
"$BINDIR/razerbladed" --probe || true
echo
if gnome-extensions info "$UUID" &>/dev/null; then
    echo "Done. If the extension was already loaded, log out and back in to reload it."
else
    echo "Done. Log out and back in (Wayland), then run: gnome-extensions enable $UUID"
fi

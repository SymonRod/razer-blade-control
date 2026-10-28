#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
set -euo pipefail

UUID="razer-blade-control@SymonRod"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"

gnome-extensions disable "$UUID" 2>/dev/null || true
rm -rf "$DATA_HOME/gnome-shell/extensions/$UUID"

systemctl --user disable --now razerbladed.service 2>/dev/null || true
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/razerbladed.service" \
      "$DATA_HOME/dbus-1/services/io.github.SymonRod.RazerBladeControl.service" \
      "$HOME/.local/bin/razerbladed"
systemctl --user daemon-reload

sudo rm -f /etc/udev/rules.d/70-razerblade.rules
sudo udevadm control --reload

# Without the extension there would be no way back from "integrated", so restore
# the driver defaults (effective at the next boot)
sudo rm -f /usr/local/libexec/razerblade-gpu \
    /usr/share/polkit-1/actions/io.github.symonrod.razerbladecontrol.gpu.policy \
    /etc/modprobe.d/razerblade-gpu.conf \
    /etc/udev/rules.d/50-razerblade-gpu.rules

echo "Uninstalled. Settings are kept in ~/.config/razerblade/."
echo "The NVIDIA GPU goes back to the driver defaults from the next boot."

# Razer Blade Control

GNOME Shell extension (GNOME 50) that controls keyboard RGB lighting, fans, performance profile and NVIDIA GPU
power on the Razer Blade 15 Advanced (2019, USB `1532:023a`), straight from Quick Settings.

- Keyboard toggle with effects (static, breathing, spectrum, wave, reactive, starlight), color presets, effect
  speed and the lid logo
- Keyboard brightness slider next to the display brightness one
- Fan toggle with balanced/gaming/creator profiles and a manual RPM slider
- NVIDIA GPU toggle: always on, automatic (runtime D3) or off
- Preferences window for custom colors, wave direction and a battery saver for the wave effect

No kernel module is needed, so it works with Secure Boot enabled.

## Installation

The extension is only the user interface: it talks to the `razerbladed` service, which needs a udev rule and,
for the GPU mode, a root helper. Installing the extension from extensions.gnome.org alone is not enough, so run
the installer once:

```bash
git clone https://github.com/SymonRod/razer-blade-control.git
cd razer-blade-control
./install.sh      # sudo is used only for the udev rule and the GPU helper
```

On Wayland a newly installed extension shows up only after logging out and back in; then:

```bash
gnome-extensions enable razer-blade-control@SymonRod
```

To uninstall: `./uninstall.sh`.

## Architecture

```
GNOME Shell ──D-Bus (session)──▶ razerbladed ──HID feature report──▶ /dev/hidraw* ──▶ laptop EC
 (extension.js, prefs.js)         (Python, user service)
```

- **`daemon/razerbladed.py`**: user service (systemd `--user`, D-Bus activatable) that speaks the Razer HID
  protocol (90-byte reports) directly over hidraw. It saves its state in `~/.config/razerblade/state.json` and
  applies it again at startup and on resume from suspend.
- **`data/70-razerblade.rules`**: `uaccess` udev rule that gives the user of the active session access to the
  Blade's hidraw nodes. No root daemon.
- **`extension/razer-blade-control@SymonRod`**: Quick Settings toggles (keyboard, fans, GPU), a keyboard
  brightness slider, an effect speed slider for Wave/Reactive/Starlight, and a preferences window for custom
  colors, speed and direction.
- **`daemon/razerblade-gpu`** + **`data/io.github.razerblade.gpu.policy`**: root helper started by the extension
  through `pkexec` (polkit action `io.github.razerblade.gpu`, `auth_admin_keep`) to choose the NVIDIA GPU mode.

## NVIDIA GPU

The **NVIDIA GPU** toggle shows the current state (on = the GPU is powered right now) and offers three modes,
which take effect at the next boot:

| Mode | What it does | Files |
|---|---|---|
| Always on (`standard`) | driver default: on Turing the GPU never powers off (~7 W idle) | comments only |
| Automatic (`hybrid`) | runtime D3: the GPU goes to D3cold when nothing uses it and wakes up by itself (HDMI, games, CUDA) | `options nvidia NVreg_DynamicPowerManagement=0x02` |
| Off (`integrated`) | nvidia modules blacklisted and PCI functions `10de` removed at boot by udev: no HDMI, GPU USB-C or CUDA | + `/etc/udev/rules.d/50-razerblade-gpu.rules` |

The configuration lives in `/etc/modprobe.d/razerblade-gpu.conf` (`# mode=` line). The daemon only reads sysfs
(`power/runtime_status`) and `/proc/driver/nvidia/params`: `nvidia-smi` would wake the GPU up. The state is
read again every second while Quick Settings is open. From a terminal:
`pkexec /usr/local/libexec/razerblade-gpu hybrid`.

## Debugging

```bash
razerbladed --probe                  # detect the control interface (read only)
journalctl --user -u razerbladed -f  # service log
busctl --user call io.github.razerblade.Daemon /io/github/razerblade/Daemon \
    io.github.razerblade.Daemon1 GetState
busctl --user call io.github.razerblade.Daemon /io/github/razerblade/Daemon \
    io.github.razerblade.Daemon1 SetEffect 'sa{sv}' static 1 color s '#ff0000'
journalctl /usr/bin/gnome-shell -f   # extension errors
```

## D-Bus API (`io.github.razerblade.Daemon1`)

| Method | Signature | Notes |
|---|---|---|
| `GetState` | `→ a{sv}` | full state, including `connected`, `error` and `gpu-available`, `gpu-mode`, `gpu-booted-mode`, `gpu-power` |
| `SetEffect` | `s, a{sv}` | effects: `none static breathing breathing-dual breathing-random spectrum wave reactive starlight`; options `color`, `color2` (`#rrggbb`), `speed`, `direction` |
| `SetBrightness` | `y` | 0–255 |
| `SetLogo` | `b` | lid logo |
| `SetPowerMode` | `s` | `balanced`, `gaming`, `creator` |
| `SetFanRpm` | `q` | `0` = automatic, otherwise clamped to 3500–5300 RPM |
| `SetBatteryThreshold` | `y` | 0–100, charge below which Wave turns static on battery (`0` = never) |
| `Reapply` | | send all settings again |

Signal `StateChanged(a{sv})` after every change.

## Protocol

The commands come from [openrazer](https://github.com/openrazer/openrazer) (effects, brightness, transaction ID
`0xFF` for this model) and [razer-laptop-control](https://github.com/Razer-Linux/razer-laptop-control-no-dkms)
(fans and profiles, class `0x0D`, 3500–5300 RPM range). The right interface and transaction ID are detected at
startup with a harmless read (keyboard brightness, `0x0E:0x84`).

The wave built into the EC has a fixed speed, so **Wave** is generated by the daemon: a full cycle of custom
frames (`0x03:0x0B` per row, then `0x03:0x0A` effect `0x05`) is pregenerated on every speed/direction change and
sent at 30 fps without waiting for the reply (~2% of one core). The animation stops at brightness 0, during
suspend and with any other effect, which the EC handles at no cost. On battery, below `battery_threshold`
(default 20%, `0` disables it; read from UPower) Wave temporarily becomes Static in the primary color and comes
back by itself on AC power or above the threshold.

To add another model, add an entry to `SUPPORTED_DEVICES` in `razerbladed.py` and its PID to the udev rule.

## Translations

User-visible strings go through gettext (domain `razer-blade-control`). After changing them:

```bash
./po/update.sh                                    # regenerate po/razer-blade-control.pot, merge into *.po
msginit -i po/razer-blade-control.pot -l de -o po/de.po   # start a new language
```

## Releasing

```bash
./pack.sh   # dist/razer-blade-control@SymonRod.shell-extension.zip, with compiled translations
```

Upload the zip at <https://extensions.gnome.org/upload/>.

## License

[GPL-2.0-or-later](LICENSE).

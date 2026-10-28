#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""razerbladed - session D-Bus service for Razer Blade keyboard RGB and fans.

Talks to the laptop's embedded controller through the Razer HID protocol
(90-byte feature reports) on /dev/hidraw*, so no kernel module is required.
Access to the hidraw node is granted to the logged-in user by a udev
"uaccess" rule (see data/70-razerblade.rules).

Protocol references: openrazer (driver/razerchromacommon.c) and
razer-laptop-control (razer_control_gui/src/daemon/device.rs).
"""

import argparse
import colorsys
import fcntl
import json
import logging
import os
import time
from pathlib import Path

import gi
from gi.repository import Gio, GLib

try:
    gi.require_version('GLibUnix', '2.0')
    from gi.repository import GLibUnix
    unix_signal_add = GLibUnix.signal_add
except (ImportError, ValueError):
    unix_signal_add = GLib.unix_signal_add

log = logging.getLogger('razerbladed')

VENDOR_ID = 0x1532
SUPPORTED_DEVICES = {
    0x023A: {
        'name': 'Razer Blade 15 Advanced (2019)',
        'fan': (3500, 5300),
        'power_modes': ['balanced', 'gaming', 'creator'],
        'logo': True,
        'matrix': (6, 16),  # rows, columns
    },
}

REPORT_LEN = 90
RAZER_STATUS_BUSY = 0x01
RAZER_STATUS_SUCCESS = 0x02
RAZER_STATUS_NOT_SUPPORTED = 0x05


def _hid_ioc(nr, size):
    # _IOC(_IOC_READ | _IOC_WRITE, 'H', nr, size)
    return (3 << 30) | (size << 16) | (ord('H') << 8) | nr


HIDIOCSFEATURE = _hid_ioc(0x06, REPORT_LEN + 1)
HIDIOCGFEATURE = _hid_ioc(0x07, REPORT_LEN + 1)

POWER_MODES = {'balanced': 0x00, 'gaming': 0x01, 'creator': 0x02}

# Effect ids for command 0x03:0x0A (standard matrix effect)
EFFECT_NONE = 0x00
EFFECT_REACTIVE = 0x02
EFFECT_BREATHING = 0x03
EFFECT_SPECTRUM = 0x04
EFFECT_CUSTOM = 0x05
EFFECT_STATIC = 0x06
EFFECT_STARLIGHT = 0x19

EFFECTS = ['none', 'static', 'breathing', 'breathing-dual', 'breathing-random',
           'spectrum', 'wave', 'reactive', 'starlight']

NOSTORE = 0x00
VARSTORE = 0x01
LOGO_LED = 0x04

# The EC's own wave has a fixed speed, so the wave is streamed as custom frames.
WAVE_FPS = 30
# Seconds for the rainbow to cross the keyboard, indexed by speed - 1 (1 is the fastest)
WAVE_PERIODS = (0.6, 0.8, 1.1, 1.5, 2.0, 2.7, 3.6, 4.8, 6.4)

NVIDIA_VENDOR = '0x10de'
GPU_CONFIG = Path('/etc/modprobe.d/razerblade-gpu.conf')  # written by razerblade-gpu
NVIDIA_PARAMS = Path('/proc/driver/nvidia/params')

UPOWER_DISPLAY_DEVICE = '/org/freedesktop/UPower/devices/DisplayDevice'
UPOWER_DISCHARGING = 2

DEFAULT_STATE = {
    'effect': 'static',
    'last_effect': 'static',
    'color': '#00ff00',
    'color2': '#0000ff',
    'speed': 2,
    'direction': 1,
    'brightness': 160,
    'logo': True,
    'power': 'balanced',
    'fan_rpm': 0,
    'last_fan_rpm': 4000,
    'battery_threshold': 20,  # below this percentage, on battery, the wave becomes static
}

STATE_FILE = Path(GLib.get_user_config_dir()) / 'razerblade' / 'state.json'

BUS_NAME = 'io.github.SymonRod.RazerBladeControl'
OBJECT_PATH = '/io/github/SymonRod/RazerBladeControl'
INTERFACE_XML = """
<node>
  <interface name="io.github.SymonRod.RazerBladeControl1">
    <method name="GetState">
      <arg name="state" type="a{sv}" direction="out"/>
    </method>
    <method name="SetEffect">
      <arg name="effect" type="s" direction="in"/>
      <arg name="options" type="a{sv}" direction="in"/>
    </method>
    <method name="SetBrightness">
      <arg name="brightness" type="y" direction="in"/>
    </method>
    <method name="SetLogo">
      <arg name="enabled" type="b" direction="in"/>
    </method>
    <method name="SetPowerMode">
      <arg name="mode" type="s" direction="in"/>
    </method>
    <method name="SetFanRpm">
      <arg name="rpm" type="q" direction="in"/>
    </method>
    <method name="SetBatteryThreshold">
      <arg name="percent" type="y" direction="in"/>
    </method>
    <method name="Reapply"/>
    <signal name="StateChanged">
      <arg name="state" type="a{sv}"/>
    </signal>
  </interface>
</node>
"""


class DeviceError(Exception):
    pass


def parse_color(value):
    value = value.strip().lstrip('#')
    if len(value) != 6:
        raise ValueError(f'invalid color: {value!r}')
    return bytes.fromhex(value)


def build_report(transaction_id, command_class, command_id, data_size, args=b''):
    report = bytearray(REPORT_LEN)
    report[1] = transaction_id
    report[5] = data_size
    report[6] = command_class
    report[7] = command_id
    report[8:8 + len(args)] = bytes(args)
    crc = 0
    for b in report[2:88]:
        crc ^= b
    report[88] = crc
    return report


def wave_row(cols, phase, direction):
    """RGB bytes for one row of the rainbow wave; the spectrum spans the whole row."""
    sign = 1 if direction == 1 else -1  # 1: left to right
    row = bytearray()
    for col in range(cols):
        r, g, b = colorsys.hsv_to_rgb((sign * col / cols - phase) % 1.0, 1, 1)
        row += bytes((round(r * 255), round(g * 255), round(b * 255)))
    return bytes(row)


def find_hidraw_nodes():
    """Yield (devnode, pid, interface_number) for supported Razer devices."""
    for entry in sorted(Path('/sys/class/hidraw').glob('hidraw*')):
        try:
            uevent = (entry / 'device' / 'uevent').read_text()
        except OSError:
            continue
        props = dict(line.split('=', 1) for line in uevent.splitlines() if '=' in line)
        bus, vid, pid = (int(x, 16) for x in props.get('HID_ID', '0:0:0').split(':'))
        if vid != VENDOR_ID or pid not in SUPPORTED_DEVICES:
            continue
        phys = props.get('HID_PHYS', '')
        iface = int(phys.rsplit('input', 1)[1]) if 'input' in phys else 99
        yield f'/dev/{entry.name}', pid, iface


def find_nvidia_gpu():
    """sysfs directory of the NVIDIA display controller, or None if it's not on the bus."""
    for dev in Path('/sys/bus/pci/devices').iterdir():
        try:
            if ((dev / 'vendor').read_text().strip() == NVIDIA_VENDOR
                    and (dev / 'class').read_text().startswith('0x03')):
                return dev
        except OSError:
            continue
    return None


def gpu_status():
    """(available, configured mode, booted mode, runtime PM status) of the NVIDIA GPU.

    Only sysfs and the driver's module parameters are read: nvidia-smi and
    /proc/driver/nvidia/gpus/* would wake the GPU up.
    """
    configured, has_config = 'standard', False
    try:
        for line in GPU_CONFIG.read_text().splitlines():
            if line.startswith('# mode='):
                configured = line.split('=', 1)[1].strip()
        has_config = True
    except OSError:
        pass

    dev = find_nvidia_gpu()
    if dev is None:
        return has_config, configured, 'integrated', 'off'
    try:
        power = (dev / 'power' / 'runtime_status').read_text().strip()
    except OSError:
        power = 'active'
    booted = 'standard'
    try:
        for line in NVIDIA_PARAMS.read_text().splitlines():
            key, _, value = line.partition(':')
            # 3, the default, turns runtime D3 on only for Ampere and newer
            if key == 'DynamicPowerManagement' and int(value) in (1, 2):
                booted = 'hybrid'
    except (OSError, ValueError):
        pass
    return True, configured, booted, power


class BladeDevice:
    def __init__(self, fd, path, pid, transaction_id):
        self.fd = fd
        self.path = path
        self.pid = pid
        self.info = SUPPORTED_DEVICES[pid]
        self.transaction_id = transaction_id

    @classmethod
    def open(cls):
        """Find the control interface by probing a harmless read command."""
        errors = []
        for path, pid, _iface in sorted(find_hidraw_nodes(), key=lambda n: n[2]):
            try:
                fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
            except OSError as e:
                errors.append(f'{path}: {e.strerror}')
                continue
            for tid in (0xFF, 0x1F):
                dev = cls(fd, path, pid, tid)
                try:
                    if dev.get_brightness() is not None:
                        log.info('using %s (transaction id 0x%02X)', path, tid)
                        return dev
                except DeviceError as e:
                    errors.append(f'{path}/0x{tid:02X}: {e}')
            os.close(fd)
        raise DeviceError('no usable Razer control interface found'
                          + (f' ({"; ".join(errors)})' if errors else ''))

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def request(self, command_class, command_id, data_size, args=b''):
        """Feature report ready for HIDIOCSFEATURE (report id 0 + payload)."""
        return b'\x00' + build_report(self.transaction_id, command_class, command_id, data_size, args)

    def send(self, command_class, command_id, data_size, args=b''):
        request = self.request(command_class, command_id, data_size, args)
        try:
            for _attempt in range(3):
                fcntl.ioctl(self.fd, HIDIOCSFEATURE, request)
                time.sleep(0.001)
                for _poll in range(10):
                    response = bytearray(REPORT_LEN + 1)
                    fcntl.ioctl(self.fd, HIDIOCGFEATURE, response, True)
                    response = response[1:]
                    if response[0] == RAZER_STATUS_BUSY:
                        time.sleep(0.002)
                        continue
                    break
                if response[0] == RAZER_STATUS_NOT_SUPPORTED:
                    raise DeviceError(f'command 0x{command_class:02X}:0x{command_id:02X} not supported')
                if (response[0] == RAZER_STATUS_SUCCESS
                        and response[6] == command_class and response[7] == command_id):
                    return response
                time.sleep(0.008)
        except OSError as e:
            raise DeviceError(f'I/O error on {self.path}: {e.strerror}') from e
        raise DeviceError(f'no valid response to 0x{command_class:02X}:0x{command_id:02X}')

    def write(self, request):
        """Send a prebuilt request without waiting for the response (animation frames)."""
        try:
            fcntl.ioctl(self.fd, HIDIOCSFEATURE, request)
        except OSError as e:
            raise DeviceError(f'I/O error on {self.path}: {e.strerror}') from e

    # Keyboard

    def get_brightness(self):
        response = self.send(0x0E, 0x84, 0x02, [VARSTORE])
        return response[9]

    def set_brightness(self, value):
        self.send(0x0E, 0x04, 0x02, [VARSTORE, value])

    def set_effect(self, effect, color, color2, speed, direction):
        if effect == 'none':
            self.send(0x03, 0x0A, 0x01, [EFFECT_NONE])
        elif effect == 'static':
            self.send(0x03, 0x0A, 0x04, [EFFECT_STATIC, *color])
        elif effect == 'breathing':
            self.send(0x03, 0x0A, 0x08, [EFFECT_BREATHING, 0x01, *color])
        elif effect == 'breathing-dual':
            self.send(0x03, 0x0A, 0x08, [EFFECT_BREATHING, 0x02, *color, *color2])
        elif effect == 'breathing-random':
            self.send(0x03, 0x0A, 0x08, [EFFECT_BREATHING, 0x03])
        elif effect == 'spectrum':
            self.send(0x03, 0x0A, 0x01, [EFFECT_SPECTRUM])
        elif effect == 'reactive':
            self.send(0x03, 0x0A, 0x05, [EFFECT_REACTIVE, min(max(speed, 1), 4), *color])
        elif effect == 'starlight':
            # Same (odd) data size as openrazer uses for this device
            self.send(0x03, 0x0A, 0x01, [EFFECT_STARLIGHT, 0x01, min(max(speed, 1), 3), *color])
        else:
            raise ValueError(f'unknown effect: {effect}')

    def custom_frame_requests(self, rows):
        """Requests that upload one frame (a row of RGB bytes per matrix row) and show it."""
        cols = self.info['matrix'][1]
        requests = [self.request(0x03, 0x0B, 0x46, bytes([0xFF, index, 0, cols - 1]) + row)
                    for index, row in enumerate(rows)]
        requests.append(self.request(0x03, 0x0A, 0x02, [EFFECT_CUSTOM, NOSTORE]))
        return requests

    def show_custom_frame(self):
        """Like the last custom_frame_requests() item, but waits so errors surface."""
        self.send(0x03, 0x0A, 0x02, [EFFECT_CUSTOM, NOSTORE])

    def set_logo(self, enabled):
        if enabled:
            self.send(0x03, 0x02, 0x03, [VARSTORE, LOGO_LED, 0x00])  # static
        self.send(0x03, 0x00, 0x03, [VARSTORE, LOGO_LED, 0x01 if enabled else 0x00])

    # Fans / power

    def set_power(self, mode, fan_rpm):
        """fan_rpm == 0 means automatic fan control."""
        mode_id = POWER_MODES[mode]
        manual = 0x01 if fan_rpm else 0x00
        for zone in (0x01, 0x02):
            self.send(0x0D, 0x82, 0x04, [0x00, zone, 0x00, 0x00])
            self.send(0x0D, 0x02, 0x04, [0x00, zone, mode_id, manual])
            if fan_rpm:
                self.send(0x0D, 0x01, 0x03, [0x00, zone, fan_rpm // 100])


class WaveAnimation:
    """Rainbow wave streamed to the keyboard; one full cycle of requests is prebuilt."""

    def __init__(self, dev, speed, direction, phase, on_error):
        self.dev = dev
        self.on_error = on_error
        self.period = WAVE_PERIODS[min(max(speed, 1), len(WAVE_PERIODS)) - 1]
        rows, cols = dev.info['matrix']
        count = round(self.period * WAVE_FPS)
        self.frames = [dev.custom_frame_requests([wave_row(cols, i / count, direction)] * rows)
                       for i in range(count)]
        # Start from the given phase so speed changes don't make the wave jump
        self.start = time.monotonic() - phase * self.period
        self.source_id = 0

    @property
    def phase(self):
        return (time.monotonic() - self.start) / self.period % 1.0

    def show(self):
        for request in self.frames[int(self.phase * len(self.frames)) % len(self.frames)]:
            self.dev.write(request)

    def set_running(self, running):
        if running and not self.source_id:
            self.source_id = GLib.timeout_add(1000 // WAVE_FPS, self.on_tick)
        elif not running and self.source_id:
            GLib.source_remove(self.source_id)
            self.source_id = 0

    def on_tick(self):
        try:
            self.show()
        except DeviceError as e:
            self.source_id = 0
            self.on_error(e)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE


class Daemon:
    def __init__(self, bus):
        self.bus = bus
        self.device = None
        self.wave = None
        self.upower = None
        self.battery_saver = False
        self.last_error = ''
        self.state = self.load_state()
        self.node_info = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
        self.interface = self.node_info.interfaces[0]
        register = getattr(bus, 'register_object_with_closures2', bus.register_object)
        register(OBJECT_PATH, self.interface, self.on_method_call)
        self.ensure_device()
        if STATE_FILE.exists():
            self.apply_all()
        Gio.bus_get(Gio.BusType.SYSTEM, None, self.on_system_bus)

    # Persistence

    @staticmethod
    def load_state():
        state = dict(DEFAULT_STATE)
        try:
            state.update(json.loads(STATE_FILE.read_text()))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning('cannot read %s: %s', STATE_FILE, e)
        return state

    def save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(STATE_FILE)

    # Device handling

    def ensure_device(self):
        if self.device:
            return True
        try:
            self.device = BladeDevice.open()
            self.last_error = ''
        except DeviceError as e:
            self.last_error = str(e)
            log.warning('%s', e)
        return self.device is not None

    def close_device(self):
        self.stop_wave()
        if self.device:
            self.device.close()
            self.device = None

    def run(self, func):
        """Run func(device), reopening the device once if it went away."""
        for _attempt in range(2):
            if not self.ensure_device():
                raise DeviceError(self.last_error)
            try:
                return func(self.device)
            except DeviceError as e:
                log.warning('%s', e)
                self.last_error = str(e)
                self.close_device()
        raise DeviceError(self.last_error)

    def clamp_rpm(self, rpm):
        if rpm == 0:
            return 0
        lo, hi = self.device_info()['fan']
        return min(max(rpm, lo), hi)

    def device_info(self):
        if self.device:
            return self.device.info
        return next(iter(SUPPORTED_DEVICES.values()))

    def apply_effect(self, dev):
        s = self.state
        phase = self.stop_wave()
        effect = s['effect']
        if effect == 'wave' and self.battery_saver:
            effect = 'static'  # streaming frames costs CPU, the EC's static effect doesn't
        if effect != 'wave':
            dev.set_effect(effect, parse_color(s['color']), parse_color(s['color2']),
                           s['speed'], s['direction'])
            return
        self.wave = WaveAnimation(dev, s['speed'], s['direction'], phase, self.on_wave_error)
        self.wave.show()
        dev.show_custom_frame()
        self.update_wave()

    # Software wave

    def stop_wave(self):
        """Stop the animation, returning its phase so a replacement can continue from there."""
        if not self.wave:
            return 0.0
        phase = self.wave.phase
        self.wave.set_running(False)
        self.wave = None
        return phase

    def update_wave(self):
        """Stream frames only while the backlight is on."""
        if self.wave:
            self.wave.set_running(self.state['brightness'] > 0)

    def on_wave_error(self, error):
        log.warning('wave stopped: %s', error)
        self.last_error = str(error)
        self.close_device()
        self.emit_state()

    def apply_power(self, dev):
        dev.set_power(self.state['power'], self.state['fan_rpm'])

    def apply_all(self):
        def apply(dev):
            self.apply_effect(dev)
            dev.set_brightness(self.state['brightness'])
            if dev.info['logo']:
                dev.set_logo(self.state['logo'])
            self.apply_power(dev)
        try:
            self.run(apply)
        except DeviceError as e:
            log.warning('apply failed: %s', e)
        self.emit_state()

    # Resume handling

    def on_system_bus(self, _source, result):
        try:
            system_bus = Gio.bus_get_finish(result)
        except GLib.Error as e:
            log.warning('no system bus: %s', e.message)
            return
        system_bus.signal_subscribe(
            'org.freedesktop.login1', 'org.freedesktop.login1.Manager', 'PrepareForSleep',
            '/org/freedesktop/login1', None, Gio.DBusSignalFlags.NONE, self.on_prepare_for_sleep)
        Gio.DBusProxy.new(
            system_bus, Gio.DBusProxyFlags.NONE, None, 'org.freedesktop.UPower',
            UPOWER_DISPLAY_DEVICE, 'org.freedesktop.UPower.Device', None, self.on_upower_proxy)

    def on_prepare_for_sleep(self, _conn, _sender, _path, _iface, _signal, params):
        (going_to_sleep,) = params.unpack()
        if going_to_sleep:
            self.stop_wave()
            return
        # The EC resets fan mode on resume; the USB device may re-enumerate too.
        log.info('resumed, reapplying settings')
        self.close_device()
        def reapply():
            self.apply_all()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add_seconds(3, reapply)

    # Battery saver

    def on_upower_proxy(self, _source, result):
        try:
            self.upower = Gio.DBusProxy.new_finish(result)
        except GLib.Error as e:
            log.warning('no UPower: %s', e.message)
            return
        self.upower.connect('g-properties-changed', lambda *_: self.update_battery_saver())
        self.update_battery_saver()

    def battery_low(self):
        if not self.upower:
            return False
        state = self.upower.get_cached_property('State')
        percentage = self.upower.get_cached_property('Percentage')
        if state is None or percentage is None:
            return False
        return (state.unpack() == UPOWER_DISCHARGING
                and percentage.unpack() <= self.state['battery_threshold'])

    def update_battery_saver(self):
        low = self.battery_low()
        if low == self.battery_saver:
            return
        self.battery_saver = low
        log.info('battery saver %s', 'on' if low else 'off')
        if self.state['effect'] == 'wave':
            try:
                self.run(self.apply_effect)
            except DeviceError as e:
                log.warning('cannot switch effect: %s', e)
        self.emit_state()

    # D-Bus

    def state_variant(self):
        info = self.device_info()
        s = self.state
        gpu_available, gpu_mode, gpu_booted_mode, gpu_power = gpu_status()
        return {
            'connected': GLib.Variant('b', self.device is not None),
            'device': GLib.Variant('s', info['name']),
            'error': GLib.Variant('s', self.last_error),
            'effect': GLib.Variant('s', s['effect']),
            'last-effect': GLib.Variant('s', s['last_effect']),
            'color': GLib.Variant('s', s['color']),
            'color2': GLib.Variant('s', s['color2']),
            'speed': GLib.Variant('y', s['speed']),
            'direction': GLib.Variant('y', s['direction']),
            'brightness': GLib.Variant('y', s['brightness']),
            'logo': GLib.Variant('b', s['logo']),
            'has-logo': GLib.Variant('b', info['logo']),
            'power': GLib.Variant('s', s['power']),
            'power-modes': GLib.Variant('as', info['power_modes']),
            'fan-rpm': GLib.Variant('q', s['fan_rpm']),
            'last-fan-rpm': GLib.Variant('q', s['last_fan_rpm']),
            'fan-min': GLib.Variant('q', info['fan'][0]),
            'fan-max': GLib.Variant('q', info['fan'][1]),
            'battery-threshold': GLib.Variant('y', s['battery_threshold']),
            'battery-saver': GLib.Variant('b', self.battery_saver),
            'gpu-available': GLib.Variant('b', gpu_available),
            'gpu-mode': GLib.Variant('s', gpu_mode),
            'gpu-booted-mode': GLib.Variant('s', gpu_booted_mode),
            'gpu-power': GLib.Variant('s', gpu_power),
        }

    def emit_state(self):
        self.bus.emit_signal(None, OBJECT_PATH, self.interface.name, 'StateChanged',
                             GLib.Variant('(a{sv})', (self.state_variant(),)))

    def on_method_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        try:
            args = params.unpack()
            if method == 'GetState':
                self.ensure_device()
                invocation.return_value(GLib.Variant('(a{sv})', (self.state_variant(),)))
                return
            handler = getattr(self, f'do_{method}')
            handler(*args)
            self.save_state()
            invocation.return_value(None)
            self.emit_state()
        except ValueError as e:
            invocation.return_dbus_error('org.freedesktop.DBus.Error.InvalidArgs', str(e))
        except DeviceError as e:
            invocation.return_dbus_error('io.github.SymonRod.RazerBladeControl.Error.Device', str(e))
            self.emit_state()

    def do_SetEffect(self, effect, options):
        if effect not in EFFECTS:
            raise ValueError(f'unknown effect: {effect}')
        new = dict(self.state)
        for key in ('color', 'color2'):
            if key in options:
                parse_color(options[key])
                new[key] = '#' + options[key].strip().lstrip('#').lower()
        for key in ('speed', 'direction'):
            if key in options:
                new[key] = int(options[key])
        new['effect'] = effect
        if effect != 'none':
            new['last_effect'] = effect
        old, self.state = self.state, new
        try:
            self.run(self.apply_effect)
        except DeviceError:
            self.state = old
            raise

    def do_SetBrightness(self, value):
        self.run(lambda dev: dev.set_brightness(value))
        self.state['brightness'] = value
        self.update_wave()

    def do_SetLogo(self, enabled):
        self.run(lambda dev: dev.set_logo(enabled))
        self.state['logo'] = enabled

    def do_SetPowerMode(self, mode):
        if mode not in self.device_info()['power_modes']:
            raise ValueError(f'unknown power mode: {mode}')
        self.run(lambda dev: dev.set_power(mode, self.state['fan_rpm']))
        self.state['power'] = mode

    def do_SetFanRpm(self, rpm):
        rpm = self.clamp_rpm(rpm)
        self.run(lambda dev: dev.set_power(self.state['power'], rpm))
        self.state['fan_rpm'] = rpm
        if rpm:
            self.state['last_fan_rpm'] = rpm

    def do_SetBatteryThreshold(self, percent):
        if percent > 100:
            raise ValueError(f'invalid percentage: {percent}')
        self.state['battery_threshold'] = percent
        self.update_battery_saver()

    def do_Reapply(self):
        self.close_device()
        self.apply_all()


def probe():
    """Print what the daemon would use; handy for checking udev permissions."""
    nodes = list(find_hidraw_nodes())
    if not nodes:
        print('No supported Razer device found.')
        return 1
    for path, pid, iface in nodes:
        access = 'rw' if os.access(path, os.R_OK | os.W_OK) else 'NO ACCESS'
        print(f'{path}: 1532:{pid:04x} interface {iface} [{access}]')
    try:
        dev = BladeDevice.open()
    except DeviceError as e:
        print(f'Error: {e}')
        return 1
    print(f'Control interface: {dev.path}, transaction id 0x{dev.transaction_id:02X}')
    print(f'Keyboard brightness: {dev.get_brightness()}')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--probe', action='store_true', help='detect the device and exit')
    parser.add_argument('--verbose', '-v', action='store_true')
    opts = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if opts.verbose else logging.INFO,
                        format='%(levelname)s: %(message)s')
    if opts.probe:
        raise SystemExit(probe())

    loop = GLib.MainLoop()
    daemon = None

    def on_bus_acquired(connection, _name):
        nonlocal daemon
        daemon = Daemon(connection)

    def on_name_lost(_connection, name):
        log.error('could not own %s (already running?)', name)
        loop.quit()

    Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                     on_bus_acquired, None, on_name_lost)
    unix_signal_add(GLib.PRIORITY_DEFAULT, 15, loop.quit)
    unix_signal_add(GLib.PRIORITY_DEFAULT, 2, loop.quit)
    loop.run()


if __name__ == '__main__':
    main()

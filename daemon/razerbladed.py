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
import functools
import json
import logging
import math
import os
import random
import time
from collections import deque
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
        # Matrix cells of the UK (ISO) keyboard: '#' has an LED, '.' has none and the
        # snake crosses it in one move, '-' has none and blocks the way (the spacebar)
        'leds': (
            '.###############',  # esc f1-f12 ins del
            '.#############.#',  # ` 1-0 - = backspace
            '.##############.',  # tab q-p [ ] enter (top half)
            '.#############..',  # caps a-l ; ' #
            '.#############.#',  # shift \ z-m , . / up shift
            '.###.#---#.#####',  # ctrl fn super alt (space) altgr ctrl left down right fn
        ),
        # Keys that touch on the keyboard but not in the matrix (row, column)
        'links': (
            ((2, 14), (4, 15)),  # enter, right shift
        ),
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
           'spectrum', 'wave', 'snake', 'chase', 'reactive', 'starlight']

NOSTORE = 0x00
VARSTORE = 0x01
LOGO_LED = 0x04

# Animations computed here are streamed as custom frames; the EC's own wave has a
# fixed speed, so the wave is one of them. A frame takes ~10 ms to be acknowledged.
ANIMATION_FPS = 20
# A frame request is usually acknowledged within 1-2 ms, rarely after ~10 ms
FRAME_ACK_TIMEOUT = 0.02
# Seconds for the rainbow to cross the keyboard, indexed by speed - 1 (1 is the fastest)
WAVE_PERIODS = (0.6, 0.8, 1.1, 1.5, 2.0, 2.7, 3.6, 4.8, 6.4)
# Seconds per snake move, indexed by speed - 1
SNAKE_STEPS = (0.05, 0.065, 0.08, 0.1, 0.125, 0.16, 0.2, 0.25, 0.32)
SNAKE_START_LENGTH = 3
# How the snake is painted: gradient from the primary color (head) to the secondary
# (tail), primary color only, or a rainbow flowing along the body
SNAKE_STYLES = ('gradient', 'solid', 'rainbow')
SNAKE_TAIL_LEVEL = 0.15  # brightness of the tail in a single color; the head is at 1
SNAKE_COLORFUL_TAIL_LEVEL = 0.5  # same for gradient and rainbow, already told apart by color
SNAKE_RAINBOW_HUE_STEP = 1 / 24  # hue difference between two segments
SNAKE_RAINBOW_SPEED = 0.25  # hue cycles per second
SNAKE_WIN_SECONDS = 3.0  # the keyboard blinks this long before a new game
# Path search through the LEDs: attempts and backtracking steps per attempt
SNAKE_SEARCH_ATTEMPTS = 50
SNAKE_SEARCH_STEPS = 3000
# Seconds per player move in Chase, indexed by speed - 1
CHASE_STEPS = (0.08, 0.1, 0.12, 0.14, 0.17, 0.2, 0.24, 0.28, 0.33)
CHASE_CHASERS = 2
CHASE_LIVES = 3
# Chaser moves per player move: they start slower and speed up at every level cleared
CHASE_CHASER_SPEED = 0.7
CHASE_LEVEL_SPEEDUP = 0.05
CHASE_MAX_CHASER_SPEED = 0.9
CHASE_FRIGHT_SPEED = 0.5  # chasers on the run after a power dot
CHASE_FRIGHT_MOVES = 30  # player moves the chasers stay on the run
CHASE_FRIGHT_BLINK_MOVES = 8  # they blink during the last ones
CHASE_RESPAWN_MOVES = 15  # player moves an eaten chaser takes to come back home
CHASE_SAFE_ROOM = 8  # cells the player wants to reach before any chaser
CHASE_DOT_LEVEL = 0.2  # brightness of the dots; power dots pulse at full brightness
CHASE_EVENT_SECONDS = {'caught': 1.5, 'over': 3.0, 'cleared': 3.0}

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
    'color3': '#ff0000',  # snake food, chase chasers
    'style': 'gradient',  # one of SNAKE_STYLES
    'speed': 2,
    'direction': 1,
    'brightness': 160,
    'logo': True,
    'power': 'balanced',
    'fan_rpm': 0,
    'last_fan_rpm': 4000,
    'battery_threshold': 20,  # below this percentage, on battery, animations become static
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
        """Send a prebuilt request (animation frames) and wait until it's processed.

        The firmware drops a request that arrives while it's still busy with the
        previous one: a fire-and-forget burst loses about a quarter of the frames.
        A request that isn't acknowledged in time is resent once; after that the
        frame is left as it is, the next one will replace it anyway.
        """
        try:
            for _attempt in range(2):
                fcntl.ioctl(self.fd, HIDIOCSFEATURE, request)
                deadline = time.monotonic() + FRAME_ACK_TIMEOUT
                while time.monotonic() < deadline:
                    response = bytearray(REPORT_LEN + 1)
                    fcntl.ioctl(self.fd, HIDIOCGFEATURE, response, True)
                    # Until the firmware takes the request, it still answers for the
                    # previous one: compare class, id and the first two arguments
                    # (they tell the matrix rows apart).
                    if response[1] != RAZER_STATUS_BUSY and response[7:11] == request[7:11]:
                        if response[1] == RAZER_STATUS_SUCCESS:
                            return
                        break
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


def dim(color, level):
    """RGB bytes of color (or of a row of colors) at a brightness level between 0 and 1."""
    return bytes(round(c * level) for c in color)


def mix(a, b, t):
    """RGB bytes between color a (t = 0) and color b (t = 1)."""
    return bytes(round(x + (y - x) * t) for x, y in zip(a, b))


def hue(h):
    """RGB bytes of the fully saturated color with hue h (0-1)."""
    return bytes(round(v * 255) for v in colorsys.hsv_to_rgb(h % 1.0, 1, 1))


def led_neighbors(leds, links=()):
    """Neighbors of every matrix cell with an LED: the nearest LED up, down, left and
    right, so that a move crosses wide keys and gaps in the matrix ('.') in one go;
    blocking cells ('-') can't be crossed. links are extra pairs of neighbors."""
    rows, cols = len(leds), len(leds[0])
    lit = {(r, c) for r in range(rows) for c in range(cols) if leds[r][c] == '#'}
    neighbors = {}
    for r, c in sorted(lit):
        near = []
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            while 0 <= nr < rows and 0 <= nc < cols and leds[nr][nc] == '.':
                nr, nc = nr + dr, nc + dc
            if (nr, nc) in lit:
                near.append((nr, nc))
        neighbors[(r, c)] = near
    for a, b in links:
        neighbors[a].append(b)
        neighbors[b].append(a)
    return neighbors


def hamiltonian_cycle(neighbors):
    """A closed path through every cell once, or None if the search gives up.

    Depth-first search that backtracks as soon as the free cells are no longer
    connected or one of them is left with fewer than two ways in and out. Some
    neighbor orders lead it astray for a very long time, so it makes short attempts
    with the neighbors shuffled differently: fixed seeds keep the result the same.
    """
    start = min(neighbors, key=lambda cell: (len(neighbors[cell]), cell))

    def exits(cell, head, visited):
        return sum(1 for n in graph[cell] if n not in visited or n == head or n == start)

    def viable(head, visited):
        free = [cell for cell in graph if cell not in visited]
        if not free:
            return start in graph[head]
        if not any(n not in visited for n in graph[start]):
            return False
        if any(exits(cell, head, visited) < 2 for cell in free):
            return False
        seen, todo = {head}, [head]
        while todo:
            for n in graph[todo.pop()]:
                if n not in visited and n not in seen:
                    seen.add(n)
                    todo.append(n)
        return len(seen) - 1 == len(free)

    def extend(path, visited):
        nonlocal steps
        steps += 1
        head = path[-1]
        if len(path) == len(graph):
            return start in graph[head]
        if steps > SNAKE_SEARCH_STEPS:
            return False
        moves = [n for n in graph[head] if n not in visited]
        for n in sorted(moves, key=lambda n: exits(n, head, visited)):
            visited.add(n)
            path.append(n)
            if viable(n, visited) and extend(path, visited):
                return True
            visited.discard(n)
            path.pop()
        return False

    for seed in range(SNAKE_SEARCH_ATTEMPTS):
        rng = random.Random(seed)
        graph = {cell: rng.sample(near, len(near)) for cell, near in neighbors.items()}
        steps = 0
        path = [start]
        if extend(path, {start}):
            return path
    return None


@functools.cache
def snake_board(leds, links=()):
    """(cycle, neighbors) for the snake on the cells with an LED, computed once."""
    neighbors = led_neighbors(leds, links)
    cycle = hamiltonian_cycle(neighbors)
    if cycle is not None:
        return cycle, neighbors
    full = ('#' * len(leds[0]),) * len(leds)
    if leds == full:
        raise RuntimeError('no snake path through the keyboard matrix')
    log.warning('no snake path through the LEDs, using the whole matrix')
    return snake_board(full)


class SnakeGame:
    """Snake that follows a Hamiltonian cycle, cutting it short towards the food when safe.

    Every move goes forward along the cycle, so the body lies on the stretch of cycle from
    the tail to the head and the cells from the head on to the tail are all free: a move
    that stays among them can never trap the snake, so it always fills the whole board.
    """

    def __init__(self, leds, links=()):
        self.cycle, self.neighbors = snake_board(leds, links)
        self.order = {cell: i for i, cell in enumerate(self.cycle)}
        self.reset()

    def reset(self):
        start = random.randrange(len(self.cycle))
        self.body = deque(self.cycle[(start + i) % len(self.cycle)]
                          for i in range(SNAKE_START_LENGTH))  # tail first
        self.occupied = set(self.body)
        self.vacated = None  # cell the tail left on the last move
        self.place_food()

    @property
    def won(self):
        return self.food is None

    def distance(self, a, b):
        """Moves from a to b going forward along the cycle."""
        return (self.order[b] - self.order[a]) % len(self.cycle)

    def place_food(self):
        free = [cell for cell in self.cycle if cell not in self.occupied]
        self.food = random.choice(free) if free else None

    def next_cell(self):
        head = self.body[-1]
        best = self.cycle[(self.order[head] + 1) % len(self.cycle)]
        best_distance = 1
        # Cells closer than the tail are free; eating keeps the tail in place for one
        # move, which still leaves the head at least one cell away from it.
        room = self.distance(head, self.body[0])
        to_food = self.distance(head, self.food)
        for cell in self.neighbors[head]:
            d = self.distance(head, cell)
            if best_distance < d < room and d <= to_food:
                best, best_distance = cell, d
        return best

    def step(self):
        cell = self.next_cell()
        ate = cell == self.food
        if ate:
            self.vacated = None
        else:
            self.vacated = self.body.popleft()
            self.occupied.discard(self.vacated)
        self.body.append(cell)
        self.occupied.add(cell)
        if ate:
            self.place_food()


class Animation:
    """Effect computed by the daemon and streamed to the keyboard as custom frames.

    Subclasses take (dev, state, previous, on_error), where previous is the animation
    being replaced (or None) so that, for example, a speed change doesn't restart it.
    """

    def __init__(self, dev, on_error):
        self.dev = dev
        self.on_error = on_error
        self.rows, self.cols = dev.info['matrix']
        self.source_id = 0

    def render(self, now):
        """The frame to show at time now: one bytes object of RGB triplets per row."""
        raise NotImplementedError

    def frame_requests(self, now):
        return self.dev.custom_frame_requests(self.render(now))

    def show(self):
        for request in self.frame_requests(time.monotonic()):
            self.dev.write(request)

    def set_running(self, running):
        if running and not self.source_id:
            self.source_id = GLib.timeout_add(1000 // ANIMATION_FPS, self.on_tick)
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


class WaveAnimation(Animation):
    """Rainbow wave; one full cycle of requests is prebuilt."""

    def __init__(self, dev, state, previous, on_error):
        super().__init__(dev, on_error)
        self.period = WAVE_PERIODS[min(max(state['speed'], 1), len(WAVE_PERIODS)) - 1]
        count = round(self.period * ANIMATION_FPS)
        self.frames = [dev.custom_frame_requests(
                           [wave_row(self.cols, i / count, state['direction'])] * self.rows)
                       for i in range(count)]
        # Continue from the previous wave so speed changes don't make it jump
        phase = previous.phase(time.monotonic()) if isinstance(previous, WaveAnimation) else 0.0
        self.start = time.monotonic() - phase * self.period

    def phase(self, now):
        return (now - self.start) / self.period % 1.0

    def frame_requests(self, now):
        return self.frames[int(self.phase(now) * len(self.frames)) % len(self.frames)]


class SnakeAnimation(Animation):
    """Snake that plays by itself, painted in one of SNAKE_STYLES; color3 is the food."""

    def __init__(self, dev, state, previous, on_error):
        super().__init__(dev, on_error)
        self.interval = SNAKE_STEPS[min(max(state['speed'], 1), len(SNAKE_STEPS)) - 1]
        self.style = state['style']
        self.color = parse_color(state['color'])
        self.tail_color = parse_color(state['color2'])
        self.food_color = parse_color(state['color3'])
        # Speed, color and style changes keep the game going
        if isinstance(previous, SnakeAnimation):
            self.game, self.won_at = previous.game, previous.won_at
        else:
            leds = dev.info.get('leds') or ('#' * self.cols,) * self.rows
            self.game, self.won_at = SnakeGame(leds, dev.info.get('links', ())), None
        self.last_move = time.monotonic()

    def advance(self, now):
        if now - self.last_move > 1.0:  # paused meanwhile: don't replay the missed moves
            self.last_move = now
        while not self.game.won and now - self.last_move >= self.interval:
            self.game.step()
            self.last_move += self.interval
        if not self.game.won:
            return
        if self.won_at is None:
            self.won_at = now
        elif now - self.won_at >= SNAKE_WIN_SECONDS:
            self.game.reset()
            self.won_at = None
            self.last_move = now

    def render(self, now):
        self.advance(now)
        if self.won_at is not None:
            # The snake fills the keyboard: blink it three times
            t = (now - self.won_at) / SNAKE_WIN_SECONDS
            if self.style == 'rainbow':
                row = wave_row(self.cols, now * SNAKE_RAINBOW_SPEED, 1)
            else:
                row = self.color * self.cols
            return [dim(row, (1 + math.cos(2 * math.pi * 3 * t)) / 2)] * self.rows

        frame = [bytearray(3 * self.cols) for _ in range(self.rows)]

        def put(cell, color):
            r, c = cell
            frame[r][3 * c:3 * c + 3] = color

        game = self.game
        last = len(game.body) - 1
        if game.vacated:  # painted like the tail, fading out until the next move
            progress = min((now - self.last_move) / self.interval, 1.0)
            put(game.vacated, dim(self.segment_color(-1, last, now), 1 - progress))
        for i, cell in enumerate(game.body):
            put(cell, self.segment_color(i, last, now))
        put(game.food, dim(self.food_color, 0.6 + 0.4 * math.cos(2 * math.pi * now)))
        return [bytes(row) for row in frame]

    def segment_color(self, i, last, now):
        """Color of body segment i, from 0 (tail) to last (head)."""
        t = max(i, 0) / last
        if self.style == 'solid':
            return dim(self.color, SNAKE_TAIL_LEVEL + (1 - SNAKE_TAIL_LEVEL) * t)
        level = SNAKE_COLORFUL_TAIL_LEVEL + (1 - SNAKE_COLORFUL_TAIL_LEVEL) * t
        if self.style == 'rainbow':
            return dim(hue(now * SNAKE_RAINBOW_SPEED - (last - i) * SNAKE_RAINBOW_HUE_STEP), level)
        return dim(mix(self.tail_color, self.color, t), level)


def graph_distances(neighbors, start):
    """Moves from start to every cell it can reach."""
    dist, todo = {start: 0}, deque([start])
    while todo:
        cell = todo.popleft()
        for n in neighbors[cell]:
            if n not in dist:
                dist[n] = dist[cell] + 1
                todo.append(n)
    return dist


@functools.cache
def chase_board(leds, links=()):
    """(neighbors, distances, player start, chaser homes, power dots) for Chase.

    The player starts on the LED nearest to the middle of the matrix, the chasers as far
    from it (and from each other) as possible, and the power dots sit in the corners.
    """
    rows, cols = len(leds), len(leds[0])
    neighbors = led_neighbors(leds, links)
    start = min(neighbors, key=lambda cell: (math.dist(cell, ((rows - 1) / 2, (cols - 1) / 2)), cell))
    # Play on the cells reachable from the start only
    reachable = graph_distances(neighbors, start)
    neighbors = {cell: [n for n in near if n in reachable]
                 for cell, near in neighbors.items() if cell in reachable}
    dist = {cell: graph_distances(neighbors, cell) for cell in neighbors}
    corners = ((0, 0), (0, cols - 1), (rows - 1, 0), (rows - 1, cols - 1))
    power = {min(neighbors, key=lambda cell: (math.dist(cell, corner), cell)) for corner in corners}
    power.discard(start)
    homes = []
    for _ in range(min(CHASE_CHASERS, len(neighbors) - len(power) - 1)):
        homes.append(max((cell for cell in neighbors
                          if cell != start and cell not in homes and cell not in power),
                         key=lambda cell: (min(dist[cell][o] for o in [start] + homes), cell)))
    return neighbors, dist, start, tuple(homes), frozenset(power)


class Chaser:
    def __init__(self, home):
        self.home = home
        self.cell = home
        self.prev = None  # cell it came from: it doesn't turn back, except in dead ends
        self.frightened = False
        self.respawn = 0  # player moves left before an eaten chaser comes back
        self.budget = 0.0  # moves owed, since chasers are slower than the player


class ChaseGame:
    """Maze chase that plays by itself on the LEDs.

    The player eats the dots, heading for the nearest one it can reach before the chasers
    and steering clear of places where they could corner it. The first chaser hunts the
    player, the second lies in wait at the dot the player is going for. Power dots put the
    chasers on the run for a while: the player hunts them, and eaten ones go back home.
    Clearing the board starts a faster level; being caught costs one of CHASE_LIVES.
    """

    def __init__(self, leds, links=()):
        self.neighbors, self.dist, self.start, self.homes, self.power_cells = chase_board(leds, links)
        self.new_game()

    def new_game(self):
        self.lives = CHASE_LIVES
        self.level = 0
        self.new_level()

    def new_level(self):
        self.power = set(self.power_cells)
        self.dots = set(self.neighbors) - self.power - {self.start}
        self.new_round()

    def new_round(self):
        self.player = self.start
        self.player_prev = None
        self.goal = None  # cell the player is heading for
        self.chasers = [Chaser(home) for home in self.homes]
        self.fright = 0
        self.event = None  # 'caught', 'over' or 'cleared' until after_event()

    @property
    def chaser_speed(self):
        return min(CHASE_CHASER_SPEED + self.level * CHASE_LEVEL_SPEEDUP, CHASE_MAX_CHASER_SPEED)

    def after_event(self):
        """Go on once the event has been shown."""
        if self.event == 'over':
            self.new_game()
        elif not self.dots and not self.power:
            self.level += 1
            self.new_level()
        else:
            self.new_round()

    def step(self):
        """One player move, then the chaser moves that fall in it."""
        self.move_player(self.player_move())
        if self.event:
            return
        if self.fright:
            self.fright -= 1
            if not self.fright:
                for chaser in self.chasers:
                    chaser.frightened = False
        for i, chaser in enumerate(self.chasers):
            if chaser.respawn:
                chaser.respawn -= 1
                if chaser.respawn:
                    continue
                chaser.cell, chaser.prev, chaser.budget = chaser.home, None, 0.0
            else:
                chaser.budget += CHASE_FRIGHT_SPEED if chaser.frightened else self.chaser_speed
                if chaser.budget >= 1:
                    chaser.budget -= 1
                    chaser.prev, chaser.cell = chaser.cell, self.chaser_move(i, chaser)
            self.collide(chaser)
            if self.event:
                return

    def move_player(self, cell):
        self.player_prev, self.player = self.player, cell
        self.dots.discard(cell)
        if cell in self.power:
            self.power.discard(cell)
            self.fright = CHASE_FRIGHT_MOVES
            for chaser in self.chasers:
                if not chaser.respawn:
                    chaser.frightened = True
                    chaser.prev = None  # free to turn back and flee
        for chaser in self.chasers:
            self.collide(chaser)
            if self.event:
                return
        if not self.dots and not self.power:
            self.event = 'cleared'

    def collide(self, chaser):
        if chaser.respawn or chaser.cell != self.player:
            return
        if chaser.frightened:
            chaser.frightened = False
            chaser.respawn = CHASE_RESPAWN_MOVES
        else:
            self.lives -= 1
            self.event = 'caught' if self.lives else 'over'

    def chaser_move(self, index, chaser):
        options = [n for n in self.neighbors[chaser.cell] if n != chaser.prev] or [chaser.prev]
        # Don't pile up on another chaser if there's another way
        others = {c.cell for c in self.chasers if c is not chaser and not c.respawn}
        options = [n for n in options if n not in others] or options
        if chaser.frightened:
            if random.random() < 0.3:
                return random.choice(options)
            return max(options, key=lambda n: (self.dist[n][self.player], random.random()))
        if index == 0:
            target, wander = self.player, 0.1
        elif index == 1:
            target, wander = self.goal or self.player, 0.2
        else:
            target, wander = self.player, 0.5
        if random.random() < wander:
            return random.choice(options)
        return min(options, key=lambda n: (self.dist[n][target], random.random()))

    def player_move(self):
        hunters = [c.cell for c in self.chasers if not c.respawn and not c.frightened]
        prey = [c.cell for c in self.chasers if not c.respawn and c.frightened]
        # Moves the closest hunter needs to reach each cell
        threat = {cell: min((self.dist[h][cell] for h in hunters), default=math.inf)
                  for cell in self.neighbors}
        targets = self.dots | self.power
        best, best_score, best_goal = None, -math.inf, None
        for n in self.neighbors[self.player]:
            # Cells the player can reach from n before any hunter
            safe = {n: 0}
            todo = deque([n]) if threat[n] > 1 else deque()
            while todo:
                cell = todo.popleft()
                for m in self.neighbors[cell]:
                    if m not in safe and safe[cell] + 1 < threat[m]:
                        safe[m] = safe[cell] + 1
                        todo.append(m)
            # The nearest dot it can safely reach, or a chaser on the run it can catch in time
            goals = [(safe[t], t) for t in targets if t in safe]
            goals += [(safe[p] / 2 - 2, p) for p in prey if p in safe and safe[p] < self.fright]
            if goals:
                goal_distance, goal = min(goals)
            else:
                goal_distance, goal = min(((self.dist[n][t] + 10, t) for t in targets),
                                          default=(0, None))
            score = -goal_distance - 50 * max(CHASE_SAFE_ROOM - len(safe), 0)
            if threat[n] <= 1:  # onto a hunter or right next to one
                score -= 1000 - 100 * threat[n]
            if n == self.player_prev:  # don't dither back and forth
                score -= 0.5
            score += random.random() * 0.1
            if score > best_score:
                best, best_score, best_goal = n, score, goal
        self.goal = best_goal
        return best


class ChaseAnimation(Animation):
    """ChaseGame: the primary color is the player, the secondary the dots, color3 the chasers."""

    FRIGHTENED_COLOR = b'\xff\xff\xff'

    def __init__(self, dev, state, previous, on_error):
        super().__init__(dev, on_error)
        self.interval = CHASE_STEPS[min(max(state['speed'], 1), len(CHASE_STEPS)) - 1]
        self.color = parse_color(state['color'])
        self.dot_color = parse_color(state['color2'])
        self.chaser_color = parse_color(state['color3'])
        # Speed and color changes keep the game going
        if isinstance(previous, ChaseAnimation):
            self.game, self.event_at = previous.game, previous.event_at
        else:
            leds = dev.info.get('leds') or ('#' * self.cols,) * self.rows
            self.game, self.event_at = ChaseGame(leds, dev.info.get('links', ())), None
        self.last_move = time.monotonic()

    def advance(self, now):
        if now - self.last_move > 1.0:  # paused meanwhile: don't replay the missed moves
            self.last_move = now
        while not self.game.event and now - self.last_move >= self.interval:
            self.game.step()
            self.last_move += self.interval
        if not self.game.event:
            return
        if self.event_at is None:
            self.event_at = now
        elif now - self.event_at >= CHASE_EVENT_SECONDS[self.game.event]:
            self.game.after_event()
            self.event_at = None
            self.last_move = now

    def render(self, now):
        self.advance(now)
        game = self.game
        if game.event in ('cleared', 'over'):
            # Board cleared: blink in the player's color; game over: fade out in the chasers'
            t = (now - self.event_at) / CHASE_EVENT_SECONDS[game.event]
            if game.event == 'cleared':
                color, level = self.color, (1 + math.cos(2 * math.pi * 3 * t)) / 2
            else:
                color, level = self.chaser_color, (1 - t) * (1 + math.cos(2 * math.pi * 4 * t)) / 2
            return [dim(color * self.cols, level)] * self.rows

        frame = [bytearray(3 * self.cols) for _ in range(self.rows)]

        def put(cell, color):
            r, c = cell
            frame[r][3 * c:3 * c + 3] = color

        for cell in game.dots:
            put(cell, dim(self.dot_color, CHASE_DOT_LEVEL))
        for cell in game.power:
            put(cell, dim(self.dot_color, 0.6 + 0.4 * math.cos(2 * math.pi * 2 * now)))
        for chaser in game.chasers:
            if chaser.respawn:  # eaten: its home glows faintly until it's back
                put(chaser.home, dim(self.chaser_color, 0.1))
        if game.event == 'caught':
            # The player blinks in the chasers' color
            caught = int((now - self.event_at) * 8) % 2
            put(game.player, self.chaser_color if caught else self.color)
        else:
            progress = min((now - self.last_move) / self.interval, 1.0)
            if game.player_prev:  # fading out until the next move
                put(game.player_prev, dim(self.color, 0.4 * (1 - progress)))
            put(game.player, self.color)
        for chaser in game.chasers:
            if chaser.respawn or (game.event == 'caught' and chaser.cell == game.player):
                continue
            color = self.chaser_color
            if chaser.frightened:
                blinking = game.fright <= CHASE_FRIGHT_BLINK_MOVES and int(now * 6) % 2
                color = self.chaser_color if blinking else dim(self.FRIGHTENED_COLOR, 0.6)
            put(chaser.cell, color)
        return [bytes(row) for row in frame]


ANIMATIONS = {'wave': WaveAnimation, 'snake': SnakeAnimation, 'chase': ChaseAnimation}


class Daemon:
    def __init__(self, bus):
        self.bus = bus
        self.device = None
        self.animation = None
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
        self.stop_animation()
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
        previous = self.stop_animation()
        effect = s['effect']
        animation = ANIMATIONS.get(effect)
        if animation and self.battery_saver:
            # Streaming frames costs CPU, the EC's static effect doesn't
            animation, effect = None, 'static'
        if not animation:
            dev.set_effect(effect, parse_color(s['color']), parse_color(s['color2']),
                           s['speed'], s['direction'])
            return
        self.animation = animation(dev, s, previous, self.on_animation_error)
        self.animation.show()
        dev.show_custom_frame()
        self.update_animation()

    # Animations

    def stop_animation(self):
        """Stop the animation and return it, so a replacement can continue from it."""
        animation, self.animation = self.animation, None
        if animation:
            animation.set_running(False)
        return animation

    def update_animation(self):
        """Stream frames only while the backlight is on."""
        if self.animation:
            self.animation.set_running(self.state['brightness'] > 0)

    def on_animation_error(self, error):
        log.warning('animation stopped: %s', error)
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
            self.stop_animation()
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
        if self.state['effect'] in ANIMATIONS:
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
            'color3': GLib.Variant('s', s['color3']),
            'style': GLib.Variant('s', s['style']),
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
        for key in ('color', 'color2', 'color3'):
            if key in options:
                parse_color(options[key])
                new[key] = '#' + options[key].strip().lstrip('#').lower()
        if 'style' in options:
            if options['style'] not in SNAKE_STYLES:
                raise ValueError(f'unknown style: {options["style"]}')
            new['style'] = options['style']
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
        self.update_animation()

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

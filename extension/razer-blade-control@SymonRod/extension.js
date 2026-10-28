// SPDX-License-Identifier: GPL-2.0-or-later
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';
import Clutter from 'gi://Clutter';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Slider} from 'resource:///org/gnome/shell/ui/slider.js';
import {QuickMenuToggle, QuickSlider, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {Extension, gettext as _} from 'resource:///org/gnome/shell/extensions/extension.js';

import {DaemonClient, EFFECTS, GPU_MODES, POWER_MODES, effectInfo} from './daemon.js';

Gio._promisify(Gio.Subprocess.prototype, 'communicate_utf8_async');

const DEBOUNCE_MS = 150;

// Installed by install.sh together with its polkit action
const GPU_HELPER = '/usr/local/libexec/razerblade-gpu';
const PKEXEC_DISMISSED = 126;

// GetState only reads sysfs and small files, so polling it is cheap
const GPU_POLL_SECONDS = 1;

// gettext is not ready at import time: mark the strings here, translate them on use
const N_ = s => s;

const GPU_MODE_HINTS = {
    standard: N_('Draws about 7 W even when idle'),
    hybrid: N_('Powers off by itself when nothing uses it'),
    integrated: N_('No HDMI, GPU USB-C or CUDA'),
};

// Gap kept between the expanded Quick Settings and the bottom of the work area
const SCREEN_MARGIN = 16;
const MIN_SCROLL_HEIGHT = 120;

// How long to wait for the shell's own Quick Settings items at login
const PLACE_SLIDER_INTERVAL_MS = 100;
const PLACE_SLIDER_ATTEMPTS = 100;

const COLOR_PRESETS = [
    '#ff0000', '#ff8000', '#ffff00', '#00ff00', '#00ffff',
    '#0040ff', '#8000ff', '#ff00c0', '#ffffff',
];

/** Run `callback` once the value stopped changing for DEBOUNCE_MS. */
class Debouncer {
    constructor(callback) {
        this._callback = callback;
        this._id = 0;
    }

    queue(value) {
        this.cancel();
        this._id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, DEBOUNCE_MS, () => {
            this._id = 0;
            this._callback(value);
            return GLib.SOURCE_REMOVE;
        });
    }

    cancel() {
        if (this._id)
            GLib.source_remove(this._id);
        this._id = 0;
    }
}

function unavailableReason(state) {
    if (!state)
        return _('Service not running');
    if (!state.connected)
        return _('Device not accessible');
    return null;
}

/** Keep a `dragging` flag on the slider so state updates don't fight the user. */
function trackDragging(slider) {
    slider.dragging = false;
    slider.connect('drag-begin', () => (slider.dragging = true));
    slider.connect('drag-end', () => (slider.dragging = false));
}

function addSliderItem(menu, slider) {
    const item = new PopupMenu.PopupBaseMenuItem({activate: false});
    item.add_child(new St.Bin({
        style_class: 'slider-bin',
        child: slider,
        x_expand: true,
        y_align: Clutter.ActorAlign.CENTER,
    }));
    menu.addMenuItem(item);
    return item;
}

/**
 * Give a Quick Settings submenu a scrollable body below its header and return
 * it. Left alone, the shell lets the submenu run past the bottom of the screen.
 */
function addScrollableSection(menu) {
    const section = new PopupMenu.PopupMenuSection();
    menu.addMenuItem(section);
    menu.box.remove_child(section.actor);
    const scrollView = new St.ScrollView({
        child: section.actor,
        hscrollbar_policy: St.PolicyType.NEVER,
        vscrollbar_policy: St.PolicyType.AUTOMATIC,
    });
    menu.box.add_child(scrollView);

    // The free space depends on the toggle's row and the screen: measure it
    // on every open, before the shell computes the height to animate to
    const open = menu.open.bind(menu);
    menu.open = animate => {
        scrollView.style = null;
        const quickSettings = Main.panel.statusArea.quickSettings.menu.box;
        const workArea = Main.layoutManager.getWorkAreaForMonitor(
            Main.layoutManager.findIndexForActor(quickSettings));
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const [, top] = quickSettings.get_transformed_position();
        const [, boxHeight] = menu.box.get_preferred_height(-1);
        const [, scrollHeight] = scrollView.get_preferred_height(-1);
        const free = workArea.y + workArea.height - SCREEN_MARGIN * scale -
            (top + quickSettings.height) - (boxHeight - scrollHeight);
        if (free < scrollHeight)
            scrollView.style = `max-height: ${Math.max(free / scale, MIN_SCROLL_HEIGHT)}px;`;
        open(animate);
    };
    return section;
}

const KeyboardToggle = GObject.registerClass(
class KeyboardToggle extends QuickMenuToggle {
    _init(client, extension, keyboardIcon) {
        super._init({
            title: _('Keyboard'),
            gicon: keyboardIcon,
            toggleMode: false,
        });
        this._client = client;
        this._keyboardIcon = keyboardIcon;

        this.connect('clicked', () => {
            const s = this._client.state;
            if (!s)
                return;
            this._client.setEffect(s.effect === 'none' ? s['last-effect'] : 'none');
        });

        this.menu.setHeader(keyboardIcon, _('RGB Keyboard'));
        const section = addScrollableSection(this.menu);

        this._effectItems = new Map();
        for (const effect of EFFECTS) {
            const item = new PopupMenu.PopupMenuItem(_(effect.label));
            item.connect('activate', () => this._client.setEffect(effect.id));
            section.addMenuItem(item);
            this._effectItems.set(effect.id, item);
        }

        section.addMenuItem(new PopupMenu.PopupSeparatorMenuItem(_('Color')));
        const swatchItem = new PopupMenu.PopupBaseMenuItem({activate: false, can_focus: false});
        const swatchBox = new St.BoxLayout({style_class: 'razerblade-swatches', x_expand: true});
        this._swatches = new Map();
        for (const color of COLOR_PRESETS) {
            const button = new St.Button({
                style_class: 'razerblade-swatch',
                style: `background-color: ${color};`,
                can_focus: true,
                accessible_name: color,
            });
            button.connect('clicked', () => this._pickColor(color));
            swatchBox.add_child(button);
            this._swatches.set(color, button);
        }
        swatchItem.add_child(swatchBox);
        section.addMenuItem(swatchItem);

        // Speed 1 is the fastest, so the slider runs from maxSpeed (left) to 1 (right)
        this._speedSeparator = new PopupMenu.PopupSeparatorMenuItem(_('Speed'));
        section.addMenuItem(this._speedSeparator);
        this._speedSlider = new Slider(0);
        this._speedSlider.accessible_name = _('Effect speed');
        trackDragging(this._speedSlider);
        this._speedSliderItem = addSliderItem(section, this._speedSlider);

        this._speedDebouncer = new Debouncer(speed => {
            const s = this._client.state;
            if (s && speed !== s.speed)
                this._client.setEffect(s.effect, {speed});
        });
        this._speedSlider.connect('notify::value', () => {
            if (!this._syncing)
                this._speedDebouncer.queue(this._sliderToSpeed());
        });
        // Snap to the nearest step once released
        this._speedSlider.connect('drag-end', () => this._setSpeedSlider(this._sliderToSpeed()));
        this.connect('destroy', () => this._speedDebouncer.cancel());

        section.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._logoItem = new PopupMenu.PopupSwitchMenuItem(_('Razer logo'), false);
        this._logoItem.connect('toggled', (_item, on) => this._client.setLogo(on));
        section.addMenuItem(this._logoItem);

        const prefsItem = new PopupMenu.PopupMenuItem(_('Custom colors and more options…'));
        prefsItem.connect('activate', () => extension.openPreferences());
        section.addMenuItem(prefsItem);
    }

    _pickColor(color) {
        const s = this._client.state;
        if (!s)
            return;
        const effect = effectInfo(s.effect).usesColor ? s.effect : 'static';
        this._client.setEffect(effect, {color});
    }

    _maxSpeed() {
        return effectInfo(this._client.state?.effect).maxSpeed ?? 1;
    }

    _sliderToSpeed() {
        const max = this._maxSpeed();
        return max - Math.round(this._speedSlider.value * (max - 1));
    }

    _setSpeedSlider(speed) {
        const max = this._maxSpeed();
        this._syncing = true;
        this._speedSlider.value = max > 1 ? (max - Math.min(speed, max)) / (max - 1) : 0;
        this._syncing = false;
    }

    sync(state) {
        const reason = unavailableReason(state);
        if (reason) {
            this.set({checked: false, subtitle: reason});
            this.menu.setHeader(this._keyboardIcon, _('RGB Keyboard'), state?.error || reason);
            return;
        }

        this.set({
            checked: state.effect !== 'none',
            subtitle: state['battery-saver'] && state.effect === 'wave'
                ? _('Static · low battery') : _(effectInfo(state.effect).label),
        });
        this.menu.setHeader(this._keyboardIcon, _('RGB Keyboard'), state.device);

        for (const [id, item] of this._effectItems)
            item.setOrnament(id === state.effect ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        for (const [color, button] of this._swatches) {
            if (color === state.color)
                button.add_style_pseudo_class('checked');
            else
                button.remove_style_pseudo_class('checked');
        }
        this._logoItem.visible = state['has-logo'];
        this._logoItem.setToggleState(state.logo);

        const usesSpeed = !!effectInfo(state.effect).usesSpeed;
        this._speedSeparator.visible = usesSpeed;
        this._speedSliderItem.visible = usesSpeed;
        if (usesSpeed && !this._speedSlider.dragging)
            this._setSpeedSlider(state.speed);
    }
});

const KeyboardBrightnessSlider = GObject.registerClass(
class KeyboardBrightnessSlider extends QuickSlider {
    _init(client, keyboardIcon) {
        super._init({gicon: keyboardIcon});
        this._client = client;
        this.slider.accessible_name = _('Keyboard brightness');
        trackDragging(this.slider);

        this._debouncer = new Debouncer(value => this._client.setBrightness(value * 255));
        this.slider.connect('notify::value', () => {
            if (!this._syncing)
                this._debouncer.queue(this.slider.value);
        });
        this.connect('destroy', () => this._debouncer.cancel());
    }

    sync(state) {
        this.visible = !unavailableReason(state);
        if (!this.visible || this.slider.dragging)
            return;
        this._syncing = true;
        this.slider.value = state.brightness / 255;
        this._syncing = false;
    }
});

const FanToggle = GObject.registerClass(
class FanToggle extends QuickMenuToggle {
    _init(client, fanIcon) {
        super._init({
            title: _('Fans'),
            gicon: fanIcon,
            toggleMode: false,
        });
        this._client = client;
        this._fanIcon = fanIcon;

        this.connect('clicked', () => {
            const s = this._client.state;
            if (!s)
                return;
            this._client.setFanRpm(s['fan-rpm'] ? 0 : s['last-fan-rpm']);
        });

        this.menu.setHeader(fanIcon, _('Fans and Performance'));
        const section = addScrollableSection(this.menu);

        section.addMenuItem(new PopupMenu.PopupSeparatorMenuItem(_('Profile')));
        this._powerItems = new Map();
        for (const [mode, label] of Object.entries(POWER_MODES)) {
            const item = new PopupMenu.PopupMenuItem(_(label));
            item.connect('activate', () => this._client.setPowerMode(mode));
            section.addMenuItem(item);
            this._powerItems.set(mode, item);
        }

        section.addMenuItem(new PopupMenu.PopupSeparatorMenuItem(_('Fan speed')));
        this._autoItem = new PopupMenu.PopupMenuItem(_('Automatic'));
        this._autoItem.connect('activate', () => this._client.setFanRpm(0));
        section.addMenuItem(this._autoItem);

        this._manualItem = new PopupMenu.PopupMenuItem(_('Manual'));
        this._manualItem.connect('activate', () =>
            this._client.setFanRpm(this._client.state?.['last-fan-rpm'] ?? 0));
        section.addMenuItem(this._manualItem);

        this._rpmSlider = new Slider(0);
        this._rpmSlider.accessible_name = _('Fan speed');
        trackDragging(this._rpmSlider);
        this._rpmSliderItem = addSliderItem(section, this._rpmSlider);

        this._debouncer = new Debouncer(rpm => this._client.setFanRpm(rpm));
        this._rpmSlider.connect('notify::value', () => {
            if (this._syncing)
                return;
            const rpm = this._sliderToRpm();
            this._manualItem.label.text = _('Manual · %d RPM').format(rpm);
            this._debouncer.queue(rpm);
        });
        this.connect('destroy', () => this._debouncer.cancel());
    }

    _sliderToRpm() {
        const s = this._client.state;
        const min = s?.['fan-min'] ?? 0, max = s?.['fan-max'] ?? 0;
        return Math.round((min + this._rpmSlider.value * (max - min)) / 100) * 100;
    }

    sync(state) {
        const reason = unavailableReason(state);
        if (reason) {
            this.set({checked: false, subtitle: reason});
            this.menu.setHeader(this._fanIcon, _('Fans and Performance'), state?.error || reason);
            return;
        }

        const rpm = state['fan-rpm'];
        const manual = rpm > 0;
        const powerLabel = state.power in POWER_MODES ? _(POWER_MODES[state.power]) : state.power;
        this.set({
            checked: manual,
            subtitle: manual ? `${rpm} RPM · ${powerLabel}` : _('Auto · %s').format(powerLabel),
        });
        this.menu.setHeader(this._fanIcon, _('Fans and Performance'), state.device);

        for (const [mode, item] of this._powerItems) {
            item.visible = state['power-modes'].includes(mode);
            item.setOrnament(mode === state.power ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        }
        this._autoItem.setOrnament(manual ? PopupMenu.Ornament.NONE : PopupMenu.Ornament.CHECK);
        this._manualItem.setOrnament(manual ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        this._manualItem.label.text = manual ? _('Manual · %d RPM').format(rpm) : _('Manual');
        this._rpmSliderItem.visible = manual;

        if (manual && !this._rpmSlider.dragging) {
            const min = state['fan-min'], max = state['fan-max'];
            this._syncing = true;
            this._rpmSlider.value = max > min ? (rpm - min) / (max - min) : 0;
            this._syncing = false;
        }
    }
});

const GpuToggle = GObject.registerClass(
class GpuToggle extends QuickMenuToggle {
    _init(client, gpuIcon) {
        super._init({
            title: _('NVIDIA GPU'),
            gicon: gpuIcon,
            toggleMode: false,
        });
        this._client = client;
        this._gpuIcon = gpuIcon;
        this._busy = false;

        // Switching modes needs a password and a reboot: too much for a single click
        this.connect('clicked', () => this.menu.open());

        this.menu.setHeader(gpuIcon, _('NVIDIA GPU'));

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem(_('Mode at next boot')));
        this._modeItems = new Map();
        for (const [mode, label] of Object.entries(GPU_MODES)) {
            const item = new PopupMenu.PopupMenuItem(_(label));
            item.connect('activate', () => this._setMode(mode));
            this.menu.addMenuItem(item);
            this._modeItems.set(mode, item);
        }

        this._hintItem = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this._hintItem.label.add_style_class_name('razerblade-hint');
        this._hintItem.label.opacity = 170; // dimmed in both light and dark styles
        this.menu.addMenuItem(this._hintItem);

        this._rebootSeparator = new PopupMenu.PopupSeparatorMenuItem();
        this.menu.addMenuItem(this._rebootSeparator);
        this._rebootItem = new PopupMenu.PopupMenuItem(_('Restart now…'));
        this._rebootItem.connect('activate', () => Gio.DBus.session.call(
            'org.gnome.SessionManager', '/org/gnome/SessionManager',
            'org.gnome.SessionManager', 'Reboot', null, null,
            Gio.DBusCallFlags.NONE, -1, null, null));
        this.menu.addMenuItem(this._rebootItem);
    }

    async _setMode(mode) {
        if (this._busy || mode === this._client.state?.['gpu-mode'])
            return;
        this._busy = true;
        try {
            const proc = Gio.Subprocess.new(['pkexec', GPU_HELPER, mode],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE);
            const [, stderr] = await proc.communicate_utf8_async(null, null);
            const status = proc.get_if_exited() ? proc.get_exit_status() : -1;
            if (status !== 0 && status !== PKEXEC_DISMISSED)
                Main.notifyError(_('NVIDIA GPU'), stderr?.trim() || _('%s failed with status %d').format(GPU_HELPER, status));
        } catch (e) {
            Main.notifyError(_('NVIDIA GPU'), e.message);
        } finally {
            this._busy = false;
            this._client.refresh();
        }
    }

    sync(state) {
        this.visible = !!state?.['gpu-available'];
        if (!this.visible)
            return;

        const mode = state['gpu-mode'];
        const booted = state['gpu-booted-mode'];
        const sleeping = state['gpu-power'] !== 'active';
        const pending = mode !== booted;

        let status;
        if (booted === 'integrated')
            status = _('Powered off');
        else if (booted === 'hybrid')
            status = sleeping ? _('Suspended') : _('In use');
        else
            status = _('Always on');

        this.set({
            checked: !sleeping,
            subtitle: pending ? _('Restart required') : status,
        });
        this.menu.setHeader(this._gpuIcon, _('NVIDIA GPU'),
            pending ? _('Now: %s · changes after restart').format(status) : status);

        for (const [id, item] of this._modeItems)
            item.setOrnament(id === mode ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        this._hintItem.label.text = mode in GPU_MODE_HINTS ? _(GPU_MODE_HINTS[mode]) : '';
        this._rebootSeparator.visible = pending;
        this._rebootItem.visible = pending;
    }
});

const RazerIndicator = GObject.registerClass(
class RazerIndicator extends SystemIndicator {
    _init(extension) {
        super._init();

        const loadIcon = name => Gio.icon_new_for_string(
            extension.dir.get_child('icons').get_child(`${name}.svg`).get_path());
        const fanIcon = loadIcon('fan-symbolic');
        const keyboardIcon = loadIcon('keyboard-rgb-symbolic');
        const gpuIcon = loadIcon('gpu-symbolic');

        // Panel icon, shown only while the fans run at a fixed speed
        this._indicator = this._addIndicator();
        this._indicator.gicon = fanIcon;
        this._indicator.visible = false;

        this._client = new DaemonClient(
            state => this._sync(state),
            (_method, error) => Main.notifyError('Razer Blade', error.message));

        this._keyboardToggle = new KeyboardToggle(this._client, extension, keyboardIcon);
        this._brightnessSlider = new KeyboardBrightnessSlider(this._client, keyboardIcon);
        this._fanToggle = new FanToggle(this._client, fanIcon);
        this._gpuToggle = new GpuToggle(this._client, gpuIcon);
        this.quickSettingsItems.push(this._keyboardToggle, this._fanToggle, this._gpuToggle);

        // The GPU's runtime PM status changes on its own and sysfs can't notify it:
        // re-read it every second, but only while Quick Settings is open.
        this._quickSettingsMenu = Main.panel.statusArea.quickSettings.menu;
        this._menuOpenId = this._quickSettingsMenu.connect('open-state-changed', (_menu, open) => {
            this._stopPolling();
            if (!open)
                return;
            this._client.refresh();
            this._pollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, GPU_POLL_SECONDS, () => {
                this._client.refresh();
                return GLib.SOURCE_CONTINUE;
            });
        });

        this._sync(this._client.state);
    }

    get brightnessSlider() {
        return this._brightnessSlider;
    }

    _sync(state) {
        this._keyboardToggle.sync(state);
        this._brightnessSlider.sync(state);
        this._fanToggle.sync(state);
        this._gpuToggle.sync(state);
        this._indicator.visible = !unavailableReason(state) && state['fan-rpm'] > 0;
    }

    _stopPolling() {
        if (this._pollId) {
            GLib.source_remove(this._pollId);
            this._pollId = 0;
        }
    }

    destroy() {
        this._stopPolling();
        this._quickSettingsMenu.disconnect(this._menuOpenId);
        this._client.destroy();
        this.quickSettingsItems.forEach(item => item.destroy());
        this._brightnessSlider.destroy();
        super.destroy();
    }
});

export default class RazerBladeExtension extends Extension {
    enable() {
        this._indicator = new RazerIndicator(this);
        const quickSettings = Main.panel.statusArea.quickSettings;
        quickSettings.addExternalIndicator(this._indicator);
        // Sliders span both columns
        quickSettings.menu.addItem(this._indicator.brightnessSlider, 2);

        // At login the extension can be enabled before Quick Settings has created
        // its own items: wait for the display brightness slider to show up
        if (this._moveBelowDisplayBrightness())
            return;
        let attempts = PLACE_SLIDER_ATTEMPTS;
        this._placeId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, PLACE_SLIDER_INTERVAL_MS, () => {
            if (this._moveBelowDisplayBrightness() || --attempts === 0) {
                this._placeId = 0;
                return GLib.SOURCE_REMOVE;
            }
            return GLib.SOURCE_CONTINUE;
        });
    }

    _moveBelowDisplayBrightness() {
        const displaySlider = Main.panel.statusArea.quickSettings._brightness?.quickSettingsItems?.at(-1);
        const slider = this._indicator.brightnessSlider;
        if (!displaySlider || displaySlider.get_parent() !== slider.get_parent())
            return false;
        slider.get_parent().set_child_above_sibling(slider, displaySlider);
        return true;
    }

    disable() {
        if (this._placeId)
            GLib.source_remove(this._placeId);
        this._placeId = 0;
        this._indicator?.destroy();
        this._indicator = null;
    }
}

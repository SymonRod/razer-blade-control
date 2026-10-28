// SPDX-License-Identifier: GPL-2.0-or-later
// D-Bus client for razerbladed, shared by extension.js and prefs.js.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const BUS_NAME = 'io.github.SymonRod.RazerBladeControl';
const OBJECT_PATH = '/io/github/SymonRod/RazerBladeControl';
const IFACE_NAME = 'io.github.SymonRod.RazerBladeControl1';

// Marks labels for xgettext: this module is shared by extension.js and prefs.js,
// so each of them translates the labels with its own gettext when showing them
const N_ = s => s;

export const EFFECTS = [
    {id: 'static', label: N_('Static'), usesColor: true},
    {id: 'breathing', label: N_('Breathing'), usesColor: true},
    {id: 'breathing-dual', label: N_('Breathing (two colors)'), usesColor: true, usesColor2: true},
    {id: 'breathing-random', label: N_('Breathing (random)')},
    {id: 'spectrum', label: N_('Spectrum')},
    // Software wave in razerbladed: maxSpeed matches len(WAVE_PERIODS)
    {id: 'wave', label: N_('Wave'), usesDirection: true, usesSpeed: true, maxSpeed: 9},
    {id: 'reactive', label: N_('Reactive'), usesColor: true, usesSpeed: true, maxSpeed: 4},
    {id: 'starlight', label: N_('Starlight'), usesColor: true, usesSpeed: true, maxSpeed: 3},
];

export const POWER_MODES = {
    balanced: N_('Balanced'),
    gaming: N_('Gaming'),
    creator: N_('Creator'),
};

export const GPU_MODES = {
    standard: N_('Always on'),
    hybrid: N_('Automatic'),
    integrated: N_('Off (Intel only)'),
};

export function effectInfo(id) {
    return EFFECTS.find(e => e.id === id) ?? {id, label: id === 'none' ? N_('Off') : id};
}

export class DaemonClient {
    /**
     * @param {(state: object|null) => void} onStateChanged
     * @param {(method: string, error: Error) => void} [onError]
     */
    constructor(onStateChanged, onError = null) {
        this._onStateChanged = onStateChanged;
        this._onError = onError;
        this._cancellable = new Gio.Cancellable();
        this.state = null;

        this._signalId = Gio.DBus.session.signal_subscribe(
            BUS_NAME, IFACE_NAME, 'StateChanged', OBJECT_PATH, null,
            Gio.DBusSignalFlags.NONE,
            (_conn, _sender, _path, _iface, _signal, params) =>
                this._setState(params.recursiveUnpack()[0]));

        this._watchId = Gio.bus_watch_name(
            Gio.BusType.SESSION, BUS_NAME, Gio.BusNameWatcherFlags.NONE,
            () => this.refresh(),
            () => this._setState(null));

        // Triggers D-Bus activation if the daemon is not running yet
        this.refresh();
    }

    _setState(state) {
        this.state = state;
        this._onStateChanged(state);
    }

    _call(method, signature = null, args = null) {
        return new Promise((resolve, reject) => {
            Gio.DBus.session.call(
                BUS_NAME, OBJECT_PATH, IFACE_NAME, method,
                signature ? new GLib.Variant(signature, args) : null,
                null, Gio.DBusCallFlags.NONE, -1, this._cancellable,
                (conn, res) => {
                    try {
                        resolve(conn.call_finish(res));
                    } catch (e) {
                        reject(e);
                    }
                });
        });
    }

    _isCancelled(e) {
        return e instanceof GLib.Error &&
            e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED);
    }

    async refresh() {
        try {
            const reply = await this._call('GetState');
            this._setState(reply.recursiveUnpack()[0]);
        } catch (e) {
            if (!this._isCancelled(e))
                this._setState(null);
        }
    }

    _invoke(method, signature, args) {
        this._call(method, signature, args).catch(e => {
            if (this._isCancelled(e))
                return;
            if (e instanceof GLib.Error)
                Gio.DBusError.strip_remote_error(e);
            console.warn(`razerblade: ${method} failed: ${e.message}`);
            this._onError?.(method, new Error(e.message));
        });
    }

    /**
     * @param {string} effect
     * @param {{color?: string, color2?: string, speed?: number, direction?: number}} options
     */
    setEffect(effect, options = {}) {
        const opts = {};
        for (const key of ['color', 'color2'])
            if (key in options)
                opts[key] = new GLib.Variant('s', options[key]);
        for (const key of ['speed', 'direction'])
            if (key in options)
                opts[key] = new GLib.Variant('y', options[key]);
        this._invoke('SetEffect', '(sa{sv})', [effect, opts]);
    }

    setBrightness(value) {
        this._invoke('SetBrightness', '(y)', [Math.round(value)]);
    }

    setLogo(enabled) {
        this._invoke('SetLogo', '(b)', [enabled]);
    }

    setPowerMode(mode) {
        this._invoke('SetPowerMode', '(s)', [mode]);
    }

    setFanRpm(rpm) {
        this._invoke('SetFanRpm', '(q)', [Math.round(rpm)]);
    }

    setBatteryThreshold(percent) {
        this._invoke('SetBatteryThreshold', '(y)', [Math.round(percent)]);
    }

    reapply() {
        this._invoke('Reapply', null, null);
    }

    destroy() {
        this._cancellable.cancel();
        Gio.DBus.session.signal_unsubscribe(this._signalId);
        Gio.bus_unwatch_name(this._watchId);
        this._onStateChanged = () => {};
        this._onError = null;
    }
}

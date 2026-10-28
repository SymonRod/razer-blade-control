// SPDX-License-Identifier: GPL-2.0-or-later
import Adw from 'gi://Adw';
import Gdk from 'gi://Gdk';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences, gettext as _} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

import {DaemonClient, EFFECTS, effectInfo} from './daemon.js';

function hexToRgba(hex) {
    const rgba = new Gdk.RGBA();
    rgba.parse(hex);
    return rgba;
}

function rgbaToHex(rgba) {
    const channel = v => Math.round(v * 255).toString(16).padStart(2, '0');
    return `#${channel(rgba.red)}${channel(rgba.green)}${channel(rgba.blue)}`;
}

export default class RazerBladePreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const page = new Adw.PreferencesPage();
        window.add(page);

        // Status
        const statusGroup = new Adw.PreferencesGroup({title: _('Device')});
        page.add(statusGroup);
        const statusRow = new Adw.ActionRow({title: _('Status')});
        const reapplyButton = new Gtk.Button({
            label: _('Reapply'),
            valign: Gtk.Align.CENTER,
            tooltip_text: _('Send all saved settings to the laptop again'),
        });
        statusRow.add_suffix(reapplyButton);
        statusGroup.add(statusRow);

        // Keyboard effect
        const effectGroup = new Adw.PreferencesGroup({title: _('Keyboard Effect')});
        page.add(effectGroup);

        const effectIds = ['none', ...EFFECTS.map(e => e.id)];
        const effectRow = new Adw.ComboRow({
            title: _('Effect'),
            model: Gtk.StringList.new(effectIds.map(id => _(effectInfo(id).label))),
        });
        effectGroup.add(effectRow);

        const makeColorRow = title => {
            const button = new Gtk.ColorDialogButton({
                dialog: new Gtk.ColorDialog({with_alpha: false}),
                valign: Gtk.Align.CENTER,
            });
            const row = new Adw.ActionRow({title, activatable_widget: button});
            row.add_suffix(button);
            effectGroup.add(row);
            return [row, button];
        };
        const [colorRow, colorButton] = makeColorRow(_('Primary color'));
        const [color2Row, color2Button] = makeColorRow(_('Secondary color'));

        // Speed 1 is the fastest: the inverted scale puts it on the right
        const speedScale = new Gtk.Scale({
            adjustment: new Gtk.Adjustment({lower: 1, upper: 4, step_increment: 1, page_increment: 1}),
            inverted: true,
            digits: 0,
            round_digits: 0,
            draw_value: false,
            hexpand: true,
            width_request: 200,
            valign: Gtk.Align.CENTER,
        });
        const speedBox = new Gtk.Box({spacing: 6, valign: Gtk.Align.CENTER});
        speedBox.append(new Gtk.Label({label: _('Slow'), css_classes: ['dim-label']}));
        speedBox.append(speedScale);
        speedBox.append(new Gtk.Label({label: _('Fast'), css_classes: ['dim-label']}));
        const speedRow = new Adw.ActionRow({title: _('Speed')});
        speedRow.add_suffix(speedBox);
        effectGroup.add(speedRow);

        const directionRow = new Adw.ComboRow({
            title: _('Direction'),
            model: Gtk.StringList.new([_('Left to right'), _('Right to left')]),
        });
        effectGroup.add(directionRow);

        // Battery
        const batteryGroup = new Adw.PreferencesGroup({title: _('Battery')});
        page.add(batteryGroup);
        const thresholdRow = new Adw.SpinRow({
            title: _('Static wave below (%)'),
            subtitle: _('On battery, below this charge Wave turns into Static in the primary color to save CPU. 0 disables it.'),
            adjustment: new Gtk.Adjustment({lower: 0, upper: 100, step_increment: 5, page_increment: 10}),
        });
        batteryGroup.add(thresholdRow);

        let syncing = false;
        const client = new DaemonClient(state => {
            syncing = true;
            const available = !!state?.connected;
            statusRow.subtitle = !state ? _('The razerbladed service is not running')
                : state.connected ? state.device : state.error || _('Device not accessible');
            effectGroup.sensitive = available;
            reapplyButton.sensitive = !!state;
            batteryGroup.sensitive = !!state;
            if (state)
                thresholdRow.value = state['battery-threshold'];
            if (available) {
                const info = effectInfo(state.effect);
                effectRow.selected = Math.max(effectIds.indexOf(state.effect), 0);
                colorButton.rgba = hexToRgba(state.color);
                color2Button.rgba = hexToRgba(state.color2);
                speedScale.adjustment.upper = info.maxSpeed ?? 4;
                speedScale.set_value(state.speed);
                directionRow.selected = state.direction - 1;
                // The wave uses the main color when the battery saver turns it static
                const isWave = state.effect === 'wave';
                colorRow.visible = !!info.usesColor || isWave;
                colorRow.subtitle = isWave ? _('Used when the battery is low') : '';
                color2Row.visible = !!info.usesColor2;
                speedRow.visible = !!info.usesSpeed;
                directionRow.visible = !!info.usesDirection;
            }
            syncing = false;
        }, (_method, error) => {
            window.add_toast(new Adw.Toast({title: error.message}));
        });

        const apply = (options = {}) => {
            if (syncing || !client.state)
                return;
            client.setEffect(effectIds[effectRow.selected], options);
        };
        effectRow.connect('notify::selected', () => apply());
        colorButton.connect('notify::rgba', () => apply({color: rgbaToHex(colorButton.rgba)}));
        color2Button.connect('notify::rgba', () => apply({color2: rgbaToHex(color2Button.rgba)}));
        speedScale.connect('value-changed', () => {
            const speed = Math.round(speedScale.get_value());
            if (speed !== client.state?.speed)
                apply({speed});
        });
        directionRow.connect('notify::selected', () => apply({direction: directionRow.selected + 1}));
        thresholdRow.connect('notify::value', () => {
            if (!syncing && client.state && thresholdRow.value !== client.state['battery-threshold'])
                client.setBatteryThreshold(thresholdRow.value);
        });
        reapplyButton.connect('clicked', () => client.reapply());

        window.connect('close-request', () => {
            client.destroy();
            return false;
        });
    }
}

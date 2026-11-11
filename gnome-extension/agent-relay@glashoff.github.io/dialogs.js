// The dialog for giving a grant or approving a request: duration, where it
// runs, and the confirmation of the full-access warning when it applies.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as CheckBox from 'resource:///org/gnome/shell/ui/checkBox.js';
import * as Dialog from 'resource:///org/gnome/shell/ui/dialog.js';
import * as ModalDialog from 'resource:///org/gnome/shell/ui/modalDialog.js';

import {oneLine} from './text.js';

function minutesToMidnight() {
    const now = GLib.DateTime.new_now_local();
    const midnight = GLib.DateTime.new_local(now.get_year(), now.get_month(),
        now.get_day_of_month(), 0, 0, 0).add_days(1);
    return Math.max(1, Math.ceil(midnight.difference(now) / 60e6));
}

const DURATIONS = [
    ['15 min', () => 15],
    ['1 hour', () => 60],
    ['4 hours', () => 240],
    ['Until midnight', minutesToMidnight],
];

// What the dialog is for: {target} (a project name or '*') to give a grant,
// or {request} (from the relay's state) to approve one. onDone(promise) gets
// the call it made.
export const AccessDialog = GObject.registerClass(
class AccessDialog extends ModalDialog.ModalDialog {
    _init(client, {target = null, request = null, kind = ''}, onDone) {
        super._init({styleClass: 'agent-relay-dialog'});
        this._client = client;
        this._request = request;
        this._target = request ? request.project : target;
        this._onDone = onDone;
        const state = client.state;
        const project = state.projects.find(p => p.name === this._target);
        this._all = this._target === '*';
        this._containerProject = !this._all && project?.dev_container;
        this._hostProject = !this._all && project && !project.dev_container;
        this._warnings = state.warnings;

        let title, description;
        if (request) {
            title = `Approve request ${request.number}`;
            description = request.kind === 'host'
                ? `${request.project} asks to run ON THE HOST, instead of its dev container:`
                : `${request.project} asks to run:`;
        } else if (this._all) {
            title = 'Grant all projects';
            description = 'Every project with a dev container, run in its dev container.';
        } else {
            title = `Grant ${this._target}`;
            description = this._hostProject
                ? 'This project has no dev container.' : 'Runs in its dev container.';
        }
        const content = new Dialog.MessageDialogContent({title, description});
        this.contentLayout.add_child(content);

        if (request) {
            for (const text of request.messages) {
                const label = new St.Label({
                    text: `“${oneLine(text, 600)}”`,
                    style_class: 'agent-relay-message',
                });
                label.clutter_text.line_wrap = true;
                this.contentLayout.add_child(label);
            }
        }

        // Duration
        const row = new St.BoxLayout({style_class: 'agent-relay-buttons'});
        this._minutes = 60;
        const select = (button, minutes) => {
            this._minutes = minutes();
            for (const b of this._durationButtons) {
                b.checked = b === button;
                if (b.checked)
                    b.add_style_class_name('selected');
                else
                    b.remove_style_class_name('selected');
            }
        };
        this._durationButtons = DURATIONS.map(([label, minutes]) => {
            const button = new St.Button({
                label, style_class: 'button', can_focus: true, toggle_mode: true,
            });
            button.connect('clicked', () => select(button, minutes));
            row.add_child(button);
            return button;
        });
        this.contentLayout.add_child(row);
        select(this._durationButtons[1], DURATIONS[1][1]);

        // Where it runs, and what it includes
        this._onHost = null;
        this._includeHost = null;
        if (this._containerProject) {
            this._onHost = new CheckBox.CheckBox('Run on the host instead of the dev container');
            this._onHost.checked = kind === 'host';
            this._onHost.connect('notify::checked', () => this._sync());
            this.contentLayout.add_child(this._onHost);
        }
        if (this._all && state.allow_host) {
            this._includeHost = new CheckBox.CheckBox('Include projects without a dev container');
            this._includeHost.connect('notify::checked', () => this._sync());
            this.contentLayout.add_child(this._includeHost);
        }
        this._endOnLock = new CheckBox.CheckBox('End when the screen locks');
        this._lockTouched = false;
        this._endOnLock.connect('clicked', () => (this._lockTouched = true));
        this.contentLayout.add_child(this._endOnLock);

        this._warning = new St.Label({style_class: 'agent-relay-warning'});
        this._warning.clutter_text.line_wrap = true;
        this.contentLayout.add_child(this._warning);

        this.addButton({label: 'Cancel', action: () => this.close(), key: Clutter.KEY_Escape});
        this._okButton = this.addButton({
            label: 'Allow', action: () => this._confirm(), default: true,
        });
        this._sync();
    }

    get _fullAccess() {
        return Boolean(this._hostProject || this._onHost?.checked || this._includeHost?.checked);
    }

    _sync() {
        const full = this._fullAccess;
        if (!this._lockTouched)
            this._endOnLock.checked = false;
        const warning = this._onHost?.checked
            ? this._warnings.on_host : this._warnings.host_project;
        this._warning.text = full ? `Full access. ${warning}` : '';
        this._warning.visible = full;
        this._okButton.label = full ? 'Allow with full access' : 'Allow';
    }

    _confirm() {
        const common = {
            kind: this._onHost?.checked ? 'host' : '',
            minutes: this._minutes,
            end_on_lock: this._endOnLock.checked,
            full_access_ok: this._fullAccess,
            when_idle: true,
        };
        const call = this._request
            ? this._client.approve({
                request: this._request.id, messages: this._request.messages.length, ...common,
            })
            : this._client.grant({
                target: this._target,
                include_host_projects: Boolean(this._includeHost?.checked),
                ...common,
            });
        this._onDone(call, this._target);
        this.close();
    }
});

// The last voice exchange in full: what was understood and what Claude
// answered. Plain text only.
export const ExchangeDialog = GObject.registerClass(
class ExchangeDialog extends ModalDialog.ModalDialog {
    _init(user, assistant) {
        super._init({styleClass: 'agent-relay-dialog'});
        this.contentLayout.add_child(new Dialog.MessageDialogContent({
            title: 'Last exchange',
            description: 'As understood by speech recognition, and as answered.',
        }));
        const box = new St.BoxLayout({vertical: true, style_class: 'agent-relay-exchange'});
        for (const [who, text] of [['You', user], ['Claude', assistant]]) {
            box.add_child(new St.Label({text: who, style_class: 'agent-relay-heading'}));
            const label = new St.Label({
                text: String(text ?? '').trim() || '(nothing)',
                style_class: 'agent-relay-message',
            });
            label.clutter_text.line_wrap = true;
            box.add_child(label);
        }
        const scroll = new St.ScrollView({
            style_class: 'agent-relay-exchange-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            x_expand: true, y_expand: true,
        });
        scroll.child = box;
        this.contentLayout.add_child(scroll);
        this.addButton({
            label: 'Close', action: () => this.close(), key: Clutter.KEY_Escape, default: true,
        });
    }
});

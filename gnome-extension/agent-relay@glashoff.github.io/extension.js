// Agent Relay: approvals for agent-relay-mcp in the top bar and in quick
// settings, with notifications for new requests, ending grants and requests
// that start.
//
// The relay decides everything; this extension only shows its state and
// passes on what is chosen here. It runs in the normal session only, not on
// the lock screen, so approving needs an unlocked computer.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as QuickSettings from 'resource:///org/gnome/shell/ui/quickSettings.js';

import {RelayClient, ServiceClient} from './client.js';
import {AccessDialog} from './dialogs.js';
import {fillMenu} from './menu.js';
import {duration, oneLine, targetName, where} from './text.js';

const ICON_LOCKED = 'changes-prevent-symbolic';
const ICON_OPEN = 'changes-allow-symbolic';
const ICON_FULL = 'dialog-warning-symbolic';
const EXPIRY_WARNING = 5 * 60;      // seconds before a grant ends
const TICK = 30;                    // seconds between updates of the times shown

function iconFor(state) {
    if (!state)
        return ICON_LOCKED;
    if (state.grants.some(g => g.full_access))
        return ICON_FULL;
    return state.grants.length ? ICON_OPEN : ICON_LOCKED;
}

function summary(state) {
    if (!state)
        return 'Not running';
    const parts = [];
    if (state.requests.length)
        parts.push(`${state.requests.length} waiting`);
    parts.push(state.grants.length ? `${state.grants.length} approved` : 'locked');
    return parts.join(', ');
}

const PanelButton = GObject.registerClass(
class PanelButton extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'Agent Relay');
        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        this.icon = new St.Icon({icon_name: ICON_LOCKED, style_class: 'system-status-icon'});
        this.count = new St.Label({
            text: '', style_class: 'agent-relay-count', y_align: Clutter.ActorAlign.CENTER,
            visible: false,
        });
        box.add_child(this.icon);
        box.add_child(this.count);
        this.add_child(box);
    }
});

const RelayToggle = GObject.registerClass(
class RelayToggle extends QuickSettings.QuickMenuToggle {
    _init() {
        super._init({title: 'Agent Relay', iconName: ICON_LOCKED, toggleMode: false});
        this.menu.setHeader(ICON_LOCKED, 'Agent Relay');
        this.connect('clicked', () => this.menu.open());
    }
});

const RelayIndicator = GObject.registerClass(
class RelayIndicator extends QuickSettings.SystemIndicator {
    _init() {
        super._init();
        this.toggle = new RelayToggle();
        this.quickSettingsItems.push(this.toggle);
    }

    destroy() {
        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});

export default class AgentRelayExtension extends Extension {
    enable() {
        this._service = new ServiceClient();
        this._serviceState = 'unavailable';
        this._warned = new Set();           // grants warned about ending soon
        this._seenRequests = new Set();
        this._requestNotes = new Map();     // request id: its notification
        this._source = null;
        this._firstState = true;

        this._button = new PanelButton();
        Main.panel.addToStatusArea(this.uuid, this._button);
        this._indicator = new RelayIndicator();
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);

        for (const menu of [this._button.menu, this._indicator.toggle.menu]) {
            menu.connect('open-state-changed', (_menu, open) => {
                if (open) {
                    this._client.refresh();
                    this._refreshService();
                }
            });
        }

        this._client = new RelayClient(() => this._render(), event => this._onEvent(event));
        this._refreshService();
        this._tickId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, TICK, () => {
            this._client.refresh();
            this._refreshService();
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        if (this._tickId)
            GLib.source_remove(this._tickId);
        this._tickId = 0;
        this._client?.destroy();
        this._client = null;
        this._dialog?.close();
        this._dialog = null;
        this._button?.destroy();
        this._button = null;
        this._indicator?.destroy();
        this._indicator = null;
        this._source?.destroy();
        this._source = null;
        this._service = null;
    }

    // --- showing the state ---------------------------------------------------

    async _refreshService() {
        const state = await this._service?.activeState();
        if (state && state !== this._serviceState) {
            this._serviceState = state;
            this._render();
        }
    }

    _render() {
        if (!this._client)
            return;
        const state = this._client.state;
        const icon = iconFor(state);
        this._button.icon.icon_name = icon;
        const waiting = state?.requests.length ?? 0;
        this._button.count.text = waiting ? String(waiting) : '';
        this._button.count.visible = waiting > 0;
        const toggle = this._indicator.toggle;
        toggle.iconName = icon;
        toggle.checked = Boolean(state?.grants.length);
        toggle.subtitle = summary(state);
        toggle.menu.setHeader(icon, 'Agent Relay', summary(state));
        const ui = this._ui();
        fillMenu(this._button.menu, ui);
        fillMenu(toggle.menu, ui);
        if (state)
            this._checkState(state);
    }

    _ui() {
        return {
            client: this._client,
            serviceState: this._serviceState,
            controlService: verb => this._run(this._service.control(verb)
                .then(() => this._refreshService()), null),
            grantDialog: (target, kind = '') => this._openDialog({target, kind}),
            approveDialog: (request, kind = '') => this._openDialog({request, kind}),
            run: (promise, what) => this._run(promise, what),
        };
    }

    _openDialog(what) {
        this._button.menu.close();
        this._indicator.toggle.menu.close();
        Main.panel.statusArea.quickSettings.menu.close();
        if (!this._client?.state)
            return;
        this._dialog = new AccessDialog(this._client, what,
            (call, target) => this._run(call, target));
        this._dialog.connect('closed', () => (this._dialog = null));
        this._dialog.open();
    }

    async _run(promise, what) {
        try {
            const result = await promise;
            if (result?.switch) {
                this._notify('Switch waits', result.switch.waiting_for, [
                    ['Cancel switch', () =>
                        this._run(this._client.call('CancelSwitch', result.switch.project))],
                ]);
            } else if (result?.grant?.cut_to_limit) {
                this._notify(`Approved ${targetName(result.grant.target)}`,
                    `Shortened to the upper limit: ${duration(result.grant.seconds_left)}.`);
            }
        } catch (error) {
            this._notify(what ? `Agent Relay: ${what}` : 'Agent Relay', error.message);
        }
    }

    // --- notifications ---------------------------------------------------------

    _forget(requestId) {
        this._requestNotes.get(requestId)?.destroy();
        this._requestNotes.delete(requestId);
    }

    _notify(title, body, actions = [], urgent = false) {
        if (!this._source) {
            this._source = new MessageTray.Source({title: 'Agent Relay', iconName: ICON_LOCKED});
            this._source.connect('destroy', () => (this._source = null));
            Main.messageTray.add(this._source);
        }
        const notification = new MessageTray.Notification({
            source: this._source, title, body: oneLine(body, 400),
            urgency: urgent ? MessageTray.Urgency.CRITICAL : MessageTray.Urgency.NORMAL,
        });
        for (const [label, callback] of actions)
            notification.addAction(label, callback);
        this._source.addNotification(notification);
        return notification;
    }

    _checkState(state) {
        const now = Date.now() / 1000;
        // After enabling (at login, and after unlocking) say what is waiting.
        if (this._firstState) {
            this._firstState = false;
            for (const r of state.requests)
                this._seenRequests.add(r.id);
            if (state.requests.length) {
                this._notify(`${state.requests.length} request(s) wait for approval`,
                    state.requests.map(r => `${r.number}: ${r.project}`).join(', '),
                    [['Show', () => this._button.menu.open()]], true);
            }
        }
        // Grants that end soon.
        for (const g of state.grants) {
            const left = g.expires - now;
            if (left <= EXPIRY_WARNING && !this._warned.has(g.id)) {
                this._warned.add(g.id);
                const name = targetName(g.target);
                const extend = g.full_access
                    ? () => this._openDialog({target: g.target, kind: g.kind})
                    : () => this._run(this._client.grant({
                        target: g.target, kind: g.target === '*' ? '' : g.kind,
                        minutes: 60, end_on_lock: g.end_on_lock,
                    }), name);
                this._notify(`Approval for ${name} ends in ${duration(left)}`,
                    'Extend it, or let it end.',
                    [[g.full_access ? 'Extend…' : 'Extend 1 h', extend]]);
            }
        }
    }

    _onEvent(event) {
        switch (event.type) {
        case 'request_added': {
            if (this._seenRequests.has(event.request))
                break;
            this._seenRequests.add(event.request);
            this._requestNotification(event);
            break;
        }
        case 'request_answered':
            this._forget(event.request);
            break;
        case 'request_expired':
            this._forget(event.request);
            this._notify(`Request for ${event.project} expired`, 'Nothing was run.');
            break;
        case 'grant_ended':
            if (event.reason !== 'revoked')
                this._notify('Approval ended', `Reason: ${event.reason}.`);
            break;
        case 'run_started': {
            const title = event.full_access
                ? `Claude Code started in ${event.project} (full access)`
                : `Claude Code started in ${event.project}`;
            this._notify(title, event.message ?? '', [
                ['Stop', () => this._run(this._client.call('StopProject', event.project, false),
                    event.project)],
                ['Stop and lock', () => this._run(
                    this._client.call('StopProject', event.project, true), event.project)],
            ]);
            break;
        }
        case 'switch_done':
            this._notify(`${event.project} now runs ${where(event.kind)}`, '');
            break;
        case 'switch_expired':
            this._notify(`Switch of ${event.project} gave up`,
                'The other side stayed busy; held messages wait for approval again.');
            break;
        case 'keep_awake_dropped':
            this._notify('No longer keeping the computer awake', event.reason ?? '');
            break;
        }
    }

    async _requestNotification(event) {
        // The event has no text; read the request from the state.
        await this._client.refresh();
        const request = this._client?.state?.requests.find(r => r.id === event.request);
        if (!request)
            return;
        const full = request.full_access;
        const title = full
            ? `Approve ${request.project}? Number ${request.number}, FULL ACCESS`
            : `Approve ${request.project}? Number ${request.number}`;
        const body = request.messages.map(m => oneLine(m, 200)).join(' / ');
        const actions = full
            ? [['Review…', () => this._openDialog({request})]]
            : [['Allow 1 h', () => this._run(this._client.approve(
                {request: request.id, minutes: 60, when_idle: true}), request.project)],
            ['Review…', () => this._openDialog({request})]];
        actions.push(['Deny', () => this._run(this._client.call('Deny', request.id),
            request.project)]);
        const notification = this._notify(title, body, actions, true);
        this._requestNotes.set(request.id, notification);
        notification.connect('destroy', () => this._requestNotes.delete(request.id));
    }
}

// The contents of the relay's menu, filled into a PopupMenu (the panel
// icon's menu and the quick settings toggle's menu get the same contents).

import St from 'gi://St';

import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {duration, oneLine, targetName, where} from './text.js';

const KEEP_AWAKE = [
    ['off', 'Off'],
    ['running', 'While a request runs'],
    ['granted', 'While anything is approved'],
];

function label(text, style = '') {
    const l = new St.Label({text, style_class: style, x_expand: true});
    l.clutter_text.line_wrap = true;
    return l;
}

// A non-clickable menu row with lines of text and a row of buttons.
function block(lines, buttons = []) {
    const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
    const box = new St.BoxLayout({vertical: true, x_expand: true});
    for (const [text, style] of lines)
        box.add_child(label(text, style));
    if (buttons.length) {
        const row = new St.BoxLayout({style_class: 'agent-relay-buttons'});
        for (const [text, action] of buttons) {
            const button = new St.Button({label: text, style_class: 'button', can_focus: true});
            button.connect('clicked', () => action());
            row.add_child(button);
        }
        box.add_child(row);
    }
    item.add_child(box);
    return item;
}

function heading(text) {
    const item = new PopupMenu.PopupMenuItem(text, {reactive: false, can_focus: false});
    item.label.add_style_class_name('agent-relay-heading');
    return item;
}

// ui: {client, serviceState, controlService(verb), grantDialog(target, kind),
//      approveDialog(request), run(promise, what), close()}
export function fillMenu(menu, ui) {
    menu.removeAll();
    const state = ui.client.state;
    const now = Date.now() / 1000;

    // The service
    const service = ui.serviceState;
    const reachable = state !== null;
    const status = service === 'unavailable'
        ? (reachable ? 'running' : 'not reachable')
        : service;
    const serviceButtons = service === 'unavailable' ? [] : [
        ['Start', () => ui.controlService('Start')],
        ['Stop', () => ui.controlService('Stop')],
        ['Restart', () => ui.controlService('Restart')],
    ];
    const lines = [[`Relay: ${status}`, 'agent-relay-heading']];
    if (!reachable)
        lines.push(['Nothing can be approved while it is not running.', 'agent-relay-dim']);
    menu.addMenuItem(block(lines, serviceButtons));
    if (!reachable)
        return;

    // Requests waiting for approval
    if (state.requests.length) {
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        menu.addMenuItem(heading('Waiting for approval'));
        for (const r of state.requests) {
            const project = state.projects.find(p => p.name === r.project);
            const rows = [[`Number ${r.number}: ${r.project}, ${duration(now - r.created)} ago`, '']];
            if (r.full_access)
                rows.push(['No dev container: full access to this computer', 'agent-relay-warning']);
            for (const m of r.messages)
                rows.push([`“${oneLine(m, 200)}”`, 'agent-relay-message']);
            rows.push([`Expires in ${duration(r.expires - now)}`, 'agent-relay-dim']);
            const buttons = [];
            if (r.full_access) {
                buttons.push(['Review…', () => ui.approveDialog(r)]);
            } else {
                buttons.push(['Allow 1 h', () => ui.run(ui.client.approve(
                    {request: r.id, minutes: 60, when_idle: true}), r.project)]);
                buttons.push(['Allow…', () => ui.approveDialog(r)]);
            }
            if (project?.dev_container && state.allow_host)
                buttons.push(['On the host…', () => ui.approveDialog(r, 'host')]);
            buttons.push(['Deny', () => ui.run(ui.client.call('Deny', r.id), r.project)]);
            menu.addMenuItem(block(rows, buttons));
        }
    }

    // Switches waiting for the other side to be idle
    for (const s of state.switches) {
        menu.addMenuItem(block([
            [`${s.project}: switching to run ${where(s.kind)} once idle`, 'agent-relay-warning'],
            [`${s.held} message(s) held; gives up in ${duration(s.expires - now)}`, 'agent-relay-dim'],
        ], [['Cancel switch', () => ui.run(ui.client.call('CancelSwitch', s.project))]]));
    }

    // Active grants
    menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
    menu.addMenuItem(heading(state.grants.length ? 'Approved' : 'Every project is locked'));
    for (const g of state.grants) {
        let name = targetName(g.target);
        if (g.target === '*' && g.include_host_projects)
            name += ', including projects without a dev container';
        const project = state.projects.find(p => p.name === g.target);
        if (g.target !== '*' && g.kind === 'host' && project?.dev_container)
            name += ' (on the host)';
        const rows = [[name, '']];
        if (g.full_access)
            rows.push(['Full access to this computer', 'agent-relay-warning']);
        const idle = g.idle_ends - now;
        const end = g.expires - now;
        let when = `${duration(Math.min(end, idle))} left`;
        if (idle < end)
            when += ' unless used';
        if (g.end_on_lock)
            when += ', ends when the screen locks';
        rows.push([when, 'agent-relay-dim']);
        menu.addMenuItem(block(rows, [
            ['Revoke', () => ui.run(ui.client.call('Revoke', g.id, false))],
            ['Revoke and stop', () => ui.run(ui.client.call('Revoke', g.id, true))],
        ]));
    }

    // Giving a grant
    const give = new PopupMenu.PopupSubMenuMenuItem('Grant access…');
    give.menu.addAction('All projects…', () => ui.grantDialog('*'));
    for (const p of state.projects) {
        const marks = [];
        if (!p.dev_container)
            marks.push('no dev container');
        if (p.grant)
            marks.push(`approved ${where(p.kind)}`);
        if (p.busy)
            marks.push('working');
        const text = marks.length ? `${p.name} (${marks.join(', ')})…` : `${p.name}…`;
        give.menu.addAction(text, () => ui.grantDialog(p.name));
    }
    menu.addMenuItem(give);

    // Keeping the computer awake
    const k = state.keep_awake;
    if (k) {
        const current = KEEP_AWAKE.find(([mode]) => mode === k.mode)?.[1] ?? k.mode;
        const awake = new PopupMenu.PopupSubMenuMenuItem(
            `Keep awake: ${current}${k.active ? ' (now)' : ''}`);
        for (const [mode, text] of KEEP_AWAKE) {
            const item = awake.menu.addAction(text, () =>
                ui.run(ui.client.call('SetKeepAwake', mode, k.lid)));
            item.setOrnament(mode === k.mode
                ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        }
        const lid = new PopupMenu.PopupSwitchMenuItem('Also with the lid closed', k.lid);
        lid.connect('toggled', (_item, on) =>
            ui.run(ui.client.call('SetKeepAwake', k.mode, on)));
        awake.menu.addMenuItem(lid);
        if (k.lid)
            awake.menu.addMenuItem(block([['Keeps running in a closed bag: it gets warm.',
                'agent-relay-warning']]));
        if (k.reason)
            awake.menu.addMenuItem(block([[`Not now: ${k.reason}`, 'agent-relay-dim']]));
        menu.addMenuItem(awake);
    }

    if (state.grants.length) {
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        menu.addAction('Revoke everything', () => ui.run(ui.client.call('RevokeAll', false)));
    }
}

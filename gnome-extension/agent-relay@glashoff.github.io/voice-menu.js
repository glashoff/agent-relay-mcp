// The "Voice" submenu: the voice service (agent-relay-voice) on and off, what
// it is doing, listening, its engines and model, and the last exchange.
//
// Unlike the rest of the menu it is updated in place, not rebuilt: the phase
// changes every few seconds while you talk, and rebuilding would close the
// submenu each time. Nothing here grants or approves relay access.

import Pango from 'gi://Pango';
import St from 'gi://St';

import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {VOICE_UNIT} from './voice.js';
import {oneLine} from './text.js';

const PHASES = {
    starting: 'Starting',
    listening: 'Listening',
    paused: 'Paused: not listening',
    muted: 'Microphone muted',
    hearing: 'Hearing you',
    transcribing: 'Transcribing',
    thinking: 'Thinking',
    speaking: 'Speaking',
    error: 'Error',
};

const CHOICES = [
    ['stt', 'stt', 'Speech recognition'],
    ['tts', 'tts', 'Voice'],
    ['claude_models', 'claude_model', 'Claude model'],
];

export const ICON_MIC = 'audio-input-microphone-symbolic';
export const ICON_MIC_OFF = 'microphone-disabled-symbolic';
export const ICON_MIC_ERROR = 'microphone-hardware-disabled-symbolic';

// What the service does, in a word or two, and an optional second line.
// voice: VoiceClient, unit: VoiceUnit
export function describe(voice, unit) {
    const state = voice.state;
    if (state) {
        const text = PHASES[state.phase] ?? oneLine(state.phase ?? 'Unknown', 30);
        const details = [];
        if (state.error)
            details.push(oneLine(state.error, 200));
        if (state.loading)
            details.push(`Loading ${oneLine(state.loading, 80)}…`);
        // Running, but not as the unit: the switch cannot stop it.
        if (unit.load === 'not-found')
            details.push(`Not started by ${VOICE_UNIT}, which is not installed.`);
        else if (unit.load === 'unavailable')
            details.push(`systemd cannot be asked about ${VOICE_UNIT}.`);
        return [text, details.join('\n')];
    }
    if (unit.active === 'deactivating')
        return ['Stopping…', ''];
    if (unit.on)
        return ['Starting…', 'Waiting for the voice service to answer.'];
    if (unit.load === 'not-found')
        return ['Off', `Not installed: ${VOICE_UNIT} was not found.`];
    if (unit.load === 'unavailable')
        return ['Off', `systemd cannot be asked about ${VOICE_UNIT}.`];
    if (unit.load !== 'loaded')
        return ['Off', `${VOICE_UNIT}: ${unit.load}.`];
    if (unit.active === 'failed')
        return ['Off (failed)', `See: journalctl --user -u ${VOICE_UNIT}`];
    return ['Off', ''];
}

// The panel's microphone: [icon name, style class] or null for hidden.
export function micFor(voice, unit) {
    const state = voice.state;
    if (!state)
        return unit.on && unit.active !== 'deactivating' ? [ICON_MIC, 'agent-relay-mic-idle'] : null;
    switch (state.phase) {
    case 'error':
        return [ICON_MIC_ERROR, 'agent-relay-mic-error'];
    case 'muted':
    case 'paused':
        return [ICON_MIC_OFF, 'agent-relay-mic-idle'];
    case 'starting':
        return [ICON_MIC, 'agent-relay-mic-idle'];
    case 'hearing':
        return [ICON_MIC, 'agent-relay-mic-hearing'];
    default:
        // listening, transcribing, thinking, speaking
        return voice.listening === false
            ? [ICON_MIC_OFF, 'agent-relay-mic-idle'] : [ICON_MIC, ''];
    }
}

// A line of text that wraps (in a vertical box, as menu.js does, so that
// it gets its height for its width).
function textItem(style = '') {
    const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
    const box = new St.BoxLayout({vertical: true, x_expand: true});
    const label = new St.Label({text: '', style_class: style, x_expand: true});
    label.clutter_text.line_wrap = true;
    label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    box.add_child(label);
    item.add_child(box);
    item.label = label;
    return item;
}

function buttonRow(buttons) {
    const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
    const row = new St.BoxLayout({style_class: 'agent-relay-buttons'});
    const made = buttons.map(([text, action]) => {
        const button = new St.Button({label: text, style_class: 'button', can_focus: true});
        button.connect('clicked', () => action());
        row.add_child(button);
        return button;
    });
    item.add_child(row);
    return [item, made];
}

// ui: {voice: VoiceClient, unit: VoiceUnit, setOn(on), run(promise, what),
//      showExchange()}
export class VoiceSection {
    constructor(ui) {
        this._ui = ui;
        this.item = new PopupMenu.PopupSubMenuMenuItem('Voice', true);
        this.item.icon.icon_name = ICON_MIC;
        const menu = this.item.menu;

        this._switch = new PopupMenu.PopupSwitchMenuItem('Voice service', false);
        // setToggleState() emits "toggled" too; only clicks count.
        this._updating = false;
        this._switch.connect('toggled', (_item, on) => {
            if (!this._updating)
                ui.setOn(on);
        });
        menu.addMenuItem(this._switch);

        // Loading, errors, and why the switch cannot be used.
        this._detail = textItem('agent-relay-dim');
        menu.addMenuItem(this._detail);

        // Everything below needs the service's socket.
        this._controls = new PopupMenu.PopupMenuSection();
        this._listening = new PopupMenu.PopupSwitchMenuItem('Listening', false);
        this._listening.connect('toggled', (_item, on) => {
            if (!this._updating)
                ui.run(ui.voice.set({listening: on}), 'Voice');
        });
        this._controls.addMenuItem(this._listening);
        const [buttons, [stopButton]] = buttonRow([
            ['Stop speaking', () => ui.run(ui.voice.request('stop_speaking'), 'Voice')],
            ['New conversation', () => ui.run(ui.voice.request('new_conversation'), 'Voice')],
        ]);
        this._stopButton = stopButton;
        this._controls.addMenuItem(buttons);

        this._exchange = new PopupMenu.PopupMenuSection();
        this._exchange.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Last exchange'));
        this._you = textItem('agent-relay-message');
        this._claude = textItem('agent-relay-message');
        this._exchange.addMenuItem(this._you);
        this._exchange.addMenuItem(this._claude);
        this._exchange.addAction('Show the whole exchange…', () => ui.showExchange());
        this._controls.addMenuItem(this._exchange);

        this._choices = new PopupMenu.PopupMenuSection();
        this._controls.addMenuItem(this._choices);
        this._optionsShown = null;
        this._choiceItems = [];         // [key, id, item]
        menu.addMenuItem(this._controls);

        this.update();
    }

    update() {
        this._updating = true;
        try {
            this._update();
        } finally {
            this._updating = false;
        }
    }

    _update() {
        const {voice, unit} = this._ui;
        const state = voice.state;
        const [text, detail] = describe(voice, unit);
        this.item.label.text = `Voice: ${text}`;
        const mic = micFor(voice, unit);
        this.item.icon.icon_name = mic?.[0] ?? ICON_MIC_OFF;

        // On and off is the systemd unit. Without it (not installed, or no
        // systemd) the switch only shows whether a service answers.
        this._switch.setToggleState(unit.on || (!unit.installed && state !== null));
        this._switch.setSensitive(unit.installed);
        this._detail.label.text = detail;
        this._detail.visible = detail !== '';

        this._controls.actor.visible = state !== null;
        if (!state)
            return;
        this._listening.setToggleState(voice.listening ?? state.phase !== 'paused');
        this._listening.setSensitive(voice.listening !== null || state.phase !== 'starting');
        // Insensitive (and greyed out) while nothing is spoken.
        this._stopButton.reactive = state.phase === 'speaking';
        this._stopButton.can_focus = state.phase === 'speaking';

        const user = state.last_user ?? '';
        const assistant = state.last_assistant ?? '';
        this._exchange.actor.visible = Boolean(user || assistant);
        this._you.label.text = `You: “${oneLine(user, 100)}”`;
        this._claude.label.text = `Claude: “${oneLine(assistant, 140)}”`;

        this._fillChoices(voice.options);
        const settings = state.settings ?? {};
        for (const [key, id, item] of this._choiceItems) {
            item.setOrnament(settings[key] === id
                ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        }
    }

    // Rebuilt only when the options themselves change.
    _fillChoices(options) {
        const json = JSON.stringify(options ?? null);
        if (json === this._optionsShown)
            return;
        this._optionsShown = json;
        this._choices.removeAll();
        this._choiceItems = [];
        if (!options)
            return;
        for (const [list, key, title] of CHOICES) {
            const entries = Array.isArray(options[list]) ? options[list] : [];
            if (!entries.length)
                continue;
            this._choices.addMenuItem(new PopupMenu.PopupSeparatorMenuItem(title));
            for (const entry of entries) {
                const id = entry?.id;
                if (typeof id !== 'string')
                    continue;
                const item = this._choices.addAction(oneLine(entry.label ?? id, 60), () =>
                    this._ui.run(this._ui.voice.set({[key]: id}), title));
                this._choiceItems.push([key, id, item]);
            }
        }
    }
}

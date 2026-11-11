#!/bin/bash
# Runs the extension in a headless GNOME Shell of its own: a private session
# bus, its own data, config, cache and runtime folders under a temporary
# directory, a relay of its own with throwaway projects, and a fake voice
# service (fake-voice-service.py) for the "Voice" submenu. Nothing of the
# real session is touched, and nothing is installed or enabled there.
#
#   gnome-extension/test-in-headless-shell.sh [relay-venv]
#
# Prints what the checks found and leaves screenshots in $OUT (default
# /tmp/agent-relay-extension-test).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
VENV=${1:-$REPO/.venv}
OUT=${OUT:-/tmp/agent-relay-extension-test}
UUID=agent-relay@glashoff.github.io

rm -rf "$OUT"
mkdir -p "$OUT"/{home,data,config,cache,runtime,state,projects/web/.devcontainer,projects/alpha}
chmod 700 "$OUT/runtime"
echo '{}' > "$OUT/projects/web/.devcontainer/devcontainer.json"
cp -r "$HERE/$UUID" "$OUT/data/gnome-shell/extensions/" 2>/dev/null || {
    mkdir -p "$OUT/data/gnome-shell/extensions"
    cp -r "$HERE/$UUID" "$OUT/data/gnome-shell/extensions/"
}
# Unsafe mode, for Eval and screenshots in this test session only.
mkdir -p "$OUT/data/gnome-shell/extensions/test-helper@local"
cat > "$OUT/data/gnome-shell/extensions/test-helper@local/metadata.json" <<'EOF'
{"uuid": "test-helper@local", "name": "Test helper", "description": "Unsafe mode for tests", "shell-version": ["48"]}
EOF
cat > "$OUT/data/gnome-shell/extensions/test-helper@local/extension.js" <<'EOF'
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
export default class TestHelper extends Extension {
    enable() { global.context.unsafe_mode = true; }
    disable() {}
}
EOF
cat > "$OUT/config.toml" <<EOF
mcp_socket = "$OUT/runtime/agent-relay/mcp.sock"
events_socket = ""
projects_dir = "$OUT/projects"
state_dir = "$OUT/state"
allow_host_projects = true
claude_command = "sleep 600"
start_command = ["true"]
keep_awake_command = ""
EOF

cat > "$OUT/session.sh" <<'SESSION'
set -u
eval_js() {
    gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell \
        --method org.gnome.Shell.Eval "$1"
}
shot() {
    gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell/Screenshot \
        --method org.gnome.Shell.Screenshot.Screenshot false false "$OUT/$1.png" > /dev/null
}
mcp() {
    curl -s --unix-socket "$OUT/runtime/agent-relay/mcp.sock" -X POST http://localhost/mcp \
        -H 'content-type: application/json' \
        -H 'accept: application/json, text/event-stream' -d "$1" > /dev/null
}
gsettings set org.gnome.shell disable-user-extensions false
gsettings set org.gnome.shell enabled-extensions "['test-helper@local', '$UUID']"
gsettings set org.gnome.shell welcome-dialog-last-shown-version '99'
"$VENV/bin/agent-relay-mcp" --config "$OUT/config.toml" > "$OUT/relay.log" 2>&1 &
RELAY=$!
gnome-shell --headless --virtual-monitor 1600x1000 --wayland --no-x11 > "$OUT/shell.log" 2>&1 &
SHELL_PID=$!
for i in $(seq 1 60); do
    sleep 0.5
    eval_js 'global.context.unsafe_mode' 2>/dev/null | grep -q true && break
done
sleep 2
echo "extension state (1 = enabled):"
eval_js "const e = Main.extensionManager.lookup('$UUID'); [e.state, String(e.error ?? '')].join(' ')"
echo "panel button:"
eval_js "Boolean(Main.panel.statusArea['$UUID'])"

# A request to a locked project, through MCP like the chat would send it.
mcp '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"send_message","arguments":{"project":"web","message":"Please refactor the <b>parser</b> & add tests"}}}'
mcp '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"send_message","arguments":{"project":"alpha","message":"Update the README"}}}'
mkdir -p "$OUT/projects/shop/.devcontainer" && echo '{}' > "$OUT/projects/shop/.devcontainer/devcontainer.json"
mcp '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"send_message","arguments":{"project":"shop","message":"Run the deploy script","run_on":"host"}}}'
sleep 2
echo "notifications:"
eval_js "Main.messageTray.getSources().flatMap(s => s.notifications.map(n => n.title + ' | ' + n.body + ' | ' + n.actions.map(a => a.label).join('/'))).join(' || ')"
echo "panel count label:"
eval_js "Main.panel.statusArea['$UUID'].count.text"
eval_js "Main.panel.statusArea['$UUID'].menu.open()" > /dev/null
sleep 1
shot menu-requests
eval_js "Main.panel.statusArea['$UUID'].menu.close()" > /dev/null

# Approve web through the extension's own client (as the button does):
# the relay accepts it only because gnome-shell is the caller.
echo "approve from gnome-shell:"
eval_js "const ext = Main.extensionManager.lookup('$UUID').stateObj; const r = ext._client.state.requests.find(r => r.project === 'web'); ext._client.approve({request: r.id, minutes: 60, when_idle: true}).then(x => { globalThis.approved = JSON.stringify(x); }).catch(e => { globalThis.approved = 'ERROR ' + e.message; }); 'sent'"
sleep 2
eval_js "globalThis.approved"
echo "the same from outside gnome-shell (must be refused):"
"$VENV/bin/agent-relay-ctl" grant web < /dev/null 2>&1 | head -2 || true
gdbus call --session --dest io.github.glashoff.AgentRelay1 --object-path /io/github/glashoff/AgentRelay1 \
    --method io.github.glashoff.AgentRelay1.Grant '{"target": "web"}' 2>&1 | head -2 || true

echo "panel icon after approving:"
eval_js "Main.panel.statusArea['$UUID'].icon.icon_name"
eval_js "Main.panel.statusArea['$UUID'].menu.open()" > /dev/null
sleep 1
shot menu-approved
eval_js "Main.panel.statusArea['$UUID'].menu.close()" > /dev/null

# The dialog for a full-access request.
echo "open the dialog for the full-access request:"
eval_js "const ext = Main.extensionManager.lookup('$UUID').stateObj; const r = ext._client.state.requests.find(r => r.project === 'alpha'); try { ext._openDialog({request: r}); String(Boolean(ext._dialog)) + ' ' + ext._dialog?.state; } catch (e) { 'ERROR ' + e.message + ' ' + e.stack; }"
sleep 1
shot dialog-full-access
echo "confirm it (Allow with full access):"
eval_js "Main.extensionManager.lookup('$UUID').stateObj._dialog._confirm(); 'confirmed'" > /dev/null
sleep 2
"$VENV/bin/agent-relay-ctl" status | sed -n '/Grants/,/Waiting/p'
echo "notifications now (answered requests are gone, no 'started' by default):"
eval_js "Main.messageTray.getSources().flatMap(s => s.notifications.map(n => n.title)).join(' || ')"

echo "with 'Notify about activity too' on, a started request notifies:"
eval_js "const ext = Main.extensionManager.lookup('$UUID').stateObj; ext._ui().setNotifyActivity(true); ext._settings.notifyActivity"
mcp '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"send_message","arguments":{"project":"web","message":"one more"}}}'
sleep 3
eval_js "Main.messageTray.getSources().flatMap(s => s.notifications.map(n => n.title)).join(' || ')"
eval_js "Main.extensionManager.lookup('$UUID').stateObj._ui().setNotifyActivity(false); 'off'" > /dev/null
echo "grant dialog for web, on the host:"
eval_js "const ext = Main.extensionManager.lookup('$UUID').stateObj; ext._openDialog({target: 'web', kind: 'host'}); ext._dialog._okButton.label"
sleep 1
shot dialog-on-host
eval_js "Main.extensionManager.lookup('$UUID').stateObj._dialog?.close(); 'closed'" > /dev/null

# Quick settings
eval_js "Main.panel.statusArea.quickSettings.menu.open(); 'open'" > /dev/null
sleep 1
shot quick-settings
eval_js "Main.panel.statusArea.quickSettings.menu.close(); 'ok'" > /dev/null
eval_js "Main.panel.statusArea.quickSettings.menu.open(); 'ok'" > /dev/null
sleep 1
eval_js "Main.extensionManager.lookup('$UUID').stateObj._indicator.toggle.menu.open(); 'ok'" > /dev/null
sleep 1
shot quick-settings-menu
eval_js "Main.panel.statusArea.quickSettings.menu.close(); 'ok'" > /dev/null

# Voice: the submenu against the fake service (fake-voice-service.py). The
# private bus has no systemd, so the unit is "unavailable" and the on/off
# switch is insensitive; the rest works over the control socket.
VOICE="Main.extensionManager.lookup('$UUID').stateObj"
voice_text() {
    eval_js "const ext = $VOICE; const s = [...ext._voiceSections][0]; const b = Main.panel.statusArea['$UUID']; [s.item.label.text, s._detail.visible ? s._detail.label.text : '-', 'switch ' + s._switch.state + (s._switch.reactive ? '' : ' (insensitive)'), 'mic ' + (b.mic.visible ? b.mic.icon_name + ' ' + b.mic.style_class : 'hidden')].join(' | ')"
}
fake() { python3 "$FAKE" send "$1" > /dev/null; }
echo "voice before the service runs:"
voice_text
python3 "$FAKE" > "$OUT/fake-voice.log" 2>&1 &
FAKE_PID=$!
sleep 4
echo "voice after the service started (reconnects by itself):"
voice_text
voice_open() {
    # Opening the menu refreshes and rebuilds it; open the submenu after that.
    eval_js "Main.panel.statusArea['$UUID'].menu.open(); 'open'" > /dev/null
    sleep 1
    eval_js "[...$VOICE._voiceSections].find(s => s.item.get_stage())?.item.setSubmenuShown(true); 'open'" > /dev/null
    sleep 1
}
voice_open
shot voice-listening
eval_js "const v = [...$VOICE._voiceSections].find(s => s.item.get_stage()).item.menu.actor.vadjustment; v.value = v.upper; 'scrolled'" > /dev/null
sleep 0.5
shot voice-choices
echo "pause listening from the menu:"
eval_js "const s = [...$VOICE._voiceSections][0]; s._listening.toggle(); 'toggled'" > /dev/null
sleep 1
voice_text
eval_js "[...$VOICE._voiceSections][0]._listening.state"
fake '{"cmd": "set", "listening": true}'
fake '{"cmd": "fake_phase", "phase": "hearing"}'
sleep 1
echo "hearing:"
voice_text
fake '{"cmd": "fake_phase", "phase": "muted"}'
sleep 1
echo "muted:"
voice_text
fake '{"cmd": "fake_phase", "phase": "speaking"}'
fake '{"cmd": "fake_exchange", "user": "Schick an das Projekt web: refactor den Parser und schreib Tests dafür, bitte mit pytest und ohne neue Abhängigkeiten.", "assistant": "Ich habe den Auftrag an das Projekt web geschickt. Es ist gesperrt und wartet auf deine Freigabe am Laptop, Nummer drei. Sobald du ihn freigibst, läuft er los, und ich sage dir Bescheid, wenn er fertig ist."}'
sleep 1
echo "speaking, with an exchange (stop button reactive?):"
voice_text
eval_js "const s = [...$VOICE._voiceSections][0]; [s._stopButton.reactive, s._you.label.text, s._claude.label.text].join(' | ')"
voice_open
shot voice-speaking
echo "stop speaking, choose speech recognition 'small' and the model 'haiku':"
eval_js "const s = [...$VOICE._voiceSections][0]; s._stopButton.emit('clicked', 1); 'ok'" > /dev/null
eval_js "const s = [...$VOICE._voiceSections][0]; s._choiceItems.find(([k, id]) => id === 'fw-small')[2].activate(null); 'ok'" > /dev/null
sleep 0.5
voice_text
eval_js "const s = [...$VOICE._voiceSections][0]; s._choiceItems.filter(([, , i]) => i._ornament === 2).map(([k, id]) => k + '=' + id).join(' ')" 2>&1 | head -1
eval_js "$VOICE._voice.set({claude_model: 'haiku'}).then(() => 'set'); JSON.stringify($VOICE._voice.state?.settings)"
sleep 2.5
eval_js "JSON.stringify($VOICE._voice.state)"
echo "a refused setting (notification):"
eval_js "$VOICE._voiceUi().run($VOICE._voice.set({tts: 'nope'}), 'Voice'); 'sent'" > /dev/null
sleep 1
eval_js "Main.messageTray.getSources().flatMap(s => s.notifications.map(n => n.title + ' | ' + n.body)).filter(t => t.includes('Voice')).join(' || ')"
fake '{"cmd": "fake_exchange", "user": "Wie ist das Wetter morgen?", "assistant": "Morgen bewölkt, zwölf Grad."}'
sleep 0.5
echo "the whole exchange in a dialog:"
eval_js "$VOICE._showExchange(); String(Boolean($VOICE._dialog))"
sleep 1
shot voice-exchange
eval_js "$VOICE._dialog?.close(); 'closed'" > /dev/null
fake '{"cmd": "fake_phase", "phase": "error", "error": "No microphone found"}'
sleep 0.5
echo "error:"
voice_text
fake '{"cmd": "fake_phase", "phase": "starting", "loading": "speech recognition model faster-whisper large-v3-turbo"}'
sleep 0.5
echo "loading:"
voice_text
echo "the service stops:"
fake '{"cmd": "fake_quit"}'
sleep 1
voice_text
python3 "$FAKE" > "$OUT/fake-voice.log" 2>&1 &
FAKE_PID=$!
sleep 4
echo "and starts again:"
voice_text
eval_js "Main.panel.statusArea['$UUID'].menu.close()" > /dev/null

echo "disable and enable again (as on lock and unlock):"
eval_js "Main.extensionManager.disableExtension('$UUID')" > /dev/null
sleep 1
eval_js "Main.extensionManager.enableExtension('$UUID')" > /dev/null
sleep 2
eval_js "const e = Main.extensionManager.lookup('$UUID'); [e.state, String(e.error ?? '')].join(' ')"
sleep 4
echo "voice after enabling again:"
voice_text
echo "errors in the shell log:"
grep -iE "agent.relay|JS ERROR|TypeError|ReferenceError" "$OUT/shell.log" | grep -v "^$" | head -20 || true
kill $RELAY $SHELL_PID $FAKE_PID 2>/dev/null
wait 2>/dev/null
SESSION

env -i HOME="$OUT/home" PATH="$PATH" LANG=en_US.UTF-8 \
    XDG_DATA_HOME="$OUT/data" XDG_CONFIG_HOME="$OUT/config" XDG_CACHE_HOME="$OUT/cache" \
    XDG_RUNTIME_DIR="$OUT/runtime" OUT="$OUT" VENV="$VENV" UUID="$UUID" \
    FAKE="$HERE/fake-voice-service.py" \
    dbus-run-session -- bash "$OUT/session.sh" 2> "$OUT/dbus.log"
echo "screenshots: $(ls "$OUT"/*.png 2>/dev/null | tr '\n' ' ')"

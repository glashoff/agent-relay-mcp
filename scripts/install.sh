#!/bin/bash
# Installs or updates agent-relay-mcp on this computer from this checkout:
# the relay (pipx, editable, with the "desktop" extra), its systemd user
# service, and the GNOME extension. Run it again after every update of the
# checkout; it only changes what needs changing.
#
#   scripts/install.sh [--dry-run] [--yes] [--no-restart] [--no-service] [--no-extension]
#
# It never touches the configuration once it exists, the relay's state
# (grants in access.json, conversations, keep-awake settings), a systemd unit
# you have changed, or git: merging and pulling are up to you.
#
#   --dry-run       show what would be done, change nothing
#   --yes           do not ask before installing from a branch other than main
#   --no-restart    do not restart the relay (it keeps running the old code)
#   --no-service    leave the systemd unit and the service alone
#   --no-extension  leave the GNOME extension alone
set -euo pipefail

DRY=0 YES=0 RESTART=1 SERVICE=1 EXTENSION=1
for arg in "$@"; do
    case $arg in
        --dry-run) DRY=1 ;;
        --yes) YES=1 ;;
        --no-restart) RESTART=0 ;;
        --no-service) SERVICE=0 ;;
        --no-extension) EXTENSION=0 ;;
        -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

SRC=$(cd "$(dirname "$0")/.." && pwd)
UUID=agent-relay@glashoff.github.io
UNIT=agent-relay-mcp.service
BUS_NAME=io.github.glashoff.AgentRelay1
CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/agent-relay-mcp
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/agent-relay-mcp
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
EXT_DIR=${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID
STAMP=$STATE_DIR/installed-pyproject.sha256
STEP=""
CHANGED=()
NOTES=()

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
ok() { printf '    %s\n' "$*"; }
note() { NOTES+=("$*"); printf '    NOTE: %s\n' "$*"; }
fail() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
run() {
    if [ "$DRY" = 1 ]; then
        printf '    would run: %s\n' "$*"
    else
        "$@"
    fi
}
trap 'printf "\033[1;31mERROR: stopped during \"%s\". Nothing after that step was changed.\033[0m\n" "$STEP" >&2' ERR

# --- checks -------------------------------------------------------------------------

STEP="checks"
say "Checking"
[ "$(id -u)" != 0 ] || fail "Run this as your own user, not as root."
for tool in git pipx systemctl python3; do
    command -v "$tool" > /dev/null || fail "$tool is missing."
done
[ -f "$SRC/pyproject.toml" ] && [ -d "$SRC/src/agent_relay_mcp" ] \
    || fail "$SRC does not look like an agent-relay-mcp checkout."
BRANCH=$(git -C "$SRC" rev-parse --abbrev-ref HEAD)
COMMIT=$(git -C "$SRC" rev-parse --short HEAD)
ok "checkout: $SRC ($BRANCH at $COMMIT)"
if [ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]; then
    note "The checkout has uncommitted changes; they are installed as they are."
fi
if [ "$BRANCH" != main ] && [ "$YES" = 0 ] && [ "$DRY" = 0 ]; then
    printf '    This installs from branch %s, not main. Continue? [y/N] ' "$BRANCH"
    read -r answer < /dev/tty || answer=""
    [ "$answer" = y ] || [ "$answer" = Y ] || fail "Cancelled; nothing was changed."
fi
SESSION_BUS=1
[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || SESSION_BUS=0
SYSTEMD_USER=1
systemctl --user show-environment > /dev/null 2>&1 || SYSTEMD_USER=0
[ "$SYSTEMD_USER" = 1 ] || { [ "$SERVICE" = 0 ] && [ "$RESTART" = 0 ]; } \
    || fail "The systemd user manager is not reachable (run this in your desktop session, or use --no-service --no-restart)."

# --- the relay (pipx, editable) -----------------------------------------------------

STEP="installing the relay"
say "Relay"
VENVS=$(pipx environment --value PIPX_LOCAL_VENVS)
BIN_DIR=$(pipx environment --value PIPX_BIN_DIR)
VENV=$VENVS/agent-relay-mcp
WANT="file://$SRC"
HAVE=""
if [ -x "$VENV/bin/python" ]; then
    HAVE=$("$VENV/bin/python" - <<'EOF' 2>/dev/null || true
import json, importlib.metadata as m
d = m.distribution("agent-relay-mcp")
info = json.loads(d.read_text("direct_url.json") or "{}")
print(("editable " if info.get("dir_info", {}).get("editable") else "copy ") + info.get("url", "?"))
EOF
)
fi
HASH=$(sha256sum "$SRC/pyproject.toml" | cut -d' ' -f1)
up_to_date=0
if [ "$HAVE" = "editable $WANT" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$HASH" ] \
        && "$VENV/bin/python" -c 'import dbus_fast' 2> /dev/null \
        && [ -x "$BIN_DIR/agent-relay-ctl" ]; then
    up_to_date=1
fi
if [ "$up_to_date" = 1 ]; then
    ok "already installed, editable from this checkout; the code is live, dependencies unchanged"
else
    if [ -z "$HAVE" ]; then
        ok "not installed yet: installing"
    elif [ "$HAVE" != "editable $WANT" ]; then
        ok "installed as $HAVE: switching to editable from $SRC"
    else
        ok "dependencies or commands changed: reinstalling"
    fi
    if [ "$DRY" = 1 ]; then
        printf '    would run: pipx install --force --editable %s\n' "$SRC[desktop]"
    elif ! pipx install --force --editable "$SRC[desktop]" > /tmp/agent-relay-pipx.log 2>&1; then
        ok "pipx did not take the [desktop] extra; installing without it and adding dbus-fast"
        pipx install --force --editable "$SRC" >> /tmp/agent-relay-pipx.log 2>&1 \
            || fail "pipx install failed; see /tmp/agent-relay-pipx.log"
        pipx inject agent-relay-mcp dbus-fast >> /tmp/agent-relay-pipx.log 2>&1 \
            || fail "pipx inject dbus-fast failed; see /tmp/agent-relay-pipx.log"
    fi
    if [ "$DRY" = 0 ]; then
        "$VENV/bin/python" -c 'import dbus_fast, agent_relay_mcp.dbus_control' \
            || fail "the installed relay cannot load its D-Bus support"
        [ -x "$BIN_DIR/agent-relay-ctl" ] || fail "agent-relay-ctl was not installed"
        mkdir -p "$STATE_DIR"
        echo "$HASH" > "$STAMP"
    fi
    CHANGED+=("relay installed")
fi
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) note "$BIN_DIR is not on your PATH; agent-relay-ctl is there." ;;
esac

# --- configuration ---------------------------------------------------------------

STEP="checking the configuration"
say "Configuration"
CONFIG=$CONFIG_DIR/config.toml
if [ -f "$CONFIG" ]; then
    ok "keeping $CONFIG"
else
    run mkdir -p "$CONFIG_DIR"
    run cp "$SRC/config.example.toml" "$CONFIG"
    note "Created $CONFIG from the example: set public_hosts there."
    CHANGED+=("configuration created")
fi
if [ "$DRY" = 0 ] || [ -f "$CONFIG" ]; then
    PY=$VENV/bin/python
    [ -x "$PY" ] || PY=python3
    if ! PYTHONPATH="$SRC/src" "$PY" -c "
import sys
from pathlib import Path
from agent_relay_mcp import config
c = config.load(Path(sys.argv[1]))
if not c.control_dbus:
    print('control_dbus is off: nothing can be approved on this computer')
" "$CONFIG" > /tmp/agent-relay-config-check.txt 2>&1; then
        cat /tmp/agent-relay-config-check.txt >&2
        fail "$CONFIG has an error (above); the running relay was not restarted."
    fi
    [ -s /tmp/agent-relay-config-check.txt ] && note "$(cat /tmp/agent-relay-config-check.txt)"
    ok "valid for this version"
fi
if [ -d "$STATE_DIR" ]; then
    ok "keeping the relay's state in $STATE_DIR (grants, conversations)"
fi

# --- the systemd user service ---------------------------------------------------------

if [ "$SERVICE" = 1 ]; then
    STEP="setting up the service"
    say "Service"
    if [ -f "$UNIT_DIR/$UNIT" ]; then
        if cmp -s "$UNIT_DIR/$UNIT" "$SRC/contrib/$UNIT"; then
            ok "$UNIT is current"
        else
            note "$UNIT_DIR/$UNIT differs from contrib/$UNIT; kept as it is (compare them if you like)."
        fi
    else
        run mkdir -p "$UNIT_DIR"
        run cp "$SRC/contrib/$UNIT" "$UNIT_DIR/$UNIT"
        run systemctl --user daemon-reload
        run systemctl --user enable "$UNIT"
        CHANGED+=("service installed")
    fi
fi

if [ "$RESTART" = 1 ]; then
    STEP="restarting the relay"
    say "Restarting the relay"
    ok "runs in progress survive this; only requests being sent right now may fail"
    run systemctl --user restart "$UNIT"
    if [ "$DRY" = 0 ]; then
        for _ in $(seq 1 30); do
            sleep 1
            if systemctl --user is-active --quiet "$UNIT"; then
                if [ "$SESSION_BUS" = 0 ] || gdbus introspect --session --dest "$BUS_NAME" \
                        --object-path /io/github/glashoff/AgentRelay1 > /dev/null 2>&1; then
                    break
                fi
            fi
        done
        if ! systemctl --user is-active --quiet "$UNIT"; then
            journalctl --user -u "$UNIT" -n 25 --no-pager >&2 || true
            fail "the relay did not start (log above)."
        fi
        if [ "$SESSION_BUS" = 1 ] && ! gdbus introspect --session --dest "$BUS_NAME" \
                --object-path /io/github/glashoff/AgentRelay1 > /dev/null 2>&1; then
            journalctl --user -u "$UNIT" -n 25 --no-pager >&2 || true
            note "The relay runs, but its approval interface is not on the session bus: nothing can be approved (log above)."
        else
            ok "running, with its approval interface on the session bus"
        fi
        CHANGED+=("relay restarted")
    fi
else
    note "The relay was not restarted; it runs the code it started with until it is."
fi

# --- the GNOME extension --------------------------------------------------------------

if [ "$EXTENSION" = 1 ]; then
    STEP="installing the GNOME extension"
    say "GNOME extension"
    if ! command -v gnome-shell > /dev/null; then
        note "GNOME Shell is not installed here; skipping the extension."
    else
        VERSION=$(gnome-shell --version | grep -oE '[0-9]+' | head -1)
        SUPPORTED=$(python3 -c "import json,sys; print(' '.join(json.load(open(sys.argv[1]))['shell-version']))" \
            "$SRC/gnome-extension/$UUID/metadata.json")
        case " $SUPPORTED " in
            *" $VERSION "*) ok "GNOME Shell $VERSION" ;;
            *) note "GNOME Shell $VERSION is not among the versions the extension is made for ($SUPPORTED); GNOME may refuse to load it." ;;
        esac
        FILES=("$SRC/gnome-extension/$UUID"/*.js "$SRC/gnome-extension/$UUID/metadata.json" \
               "$SRC/gnome-extension/$UUID/stylesheet.css")
        same=1
        if [ -d "$EXT_DIR" ]; then
            for f in "${FILES[@]}"; do
                cmp -s "$f" "$EXT_DIR/$(basename "$f")" || same=0
            done
            for f in "$EXT_DIR"/*; do
                [ -e "$SRC/gnome-extension/$UUID/$(basename "$f")" ] || same=0
            done
        else
            same=0
        fi
        EXT_UPDATED=0
        if [ "$same" = 1 ]; then
            ok "already current in $EXT_DIR"
        else
            # A copy, not a link: GNOME Shell must not run a folder Claude Code works in.
            if [ "$DRY" = 1 ]; then
                printf '    would copy the extension to %s\n' "$EXT_DIR"
            else
                tmp=$(mktemp -d "$(dirname "$EXT_DIR")/.$UUID.XXXX" 2> /dev/null || true)
                if [ -z "$tmp" ]; then
                    mkdir -p "$(dirname "$EXT_DIR")"
                    tmp=$(mktemp -d "$(dirname "$EXT_DIR")/.$UUID.XXXX")
                fi
                cp "${FILES[@]}" "$tmp"/
                rm -rf "$EXT_DIR.old"
                [ -d "$EXT_DIR" ] && mv "$EXT_DIR" "$EXT_DIR.old"
                mv "$tmp" "$EXT_DIR"
                rm -rf "$EXT_DIR.old"
                ok "copied to $EXT_DIR"
            fi
            EXT_UPDATED=1
            CHANGED+=("extension copied")
        fi
        # Enabled? Listed in enabled-extensions, the extension loads at login.
        ENABLED=$(gsettings get org.gnome.shell enabled-extensions 2> /dev/null || echo "")
        if [ "$(gsettings get org.gnome.shell disable-user-extensions 2> /dev/null)" = true ]; then
            note "User extensions are switched off in GNOME (Extensions app); the extension cannot run until they are on."
        fi
        case "$ENABLED" in
            *"'$UUID'"*) ok "enabled" ;;
            *)
                if [ "$DRY" = 1 ]; then
                    printf '    would add %s to the enabled extensions\n' "$UUID"
                else
                    python3 - "$UUID" <<'EOF'
import ast, subprocess, sys
uuid = sys.argv[1]
raw = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"],
                     capture_output=True, text=True, check=True).stdout.strip()
raw = raw.removeprefix("@as ")
current = ast.literal_eval(raw) if raw else []
if uuid not in current:
    current.append(uuid)
    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions",
                    str(current)], check=True)
EOF
                fi
                ok "enabled (it starts with your next login)"
                EXT_UPDATED=1
                CHANGED+=("extension enabled")
                ;;
        esac
        if [ "$EXT_UPDATED" = 1 ]; then
            note "Log out and back in to load the extension: GNOME Shell on Wayland only loads extension code at login."
        fi
    fi
fi

# --- summary ----------------------------------------------------------------------

STEP="summary"
trap - ERR
say "Done"
if [ "${#CHANGED[@]}" = 0 ]; then
    ok "everything was already up to date"
else
    for c in "${CHANGED[@]}"; do ok "$c"; done
fi
if [ "$DRY" = 1 ]; then
    ok "(dry run: nothing was changed)"
fi
if [ "${#NOTES[@]}" -gt 0 ]; then
    echo
    echo "To do:"
    for n in "${NOTES[@]}"; do echo "  - $n"; done
fi
echo
echo "Approve a project:  agent-relay-ctl grant <project> --for 2h   (state: agent-relay-ctl)"
echo "In an emergency:    agent-relay-ctl revoke --all --stop   or   systemctl --user stop $UNIT"
echo "Go back to an older version:  scripts/rollback.sh <commit>"

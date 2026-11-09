#!/bin/bash
# Emergency way back: runs the relay of an older commit, without touching
# your checkout. The relay is installed from that commit as a fixed copy
# (pipx, not editable) and restarted. If that commit has no GNOME extension,
# the extension is switched off (its files stay). Configuration and state
# (access.json, conversations) stay as they are.
#
#   scripts/rollback.sh <commit> [--dry-run] [--yes]
#
# For example, to the version before access control:  scripts/rollback.sh 4f38a09
# Back to the current version afterwards:              scripts/install.sh
#
# Quicker in many cases:  agent-relay-ctl revoke --all --stop   (lock everything)
#                         systemctl --user stop agent-relay-mcp  (stop the relay)
set -euo pipefail

COMMIT="" DRY=0 YES=0
for arg in "$@"; do
    case $arg in
        --dry-run) DRY=1 ;;
        --yes) YES=1 ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
        *) COMMIT=$arg ;;
    esac
done

SRC=$(cd "$(dirname "$0")/.." && pwd)
UUID=agent-relay@glashoff.github.io
UNIT=agent-relay-mcp.service
STAMP=${XDG_STATE_HOME:-$HOME/.local/state}/agent-relay-mcp/installed-pyproject.sha256
STEP=""

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
ok() { printf '    %s\n' "$*"; }
fail() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
run() {
    if [ "$DRY" = 1 ]; then printf '    would run: %s\n' "$*"; else "$@"; fi
}
trap 'printf "\033[1;31mERROR: stopped during \"%s\".\033[0m\n" "$STEP" >&2' ERR

STEP="checks"
[ -n "$COMMIT" ] || {
    echo "Which commit? Recent ones:" >&2
    git -C "$SRC" log --oneline -8 main >&2 || true
    fail "usage: scripts/rollback.sh <commit>"
}
[ "$(id -u)" != 0 ] || fail "Run this as your own user, not as root."
SHA=$(git -C "$SRC" rev-parse --verify --quiet "$COMMIT^{commit}") \
    || fail "$COMMIT is not a commit in $SRC."
SUBJECT=$(git -C "$SRC" log -1 --format=%s "$SHA")
say "Rolling the relay back to ${SHA:0:7}: $SUBJECT"
if [ "$YES" = 0 ] && [ "$DRY" = 0 ]; then
    printf '    The running relay is replaced by this version and restarted. Continue? [y/N] '
    read -r answer < /dev/tty || answer=""
    [ "$answer" = y ] || [ "$answer" = Y ] || fail "Cancelled; nothing was changed."
fi

STEP="installing the old version"
say "Relay"
run pipx install --force "git+file://$SRC@$SHA"
if git -C "$SRC" show "$SHA:src/agent_relay_mcp/dbus_control.py" > /dev/null 2>&1; then
    run pipx inject agent-relay-mcp dbus-fast
fi
run rm -f "$STAMP"           # so that scripts/install.sh reinstalls next time
ok "installed as a fixed copy of ${SHA:0:7}"

STEP="the GNOME extension"
say "GNOME extension"
if git -C "$SRC" show "$SHA:gnome-extension/$UUID/metadata.json" > /dev/null 2>&1; then
    ok "this version has the extension; left as it is"
elif command -v gsettings > /dev/null; then
    if [ "$DRY" = 1 ]; then
        printf '    would switch off %s\n' "$UUID"
    else
        gnome-extensions disable "$UUID" 2> /dev/null || true
        python3 - "$UUID" <<'EOF'
import ast, subprocess, sys
uuid = sys.argv[1]
raw = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"],
                     capture_output=True, text=True, check=True).stdout.strip()
current = ast.literal_eval(raw.removeprefix("@as ")) if raw else []
if uuid in current:
    current.remove(uuid)
    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions",
                    str(current)], check=True)
EOF
    fi
    ok "switched off (its files stay; scripts/install.sh switches it on again)"
fi

STEP="restarting the relay"
say "Restarting the relay"
run systemctl --user restart "$UNIT"
if [ "$DRY" = 0 ]; then
    sleep 3
    if ! systemctl --user is-active --quiet "$UNIT"; then
        journalctl --user -u "$UNIT" -n 25 --no-pager >&2 || true
        fail "the relay did not start (log above)."
    fi
    ok "running ${SHA:0:7}"
fi

trap - ERR
say "Done"
echo "The relay runs ${SHA:0:7} now, independent of your checkout."
echo "Back to the version of your checkout:  $SRC/scripts/install.sh"

#!/bin/sh
# Installs the systemd user unit agent-relay-voice.service (does not enable
# or start it) and links the launcher into ~/.local/bin.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
launcher="$here/agent-relay-voice"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
sed "s|@LAUNCHER@|$launcher|g" "$here/agent-relay-voice.service" > "$unit_dir/agent-relay-voice.service"
systemctl --user daemon-reload
echo "installed $unit_dir/agent-relay-voice.service"
if [ -d "$HOME/.local/bin" ]; then
    ln -sf "$launcher" "$HOME/.local/bin/agent-relay-voice"
    echo "linked $HOME/.local/bin/agent-relay-voice"
fi
echo "start it with: systemctl --user start agent-relay-voice"

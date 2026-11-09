#!/bin/sh
# Copies the extension into ~/.local/share/gnome-shell/extensions (a copy,
# not a link: a folder Claude Code works in must not be what GNOME Shell
# runs). Then log out and in once (Wayland), and enable it:
#   gnome-extensions enable agent-relay@glashoff.github.io
set -eu
UUID=agent-relay@glashoff.github.io
SRC=$(cd "$(dirname "$0")" && pwd)/$UUID
DEST=${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID
rm -rf "$DEST"
mkdir -p "$DEST"
cp "$SRC"/*.js "$SRC"/metadata.json "$SRC"/stylesheet.css "$DEST"/
echo "Installed to $DEST. Log out and in, then: gnome-extensions enable $UUID"

#!/bin/sh
# Adds a menu entry that runs Slice straight from this folder, without root.
# Changing disks still asks for your password. For the proper install, use the deb package.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
mkdir -p "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
cp "$ROOT/data/io.github.webhookstudios.Slice.svg" "$DATA/icons/hicolor/scalable/apps/"
sed -e "s|^Exec=.*|Exec=env PYTHONPATH=$ROOT python3 -m slicepm %F|" \
    "$ROOT/data/io.github.webhookstudios.Slice.desktop" > "$DATA/applications/io.github.webhookstudios.Slice.desktop"
update-desktop-database "$DATA/applications" 2>/dev/null || true
echo "Slice is in your apps menu. Or run: PYTHONPATH=$ROOT python3 -m slicepm"

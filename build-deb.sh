#!/bin/sh
# Builds dist/slice_<version>_all.deb. Needs only dpkg-deb.
#
#   /usr/lib/slice/             the program (Python)
#   /usr/lib/slice/slice-helper the part that runs as root through pkexec
#   /usr/bin/slice              the app
#   /usr/share/polkit-1/actions the permission prompt for the helper
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.webhookstudios.Slice
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/slicepm/__init__.py")"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

LIB="$STAGE/usr/lib/slice"
install -d "$LIB/slicepm/ui" "$STAGE/DEBIAN" "$STAGE/usr/bin" \
    "$STAGE/usr/share/applications" "$STAGE/usr/share/polkit-1/actions" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/doc/slice"

install -m 644 "$ROOT"/slicepm/*.py "$LIB/slicepm/"
install -m 644 "$ROOT"/slicepm/ui/*.py "$LIB/slicepm/ui/"
install -m 755 "$ROOT/data/slice-helper" "$LIB/slice-helper"
install -m 755 "$ROOT/data/slice" "$STAGE/usr/bin/slice"
install -m 644 "$ROOT/data/$APP_ID.desktop" "$STAGE/usr/share/applications/"
install -m 644 "$ROOT/data/io.github.webhookstudios.slice.policy" "$STAGE/usr/share/polkit-1/actions/"
install -m 644 "$ROOT/data/$APP_ID.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"

cat > "$STAGE/usr/share/doc/slice/copyright" <<EOT
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Slice
Source: https://github.com/webhookstudios/slice

Files: *
Copyright: $(date +%Y) webhookstudios
License: GPL-2+
 On Debian systems, the full text of the GNU General Public License
 version 2 can be found in /usr/share/common-licenses/GPL-2.
EOT
chmod 644 "$STAGE/usr/share/doc/slice/copyright"

cat > "$STAGE/DEBIAN/control" <<EOT
Package: slice
Version: $VERSION
Architecture: all
Maintainer: webhookstudios <354619278+webhookstudios@users.noreply.github.com>
Installed-Size: $(du -sk "$STAGE/usr" | cut -f1)
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), polkitd | policykit-1, util-linux, fdisk, parted, gdisk, e2fsprogs, dosfstools
Recommends: ntfs-3g, exfatprogs, udisks2, smartmontools
Suggests: btrfs-progs, xfsprogs
Section: admin
Priority: optional
Homepage: https://github.com/webhookstudios/slice
Description: Partition manager with a visual disk map
 Slice shows each disk as a coloured map and lets you create, delete,
 format, resize, move, label, copy and check partitions, clone or wipe
 whole disks, convert between GPT and MBR, and read disk health. Changes
 are queued and shown to you first, and nothing is written until you
 press Apply.
EOT

find "$STAGE" -type d -exec chmod 755 {} +
mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$ROOT/dist/slice_${VERSION}_all.deb"

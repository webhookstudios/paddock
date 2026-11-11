#!/bin/sh
# Builds dist/paddock_<version>_all.deb. Needs only dpkg-deb.
#
#   /usr/lib/paddock/             the program (Python)
#   /usr/lib/paddock/paddock-helper the part that runs as root through pkexec
#   /usr/bin/paddock            the app (Ubuntu's own 'slice' package owns /usr/bin/slice)
#   /usr/share/polkit-1/actions the permission prompt for the helper
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.webhookstudios.Paddock
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/paddock/__init__.py")"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

LIB="$STAGE/usr/lib/paddock"
install -d "$LIB/paddock/ui" "$STAGE/DEBIAN" "$STAGE/usr/bin" \
    "$STAGE/usr/share/applications" "$STAGE/usr/share/polkit-1/actions" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/doc/paddock"

install -m 644 "$ROOT"/paddock/*.py "$LIB/paddock/"
cp -r "$ROOT/paddock/sedge" "$LIB/paddock/"
find "$LIB/paddock/sedge" -type f -exec chmod 644 {} +
install -m 644 "$ROOT"/paddock/ui/*.py "$LIB/paddock/ui/"
install -m 755 "$ROOT/data/paddock-helper" "$LIB/paddock-helper"
install -m 755 "$ROOT/data/paddock" "$STAGE/usr/bin/paddock"
install -m 644 "$ROOT/data/$APP_ID.desktop" "$STAGE/usr/share/applications/"
install -m 644 "$ROOT/data/io.github.webhookstudios.paddock.policy" "$STAGE/usr/share/polkit-1/actions/"
install -m 644 "$ROOT/data/$APP_ID.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"

cat > "$STAGE/usr/share/doc/paddock/copyright" <<EOT
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Paddock (formerly Slice)
Source: https://github.com/webhookstudios/paddock

Files: *
Copyright: $(date +%Y) webhookstudios
License: GPL-2+
 On Debian systems, the full text of the GNU General Public License
 version 2 can be found in /usr/share/common-licenses/GPL-2.

Files: */sedge/fonts/*
Copyright: 2023 The Young Serif Project Authors; 2015 The Public Sans Project Authors
License: OFL-1.1
 The full license texts are in the sedge/fonts/ folder.
EOT
chmod 644 "$STAGE/usr/share/doc/paddock/copyright"

# Replaces/Breaks take over from this app's old package name, slice 1.x.
# Ubuntu's unrelated 'slice' package (2.x) is never touched.
cat > "$STAGE/DEBIAN/control" <<EOT
Package: paddock
Version: $VERSION
Architecture: all
Maintainer: webhookstudios <354619278+webhookstudios@users.noreply.github.com>
Installed-Size: $(du -sk "$STAGE/usr" | cut -f1)
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), polkitd | policykit-1, util-linux, fdisk, parted, gdisk, e2fsprogs, dosfstools
Replaces: slice (<< 2~)
Breaks: slice (<< 2~)
Recommends: ntfs-3g, exfatprogs, udisks2, smartmontools
Suggests: btrfs-progs, xfsprogs
Section: admin
Priority: optional
Homepage: https://github.com/webhookstudios/paddock
Description: Partition manager with a visual disk map
 Paddock (formerly Slice) shows each disk as a coloured map and lets you create, delete,
 format, resize, move, label, copy and check partitions, clone or wipe
 whole disks, convert between GPT and MBR, and read disk health. Changes
 are queued and shown to you first, and nothing is written until you
 press Apply.
EOT

find "$STAGE" -type d -exec chmod 755 {} +
mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$ROOT/dist/paddock_${VERSION}_all.deb"

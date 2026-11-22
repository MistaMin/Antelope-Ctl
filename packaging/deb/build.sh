#!/bin/bash
# Build antelope-ctl_<version>_all.deb from this checkout into OUTDIR (default: next to this script).
# Uses dpkg-deb when installed; otherwise assembles the ar archive itself (needs ar, GNU tar, xz, md5sum).
# Usage: packaging/deb/build.sh [OUTDIR]
set -e
src=$(cd "$(dirname "$0")/../.." && pwd)
out=$(cd "${1:-$(dirname "$0")}" && pwd)
version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$src/pyproject.toml")
deb=$out/antelope-ctl_${version}_all.deb
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
root=$work/root
umask 022

"$src/packaging/install-tree.sh" "$root"
doc=$root/usr/share/doc/antelope-ctl
mkdir -p "$doc"
{
  echo "antelope-ctl: $(sed -n 's/^Homepage = "\(.*\)"/\1/p' "$src/pyproject.toml")"
  echo
  echo "Code, CLI and WebUI: GPL-3.0-or-later, see /usr/share/common-licenses/GPL-3."
  echo "Device profiles (/usr/share/antelope-ctl/profiles): MIT, full text below."
  echo
  cat "$src/profiles/LICENSE"
} >"$doc/copyright"

mkdir -p "$root/DEBIAN"
cat >"$root/DEBIAN/control" <<EOF
Package: antelope-ctl
Version: $version
Architecture: all
Maintainer: MistaMin <MistaMin@users.noreply.github.com>
Installed-Size: $(du -sk "$root/usr" | cut -f1)
Depends: python3 (>= 3.10)
Recommends: python3-fastapi, python3-uvicorn (>= 0.22), xdg-utils
Section: sound
Priority: optional
Homepage: https://github.com/MistaMin/Antelope-Ctl
Description: CLI and local WebUI for Antelope Audio USB interfaces
 Profile-driven control of Antelope Audio interfaces over USB HID: a
 standard-library Python CLI and a local browser UI (FastAPI) for mixer,
 routing and effects, with device profiles for every supported interface,
 the udev rule for desktop access and systemd user units for the WebUI.
EOF
(cd "$root" && find usr -type f | LC_ALL=C sort | xargs md5sum) >"$root/DEBIAN/md5sums"
chmod 644 "$root/DEBIAN/control" "$root/DEBIAN/md5sums"

if command -v dpkg-deb >/dev/null; then
  dpkg-deb --root-owner-group -Zxz --build "$root" "$deb"
else
  owner=(--owner=0 --group=0 --numeric-owner --sort=name --format=gnu)
  echo 2.0 >"$work/debian-binary"
  tar "${owner[@]}" -C "$root/DEBIAN" -cJf "$work/control.tar.xz" ./control ./md5sums
  tar "${owner[@]}" -C "$root" --exclude=./DEBIAN -cJf "$work/data.tar.xz" .
  rm -f "$deb"
  (cd "$work" && ar rcD "$deb" debian-binary control.tar.xz data.tar.xz)
fi
ls -l "$deb"

#!/bin/sh
# Install antelope-ctl into DESTDIR/usr as the Arch, deb and rpm packages ship it (the AppImage reuses it):
# the source tree unchanged in /usr/share/antelope-ctl, so its relative profile/static paths still hold,
# plus the CLI wrapper, the WebUI launcher, the udev rule, the systemd user units, the desktop entry and icon.
# Licence files are left to each package format. Usage: packaging/install-tree.sh DESTDIR
set -e
[ -n "$1" ] || { echo "usage: $0 DESTDIR" >&2; exit 2; }
dest=$1; app=/usr/share/antelope-ctl
umask 022
cd "$(dirname "$0")/.."
# Tracked and new files only (no __pycache__, venvs or generated previews); plain find in a release tarball.
if [ -e .git ]; then
  git ls-files -co --exclude-standard -- LICENSE antelope profiles 'webui/*.py' webui/static webui/assets
else
  find LICENSE antelope profiles webui/static webui/assets -type f ! -path '*/__pycache__/*'
  ls webui/*.py
fi | while read -r f; do install -Dm644 "$f" "$dest$app/$f"; done

mkdir -p "$dest/usr/bin" "$dest/usr/lib/systemd/user"
install -m755 packaging/antelope-webui "$dest/usr/bin/antelope-webui"
# The tree sits in ../share/antelope-ctl next to this wrapper (also inside the AppImage).
cat >"$dest/usr/bin/antelope-ctl" <<'EOF'
#!/bin/sh
exec env PYTHONPATH="$(dirname "$0")/../share/antelope-ctl" python3 -P -m antelope.cli "$@"
EOF
chmod 755 "$dest/usr/bin/antelope-ctl"
install -Dm644 packaging/70-antelope.rules "$dest/usr/lib/udev/rules.d/70-antelope.rules"
for unit in antelope-webui.service antelope-webui@.service; do
  sed -e '/install-user.sh/d' -e "s|@REPO@/webui/.venv/bin/python|/usr/bin/python3|" -e "s|@REPO@|$app|g" \
    "packaging/$unit" >"$dest/usr/lib/systemd/user/$unit"
  chmod 644 "$dest/usr/lib/systemd/user/$unit"
done
mkdir -p "$dest/usr/share/applications"
sed 's|^Exec=antelope-ctl$|Exec=antelope-webui|' packaging/flatpak/io.github.mistamin.AntelopeCtl.desktop \
  >"$dest/usr/share/applications/io.github.mistamin.AntelopeCtl.desktop"
chmod 644 "$dest/usr/share/applications/io.github.mistamin.AntelopeCtl.desktop"
install -Dm644 webui/assets/mon-knob.svg "$dest/usr/share/icons/hicolor/scalable/apps/io.github.mistamin.AntelopeCtl.svg"

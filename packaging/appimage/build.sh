#!/bin/bash
# Build antelope-ctl-<version>-x86_64.AppImage into OUTDIR (default: next to this script).
# The AppImage carries its own Python: FastAPI needs pydantic-core, a compiled wheel built for one
# Python version, so host-Python AppImages would break whenever the host Python differs.
# Usage: packaging/appimage/build.sh PYTHON_TARBALL [OUTDIR]
set -e
here=$(cd "$(dirname "$0")" && pwd)
src=$(cd "$here/../.." && pwd)
py=$1
out=$(cd "${2:-$here}" && pwd)
if [ ! -f "$py" ] || ! command -v appimagetool >/dev/null; then
  cat >&2 <<'EOF'
usage: packaging/appimage/build.sh PYTHON_TARBALL [OUTDIR]
This script downloads nothing by itself except FastAPI/Uvicorn from PyPI (it says so first). It needs:
  PYTHON_TARBALL  a relocatable CPython (3.10 or newer), the file named
                  cpython-<version>+<date>-x86_64-unknown-linux-gnu-install_only.tar.gz from
                  https://github.com/astral-sh/python-build-standalone/releases
  appimagetool    on PATH, from https://github.com/AppImage/appimagetool/releases
EOF
  exit 2
fi
version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$src/pyproject.toml")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
appdir=$work/AntelopeCtl.AppDir

"$src/packaging/install-tree.sh" "$appdir"
rm -r "$appdir/usr/lib"  # the udev rule and systemd units belong to the system packages
tar -xzf "$py" -C "$appdir/usr" --strip-components=1  # python/bin/python3 -> usr/bin/python3
echo "Installing webui/requirements.txt (FastAPI, Uvicorn and dependencies) from PyPI into the bundled Python"
PIP_USER=0 "$appdir/usr/bin/python3" -m pip install --no-compile --no-warn-script-location \
  -r "$src/webui/requirements.txt"
install -m755 "$here/AppRun" "$appdir/AppRun"
cp "$appdir/usr/share/applications/io.github.mistamin.AntelopeCtl.desktop" \
  "$appdir/usr/share/icons/hicolor/scalable/apps/io.github.mistamin.AntelopeCtl.svg" "$appdir/"
ARCH=x86_64 appimagetool "$appdir" "$out/antelope-ctl-$version-x86_64.AppImage"

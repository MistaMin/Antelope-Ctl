#!/bin/bash
# Build the Flatpak and write antelope-ctl.flatpak next to this script.
# Needs flatpak and flatpak-builder; installs the freedesktop 25.08 runtime and SDK for the current user.
set -e
cd "$(dirname "$0")"
id=io.github.mistamin.AntelopeCtl
work=${FLATPAK_WORK:-${XDG_CACHE_HOME:-$HOME/.cache}/antelope-ctl-flatpak}
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak-builder --user --force-clean --install-deps-from=flathub --state-dir="$work/state" \
  --repo="$work/repo" "$work/build" $id.yml
flatpak build-bundle --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
  "$work/repo" antelope-ctl.flatpak $id
ls -lh antelope-ctl.flatpak

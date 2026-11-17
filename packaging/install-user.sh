#!/bin/bash
# Install the WebUI user service and app launcher for the current user.
# The service is started on demand by the launcher; to start it at login:
#   systemctl --user enable antelope-webui
set -e
repo=$(cd "$(dirname "$0")/.." && pwd)
unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
app_dir=${XDG_DATA_HOME:-$HOME/.local/share}/applications
mkdir -p "$unit_dir" "$app_dir"
sed "s|@REPO@|$repo|g" "$repo/packaging/antelope-webui.service" >"$unit_dir/antelope-webui.service"
sed "s|@REPO@|$repo|g" "$repo/packaging/antelope-webui.desktop" >"$app_dir/antelope-webui.desktop"
sed "s|@REPO@|$repo|g" "$repo/packaging/antelope-webui@.service" >"$unit_dir/antelope-webui@.service"
# Second device (Discrete 4) on its own port; edit or delete if you do not have one.
sed "s|@REPO@|$repo|g" "$repo/packaging/antelope-webui-discrete4.desktop" >"$app_dir/antelope-webui-discrete4.desktop"
conf=${XDG_CONFIG_HOME:-$HOME/.config}/antelope-ctl; mkdir -p "$conf"
[ -e "$conf/discrete4.env" ] || printf 'ANTELOPE_PROFILE=discrete_4_a270.json\nANTELOPE_PORT=8715\n' >"$conf/discrete4.env"
systemctl --user daemon-reload
echo "Installed. Launch 'Antelope Control' from the app menu."

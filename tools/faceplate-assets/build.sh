#!/bin/bash
# Render the sprites the layouts use with GoodLookin DRI, then pack them as WebP (PNG would be ~23 MB).
set -euo pipefail
cd "$(dirname "$0")"
out=../../webui/static/faceplate
rm -rf "${out:?}/knobs" "${out:?}/switches" "${out:?}/plates"
cargo run --release -- "$out/layouts" "$out"
for png in "$out"/knobs/*.png "$out"/switches/*.png "$out"/plates/*.png; do
  magick "$png" -quality 82 -define webp:alpha-quality=90 "${png%.png}.webp" && rm "$png"
done
sed -i 's/\.png"/.webp"/g' "$out/manifest.json"
du -sh "$out"

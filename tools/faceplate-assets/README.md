# faceplate-assets

Pre-renders the knobs, switches and plates of GoodLookin DRI
(MIT, Rust) as PNG sprite sheets for the web UI, which cannot run the toolkit itself.

```sh
cd tools/faceplate-assets
cargo run --release -- ../../webui/static/faceplate/layouts ../../webui/static/faceplate
cargo run --release -- --all <layouts_dir> <scratch_dir>   # every style, about 41 MB, about 2 minutes
```

It reads every `*.json` layout, renders only the styles they use, and writes:

- `knobs/<slug>.png`: 64 frames of 128x128 side by side, transparent. Frame 0 is the minimum
  (pointer at -135 degrees), frame 63 the maximum (+135). The knob is 80 px across in its frame
  (`knob` in the manifest) and centred on its base; the rest is room for its shadow. Lit tier,
  or the classic drawing for the 12 styles with no lit model (`lit: false`).
- `switches/<slug>.png`: 2 frames, off/released then on/pressed-and-lit, transparent, drawn at 2x.
- `plates/<slug>.png`: 760x300 opaque tile; `marks` says whether print on it should be light or dark.
- `manifest.json`: file, frame count and frame size of everything rendered.
- `styles.json`: every style name the toolkit has, whatever was rendered.

Style names are the toolkit's own: knob and plate `code` values from its `src/design.rs`
(`Chk`, `Olive`), the 15 lit-only knob presets (`satin`), and the switch names in
`src/switches.rs` (`Bat toggle`). An unknown name in a layout stops the run with exit code 1.

## Where the toolkit is

`Cargo.toml` points at `../../../../Dependencies/GoodLookinDri`, that is `~/Dependencies/GoodLookinDri`
when this repo is at `~/dev/Antelope-Ctl`. If yours is elsewhere, edit the two `path =` lines.

## Limits

- The printed scale around lit knobs is left out: its ink colour depends on the plate.
- The toolkit's rasteriser is opaque, so each frame is painted on black and on white and alpha is
  solved from the difference. Additive glows come out as ordinary translucent light.
- Knob caps use the toolkit's neutral grey; colours are constants at the top of `src/main.rs`.

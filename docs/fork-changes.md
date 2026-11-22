# What this fork adds

Fork of [diafebus/Antelope-Ctl](https://github.com/diafebus/Antelope-Ctl), branch
`MARCOS/antelope-audio-linux-driver`. Upstream targets the Orion Studio Synergy Core; this fork adds the
**Discrete 4 Synergy Core (23e5:a270)** and **Zen Go Synergy Core (23e5:a015)**. Everything below was
worked out clean-room from USB captures of the owner's own devices and checked on the hardware.
Detailed status per feature: [linux-parity-plan.md](linux-parity-plan.md).

## Working now

| Area | Discrete 4 | Zen Go |
|------|-----------|--------|
| Profile, state report, bounded readbacks | yes | yes |
| Preamp gain | yes (4 preamps) | upstream profile |
| Input mode Mic / Line / Hi-Z, phase, preamp link | yes | upstream profile |
| Monitor and headphone 1 level, mute, dim | yes | upstream profile |
| Mixers: fader, pan, mute, solo, strip link, strip source | yes (4 mixes x 16 strips) | upstream profile |
| AFX racks: show, load, remove effects | yes | yes (63 of 72 types loadable) |
| Effect settings (knobs) | 45 effects + ClearQ | the same 45 + ClearQ + 22 Zen-Go-only effects |
| Effect switches and selectors | ported from the Zen Go maps, Discrete 4 live trial pending | 85 on 38 effects (shown as plain controls) |
| Effect bypass | per instance, ported from the Zen Go (frame seen on the Discrete 4); live trial pending | yes, read back from the device (first instance) |
| Guitar amp | load / remove only; knob and switch fields ported from the Zen Go, still not editable (no settings read captured), live trial pending | model, output level, all knobs and switches |
| Effect faceplates in the WebUI | 47 layouts, own artwork | same layouts; the 22 Zen-Go-only effects show plain controls |

- **Faceplates** are drawn with the owner's GoodLookin DRI knob/switch/plate renderer
  (`tools/faceplate-assets/`). Only colour schemes follow the familiar look of each plugin; no vendor
  artwork is used.
- **One server per device**: `ANTELOPE_PROFILE` and `ANTELOPE_PORT` select device and port
  (Zen Go 8714, Discrete 4 8715). `packaging/install-user.sh` installs both launchers and the
  `antelope-webui@.service` template.
- **Safety**: writes are refused (HTTP 400) outside the profile's bounds; readback indices are bounded
  because an out-of-range index can crash the device's controller; the monitor bus mute is protected.
- **Tests**: `python tools/offline_checks.py` runs everything that needs no hardware.

## Implemented from the vendor panel run, live-tested

Zen Go, from the vendor panel run of 2026-11-21 (every non-effect control of the panel). The frames
and readbacks are rebuilt in the offline tests from the captured bytes. On 2026-11-21 every write path
below was sent once through this project's server on the owner's unit, read back raw (changed bytes were
exactly the expected ones, mixer maps 6-9 untouched), and restored; a raw snapshot before and after was
identical. Dim on a muted output was refused. The JS page changes were not exercised in a browser.

| Area | Zen Go | Where |
|------|--------|-------|
| Output status as a bit field; dim can no longer release a mute | fix | all three outputs: dim is refused while muted (WebUI, CLI) |
| Mixer strip number on the wire (was one strip off) | fix | profile `channel_base` |
| Gazelle Reverb: 8 knobs, FX on/off | yes | reverb panel |
| Reverb send per strip (mix 1) | yes | mixer send knob |
| Reverb send pan, reverb returns (level, mute) | API only | `POST /api/mix` `send_pan`, `POST /api/reverb-return` |
| Monitor out trim, brightness | yes | Settings panel |
| Panning law, oscillator | API only | `POST /api/pan-law`, `POST /api/oscillator` |
| Routing: Record 1-8, S/PDIF out, AFX rack inputs 1-4, loopback and AFX OUT sources | yes | Routing tab |
| AFX racks: reorder by drag | yes | rack list |

## Not done yet

- Discrete 4: headphones 2-4, line outs and trims, digital outs, output routing, clock source, sample
  rate, 48V, mic modelling, AuraVerb, oscillator.
- Zen Go: mic modelling (models were greyed in the vendor panel: no frame captured), clock source and
  sample rate writes on the owner's unit (greyed too), page controls for panning law, oscillator,
  reverb returns and send pan, sources of the Monitor / HP outputs (no selector in the vendor panel).
- Discrete 4: switches, menus, stacked rings and effect bypass are ported from the Zen Go maps, live trial pending. Both: cabinet (no control found).

## Install

```
sudo cp packaging/70-antelope.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger
python -m venv webui/.venv && webui/.venv/bin/pip install -r webui/requirements.txt
packaging/install-user.sh
```

A Flatpak manifest lives in `packaging/flatpak/` (`build.sh` writes `antelope-ctl.flatpak`). It is
**not yet built or tested**; no binary is published so far. The udev rule above is needed either way.

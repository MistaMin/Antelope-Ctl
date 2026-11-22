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
| Effect switches and selectors | ported from the Zen Go maps, live-tested (shown as plain controls) | 85 on 38 effects (shown as plain controls) |
| Effect bypass | yes, per instance, read back from the device (two instances tested) | yes, read back from the device (first instance) |
| Guitar amp | model, output level, all knobs and switches (settings read works through this project although the vendor panel never reads it) | model, output level, all knobs and switches |
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
| Reverb send pan, reverb returns (level, mute) | yes | mixer SEND PAN knob, reverb panel returns |
| Monitor out trim, brightness | yes | Settings panel |
| Panning law, oscillator | yes | Settings panel |
| Routing: Record 1-8, S/PDIF out, AFX rack inputs 1-4, loopback and AFX OUT sources | yes | Routing tab |
| AFX racks: reorder by drag | yes | rack list |

## Discrete 4: implemented from the vendor panel run, live-tested

Discrete 4, from the vendor panel run of 2026-11-22 (every non-effect control of the panel). The frames
and readbacks are rebuilt in the offline tests from the captured bytes. On 2026-11-22 each write path
below was sent once through this project's server on the owner's unit, read back raw and restored; a raw
snapshot before and after was identical. The JS page changes were not exercised in a browser.

| Area | Discrete 4 | Where |
|------|-----------|-------|
| Phase state read back (status byte 44 + channel, bit `0x40`) | fix | preamp strip shows the device's value |
| Hi-Z only on preamps 1-2 (3-4 are Mic / Line) | fix | refused on 3-4 (WebUI, API, CLI) |
| Preamp 3+4 link, link state read back (cat `0x0b` idx 0) | yes | preamp strip |
| Gain range Mic -12..65, Line -6..20 (swept); Hi-Z stays 0..20 | fix | preamp knob |
| Headphones 2-4 level, mute, dim | yes | output strips |
| Dim keeps the mute on this device: dim on a muted Monitor allowed | fix | output strips, CLI |
| Line Out volume and mute (bus 5) | yes | Settings panel |
| Line Out 1-4 trims and their link | yes | Settings panel |
| Monitor out trim, brightness | yes | Settings panel |
| Panning law, oscillator | yes | Settings panel |
| Gazelle Reverb: 8 knobs, FX on/off | yes | reverb panel |
| Reverb send per strip (MONITOR/HP1 mix) | yes | mixer send knob |
| Reverb send pan, reverb returns 1-4 (level, mute) | yes | mixer SEND PAN knob, reverb panel returns |
| Routing: Line Out 1-4, ADAT Out 1-8, S/PDIF Out 1-2, Record 1-4 = AFX rack inputs, full source list | yes | Routing tab |
| Mixer strip links: mix 0 strips 3+4 and 15+16, mix 1 strips 7+8; link state read back | yes | mixer |
| AFX racks: reorder by drag | yes | rack list |

## Not done yet

- Discrete 4 (not captured in the 2026-11-22 run): 48V, mic modelling (the selector did not move), clock
  source and sample rate writes (greyed), the reverb returns of headphones 2-4, the Hi-Z gain range,
  mixer links other than the captured pairs.
- Zen Go: mic modelling (models were greyed in the vendor panel: no frame captured), clock source and
  sample rate writes on the owner's unit (greyed too), sources of the Monitor / HP outputs (no selector in the vendor panel).
- Both: cabinet (no control found). The page controls for panning law, oscillator, reverb returns,
  send pan (and the Discrete 4 Line Out 1-4 trims) exist but have not been clicked in a browser on the devices.

## Install

```
sudo cp packaging/70-antelope.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger
python -m venv webui/.venv && webui/.venv/bin/pip install -r webui/requirements.txt
packaging/install-user.sh
```

A Flatpak manifest lives in `packaging/flatpak/` (`build.sh` writes `antelope-ctl.flatpak`). It is
**not yet built or tested**; no binary is published so far. The udev rule above is needed either way.

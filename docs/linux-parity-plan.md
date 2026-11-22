# Linux parity plan: Discrete 4 (0xa270) and Zen Go Synergy Core (0xa015)

This fork extends `antelope-ctl` from the Orion to two more devices the owner
has: the original **Discrete 4** and the **Zen Go Synergy Core**. The goal is
Linux control matching **everything the vendor mixer panel does**. AFX
(device-side effects) joined the scope on 2026-11-16 by owner decision, in the
stages set out in §2. The work is clean-room and the result stays GPL-3.0 and shareable.

Read [AGENTS.md](../AGENTS.md), [SCOPE.md](../SCOPE.md) and the
[Discrete playbook](discrete-remote-agent-playbook.md) before touching a
device. This document adds the fork's scope, hosts, team layout and checklist.
It does not relax any rule in those files.

## 1. Devices

| device | USB id | profile | starting point |
|---|---|---|---|
| Discrete 4 (original, not Synergy Core) | `23e5:a270` | [`discrete_4_a270.json`](../profiles/discrete_4_a270.json) | Started as identity and USB interface classes only. Now maps what the owner's vendor-panel captures show (see §7); everything inferred is marked unconfirmed and is not writable. |
| Zen Go Synergy Core | `23e5:a015` | [`zen_go_sc.json`](../profiles/zen_go_sc.json) | Upstream first-pass profile: preamps, buses, routing, 2 mixers, sample rate and clock are mapped. Open items in [PROTOCOL.md §14](../PROTOCOL.md) and the profile's `open_questions`. Needs live round-trip testing on the owner's unit. **Owner's unit reports bcdDevice `05.04`; the profile was captured on `06.06`**, so re-check offsets (or update firmware first) before trusting any mapping. |

`discrete_4_sc.json` (`0xa2be`) is a **different product**. Do not merge it
with the `0xa270` profile and do not copy its offsets.

## 2. Scope

**In scope** (both devices, where the hardware has the feature):

- Audio streaming on Linux through `snd-usb-audio`. A kernel quirk is added
  only if testing proves one is needed.
- Preamps: gain, input mode (mic/line/instrument), 48 V phantom, phase, link.
- Digital inputs (ADAT / S/PDIF where present): gain and link.
- Output buses: level, mute, dim, mono, output trim and the headphone outputs.
- Routing matrix.
- Virtual mixers: fader, pan, mute, solo, channel link and mixer sends.
- Bundled reverb (Gazelle Reverb, protocol name AuraVerb): config, sends,
  returns.
- Settings: sample rate, clock source, pan law, oscillator, talkback,
  brightness, DC-coupling, if present.
- Meters.
- Device-side mic emulation (emuMic). It is in upstream scope, but its model
  catalogue depends on the owner's mics, so it comes last.
- Host-side conveniences such as named snapshots/presets, implemented in
  this project's WebUI, not copied from the vendor.
- AFX real-time effects (owner decision, 2026-11-16), in this order:
  1. **Read-only insert display**: decode each device's own strip-order
     readback and show the loaded inserts in the mixer. No writes.
  2. **Bypass, load/remove/reorder and parameters**, one effect at a time,
     each only after an isolated capture from the vendor panel on that
     device. Orion type IDs, instance ranges and encodings are candidates
     only, never evidence for `0xa270` or `0xa015`.
  Effect names shown in the UI come from the shared catalog plus the owner's
  own confirmation of what a slot holds. Which effects the owner's account
  includes is recorded privately from owner confirmation, never read from
  account or licensing traffic, and never committed.

  Public-manual facts that shape the insert display (facts only, not wire
  evidence):
  - **Zen Go** (Zen Go Synergy Core User Manual, pp. 46-48 and 51-52): two
    virtual mixers that "share their audio inputs and applied Synergy Core
    FX"; "Mixer channels 1 to 4 let you apply Synergy Core FX to the input
    signal", each with an AFX area and a per-channel 'BP' button; the rack
    has add, drag-reorder, per-effect 'BP', 'BP ALL' and 'DEL ALL'. The
    manual does not state the number of slots per rack.
  - **Discrete 4** (Discrete 4 user manual, §6.2.3 and §6.3): the AFX Rack
    inserts effects "into a mixer channel, forming a Channel Strip"; the
    mixer has 16 channels with a 'BP' button each; "Discrete 4 Premium – 4
    channel strips with 4 FX instances each" (Basic: 2 and 2).

**Out of scope:**

- AFX2DAW, Native and Cosmos plugins.
- Firmware updates. Keep the vendor tool for those.
- Licensing, activation, account and entitlement traffic, under SCOPE.md
  bucket F. This includes the panel's HTTP login calls seen in its logs.

## 3. Clean-room rules for this fork

These are SCOPE.md §2 applied to what exists on the owner's machines:

- **Allowed evidence:**
  - USB captures from the owner's devices.
  - Behaviour observed in the running vendor panel: what a control does, its
    range, and which captured frames it produces.
  - Public manuals, as facts only.
  - Names and categories printed in the vendor's runtime logs, as **hints**
    for planning captures, never as evidence.
- **Forbidden:**
  - Opening, unpacking, disassembling or decompiling any vendor binary,
    including the PyInstaller-packed manager server, the panel apps and the
    macOS kext.
  - Copying tables or constants out of vendor bundles. For example, the
    kext's `Info.plist` latency arrays must not be used.
  - Capturing or replaying licensing traffic.
  - Copying vendor UI text or images. The WebUI keeps its original design.
- **Keep out of Git:** serials, raw captures, logs and account data. Raw
  captures stay in the ignored `captures/`. Commit only redacted summaries
  and SHA-256 hashes.

### Who reads what

Agents on this work take one of four roles. One agent may hold several
roles, but must say which role it is acting in for each change.

| role | reads | writes |
|---|---|---|
| **Operator** | vendor panel UI, raw captures | action logs, private captures |
| **Analyst** | raw captures, action logs | `PROTOCOL.md`, profile entries (`unconfirmed`/`observed`), offline fixtures |
| **Implementer** | `PROTOCOL.md`, profiles, fixtures, this repo's code | `antelope/`, `webui/`, `tools/` tests |
| **Verifier** | profiles, this repo's CLI | bounded write → verify → restore results; flips `confirmed` |

Implementers work only from the spec. They do not need raw captures or the
vendor panel. A Verifier only uses this project's own client.

## 4. Hosts

| host | role | state on 2026-11-16 |
|---|---|---|
| Owner's Mac | vendor software runs natively. Optional native capture, see [CAPTURING.md](../CAPTURING.md#capturing-on-native-macos-instead-of-the-vm). | Discrete 4 attached; vendor driver, manager server and panels installed. SIP on. |
| `omarchy` (Arch-based Omarchy, kernel 7.2, KVM available, 8 cores / 15 GB) | **primary rig**: Linux host + usbmon + Windows VM running the vendor Launcher, as in CAPTURING.md | No Antelope device attached. Missing: `usbutils`, `wireshark-cli`, `qemu`/`libvirt`, a Windows VM. |

### Owner actions needed (agents cannot do these)

1. Move the Discrete 4 and the Zen Go to `omarchy`.
2. Install the capture and VM tools. On Arch:
   `sudo pacman -S usbutils wireshark-cli qemu-full libvirt virt-manager dnsmasq`
   Then add the user to the `wireshark` group and enable `libvirtd`.
3. Create a Windows VM, install the vendor Launcher and panels in it, and
   accept the vendor terms yourself. Contributors are responsible for terms
   they accepted (SCOPE.md §5).
4. Decide how controls in the VM panel are clicked: the owner clicks while
   the agent captures, or the agent drives the VM display through VNC
   screenshots plus input. The first is simpler and was used upstream.

## 5. Running several agents

The hardware is the bottleneck, not analysis:

- **Only one client drives a device's HID interface at a time.** That is
  either the VM Launcher or this project's daemon, never both, and never two
  agents. Each device gets one *device-holder* agent at a time. With two
  devices, two device-holders can work in parallel.
- Everything else runs in parallel without hardware: analysts decoding
  finished captures, implementers building from the spec, and test/fixture
  work.
- A coordinator owns the checklist in §7. It assigns one control per
  capture, and tells each agent its role and the files it owns. Use
  separate branches or worktrees per agent, and merge through the
  coordinator.

Suggested first wave:

| agent | device | role | task |
|---|---|---|---|
| A | Discrete 4 | Operator + Analyst | Phases 1–2 below; then capture the vendor connect walk. |
| B | Zen Go | Verifier | Audio check; then round-trip every already-enabled Zen Go control, with restore. |
| C | — | Analyst | Turn A's connect-walk capture into `frame.readback` and safe `category_counts` for `0xa270`. |
| D | — | Implementer | `udev` rule and `systemd --user` unit for the WebUI daemon; offline tests. |

## 6. Phases

**Phase 0: rig.** Do the owner actions in §4. Then confirm the device is
visible with `lsusb -d 23e5:` on `omarchy`.

**Phase 1: audio.**
- Run `lsusb -v -d 23e5:<pid>`. Record the UAC2 clock source, channel
  clusters, alt settings and endpoints in the profile (`usb_interfaces`).
- Run `aplay -l` / `arecord -l` and check PipeWire.
- Test playback and record at every rate the descriptors declare, plus the
  device's clock-source switching.
- Only if something fails: write up the failure, then consider an ALSA
  quirk table entry as a separate upstream kernel patch.

**Phase 2: transport.**
- Run `tools/hid_probe.py` and a passive `tools/readback_probe.py`, as in
  the playbook.
- Establish: report size, report-ID framing on hidraw, endpoint addresses,
  passive report magics and rates.

**Phase 3: capture campaign.** Go through §7 one control at a time:
1. Capture an isolated action in the VM Launcher with usbmon.
2. Analyse it.
3. Add the profile entry as `unconfirmed`.
4. Add an offline fixture/test.

The vendor connect walk is captured first. It is the only safe source of
readback bounds.

**Phase 4: implement and verify.**
- Implementers wire each mapped control into the CLI and WebUI.
- Verifiers run bounded write → verify → restore, then flip the entry to
  `confirmed`.

**Phase 5: polish.** Snapshots/presets (host-side), packaging, README for
end users, and an upstream PR per device once a feature group is confirmed.

## 7. Parity checklist

Every row needs code, a GUI control and verification evidence before it
counts as done. Work out which rows apply to each device from that device's
own vendor panel and public manual; mark the rest "n/a". `0xa270` hints come
from its log-named connect walk (`intake_hints` in the profile).

| feature | Discrete 4 `a270` | Zen Go `a015` |
|---|---|---|
| Audio stream, all rates (snd-usb-audio) | 16×16 S24_3LE 32k–192k enumerates, no quirk; record + play OK at 48k; other rates todo | same 16×16 layout enumerates; playback OK. **Capture alone fails** (I/O error): usbmon shows UAC2 SET_CUR rate + SET_INTERFACE succeed but the device returns 0-byte iso IN packets. **Capture works while playback runs** (2 s/96k/16ch exact). Next: test snd-usb-audio `quirk_flags` (needs root), else document "open playback too" |
| Transport (report size, framing, endpoints) | readback requests work through hidraw with the generic 320-byte path (CLI `readback 0x19 0`, 2026-11-17); command magic `0x70`, opcode at offset 4, payload from 16 observed | upstream confirmed; re-check on owner unit |
| Preamp gain / mode / 48V / phase | **gain, mode and phase mapped from vendor captures (2026-11-17)**: gain `0x50` (state bytes 40-43, written and read back by this project on preamp 1), input mode `0x4f` Mic / Line / Hi-Z (each mode keeps its own gain), phase `0x52`. **2026-11-22 vendor panel run, implemented from the captures, live-tested 2026-11-22 (each write path sent once through this project, raw read-back, restored):** status byte 44 + channel = mode bits 0-1 + phase bit `0x40` (phase now shown from the device); all four preamps exercised; Hi-Z on preamps 1-2 only (3-4 are Mic / Line; Hi-Z refused there); gain range Mic -12..65, Line -6..20 (swept on preamp 3), Hi-Z held to 0-20 (not captured). 48V: not captured (not clicked), not mapped, stays off | all verified on owner fw 05.04 with readback and exact restore (no mics connected): gain, 48V and phase on both preamps; mode Mic/Line on both, HiZ on ch 0 only (ch 1 HiZ not captured). Device keeps a separate gain per mode (ch 0: Mic 20 / Line 15 / HiZ 12 dB) |
| Preamp link | preamp 1+2 link mapped (`a2 00 00 <on>`, vendor capture). **2026-11-22 vendor panel run, implemented from the captures, live-tested 2026-11-22 (each write path sent once through this project, raw read-back, restored):** preamps 3+4 (`a2 00 01 <on>`), readback cat `0x0b` idx 0 byte pair (the WebUI follows it); no gain copy seen (gains were equal) | frame and readback captured (2026-11-21 vendor panel run): `a2 00 00 <on>`, cat `0x0b` idx 0 byte 0. Side effect: link ON makes the panel copy the CH 1 gain to CH 2 (`50 01 <gain>`), link OFF does not put it back. Recorded in the profile (`channels.link_pairs`); not offered as a control |
| Digital input gain / link | todo | n/a: the vendor panel has no digital input gain |
| Output buses: level / mute / dim / mono | Monitor and Headphone 1 mapped from vendor captures: level `0x47` and mute `0x48` on bus 0 / 1, dim `0x66` on id 4 (Monitor) / 0 (Headphone 1), state bytes 28-31. **2026-11-22 vendor panel run, implemented from the captures, live-tested 2026-11-22 (each write path sent once through this project, raw read-back, restored):** level 0-96 swept on every bus; **dim keeps the mute bit on this device** (`01` -> `03` -> `01` on all five buses), so dim on the muted Monitor is now allowed (`flags_combine` true); the Monitor mute stays protected (self-tests skip it, only an explicit mute write releases it). Line Out volume and mute = bus 5 (`47 05` / `48 05`, bytes 38 / 39, no dim), in the Settings panel. No mono control on the panel | level, mute and dim on Monitor / HP1 / HP2 (dim verified on all 3 buses, mute set verified, selftest fw 05.04). **Corrected 2026-11-21 vendor panel run**: the status byte is a bit field (bit0 mute, bit1 dim, `03` = both; on Monitor bits 3-5 repeat the trim index), not an exclusive enum. Device rule: a dim write, on or off, clears the mute bit. All three buses are protected: the WebUI and the CLI refuse dim (and the mono candidate) while a bus reads muted or unread (offline test; live check todo). No mono control exists in the panel |
| Output trim | **2026-11-22 vendor panel run, implemented from the captures, live-tested 2026-11-22 (each write path sent once through this project, raw read-back, restored):** Monitor Out Trim `cb 42 00 01 <n>` (opcode `0x54`, state byte 25 = 4n) in the Settings panel. Line Out 1-4 trims + their one link `cb 42 01 <link> t1 00 t2 00 t3 00 t4` (whole frame each time, cat `0x0e` idx 0), API only (`POST /api/line-trim`) | Monitor Out Trim mapped (2026-11-21 vendor panel run): opcode `0x54`, `cb 42 00 01 <n>`, n 0-6 = 20-14 dBu, state byte 25 = n*4. In the Settings panel; implemented from the capture, live verify todo |
| Headphone outputs | Headphone 1 as above. **2026-11-22 vendor panel run, implemented from the captures, live-tested 2026-11-22 (each write path sent once through this project, raw read-back, restored):** Headphones 2-4 confirmed as guessed (bus ids 2-4, dim ids 1-3, state bytes 32-37): shown and writable like Headphone 1 | HP1 and HP2 are buses 1 and 2 (row above). HP2 level re-seen in the 2026-11-21 vendor panel run (`47 02 <v>`, state byte 32); its mute and dim were not clicked in that run |
| Routing matrix | mixer strip source map: one map of 16 strips mirrored in groups 6, 10, 11, 12, 13 (opcode `0x53`, five frames per change, read back from cat `0x03`); strips 1-4 locked there. **2026-11-22 vendor panel run, implemented from the captures, live-tested 2026-11-22 (each write path sent once through this project, raw read-back, restored):** Routing tab with Line Out 1-4 (group 0), ADAT Out 1-8 (7), S/PDIF Out 1-2 (8), Record 1-4 = AFX rack inputs (9) and the mixer map; each change writes only its group. Full source list: PREAMP 1-4, COMPUTER PLAY 1-16, ADAT IN 1-8, S/PDIF IN 1-2, AFX OUT (display only), LOOPBACK HP 1/MON and HP 2 (offered only for group 9), MUTE, OSCILLATOR 1-2, EMU MIC 1-4 (`destination_sources`). Groups 1-5 (probably the fixed Monitor / HP sources) have no panel control | four destinations in the Routing tab: mixer inputs (groups 6-9, strips 5-16; strips 1-4 locked there), **Record 1-8** (group 3), **S/PDIF Out** (group 4), **AFX rack inputs 1-4** (group 5), each read back from cat `0x03`. Banks identified: `03` AFX OUT, `04` / `05` LOOPBACK HP 1/MON / HP 2 (Record and S/PDIF Out only). Each change writes only its own group: the panel's rewrite of groups 6-9 after a Record change is not replayed. Group 5 write -> verify -> restore passed earlier (fw 05.04); groups 3 and 4 implemented from the 2026-11-21 vendor panel run and live-tested (one slot each, raw read-back, restored). No selector exists for Monitor / HP1 / HP2 sources |
| Virtual mixers: fader / pan / mute / solo | mapped from vendor captures: `d4 04 <mix 0-3> <strip 1-16> <fader> <pan+flags>` (opcode `0x16`), fader / pan / mute / solo on mix 0 strips 1, 2, 5, 8 and mute on strip 1 of mixes 1-3; cat `0x04` readback layout (strip N at bytes 2N, 2N+1) matches the values the panel's own frames carried. WebUI mixer enabled for 4 mixes x 16 strips. Write -> read round trip by this project todo | mapped, 2 mixes × 16 (the panel's two pages; the device also answers mixers 3 and 4, unused). **Corrected 2026-11-21 vendor panel run**: the strip byte is the strip number 1-16 (the profile sent the 0-based index, i.e. one strip to the left); fixed with `channel_base`, live round trip todo |
| Mixer channel link | strips 1+2 of mix 0 mapped (`a2 04 00 <on>` then `a2 03 00 <on>`). **2026-11-22 vendor panel run, implemented from the captures, live-tested 2026-11-22 (each write path sent once through this project, raw read-back, restored):** mix 0 strips 3+4 (`a2 04 01` then `a2 03 01`) and 15+16 (`a2 03 07`), mix 1 strips 7+8 (`a2 03 13`: wire pair = mix*16 + pair); readback cat `0x0b` idx 3 byte mix*16 + pair (the WebUI follows it). Other pairs and mixes 2-3 not captured and not writable | mapped; strips 1+2 and 3+4 also carry the effect-rack link (`a2 04 <pair> <on>` first, readback cat `0x0b` idx 4), seen in the 2026-11-21 vendor panel run and not sent by this project |
| Reverb: config / sends / returns | **2026-11-22 vendor panel run, implemented from the captures, live-tested 2026-11-22 (each write path sent once through this project, raw read-back, restored):** the Zen Go frames byte for byte: Gazelle Reverb panel (8 knobs + FX on/off, `da 0b ...`, cat `0x0a`), send per strip on the MONITOR/HP1 mix (`e9 04 00 <strip> <send> <pan>`, cat `0x04` / `0x18`). Send pan and the Monitor/HP1 return (`68 00 <level or 0x80 mute>`, cat `0x17`) API only; the HP2-4 returns were not captured and are not writable | **implemented from the 2026-11-21 vendor panel run and live-tested the same day (write, raw read-back, restore).** Gazelle Reverb panel: 8 knobs + FX on/off, whole frame `da 0b ...` (opcode `0x1d`), read from cat `0x0a`. Send per strip of mix 1 in the mixer (`e9 04 00 <strip> <send> <pan>`, read from cat `0x04` / `0x18`). Send pan and the two returns (`68 <bus> <level or 0x80 mute>`, cat `0x17`) are API only, no page control. Factory presets are not stored on the device |
| Pan law | **2026-11-22 vendor panel run, implemented from the captures, live-tested 2026-11-22 (each write path sent once through this project, raw read-back, restored):** `24 <n>`, same order as the Zen Go; cat `0x12`. API only (`POST /api/pan-law`) | mapped (2026-11-21 vendor panel run): `24 <n>`, 0 = 0 dB, 1 = -6, 2 = -3, 3 = -4.5; read from cat `0x12`. API only (`POST /api/pan-law`), live verify todo |
| Sample rate / clock source | **not captured**: both dropdowns greyed in the 2026-11-22 vendor panel run (USB / 48 kHz), nothing sent; state bytes 18 = `02`, 19 = `05` throughout | read verified on owner unit; **writes not captured on the owner's unit**: both dropdowns were greyed in the 2026-11-21 vendor panel run (USB / 48 kHz) and sent nothing. The profile's write frames come from the 2026-08 captures of another unit and are untested here (they drop audio) |
| Oscillator / talkback / brightness / DC-coupling | **2026-11-22 vendor panel run, implemented from the captures, live-tested 2026-11-22 (each write path sent once through this project, raw read-back, restored):** brightness `0e <0-100>` (state byte 26, Settings panel slider; reads one off the written value and read raw `ff` before the run, shown as read), oscillator `0a <bits>` as on the Zen Go (state byte 20), API only. No talkback or DC-coupling on the panel | brightness mapped (`0e <0-100>`, state byte 26; Settings panel slider). Oscillator mapped (`0a <bits>`: two frequencies, level, two mutes; state byte 20), API only (`POST /api/oscillator`). Both from the 2026-11-21 vendor panel run, live-tested (write, state byte, restore). The panel has no talkback and no DC-coupling |
| Meters | todo | `0x83` magic known; calibration open |
| Mic emulation (emuMic) | **not captured**: the selector in the channel window did not move and sent nothing (2026-11-22 vendor panel run); cat `0x16` read `00 00 00 32` per preamp. EMU MIC 1-4 exist as routing sources | **not captured**: all models were greyed in the vendor panel on the owner's unit (2026-11-21 vendor panel run); state cat `0x16` read `00 00 00 32` per channel. No write frame is known |
| AFX inserts: read-only display | readback done, display blocked on the mixer: three captures (2026-11-17) give cat `0x19` = 36 records of {type, inst} pairs; record 0 = mixer channel 1; types 27 / 1 = Master De-esser / ClearQ, loaded by name in the vendor panel and read back. This project's CLI reads records 0 and 35 from the device and refuses 36. The WebUI has no Discrete 4 mixer to draw them on yet | **done, read-only**: vendor panel 1.1.26 connect walk gives cat `0x19` = 12 records; record 0 = mixer channel 1 and types 1 / 36 / 27 = ClearQ / SMT-100A / Master De-esser confirmed against the vendor panel; WebUI mixer shows racks on channels 1-4 from live readback (fw 05.04). Records 1-3 = channels 2-4 still derived; records 4-11 unknown |
| AFX inserts: load / remove | **done** on all four racks: whole-rack chain write (opcode `0x5b`) from vendor captures on strips 0-1, extended to strips 2-3 by write / readback / restore trials. Loadable: 45 effects, two instances each, plus ClearQ and the guitar amp (instance 0), whose load is followed by the vendor's captured default-settings frames (`load_defaults`: five band frames / one 16-byte block); these two loads are implemented from captures and **not yet run on the device**. The cabinet is not loadable: its load is impulse-response data computed by the vendor software. Any identified effect can be removed. WebUI "Effect racks" section | **done** on all four racks: chain write (opcode `0x23`, the Orion's assign frame) from a vendor capture on strip 1, extended to strips 0, 2, 3 by trials that left the owner's channel 1 rack unchanged. Loadable: Master De-esser, PowerGate, PowerEx (the profile's `writes.effects` has since grown to every effect whose vendor load was captured). ClearQ and the guitar amp are added with the vendor's captured default-settings frames after the chain write (`load_defaults`); not yet run on the device. The cabinet is not loadable. WebUI "Effect racks" section |
| AFX inserts: effect parameters | **45 effects done** (2026-11-17): parameter frame `d5 | len | type | inst | block`, state read cat `0x07` index = type (record length differs per effect: `read_stride`). 253 knob controls mapped by wheeling each control to both ends in the vendor panel; every effect then passed a load / read / change / restore / remove trial on two instances through this project's server. Raw units, no physical units claimed. Not mapped: switches, menus and stacked rings the wheel did not reach (their bytes are written back unchanged). **ClearQ** (2026-11-17, from captures, live round trip todo): one write frame per band (`<band> freq u16, Q u16 /100, gain i16, shape u8`), read under index 0 as 5 x 7 bytes + `01` per instance; six swept controls editable (band 1 frequency, band 2 Q and gain, band 3 frequency, Q and gain), the other band controls need a sweep capture. **Guitar amp**: model byte (0-10) and output level (-48..12) decoded, knobs seen for one notch only; not editable because no read of it has been captured (the 2026-11-22 vendor panel run shows the panel never sends one: no cat `0x07` request on reopen). **Cabinet**: not controllable (every change re-sends impulse-response data). **Ported from the Zen Go maps, live-tested on the Discrete 4 (2026-11-22: 47 effects, 395 controls, change / restore; guitar amp editable, its settings read works through this project)**: for the 30 effects both devices share, the switches, selectors, frequency rings, gains, trims and ClearQ band fields mapped on the Zen Go (103 fields, same type ids and blocks) are in the Discrete 4 profile; the guitar amp's knobs and switches too, still not editable here | **45 effects done** (2026-11-17): the effects it shares with the Discrete 4 use the same type ids and parameter blocks, so the same control maps apply. Each was loaded into an empty rack, read (tagged index `0x80000000 | type`, one instance per reply), had one control changed and restored, and was removed, through this project's server; three effects in the owner's racks were read only. First instance only. **22 Zen-Go-only effects done** (2026-11-20; Opto 2A, Vari-Speed Tremolo, Space Flanger, Marble White AutoWah, Adaptive Vibrato, Memory Cat, V12 Chorus, BBD Chorus, VEQ-432C, MG4+, Audio Baton (volume only), Filtek MK3, VEQ56, Instinct, Gyratec XIV, Comp-4K-Strip, Comp-4K-Bus, NEU-473A, Diode609, Brainiac, Drum All Nite, Gate Keeper): 170 knob controls from wheel sweeps in the vendor panel, each moved by one step and restored through this project's server on the owner's unit. Instinct's block is 26 float32 values (field type `f32`): 21 mapped, five that no swept control moved are written back unchanged. **Switches done** (2026-11-21): a click probe over 65 effects in the vendor panel found each switch and selector byte; 85 of them (38 effects), plus Gyratec XIV's Hi Mid Q and Audio Baton's nine band thumbwheels, were added to this profile (shared effects: on the Zen Go only, the Discrete 4 has no capture of them) and all 383 controls of the 40 changed effects passed the one-step change / restore trial. Guitar amp: settings read captured (flag byte + 16-byte block), so model, output and Bright are editable here. **Bypass done** (2026-11-21): frame `98 00 <type> <1 active / 0 bypassed>`; the state is the flag byte in front of the block in the cat `0x07` reply (own trial on the unit), so it is written and verified like a setting and shown as a 0/1 "Active" control. Switch names and value meanings were then checked against the per-state pictures (10 of 85 renamed). **Second panel run** (2026-11-21, other gestures: outer rings, trims, drags, menus): 55 more controls on 21 effects, among them the BAE frequency rings, VEQ-55A/B gains, PowerFFC detector menu and gain, all Guitar Amp knobs and switches (one block, knob names per model in the profile notes) and the switches of the four effects in the owner's racks (probed on a second instance; then, with the owner's go-ahead, all 37 controls of those four effects changed by one step and restored through this project, ClearQ's first live round trip). Open: the mode position "Out" of Brainiac / Gate Keeper / Drum-All-Nite also sends bypass in the vendor panel and does not here, about 25 bytes that answered no gesture (listed per effect in the profile notes or the private run report), Liverpool byte 5 (no control found in a third run; Tube 176 byte 1 = Ratio was found there). ClearQ: same band frames (vendor load captured to instance 1) and the vendor panel's read (sub-command `0x10`, 5 x 7 bytes + `01`) captured with the owner's ClearQ loaded; editable from that capture, live round trip todo. Guitar amp: eleven per-model default blocks captured. Cabinet: not controllable |
| AFX inserts: bypass / reorder | bypass frame observed (opcode byte `0x14`, `98 00 <type> <enabled>`; PowerGate's ON/OFF switch sends it), implemented per instance (`98 <instance> <type> <enabled>`, state = flag byte of that instance's block), **live-tested 2026-11-22: two PowerGate instances bypassed and re-activated one at a time, only the addressed flag changed**; rack reorder: **2026-11-22 vendor panel run, implemented from the captures, live-tested 2026-11-22 (each write path sent once through this project, raw read-back, restored):** one chain write in the new order (`d7 49 00 1b 00 27` -> `d7 49 00 27 00 1b`), enabled (`writes.reorder`). The same run confirmed the bypass frame on instances 0 and 1 (`98 00 1b`, `98 01 1b`) | bypass done (row above). **Reorder** (2026-11-21 vendor panel run): dragging one effect onto another is one chain write in the new order (`d7 11 02 33 00 27`); implemented as the rack operation `move` with the same read / write / read-back check, drag in the WebUI rack list. Captured with two effects; live verify todo |
| Vendor panel on Linux for captures | Launcher + Manager Server 1.8.20 + panel run under Wine 11.20 and reach the device over hidraw; needs the service start workaround (see CAPTURING.md) | same; panel 1.1.26 |
| Host-side snapshots / presets | todo | todo. Vendor session save sends nothing to the device; session load replays ordinary control frames (2026-11-21 vendor panel run) |
| Packaging: udev, systemd user unit | udev rule installed; user unit + launcher in `packaging/` (device-independent). The WebUI runs on this profile (inputs, Monitor / Headphones 1-4, Line Out, mixers, routing, Gazelle Reverb, Settings, insert racks) with `ANTELOPE_PROFILE=discrete_4_a270.json` | udev rule installed; user unit + app launcher in `packaging/` (`install-user.sh`); unit and desktop entry validate; live start under systemd not yet run |

## 8. Hand-off record

Each agent ends with the playbook's compact record, plus:

- role(s) acted in this session;
- which checklist rows moved, and to what status;
- branch and commit;
- any vendor-terms or clean-room question for the owner.

**Discrete 4 live trials, 2026-11-17** (this project's server on the owner's unit, each change read back and restored):
input mode Line -> Mic -> Line on all four preamps (state byte 44+ch, per-mode gain follows); gain on preamps 3 and 4;
Headphone 1 level, mute and dim (bytes 30/31); mixer fader, pan, mute and solo on mixes 0-3 (strips 1, 2, 3, 9, 16) via
cat 0x04; strip 6 source through the five mirrored routing groups. Hi-Z on preamps 2-4, 48V and Headphones 2-4 were
refused as unconfirmed. Not trialled: Monitor writes (left as found), phase and link state (no readback found).

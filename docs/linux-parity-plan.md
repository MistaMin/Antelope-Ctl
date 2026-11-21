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
| Preamp gain / mode / 48V / phase | **gain, mode and phase mapped from vendor captures (2026-11-17)**: gain `0x50` (state bytes 40-43, written and read back by this project on preamp 1), input mode `0x4f` Mic / Line / Hi-Z (its own state byte 44 + channel; each mode keeps its own gain), phase `0x52` (write-only: no state byte identified, the WebUI shows the last value sent). Captured on preamp 1 (gain also preamp 2); other preamps assumed, Hi-Z held to preamp 1. Gain held to 0-20 dB in every mode (manual maxima Mic 65 / Line 20 / Hi-Z 39 not captured). 48V: not captured, not mapped, stays off. Mode and phase not yet round-tripped by this project | all verified on owner fw 05.04 with readback and exact restore (no mics connected): gain, 48V and phase on both preamps; mode Mic/Line on both, HiZ on ch 0 only (ch 1 HiZ not captured). Device keeps a separate gain per mode (ch 0: Mic 20 / Line 15 / HiZ 12 dB) |
| Preamp link | preamp 1+2 link mapped (`a2 00 00 <on>`, vendor capture); write-only, cat `0x0b` idx 0 read all zeros with no link on and is an unconfirmed readback guess; preamps 3+4 not captured; live verify todo | mapped; live verify |
| Digital input gain / link | todo | todo |
| Output buses: level / mute / dim / mono | Monitor and Headphone 1 mapped from vendor captures: level `0x47` and mute `0x48` on bus 0 / 1, dim `0x66` on id 4 (Monitor) / 0 (Headphone 1), state bytes 28-31 (level, flags: 01 mute, 02 dim; 03 never seen). Monitor mute is protected: no automatic action releases it and dim is refused while it is muted. Level range 0-96 assumed, not swept. No mono control captured. Live verify todo | dim verified on all 3 buses, full status restored (selftest, fw 05.04); mute set verified; status is an exclusive enum (dim replaces mute); no mono flag in the state report |
| Output trim | todo (hint: cat `0x0e`) | todo |
| Headphone outputs | Headphone 1 as above. Headphones 2-4 are unconfirmed guesses in the profile (bus ids 2-4, dim ids 1-3, state bytes 32-37): not shown, not writable, one line each to confirm after a live trial | todo |
| Routing matrix | mixer strip source map only: one map of 16 strips mirrored in groups 6, 10, 11, 12, 13 (opcode `0x53`, five frames per change, read back from cat `0x03`); strips 5-16 writable with the sources seen (COMPUTER PLAY 1-2, S/PDIF IN 1-2, ADAT IN 1-8, EMU MIC 1-2, MUTE); strips 1-4 locked (the panel changes them through group 9, not understood). Line / digital / DAW destinations not captured; matrix tab disabled | readback parses; dest 5 write → verify → restore passed (fw 05.04); dest 6–9 lockstep; roles of dest 3/5 and bank `0x03` open |
| Virtual mixers: fader / pan / mute / solo | mapped from vendor captures: `d4 04 <mix 0-3> <strip 1-16> <fader> <pan+flags>` (opcode `0x16`), fader / pan / mute / solo on mix 0 strips 1, 2, 5, 8 and mute on strip 1 of mixes 1-3; cat `0x04` readback layout (strip N at bytes 2N, 2N+1) matches the values the panel's own frames carried. WebUI mixer enabled for 4 mixes x 16 strips. Write -> read round trip by this project todo | mapped, 2 mixes × 16 |
| Mixer channel link | strips 1+2 of mix 0 mapped (`a2 04 00 <on>` then `a2 03 00 <on>`), write-only; other pairs and mixes 1-3 not captured and not writable; cat `0x0b` idx 3 readback is an unconfirmed guess | mapped |
| Reverb: config / sends / returns | todo (hints: cats `0x0a`, `0x18`, `0x17`) | todo |
| Pan law | todo (hint: cat `0x12`) | todo |
| Sample rate / clock source | todo | read verified on owner unit (96 kHz, Internal); writes not tested (drop audio) |
| Oscillator / talkback / brightness / DC-coupling | check panel | check panel |
| Meters | todo | `0x83` magic known; calibration open |
| Mic emulation (emuMic) | todo, last (hint: cat `0x16`) | candidate `0x1a` frame; last |
| AFX inserts: read-only display | readback done, display blocked on the mixer: three captures (2026-11-17) give cat `0x19` = 36 records of {type, inst} pairs; record 0 = mixer channel 1; types 27 / 1 = Master De-esser / ClearQ, loaded by name in the vendor panel and read back. This project's CLI reads records 0 and 35 from the device and refuses 36. The WebUI has no Discrete 4 mixer to draw them on yet | **done, read-only**: vendor panel 1.1.26 connect walk gives cat `0x19` = 12 records; record 0 = mixer channel 1 and types 1 / 36 / 27 = ClearQ / SMT-100A / Master De-esser confirmed against the vendor panel; WebUI mixer shows racks on channels 1-4 from live readback (fw 05.04). Records 1-3 = channels 2-4 still derived; records 4-11 unknown |
| AFX inserts: load / remove | **done** on all four racks: whole-rack chain write (opcode `0x5b`) from vendor captures on strips 0-1, extended to strips 2-3 by write / readback / restore trials. Loadable: 45 effects, two instances each, plus ClearQ and the guitar amp (instance 0), whose load is followed by the vendor's captured default-settings frames (`load_defaults`: five band frames / one 16-byte block); these two loads are implemented from captures and **not yet run on the device**. The cabinet is not loadable: its load is impulse-response data computed by the vendor software. Any identified effect can be removed. WebUI "Effect racks" section | **done** on all four racks: chain write (opcode `0x23`, the Orion's assign frame) from a vendor capture on strip 1, extended to strips 0, 2, 3 by trials that left the owner's channel 1 rack unchanged. Loadable: Master De-esser, PowerGate, PowerEx (the profile's `writes.effects` has since grown to every effect whose vendor load was captured). ClearQ and the guitar amp are added with the vendor's captured default-settings frames after the chain write (`load_defaults`); not yet run on the device. The cabinet is not loadable. WebUI "Effect racks" section |
| AFX inserts: effect parameters | **45 effects done** (2026-11-17): parameter frame `d5 | len | type | inst | block`, state read cat `0x07` index = type (record length differs per effect: `read_stride`). 253 knob controls mapped by wheeling each control to both ends in the vendor panel; every effect then passed a load / read / change / restore / remove trial on two instances through this project's server. Raw units, no physical units claimed. Not mapped: switches, menus and stacked rings the wheel did not reach (their bytes are written back unchanged). **ClearQ** (2026-11-17, from captures, live round trip todo): one write frame per band (`<band> freq u16, Q u16 /100, gain i16, shape u8`), read under index 0 as 5 x 7 bytes + `01` per instance; six swept controls editable (band 1 frequency, band 2 Q and gain, band 3 frequency, Q and gain), the other band controls need a sweep capture. **Guitar amp**: model byte (0-10) and output level (-48..12) decoded, knobs seen for one notch only; not editable because no read of it has been captured. **Cabinet**: not controllable (every change re-sends impulse-response data) | **45 effects done** (2026-11-17): the effects it shares with the Discrete 4 use the same type ids and parameter blocks, so the same control maps apply. Each was loaded into an empty rack, read (tagged index `0x80000000 | type`, one instance per reply), had one control changed and restored, and was removed, through this project's server; three effects in the owner's racks were read only. First instance only. **22 Zen-Go-only effects done** (2026-11-20; Opto 2A, Vari-Speed Tremolo, Space Flanger, Marble White AutoWah, Adaptive Vibrato, Memory Cat, V12 Chorus, BBD Chorus, VEQ-432C, MG4+, Audio Baton (volume only), Filtek MK3, VEQ56, Instinct, Gyratec XIV, Comp-4K-Strip, Comp-4K-Bus, NEU-473A, Diode609, Brainiac, Drum All Nite, Gate Keeper): 170 knob controls from wheel sweeps in the vendor panel, each moved by one step and restored through this project's server on the owner's unit. Instinct's block is 26 float32 values (field type `f32`): 21 mapped, five that no swept control moved are written back unchanged. **Switches done** (2026-11-21): a click probe over 65 effects in the vendor panel found each switch and selector byte; 85 of them (38 effects), plus Gyratec XIV's Hi Mid Q and Audio Baton's nine band thumbwheels, were added to this profile (shared effects: on the Zen Go only, the Discrete 4 has no capture of them) and all 383 controls of the 40 changed effects passed the one-step change / restore trial. Guitar amp: settings read captured (flag byte + 16-byte block), so model, output and Bright are editable here. **Bypass done** (2026-11-21): frame `98 00 <type> <1 active / 0 bypassed>`; the state is the flag byte in front of the block in the cat `0x07` reply (own trial on the unit), so it is written and verified like a setting and shown as a 0/1 "Active" control. Switch names and value meanings were then checked against the per-state pictures (10 of 85 renamed). Open: the mode position "Out" of Brainiac / Gate Keeper / Drum-All-Nite also sends bypass in the vendor panel and does not here, bytes that answered neither wheel nor click, PowerFFC's detector menu, the switches of the four effects in the owner's racks (ClearQ, SMT-100A, Master De-esser, VEQ-STU 169), the FET ratio selector as a proper button row (values 0, 1, 2, 4, 8). ClearQ: same band frames (vendor load captured to instance 1) and the vendor panel's read (sub-command `0x10`, 5 x 7 bytes + `01`) captured with the owner's ClearQ loaded; editable from that capture, live round trip todo. Guitar amp: eleven per-model default blocks captured. Cabinet: not controllable |
| AFX inserts: bypass / reorder | bypass frame observed (opcode byte `0x14`, `98 00 <type> <enabled>`; PowerGate's ON/OFF switch sends it), not implemented | not captured |
| Vendor panel on Linux for captures | Launcher + Manager Server 1.8.20 + panel run under Wine 11.20 and reach the device over hidraw; needs the service start workaround (see CAPTURING.md) | same; panel 1.1.26 |
| Host-side snapshots / presets | todo | todo |
| Packaging: udev, systemd user unit | udev rule installed; user unit + launcher in `packaging/` (device-independent). The WebUI runs on this profile (inputs, Monitor / Headphone 1, mixers, strip sources, insert racks) with `ANTELOPE_PROFILE=discrete_4_a270.json` | udev rule installed; user unit + app launcher in `packaging/` (`install-user.sh`); unit and desktop entry validate; live start under systemd not yet run |

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

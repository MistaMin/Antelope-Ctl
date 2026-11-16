# Linux parity plan: Discrete 4 (0xa270) and Zen Go Synergy Core (0xa015)

This fork extends `antelope-ctl` from the Orion to two more devices the owner
has: the original **Discrete 4** and the **Zen Go Synergy Core**. The goal is
Linux control matching **everything the vendor mixer panel does, except AFX
plugins**. The work is clean-room and the result stays GPL-3.0 and shareable.

Read [AGENTS.md](../AGENTS.md), [SCOPE.md](../SCOPE.md) and the
[Discrete playbook](discrete-remote-agent-playbook.md) before touching a
device. This document adds the fork's scope, hosts, team layout and checklist.
It does not relax any rule in those files.

## 1. Devices

| device | USB id | profile | starting point |
|---|---|---|---|
| Discrete 4 (original, not Synergy Core) | `23e5:a270` | [`discrete_4_a270.json`](../profiles/discrete_4_a270.json) | Identity and USB interface classes only. Every feature disabled. |
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

**Out of scope:**

- AFX real-time effects: loading, parameters, strip order, AFX links.
  Readbacks may be decoded and shown read-only if a decoder needs them to
  parse a shared category. No writes.
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
| Transport (report size, framing, endpoints) | todo | upstream confirmed; re-check on owner unit |
| Preamp gain / mode / 48V / phase | todo | gain verified on owner fw 05.04 (ch 1 0→1→0 dB, readback); mode/phase not yet toggled; **48V deliberately not toggled** (mic safety, owner present only) |
| Preamp link | todo (hint: cat `0x0b` idx 0) | mapped; live verify |
| Digital input gain / link | todo | todo |
| Output buses: level / mute / dim / mono | todo | dim verified on all 3 buses, full status restored (selftest, fw 05.04); mute set verified; status is an exclusive enum (dim replaces mute); no mono flag in the state report |
| Output trim | todo (hint: cat `0x0e`) | todo |
| Headphone outputs | todo | todo |
| Routing matrix | todo (hint: cat `0x03`, 14 banks) | readback parses; dest 5 write → verify → restore passed (fw 05.04); dest 6–9 lockstep; roles of dest 3/5 and bank `0x03` open |
| Virtual mixers: fader / pan / mute / solo | todo (hint: cat `0x04`, 4 mixes) | mapped, 2 mixes × 16 |
| Mixer channel link | todo (hint: cat `0x0b` idx 3) | mapped |
| Reverb: config / sends / returns | todo (hints: cats `0x0a`, `0x18`, `0x17`) | todo |
| Pan law | todo (hint: cat `0x12`) | todo |
| Sample rate / clock source | todo | read verified on owner unit (96 kHz, Internal); writes not tested (drop audio) |
| Oscillator / talkback / brightness / DC-coupling | check panel | check panel |
| Meters | todo | `0x83` magic known; calibration open |
| Mic emulation (emuMic) | todo, last (hint: cat `0x16`) | candidate `0x1a` frame; last |
| Host-side snapshots / presets | todo | todo |
| Packaging: udev, systemd user unit | todo | todo |

## 8. Hand-off record

Each agent ends with the playbook's compact record, plus:

- role(s) acted in this session;
- which checklist rows moved, and to what status;
- branch and commit;
- any vendor-terms or clean-room question for the owner.

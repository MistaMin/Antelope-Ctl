"""The device actor: one worker thread owns the HID transport, runs queued
commands, reads the state report and the slow readback sweep, and publishes
snapshots. No web code here; webui/server.py builds the HTTP API on top.
"""
import queue
import sys
import threading
import traceback
import time

from antelope import protocol as proto
from antelope.transport import open_transport

ROUTING_CAT = proto.ROUTING_READBACK_CATEGORY   # 0x03
MIXER_CAT = proto.MIXER_READBACK_CATEGORY       # 0x04
SURROUND_EQ_CAT = proto.SURROUND_SPEAKER_EQ_READBACK_CATEGORY   # 0x1a
SURROUND_GLOBAL_CAT = proto.SURROUND_GLOBAL_READBACK_CATEGORY   # 0x1b
READBACK_REFRESH_S = 45.0   # slow background poll, one record per fast-state cycle

STRUCTURED_READBACK_SAFE_STATUSES = frozenset({
    "confirmed", "capture-confirmed",
})


def _record_layout_indices(layout):
    """Return the explicitly declared outer indices for one nested layout."""
    try:
        if layout.get("index") is not None:
            return [proto._as_int(layout["index"])]
        lo, hi = layout["index_range"]
        lo, hi = proto._as_int(lo), proto._as_int(hi)
        return list(range(lo, hi + 1)) if lo <= hi else []
    except (KeyError, TypeError, ValueError):
        return []


def _confirmed_profile_block(block):
    """Whether a profile metadata block is safe to drive automatically."""
    return isinstance(block, dict) and str(block.get("status", "")).strip().lower() in \
        STRUCTURED_READBACK_SAFE_STATUSES


def _mixer_surface_spec(profile):
    """Return a confirmed profile-defined mixer-surface selector, if any."""
    spec = (profile.get("mixer", {}) or {}).get("surface_selection")
    if not _confirmed_profile_block(spec):
        return None
    values = spec.get("value_by_mix")
    return spec if isinstance(values, dict) else None


def _mixer_surface_value(profile, mix):
    """Resolve a logical mixer index to its profile-defined selector value."""
    spec = _mixer_surface_spec(profile)
    if spec is None:
        return None
    values = spec.get("value_by_mix", {})
    raw = values.get(str(mix), values.get(mix))
    if raw is None:
        return None
    try:
        return proto._as_int(raw)
    except (TypeError, ValueError):
        return None


def _mixer_surface_for_value(profile, value):
    """Resolve a state-report selector value to its logical mixer index."""
    spec = _mixer_surface_spec(profile)
    if spec is None:
        return None
    for mix, raw in spec.get("value_by_mix", {}).items():
        try:
            if proto._as_int(raw) == value:
                return proto._as_int(mix)
        except (TypeError, ValueError):
            continue
    return None


def _mixer_surface_packet(profile, mix):
    """Build a confirmed profile-defined mixer surface selection command."""
    spec = _mixer_surface_spec(profile)
    value = _mixer_surface_value(profile, mix)
    if spec is None or value is None:
        return None
    target = proto._as_int(spec.get("target", 0))
    param = spec.get("param")
    if isinstance(param, str) and param in profile.get("params", {}):
        packet = proto.build_command(profile, param, target, value)
    else:
        packet = proto.build_raw_command(profile, proto._as_int(param), target, value)
    return packet, value, target


def _surround_global_contract(profile):
    """Return the safe surround-global contract, if the profile has one."""
    frame = profile.get("frame", {}).get("surround_global_command", {})
    contract = frame.get("contract", {}) if isinstance(frame, dict) else {}
    status = str(contract.get("status", frame.get("runtime_status", ""))) \
        .strip().lower()
    if status not in STRUCTURED_READBACK_SAFE_STATUSES:
        return None
    try:
        if not proto.readback_indices_available(profile, SURROUND_GLOBAL_CAT, [0]):
            return None
        count = proto.readback_category_count(profile, SURROUND_EQ_CAT)
        if count is None or count <= 0:
            return None
        if not proto.readback_indices_available(
                profile, SURROUND_EQ_CAT, range(count)):
            return None
    except (KeyError, TypeError, ValueError, proto.ConstraintError):
        return None
    return contract


def _strip_surround_raw(value):
    """Keep decoded surround state JSON-friendly and human-sized."""
    if isinstance(value, dict):
        return {
            key: _strip_surround_raw(item)
            for key, item in value.items()
            if key not in {"raw", "header"}
        }
    if isinstance(value, list):
        return [_strip_surround_raw(item) for item in value]
    return value


def _surround_speaker_label(index, global_state):
    if index == 0:
        return "L"
    if index == 1:
        return "R"
    if global_state and index == global_state.get("lfe_index"):
        return "LFE"
    return f"Speaker {index + 1}"


def _surround_format_name(profile, global_state, raw_body):
    """Resolve a global readback to a profile-defined packed channel order."""
    if not isinstance(global_state, dict) or not isinstance(raw_body, (bytes, bytearray)):
        return None
    return proto.surround_format_name(profile, raw_body)


def _structured_readback_targets(profile):
    """Yield safe ``(category, index)`` targets from record_layouts.

    A nested body schema is not enough to authorize an outer query.  Only the
    profile statuses that carry capture-confirmed outer-index evidence are
    scheduled by the daemon; schema-only entries remain visible through the
    metadata API with ``capture_required`` set.
    """
    layouts = profile.get("frame", {}).get("readback", {}).get(
        "record_layouts", []) or []
    if isinstance(layouts, dict):
        layouts = list(layouts.values())
    seen = set()
    for layout in layouts:
        if not isinstance(layout, dict):
            continue
        if str(layout.get("status", "")).strip().lower() not in \
                STRUCTURED_READBACK_SAFE_STATUSES:
            continue
        try:
            category = proto._as_int(layout["category"])
        except (KeyError, TypeError, ValueError):
            continue
        kind = layout.get("kind")
        for index in _record_layout_indices(layout):
            target = (category, index)
            if target in seen:
                continue
            try:
                # Keep the safety guard in the final scheduling path too. This
                # also handles profiles that declare a malformed range or an
                # index outside their category_counts.
                proto.check_readback_index(profile, category, index)
                if proto.readback_record_layout(profile, category, index,
                                                kind=kind) is None:
                    continue
            except (KeyError, proto.ConstraintError, TypeError, ValueError):
                continue
            seen.add(target)
            yield target

# The free-running meter report and a readback RESPONSE are BOTH magic 0x75.
# They differ only at byte 1: meter = 0x1f, readback response = 0x00 (see
# frame.readback.discriminator_note). transport.read_one() matches byte 0
# only, so meters must be filtered here or a stray readback frame lands in
# the meter array.
METER_DISCRIMINATOR_OFFSET = 1
METER_DISCRIMINATOR = 0x1F


def _state_report_offset(profile, state_spec, mapping, key, default_basis):
    """Resolve a profile offset into the full state-report coordinate space."""
    offset = proto._as_int(mapping[key])
    basis = str(mapping.get("offset_basis", default_basis)).strip().lower()
    if basis in {"payload", "snapshot_payload", "snapshot-payload"}:
        offset += proto._as_int(state_spec.get("snapshot_payload_offset", 0))
    return offset


def _meter_is_silent(raw, silence_raw, noise_floor_raw=None,
                    direction="inverted"):
    """Apply an explicitly profile-declared raw idle floor."""
    if silence_raw is not None and raw == silence_raw:
        return True
    return (noise_floor_raw is not None
            and direction == "inverted"
            and raw >= noise_floor_raw)

# ---------------------------------------------------------------- device thread

class Device:
    """Owns the HID node. All device I/O happens on .run()'s thread.

    The command queue holds callables `fn(transport)`; the web handlers build
    the closure (so a routing/mixer write can read-modify-write on this
    thread, the only place it is safe to touch the node)."""

    def __init__(self, profile):
        self.profile = profile
        dev = profile["device"]
        self.vid = int(dev["vid"], 16) if isinstance(dev["vid"], str) else dev["vid"]
        self.pid = int(dev["pid"], 16) if isinstance(dev["pid"], str) else dev["pid"]
        self.report_size = profile["transport"]["report_size"]
        self.n_ch = proto.space_channel_count(profile, "input") or 12
        self.n_adat = int(profile.get("adat", {}).get("count", 0))
        self.n_spdif = int(profile.get("spdif", {}).get("count", 0))
        # channel_space_bounds drops buses the profile marks unconfirmed
        self.bus_ids = proto.channel_space_bounds(profile, "bus") or []
        self.state_magic = proto.state_report_magic(profile)
        self.meter_magic = proto.meter_report_magic(profile)
        readback_magic = profile["frame"].get("readback", {}).get("response_magic")
        self.meter_shares_readback_magic = (
            isinstance(readback_magic, str)
            and self.meter_magic == int(readback_magic, 0))

        rc = profile["frame"].get("routing_command", {})
        self.route_dests = sorted(int(k) for k in rc.get("destination_channels", {}))
        mixer = profile.get("mixer", {})
        self.n_mixes = int(mixer.get("mixes")
                           or proto.readback_category_count(profile, MIXER_CAT) or 0)
        self.mix_channels = int(mixer.get("channels_per_mix", 0))
        self.mixer_has_master = bool(mixer.get("has_master", True))
        layout_indices = proto.readback_layout_indices(
            profile, MIXER_CAT, kind="mixer_state")
        category_count = proto.readback_category_count(profile, MIXER_CAT)
        self.mixer_indices = layout_indices or (
            list(range(category_count)) if category_count is not None else [])
        input_routing = mixer.get("input_routing", {}) or {}
        self.mixer_source_destinations = [
            int(dest) for dest in input_routing.get("destinations", [])]
        self.routing_readback = proto.readback_indices_available(
            profile, ROUTING_CAT, self.route_dests)
        self.mixer_readback = bool(
            self.n_mixes and all(m in self.mixer_indices
                                 for m in range(self.n_mixes)))
        self.routing_available = bool(self.route_dests and self.routing_readback)
        self.mixer_available = bool(
            self.n_mixes and self.mix_channels and self.mixer_readback
            and profile["frame"].get("mix_command"))
        self.mixer_source_available = bool(
            self.mixer_available
            and self.routing_available
            and self.mixer_source_destinations
            and all(dest in self.route_dests
                    and proto.route_dest_channels(profile, dest) >= self.mix_channels
                    for dest in self.mixer_source_destinations)
            and proto.readback_indices_available(
                profile, ROUTING_CAT, self.mixer_source_destinations))
        self.surround_contract = _surround_global_contract(profile)
        self.surround_speaker_count = (
            proto.readback_category_count(profile, SURROUND_EQ_CAT) or 0)
        self.surround_available = bool(self.surround_contract)

        self.cmds = queue.Queue()          # callables: fn(transport) -> None
        self.snapshot = {"online": False}  # last known state, read by the web side
        self.version = 0
        self.rb_ver = 0
        self.link_rb_ver = 0           # fresh link-table reads after link writes
        self.routing = {}                  # dest_id(int) -> [(bank, idx), ...]
        self.mixer = {}                    # mix(int)     -> [slot dict, ...]
        self.auraverb = {}                 # readback index -> list of mix dicts
        self.structured = {}               # (category, index) -> parsed records
        self.surround_global = None        # parsed category-0x1b record
        self.surround_global_raw = None    # body used for safe global RMW
        self.surround_speakers = {}        # speaker index -> parsed EQ record
        auraverb_target = proto.auraverb_readback_target(profile)
        self.auraverb_category = (auraverb_target[0]
                                  if auraverb_target is not None
                                  else proto.AURAVERB_READBACK_CATEGORY)
        self.auraverb_index = (auraverb_target[1]
                               if auraverb_target is not None else 0)
        self.auraverb_available = proto.auraverb_readback_available(profile)
        self.solo_state = {}               # mix -> {restore: flags, active: channels}
        self._lock = threading.Lock()
        self._t = None
        self._stop = threading.Event()
        self._transport = None
        self.connection_generation = 0

    def start(self):
        if self._t is not None and self._t.is_alive():
            return
        self._stop.clear()
        self._t = threading.Thread(target=self.run, daemon=True)
        self._t.start()

    def _routing_write_destinations(self, dest):
        """Physical records behind one routing destination; the server maps logical UI views."""
        return [int(dest)]

    def _drop_transport(self, transport):
        """Close a platform transport and forget it, if it is still current."""
        if transport is not None:
            close = getattr(transport, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:                           # noqa: BLE001
                    pass
        with self._lock:
            if self._transport is transport:
                self._transport = None

    def request_stop(self):
        """Request an immediate worker/transport shutdown from a signal path.

        This deliberately does not join the worker.  Terminal signal handlers
        must stay short; ``stop()`` performs the bounded join once Uvicorn has
        left its event loop.
        """
        self._stop.set()
        with self._lock:
            transport = self._transport
        self._drop_transport(transport)
        # Wake a worker that is between fast-state reads.  None is an internal
        # sentinel and is never submitted by an HTTP handler.
        self.cmds.put(None)

    def stop(self, timeout=2.0):
        """Stop the HID worker so terminal Ctrl+C can end the WebUI cleanly."""
        self.request_stop()
        worker = self._t
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, float(timeout)))

    def submit(self, fn):
        """Queue fn(transport). At most one queued function runs per fast
        state cycle, so a burst of controls cannot starve meter updates."""
        self.cmds.put(fn)

    def _discard_queued(self):
        """Forget commands queued while offline: reconnecting must not replay them."""
        try:
            while True:
                self.cmds.get_nowait()   # a stop sentinel may go too: _stop is already set
        except queue.Empty:
            pass

    def get(self):
        with self._lock:
            return {**self.snapshot, 'connection_generation': self.connection_generation}, self.version

    def routing_json(self):
        with self._lock:
            out = {}
            for dest, pairs in self.routing.items():
                items = []
                for bank, index in pairs:
                    item = {
                        "bank": bank,
                        "idx": index,
                        "label": proto.route_source_label(
                            self.profile, bank, index),
                    }
                    key = proto.route_source_key(self.profile, bank, index)
                    if key is not None:
                        item["key"] = key
                    items.append(item)
                out[str(dest)] = items
            return out

    def mixer_json(self):
        with self._lock:
            return {
                str(m): [
                    {"ch": i, "fader": s["fader"], "pan": s["pan"],
                     "send": s["send"], "mute": s["mute"], "solo": s["solo"],
                     **({"send_pan": s["send_pan"]} if "send_pan" in s else {})}
                    for i, s in enumerate(slots)
                ]
                for m, slots in self.mixer.items()
            }

    def auraverb_json(self):
        with self._lock:
            records = self.auraverb.get(self.auraverb_index)
            if records is None:
                return None
            return [{
                "params": dict(record.get("params", {})),
                "enabled": record.get("enabled"),
                "wet": record.get("wet"),
            } for record in records]

    def surround_json(self):
        """Return decoded surround state and the profile's write boundary."""
        with self._lock:
            global_state = self.surround_global
            global_raw = self.surround_global_raw
            speakers = dict(self.surround_speakers)

        global_view = _strip_surround_raw(global_state)
        if global_view is not None:
            global_view["format"] = (
                _surround_format_name(
                    self.profile, global_state, global_raw)
                or ("2.1" if global_state.get("lfe_present") else "2.0"))

        contract = self.surround_contract or {}
        delay_range = list(contract.get("delay_range", []))
        level_range = list(contract.get("level_range", []))
        delay_step = float(contract.get("delay_step_ms", 0.1))
        level_step = float(contract.get("level_step_db", 0.1))
        level_zero = int(contract.get("level_zero_raw", 600))
        write = {
            "available": self.surround_available,
            "enabled": False,
            "fields": ["delay_ms", "level_db"],
            "delay_step_ms": delay_step,
            "level_step_db": level_step,
            "delay_ms_range": [value * delay_step for value in delay_range],
            "level_db_range": [
                (value - level_zero) * level_step for value in level_range
            ],
            "readback_category": SURROUND_GLOBAL_CAT,
            "note": (
                "Global delay and level use a fresh category-0x1b read-modify-write "
                "in the normal 2.0/2.1 formats."
            ),
        }
        format_options = []
        format_write_enabled = False
        for item in contract.get("formats", []) or []:
            try:
                name = str(item["name"])
                flags_a = proto._as_int(item["flags_a"])
                flags_b = proto._as_int(item["flags_b"])
                channel_order = [proto._as_int(value)
                                 for value in item["channel_order"]]
                proto.pack_surround_channel_order(
                    channel_order,
                    int(contract.get("channel_order_size", 10)))
            except (KeyError, TypeError, ValueError):
                continue
            normal_write = bool(item.get("format_writable"))
            format_options.append({
                "name": name,
                "flags_a": flags_a,
                "flags_b": flags_b,
                "channel_order": channel_order,
                "channel_count": len(channel_order),
                "writable": normal_write,
                "experimental": not normal_write,
            })
            if normal_write and global_state is not None \
                    and global_raw is not None:
                try:
                    proto.build_surround_global_format_command(
                        self.profile, global_raw, name)
                except (KeyError, TypeError, ValueError, proto.ConstraintError):
                    pass
                else:
                    format_write_enabled = True
        write["format"] = {
            "enabled": format_write_enabled,
            "fields": ["format"],
            "options": format_options,
            "readback_category": SURROUND_GLOBAL_CAT,
            "note": (
                "Only 2.0 and 2.1 are enabled for normal format writes. "
                "Higher layouts remain visible for guarded self-tests."
            ),
        }
        position_contract = contract.get("eq_position_write", {}) or {}
        position_values = position_contract.get(
            "values", {"pre": 0, "post": 1})
        position_status = str(position_contract.get(
            "status", "unavailable")).strip().lower()
        eq_position_write = {
            "enabled": False,
            "experimental": position_status in {
                "experimental", "experimental-unverified"},
            "status": position_contract.get("status", "unavailable"),
            "fields": [str(value) for value in position_values],
            "values": {
                str(name): proto._as_int(value)
                for name, value in position_values.items()
            } if isinstance(position_values, dict) else {},
            "formats": [str(name) for name in position_contract.get(
                "formats", [])],
            "readback_category": SURROUND_GLOBAL_CAT,
            "note": position_contract.get(
                "note",
                "EQ PRE/POST writes use a fresh category-0x1b readback.",
            ),
        }
        if global_raw is not None and eq_position_write["values"]:
            try:
                proto.build_surround_global_eq_position_command(
                    self.profile, global_raw, "post", allow_experimental=True)
            except (KeyError, TypeError, ValueError, proto.ConstraintError):
                pass
            else:
                eq_position_write["enabled"] = True
        write["eq_position"] = eq_position_write
        speaker_contract = self.profile.get("runtime_contracts", {}).get(
            "surround_speaker_eq", {}) or {}
        speaker_write_contract = speaker_contract.get("write_contract", {}) or {}
        head_specs = speaker_contract.get("head_fields", {}) or {}
        requested_head_fields = speaker_write_contract.get(
            "head_fields", []) or []
        head_fields = [
            str(name) for name in requested_head_fields
            if isinstance(head_specs.get(name), dict)
            and head_specs[name].get("writable", False)
        ]
        head_controls = {}
        for name in head_fields:
            spec = head_specs[name]
            if spec.get("boolean"):
                head_controls[name] = {
                    "label": str(spec.get("label", name)),
                    "boolean": True,
                }
                continue
            try:
                display_range = [float(value) for value in spec["display_range"]]
                step = float(spec["step"])
                digits = int(spec.get("digits", 1))
            except (KeyError, TypeError, ValueError):
                continue
            if len(display_range) != 2 or step <= 0:
                continue
            head_controls[name] = {
                "label": str(spec.get("label", name)),
                "unit": str(spec.get("unit", "")),
                "range": display_range,
                "step": step,
                "digits": digits,
            }
        head_formats = [str(name) for name in speaker_write_contract.get(
            "head_formats", [])]
        head_write = {
            "enabled": False,
            "experimental": str(speaker_write_contract.get(
                "status", "")).strip().lower() in {
                    "experimental", "experimental-unverified"},
            "status": speaker_write_contract.get("status", "unavailable"),
            "fields": list(head_controls),
            "controls": head_controls,
            "formats": head_formats,
            "readback_category": SURROUND_EQ_CAT,
            "note": speaker_write_contract.get(
                "head_notes",
                "Speaker delay/level/phase writes preserve the "
                "complete category-0x1a record and should be compared with "
                "fresh readback."),
        }
        current_format = global_view.get("format") if isinstance(
            global_view, dict) else None
        format_allowed = bool(current_format) and (
            not head_formats or current_format in head_formats)
        if format_allowed and head_fields:
            sample_field = next((name for name in head_fields
                                 if not head_specs[name].get("boolean")), None)
            if sample_field is None:
                sample_field = head_fields[0]
            sample_spec = head_specs[sample_field]
            try:
                sample_value = (False if sample_spec.get("boolean") else
                                head_controls[sample_field]["range"][0])
                record_size = int(speaker_contract["record_size"])
                proto.build_surround_speaker_head_command(
                    self.profile, bytes(record_size), 0, sample_field,
                    sample_value, allow_experimental=True)
            except (KeyError, TypeError, ValueError, proto.ConstraintError):
                pass
            else:
                head_write["enabled"] = True
        write["speaker_head"] = head_write
        speaker_mask_contract = contract.get("speaker_mask_write", {}) or {}
        speaker_mask_fields_contract = speaker_mask_contract.get(
            "fields", {}) or {}
        speaker_mask_fields = [
            str(name) for name, spec in speaker_mask_fields_contract.items()
            if isinstance(spec, dict) and spec.get("writable", True)
        ]
        mask_formats = [str(name) for name in speaker_mask_contract.get(
            "formats", [])]
        mask_format_allowed = bool(current_format) and (
            not mask_formats or current_format in mask_formats)
        speaker_bypass_write = {
            "enabled": False,
            "experimental": str(speaker_mask_contract.get(
                "status", "")).strip().lower() in {
                    "experimental", "experimental-unverified"},
            "status": speaker_mask_contract.get("status", "unavailable"),
            "fields": speaker_mask_fields,
            "formats": mask_formats,
            "speaker_count": int(speaker_mask_contract.get(
                "speaker_count", self.surround_speaker_count)),
            "readback_category": SURROUND_GLOBAL_CAT,
            "note": speaker_mask_contract.get(
                "note", "Per-speaker mask writes use a fresh category-0x1b "
                "global readback."),
        }
        if (mask_format_allowed and global_raw is not None
                and speaker_mask_fields):
            sample_field = speaker_mask_fields[0]
            try:
                proto.build_surround_global_speaker_mask_command(
                    self.profile, global_raw, 0, sample_field, False,
                    allow_experimental=True)
            except (KeyError, TypeError, ValueError, proto.ConstraintError):
                pass
            else:
                speaker_bypass_write["enabled"] = True
        write["speaker_bypass"] = speaker_bypass_write
        bass_contract = contract.get("bass_management_write", {}) or {}
        bass_fields_contract = bass_contract.get("fields", {}) or {}
        bass_fields = [
            str(name) for name, spec in bass_fields_contract.items()
            if isinstance(spec, dict) and spec.get("writable", True)
        ]
        bass_filter_type_values = {}
        bass_link_fields = []
        for name, spec in bass_fields_contract.items():
            if (not isinstance(spec, dict)
                    or str(spec.get("scope", "block")).strip().lower() != "global"):
                continue
            kind = str(spec.get("kind", "")).strip().lower()
            if kind == "link":
                bass_link_fields.append(str(name))
                continue
            if kind != "filter_type":
                continue
            display_values = spec.get("display_values")
            raw_values = spec.get("raw_values")
            if (not isinstance(display_values, list)
                    or not isinstance(raw_values, list)
                    or len(display_values) != len(raw_values)):
                continue
            bass_filter_type_values[str(name)] = [
                {"value": display, "label": str(display)}
                for display in display_values
            ]
        filter_types = {}
        if isinstance(global_state, dict):
            filter_types = dict(global_state.get(
                "bass_mgmt_filter_types", {}) or {})
        link_states = {}
        if isinstance(global_state, dict):
            link_states = dict(global_state.get(
                "bass_mgmt_links", {}) or {})
        bass_write = {
            "enabled": False,
            "experimental": str(bass_contract.get(
                "status", "")).strip().lower() in {
                    "experimental", "experimental-unverified"},
            "status": bass_contract.get("status", "unavailable"),
            "fields": bass_fields,
            "formats": [str(name) for name in bass_contract.get("formats", [])],
            "block_count": int(bass_contract.get("block_count", 0)),
            "readback_category": SURROUND_GLOBAL_CAT,
            "cutoff_range_hz": [20, 320],
            "fader_range_db": [-60, 16],
            "order_values": [2, 4, 8],
            "filter_types": filter_types,
            "filter_type_values": bass_filter_type_values,
            "link_fields": bass_link_fields,
            "link_states": link_states,
            "links": bass_contract.get("links", {}) or {},
            "note": (
                "One-field writes use a fresh category-0x1b readback. "
                "Cutoffs, orders, bypass, fader, mute, filter type, Link, "
                "and Solo mappings were confirmed by the hardware probe; "
                "meters remain guarded."
            ),
        }
        if global_raw is not None and bass_fields:
            sample_field = bass_fields[0]
            sample_spec = bass_fields_contract[sample_field]
            if sample_spec.get("boolean"):
                sample_value = False
            elif isinstance(sample_spec.get("display_values"), list) \
                    and sample_spec["display_values"]:
                sample_value = sample_spec["display_values"][0]
            else:
                sample_value = (2 if "order" in sample_field else
                                0 if "fader" in sample_field else 80)
            try:
                proto.build_surround_global_bass_command(
                    self.profile, global_raw, 0, sample_field, sample_value,
                    allow_experimental=True)
            except (KeyError, TypeError, ValueError, proto.ConstraintError):
                pass
            else:
                bass_write["enabled"] = True
        write["bass"] = bass_write
        eq_contract = self.profile.get("runtime_contracts", {}).get(
            "surround_speaker_eq", {}) or {}
        eq_write_contract = eq_contract.get("write_contract", {}) or {}
        eq_write = {
            "enabled": False,
            "experimental": str(eq_write_contract.get(
                "status", "")).strip().lower() in {
                    "experimental", "experimental-unverified"},
            "status": eq_write_contract.get("status", "unavailable"),
            "fields": ["frequency", "q", "gain", "mode"],
            "readback_category": SURROUND_EQ_CAT,
            "speaker_count": self.surround_speaker_count,
            "band_count": int(eq_contract.get("band_count", 16)),
            "frequency_range": list(eq_contract.get(
                "frequency_range", [20, 20000])),
            "q_range": [
                float(value) / 100
                for value in eq_contract.get("q_raw_range", [10, 1800])
            ],
            "gain_range": [
                float(value) / 100
                for value in eq_contract.get("gain_raw_range", [-2400, 1200])
            ],
            "mode_range": list(eq_contract.get("mode_range", [0, 255])),
            "note": (
                "One-field EQ writes use a fresh category-0x1a readback and "
                "preserve the rest of the speaker record."
            ),
        }
        reset_preset = eq_contract.get("reset_preset")
        if isinstance(reset_preset, dict):
            eq_write["reset"] = {
                "frequency_hz": list(reset_preset.get("frequency_hz", [])),
                "q": reset_preset.get("q"),
                "gain_db": reset_preset.get("gain_db"),
            }
        try:
            record_size = int(eq_contract["record_size"])
            proto.build_surround_speaker_eq_command(
                self.profile, bytes(record_size), 0, 0, {},
                allow_experimental=True)
        except (KeyError, TypeError, ValueError, proto.ConstraintError):
            pass
        else:
            eq_write["enabled"] = True
        write["eq"] = eq_write
        if global_state is not None and self.surround_global_raw is not None:
            try:
                delay_raw = round(global_state["global_delay_ms"] / delay_step)
                level_raw = round(global_state["level_db"] / level_step + level_zero)
                proto.build_surround_global_command(
                    self.profile, self.surround_global_raw,
                    global_delay=delay_raw, global_level=level_raw)
            except (KeyError, TypeError, ValueError, proto.ConstraintError):
                pass
            else:
                write["enabled"] = True

        active_count = None
        if global_state is not None:
            try:
                count = int(global_state.get("flags_a_raw", 0)) & 0x1F
            except (TypeError, ValueError):
                count = 0
            if 0 < count <= self.surround_speaker_count:
                active_count = count
        speaker_view = []
        for index in range(self.surround_speaker_count):
            record = speakers.get(index)
            speaker_data = _strip_surround_raw(record or {})
            head = speaker_data.get("head", {})
            bypass_mask = (global_state.get("bypass_mask")
                           if isinstance(global_state, dict) else None)
            bypass = (None if bypass_mask is None else
                      not bool(int(bypass_mask) & (1 << index)))
            speaker_view.append({
                "index": index,
                "label": _surround_speaker_label(index, global_state),
                "active": active_count is None or index < active_count,
                "readback": record is not None,
                "head_readback": bool(record is not None and head),
                "head": head,
                "bypass": bypass,
                "bypass_readback": bypass_mask is not None,
                "bands": speaker_data.get("bands", []),
            })
        return {
            "available": self.surround_available,
            "global": global_view,
            "speakers": speaker_view,
            "speaker_count": self.surround_speaker_count,
            "active_speaker_count": active_count,
            "write": write,
        }

    def _surround_speaker_body_for_write(self, transport, speaker):
        """Read a fresh per-speaker EQ record before a bounded write."""
        if not self.surround_available:
            raise RuntimeError("surround state is not safely mapped")
        if not 0 <= int(speaker) < self.surround_speaker_count:
            raise RuntimeError(f"surround speaker {speaker} is out of range")
        req = proto.build_readback_query(
            self.profile, SURROUND_EQ_CAT, int(speaker))
        data = transport.query(
            req,
            lambda x: proto.is_readback_response(
                self.profile, x, SURROUND_EQ_CAT, int(speaker)),
            timeout=1.5)
        if data is None:
            raise RuntimeError(
                f"no surround speaker readback for {speaker} -- not writing blind")
        body = proto.readback_body(self.profile, data)
        self._cache_surround_speaker_body(speaker, body)
        return bytes(body)

    def _cache_surround_speaker_body(self, speaker, body):
        """Cache one parsed speaker record and announce a changed readback."""
        record = proto.parse_surround_speaker_eq_record(self.profile, body)
        with self._lock:
            changed = self.surround_speakers.get(int(speaker)) != record
            self.surround_speakers[int(speaker)] = record
            if changed:
                self.rb_ver += 1

    def _cache_surround_speaker_packet(self, speaker, packet):
        """Update the local cache from a complete speaker write packet."""
        contract = self.profile.get("runtime_contracts", {}).get(
            "surround_speaker_eq", {}) or {}
        write = contract.get("write_contract", {}) or {}
        payload_offset = int(write.get("payload_offset", 19))
        record_size = int(contract.get("record_size", 116))
        body = bytes(packet[payload_offset:payload_offset + record_size])
        self._cache_surround_speaker_body(speaker, body)

    def _surround_global_body_for_write(self, transport):
        """Read fresh global surround state before a complete-state write."""
        if not self.surround_available:
            raise RuntimeError("surround global state is not safely mapped")
        req = proto.build_readback_query(self.profile, SURROUND_GLOBAL_CAT, 0)
        data = transport.query(
            req,
            lambda x: proto.is_readback_response(
                self.profile, x, SURROUND_GLOBAL_CAT, 0),
            timeout=1.5)
        if data is None:
            raise RuntimeError("no surround global readback -- not writing blind")
        body = proto.readback_body(self.profile, data)
        parsed = proto.parse_surround_global_record(self.profile, body)
        with self._lock:
            self.surround_global = parsed
            self.surround_global_raw = bytes(body)
        return bytes(body)

    def _cache_surround_global_packet(self, packet):
        """Update the local surround cache after a queued global write."""
        contract = self.surround_contract or {}
        offset = int(contract.get("readback_payload_offset", 18))
        size = int(contract.get("template_size", 151))
        body = bytes(packet[offset:offset + size])
        parsed = proto.parse_surround_global_record(self.profile, body)
        with self._lock:
            if self.surround_global == parsed:
                self.surround_global_raw = body
                return
            self.surround_global = parsed
            self.surround_global_raw = body
            self.rb_ver += 1

    def structured_readbacks_json(self):
        """Serialize profile-declared nested readbacks for the WebUI.

        ``raw`` bytes are represented as hex so this endpoint stays ordinary
        JSON.  Unsafe schema-only layouts are included as metadata, but their
        ``current`` map is deliberately empty because the daemon never probes
        their unbounded outer indices.
        """
        layouts = self.profile.get("frame", {}).get("readback", {}).get(
            "record_layouts", []) or []
        if isinstance(layouts, dict):
            layouts = list(layouts.values())
        with self._lock:
            cached = dict(self.structured)
        out = []
        for layout in layouts:
            if not isinstance(layout, dict):
                continue
            try:
                category = proto._as_int(layout["category"])
            except (KeyError, TypeError, ValueError):
                continue
            indices = _record_layout_indices(layout)
            status = str(layout.get("status", ""))
            safe = status.strip().lower() in STRUCTURED_READBACK_SAFE_STATUSES
            current = {}
            if safe:
                for index in indices:
                    records = cached.get((category, index))
                    if records is None:
                        continue
                    current[str(index)] = [
                        {key: (value.hex() if isinstance(value, bytes) else value)
                         for key, value in record.items()}
                        for record in records
                    ]
            item = {
                "kind": layout.get("kind"),
                "name": layout.get("name", layout.get("kind", "readback")),
                "category": category,
                "status": status,
                "safe": safe,
                "capture_required": not safe,
                "indices": indices,
                "record_count": layout.get("record_count"),
                "record_stride": layout.get("record_stride"),
                "fields": layout.get("fields", []),
                "current": current,
            }
            if layout.get("index") is not None:
                item["index"] = proto._as_int(layout["index"])
            elif layout.get("index_range") is not None:
                item["index_range"] = list(layout["index_range"])
            out.append(item)
        return {
            "available": bool(out),
            "refresh_seconds": READBACK_REFRESH_S,
            "layouts": out,
        }

    def _publish(self, snap):
        with self._lock:
            snap["rb_ver"] = self.rb_ver
            snap["link_rb_ver"] = self.link_rb_ver
            self.snapshot = snap
            self.version += 1

    def _cache_routing(self, dest, pairs):
        """Store the serialized routing record after a successful write."""
        with self._lock:
            if self.routing.get(dest) == pairs:
                return
            self.routing[dest] = pairs
            self.rb_ver += 1

    def _cache_mixer(self, mix, slots):
        """Store the serialized mixer record after a successful write."""
        with self._lock:
            if self.mixer.get(mix) == slots:
                return
            self.mixer[mix] = slots
            self.rb_ver += 1

    def _cache_auraverb(self, records):
        with self._lock:
            if self.auraverb.get(self.auraverb_index) == records:
                return
            self.auraverb[self.auraverb_index] = records
            self.rb_ver += 1

    def _routing_record_for_write(self, transport, dest):
        """Copy the serialized cache, querying once only if it is not ready."""
        with self._lock:
            cached = self.routing.get(dest)
            if cached is not None:
                return list(cached)
        req = proto.build_readback_query(self.profile, ROUTING_CAT, dest)
        data = transport.query(
            req,
            lambda x: proto.is_readback_response(self.profile, x, ROUTING_CAT, dest),
            timeout=1.5)
        if data is None:
            raise RuntimeError(f"no routing readback for dest {dest} -- not writing blind")
        _dest, pairs = proto.parse_routing_record(
            self.profile, proto.readback_body(self.profile, data))
        return list(pairs)

    def _set_mixer_source(self, transport, channel, target):
        """Change one global mixer-input slot across Zen Go's four mirrors.

        The device's 6/7/8/9 records are four serialized views of the same
        32-slot input map.  Read every group before writing any group so a
        missing readback never leaves the map partially changed.
        """
        self._set_routing_changes(
            transport, self.mixer_source_destinations, [(channel, target)])

    def _set_routing_changes(self, transport, destinations, changes):
        """Read, modify, and write one or more mirrored routing records."""
        destinations = [int(dest) for dest in destinations]
        if not destinations:
            raise RuntimeError("routing destination group is empty")
        if any(dest not in self.route_dests for dest in destinations):
            raise RuntimeError("routing destination is not safely mapped")
        strict = self.profile.get("mixer", {}).get("input_routing", {})
        if strict.get("strict") and set(destinations) & set(self.mixer_source_destinations):
            # Discrete 4 0xa270: the map is only captured as one write to all
            # of its mirrors, on the unlocked strips, with captured sources.
            if destinations != self.mixer_source_destinations:
                raise RuntimeError("the mixer source map must be written to all its groups")
            for channel, target in changes:
                if not 0 <= channel < int(strict["channels"]) \
                        or channel in strict.get("locked_channels", []) \
                        or target[0] in strict.get("display_only_source_banks", []):
                    raise RuntimeError("that strip or source is not captured for this device")
        records = []
        for dest in destinations:
            pairs = self._routing_record_for_write(transport, dest)
            for channel, target in changes:
                if channel < 0 or channel >= len(pairs):
                    raise RuntimeError(
                        f"routing destination {dest} has no slot {channel}")
                pairs[channel] = target
            records.append((dest, pairs))
        for dest, pairs in records:
            transport.write(proto.build_route_command(
                self.profile, dest, pairs))
            self._cache_routing(dest, pairs)

    def _set_routing_source(self, transport, dest, channel, target):
        """Write a logical routing destination, including its mirrors."""
        self._set_routing_changes(
            transport, self._routing_write_destinations(dest), [(channel, target)])

    def _set_routing_batch(self, transport, dest, changes):
        """Write a logical routing destination in one mirrored batch."""
        self._set_routing_changes(
            transport, self._routing_write_destinations(dest), changes)

    def _mixer_record_for_write(self, transport, mix):
        """Copy the serialized cache, querying once only if it is not ready."""
        with self._lock:
            cached = self.mixer.get(mix)
            if cached is not None:
                return [dict(slot) for slot in cached]
        if mix not in self.mixer_indices:
            raise RuntimeError(
                f"mixer readback index {mix} is not declared safe for this profile")
        req = proto.build_readback_query(self.profile, MIXER_CAT, mix)
        data = transport.query(
            req,
            lambda x: proto.is_readback_response(self.profile, x, MIXER_CAT, mix),
            timeout=1.5)
        if data is None:
            raise RuntimeError(f"no mixer readback for mix {mix} -- not writing blind")
        return proto.parse_mixer_record(
            self.profile, proto.readback_body(self.profile, data))

    def _auraverb_record_for_write(self, transport):
        """Return the full Gazelle Reverb readback, refusing a blind write."""
        with self._lock:
            cached = self.auraverb.get(self.auraverb_index)
            if cached is not None:
                return [{**record, "params": dict(record.get("params", {}))}
                        for record in cached]
        if not self.auraverb_available:
            raise RuntimeError("Gazelle Reverb readback is not safely mapped for this profile")
        req = proto.build_readback_query(
            self.profile, self.auraverb_category, self.auraverb_index)
        data = transport.query(
            req,
            lambda x: proto.is_readback_response(
                self.profile, x, self.auraverb_category, self.auraverb_index),
            timeout=1.5)
        if data is None:
            raise RuntimeError("no Gazelle Reverb readback -- not writing blind")
        return proto.parse_auraverb_record(
            self.profile, proto.readback_body(self.profile, data))

    def run(self):
        # An unexpected error (a parser bug, a short report) must not end the worker
        # silently with the page still showing "online": log it, go offline, reconnect.
        while not self._stop.is_set():
            try:
                return self._serve()
            except Exception:                                   # noqa: BLE001
                traceback.print_exc()
                with self._lock:
                    transport = self._transport
                self._drop_transport(transport)
                self._publish({"online": False})
                self._discard_queued()
                self._stop.wait(2.0)

    def _serve(self):
        transport = None
        readback_plan = []
        readback_changed = False
        last_debug_meter = 0.0
        while not self._stop.is_set():
            if transport is None:
                try:
                    transport = open_transport(self.vid, self.pid, self.report_size)
                except (SystemExit, OSError):
                    self._publish({"online": False})
                    self._discard_queued()
                    if self._stop.wait(2.0):
                        break
                    continue
                if self._stop.is_set():
                    self._drop_transport(transport)
                    transport = None
                    break
                # Also covers the retry wait and a transport dropped mid-command.
                self._discard_queued()
                with self._lock:
                    self._transport = transport
                    self.connection_generation += 1
                    # AFX inventory and link flags belong to this connection.
                    self.structured = {key: value for key, value in self.structured.items()
                                       if key[0] != 0x19 and key != (0x0b, 4)}
                    self.rb_ver += 1
                last_readback = time.time()
                readback_plan = self._readback_plan()
                readback_changed = False

            # 1. Run at most one queued command. Draining the queue here used
            # to freeze meters for the duration of every pending HID write.
            try:
                fn = self.cmds.get_nowait()
                if fn is None:
                    break
                try:
                    fn(transport)
                except OSError:
                    self._drop_transport(transport)
                    transport = None
                except Exception as e:                           # noqa: BLE001
                    print(f"[cmd] {e!r}", file=sys.stderr)
            except queue.Empty:
                pass
            if transport is None:
                self._publish({"online": False})
                continue

            # 2. Prepare a bounded slow-state sweep. Only one record is read
            # after each fast-state publish below, keeping meter cadence live.
            now = time.time()
            if not readback_plan and now - last_readback > READBACK_REFRESH_S:
                readback_plan = self._readback_plan()
                readback_changed = False
                last_readback = now

            # 3. Fast state. Visible meters live in 0x73. Sample the legacy
            # 0x75 diagnostic bank only twice a second; polling it every loop
            # added avoidable latency without improving any visible meter.
            try:
                state = transport.read_one(self.state_magic, timeout=0.4)
                meter = None
                monotonic_now = time.monotonic()
                if monotonic_now - last_debug_meter >= 0.5:
                    meter = self._read_meter(transport, timeout=0.01)
                    last_debug_meter = monotonic_now
            except OSError:
                self._drop_transport(transport)
                transport = None
                self._publish({"online": False})
                continue

            snap = {"online": True, "ts": time.time()}
            if state:
                snap["channels"] = self._parse_channels(state)
                snap["buses"] = self._parse_buses(state)
                snap["adat"] = self._parse_adat(state)
                snap["spdif"] = self._parse_spdif(state)
                snap["trim"] = self._parse_output_trim(state)
                try:
                    snap["brightness"] = proto.parse_state_scalar(
                        self.profile, state, "screen_brightness_byte_offset")
                except Exception:
                    pass
                try:
                    snap["sample_rate_idx"] = proto.parse_state_scalar(
                        self.profile, state, "sample_rate_byte_offset")
                except Exception:
                    pass
                try:
                    snap["oscillator"] = proto.parse_state_scalar(
                        self.profile, state, "oscillator_byte_offset")
                except Exception:
                    pass
                try:
                    snap["clock_source_idx"] = proto.parse_state_scalar(
                        self.profile, state, "clock_source_byte_offset")
                except Exception:
                    pass
                # Profile-declared physical/preamp meter bank (when available).
                snap["input_meters"] = self._parse_meters(state)
                # Profile-declared selected mixer surface/meter bank.
                mixer_meters = self._parse_mixer_meters(state)
                if mixer_meters is not None:
                    snap["mixer_meters"] = mixer_meters
                output_meters = self._parse_output_meters(state)
                if output_meters is not None:
                    snap["output_meters"] = output_meters
                # Raw slice covering the profile's mixer/preamp meter region
                # for ?meterdebug=1; it remains diagnostic only.
                snap["state_raw"] = {"base": 150, "bytes": list(state[150:240])}
            if meter:
                # 0x75 @32/@48 and @33/@49 are route-correlated lane pairs.
                # Their fixed ownership is unresolved; retain raw debug only.
                base = self.profile["frame"]["meter_report"].get(
                    "channel_meter_base_offset")
                # Some profiles identify a meter report without having a
                # confirmed channel offset yet (Zen Go). Keep the report
                # available to future mapping work, but never crash the
                # device thread by treating an unknown offset as Orion's.
                if base is not None:
                    base = int(base)
                    snap["meters_raw"] = {"base": base,
                                          "bytes": list(meter[base:base + 32])}
            # keep last values if this cycle only got one of the two frames
            with self._lock:
                for k in ("channels", "buses", "adat", "spdif", "trim", "brightness",
                          "sample_rate_idx", "clock_source_idx", "oscillator", "input_meters",
                          "mixer_meters", "output_meters", "state_raw",
                          "meters_raw"):
                    if k not in snap and k in self.snapshot:
                        snap[k] = self.snapshot[k]
            self._publish(snap)

            # 4. One slow readback record per meter cycle. Changes are
            # announced together when the sweep completes, avoiding a browser
            # refetch after every individual routing destination.
            if readback_plan:
                category, index = readback_plan.pop(0)
                try:
                    readback_changed = self._refresh_readback_one(
                        transport, category, index) or readback_changed
                except OSError:
                    self._drop_transport(transport)
                    transport = None
                    self._publish({"online": False})
                    continue
                if not readback_plan and readback_changed:
                    with self._lock:
                        self.rb_ver += 1
        self._drop_transport(transport)
        self._publish({"online": False})

    # -- readback (routing + mixer + surround + structured state) ------------

    def _readback_plan(self):
        routes = [(ROUTING_CAT, d) for d in self.route_dests] \
            if self.routing_available else []
        mixes = [(MIXER_CAT, m) for m in self.mixer_indices
                 if 0 <= m < self.n_mixes] \
            if self.mixer_available else []
        auraverb = [(self.auraverb_category, self.auraverb_index)] \
            if self.auraverb_available else []
        surround = ([(SURROUND_GLOBAL_CAT, 0)]
                    + [(SURROUND_EQ_CAT, index)
                       for index in range(self.surround_speaker_count)]) \
            if self.surround_available else []
        structured = list(_structured_readback_targets(self.profile))
        return routes + mixes + auraverb + surround + structured

    def _refresh_readback_one(self, transport, category, index):
        """Read one bounded record; return changed, or None without a valid response."""
        try:
            req = proto.build_readback_query(self.profile, category, index)
        except proto.ConstraintError:
            return None
        data = transport.query(
            req,
            lambda x: proto.is_readback_response(self.profile, x, category, index),
            timeout=0.1)
        if data is None:
            return None
        try:
            body = proto.readback_body(self.profile, data)
            if category == SURROUND_GLOBAL_CAT:
                value = proto.parse_surround_global_record(self.profile, body)
                with self._lock:
                    changed = self.surround_global != value
                    self.surround_global = value
                    self.surround_global_raw = bytes(body)
                    return changed
            if category == SURROUND_EQ_CAT:
                value = proto.parse_surround_speaker_eq_record(self.profile, body)
                with self._lock:
                    changed = self.surround_speakers.get(index) != value
                    self.surround_speakers[index] = value
                    return changed
            layout = proto.readback_record_layout(self.profile, category, index)
            if layout is not None:
                value = proto.parse_readback_records(
                    self.profile, body, category, index,
                    kind=layout.get("kind"))
                cache = self.structured
                cache_key = (category, index)
            elif category == ROUTING_CAT:
                _dest, value = proto.parse_routing_record(self.profile, body)
                cache = self.routing
                cache_key = index
            elif category == MIXER_CAT:
                value = proto.parse_mixer_record(self.profile, body)
                cache = self.mixer
                cache_key = index
            elif category == self.auraverb_category:
                value = proto.parse_auraverb_record(self.profile, body)
                cache = self.auraverb
                cache_key = index
            else:
                return None
        except (TypeError, ValueError):
            return None
        with self._lock:
            if cache.get(cache_key) == value:
                return False
            cache[cache_key] = value
            return True

    def _read_meter(self, transport, timeout=0.03):
        """Read a meter frame, filtering readbacks only where they share the
        meter report magic (as they do on the Orion)."""
        for data in transport.read_reports(self.meter_magic, timeout):
            if not self.meter_shares_readback_magic or (
                    len(data) > METER_DISCRIMINATOR_OFFSET
                    and data[METER_DISCRIMINATOR_OFFSET] == METER_DISCRIMINATOR):
                return data
        return None

    # -- fast-state parsers ------------------------------------------------

    def _parse_channels(self, data):
        out = []
        for ch in range(self.n_ch):
            try:
                s = proto.parse_state(self.profile, data, ch)
            except ValueError:
                break
            out.append({
                "ch": ch,
                "gain": s["gain"],
                "mode": proto.mode_name(self.profile, s.get("input_mode", -1)),
                "phantom": bool(s.get("phantom")),
                "phase_invert": bool(s.get("phase_invert")),
                # state fields this profile cannot read: the page keeps the
                # last value it sent instead of showing a made-up "off"
                "unread": [k for k in ("phantom", "phase_invert") if k not in s],
            })
        return out

    def _parse_buses(self, data):
        out = []
        for bid in self.bus_ids:
            try:
                b = proto.parse_bus_state(self.profile, data, bid)
            except ValueError:
                continue
            out.append({
                "bus": bid,
                "name": proto.bus_key(self.profile, bid),
                "label": proto.bus_name(self.profile, bid),
                "level": b["level"],
                "dim": bool(b.get("dim")),
                "mute": bool(b.get("mute")),
                "mute_ambiguous": bool(b.get("mute_ambiguous")),
                "mono": bool(b.get("mono")),
            })
        return out

    def _parse_adat(self, data):
        """The 16 ADAT input gains (int8 dB) from the 0x73 state report at
        state_report.adat_gain_base_offset + channel. ADAT has gain + link
        only -- no mode / phantom / phase / meter. See profile["adat"]."""
        out = []
        for ch in range(self.n_adat):
            try:
                out.append({"ch": ch,
                            "gain": proto.parse_adat_gain(self.profile, data, ch)})
            except ValueError:
                break
        return out

    def _parse_spdif(self, data):
        """The 2 S/PDIF input gains (0 = L, 1 = R), int8 dB, from
        state_report.spdif_gain_base_offset + channel. Gain + link only."""
        out = []
        for ch in range(self.n_spdif):
            try:
                out.append({"ch": ch,
                            "gain": proto.parse_spdif_gain(self.profile, data, ch)})
            except ValueError:
                break
        return out

    def _parse_output_trim(self, data):
        """The 3 settings-tab output-trim selectors (0-6) from the packed
        state_report.output_trim_block: target 0 = Monitor A, 1 = Monitor B,
        2 = Line Out. Physical meaning of the 7 steps is not decoded -- raw
        index only (see profile params.output_trim)."""
        blk = self.profile["frame"]["state_report"].get("output_trim_block")
        if not blk:
            return {}
        out = {}
        names = {0: "monitor_a", 1: "monitor_b", 2: "line_out"}
        for key, f in blk.get("fields", {}).items():
            off = int(f["byte_offset"])
            if off >= len(data):
                continue
            tgt = int(key.rsplit("_", 1)[1])
            mask = int(f["mask"], 16) if isinstance(f["mask"], str) else int(f["mask"])
            out[names.get(tgt, str(tgt))] = (data[off] & mask) >> int(f["shift"])
        return out

    def _parse_meters(self, state):
        """Return source-aware samples from the configured 0x73 meter bank.

        A normal profile uses ``channel_meter_base_offset``.  Zen Go has no
        decoded contiguous physical-input bank yet, but its profile records
        two observed candidate lanes at payload 0xce/0xcf.  Expose those as
        raw-only meters instead of silently returning an empty array.
        """
        source = "state_report"
        spec = self.profile["frame"][source]
        base = spec.get("channel_meter_base_offset")
        candidates = None
        if base is None:
            candidates = {
                int(item["input_index"]): item
                for item in spec.get("candidate_preamp_meters", []) or []
                if isinstance(item, dict) and item.get("input_index") is not None
            }
            if not candidates:
                return []
        out = []
        for ch in range(self.n_ch):
            mapping = None
            if base is not None:
                off = proto._as_int(base) + ch
                raw_range = spec.get("physical_meter_raw_range")
                direction = spec.get("physical_meter_direction")
                silence_raw = (proto._as_int(raw_range[1])
                               if direction == "inverted"
                               and isinstance(raw_range, list) and len(raw_range) == 2
                               else None)
                noise_floor_raw = None
            else:
                mapping = candidates.get(ch)
                if mapping is None:
                    break
                try:
                    offset_key = ("offset" if mapping.get("offset") is not None
                                  else "payload_offset")
                    off = _state_report_offset(
                        self.profile, spec, mapping, offset_key,
                        "snapshot_payload")
                    raw_range = mapping.get("raw_range", [0, 96])
                    raw_lo, raw_hi = (proto._as_int(raw_range[0]),
                                      proto._as_int(raw_range[1]))
                    direction = mapping.get("direction", "inverted")
                    silence_raw = proto._as_int(
                        mapping.get("silence_raw", raw_hi))
                    noise_floor_raw = (proto._as_int(mapping["noise_floor_raw"])
                                       if mapping.get("noise_floor_raw") is not None
                                       else None)
                except (KeyError, TypeError, ValueError, IndexError):
                    break
            if off >= len(state):
                break
            raw = state[off]
            db = proto.raw_to_db(self.profile, raw, source)
            led = proto.meter_led(self.profile, db, source)
            out.append({
                "raw": raw,
                "db": round(db, 1) if db is not None else None,
                "clip": led["clip"] if led is not None else None,
                "silence": _meter_is_silent(
                    raw, silence_raw, noise_floor_raw, direction)
                    if silence_raw is not None or noise_floor_raw is not None
                    else None,
            })
        return out

    def _parse_mixer_meters(self, state):
        """Return the profile-declared virtual-mixer strip meter bank.

        Orion profiles use ``mixer_window_selection`` plus a
        ``meter_mappings`` entry. Zen Go uses the TUI-confirmed
        ``mixer_strip_meters`` lanes and its profile-defined surface selector.
        Both paths return the same browser-facing shape.
        """
        spec = self.profile["frame"].get("state_report", {})
        selection = spec.get("mixer_window_selection", {})
        mapping = next((m for m in spec.get("meter_mappings", [])
                        if m.get("target") == "mixer_window_strip"), None)
        if selection and mapping:
            try:
                selector_off = proto._as_int(selection.get("state_byte_offset", 122))
                base = proto._as_int(mapping.get("payload_offset_base", 157))
                strip_range = mapping.get("strip_index_range", [1, 32])
                raw_range = mapping.get("raw_range", [0, 96])
                raw_lo, raw_hi = (proto._as_int(raw_range[0]),
                                  proto._as_int(raw_range[1]))
                silence_raw = proto._as_int(mapping.get("silence_raw", raw_hi))
                noise_floor_raw = (proto._as_int(mapping["noise_floor_raw"])
                                   if mapping.get("noise_floor_raw") is not None
                                   else None)
                direction = mapping.get("direction", "inverted")
            except (KeyError, TypeError, ValueError, IndexError):
                return None
            if selector_off < 0 or selector_off >= len(state) or len(strip_range) != 2:
                return None
            selected = state[selector_off]
            allowed = {proto._as_int(v) for v in selection.get("values", [])}
            if allowed and selected not in allowed:
                return {"mix": selected, "strips": [],
                        "raw_range": [raw_lo, raw_hi],
                        "silence_raw": silence_raw,
                        "noise_floor_raw": noise_floor_raw}
            first, last = proto._as_int(strip_range[0]), proto._as_int(strip_range[1])
            strips = []
            for ch in range(first, last + 1):
                off = base + (ch - first)
                if off < 0 or off >= len(state):
                    break
                raw = state[off]
                strips.append({"ch": ch, "raw": raw,
                               "silence": _meter_is_silent(
                                   raw, silence_raw, noise_floor_raw, direction)})
            return {"mix": selected, "strips": strips,
                    "raw_range": [raw_lo, raw_hi],
                    "silence_raw": silence_raw,
                    "noise_floor_raw": noise_floor_raw}

        # Zen Go: the selected front-panel surface gates the 16 shared
        # mixer-strip lanes at the profile's full-report base offset.
        mapping = spec.get("mixer_strip_meters")
        surface = _mixer_surface_spec(self.profile)
        if not isinstance(mapping, dict) or surface is None:
            return None
        try:
            selector_off = proto._as_int(surface.get("state_byte_offset", 122))
            base = mapping.get("full_report_base_offset")
            if base is None:
                base = (proto._as_int(mapping["payload_base_offset"])
                        + proto._as_int(spec.get("snapshot_payload_offset", 16)))
            else:
                base = proto._as_int(base)
            count = proto._as_int(mapping.get("count", 0))
            stride = proto._as_int(mapping.get("stride", 1))
            raw_range = mapping.get("raw_range", [0, 96])
            raw_lo, raw_hi = (proto._as_int(raw_range[0]),
                              proto._as_int(raw_range[1]))
            silence_raw = proto._as_int(mapping.get("silence_raw", raw_hi))
            noise_floor_raw = (proto._as_int(mapping["noise_floor_raw"])
                               if mapping.get("noise_floor_raw") is not None
                               else None)
            direction = mapping.get("direction", "inverted")
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        if selector_off < 0 or selector_off >= len(state) or count <= 0 or stride <= 0:
            return None
        selected = state[selector_off]
        mix = _mixer_surface_for_value(self.profile, selected)
        if mix is None:
            return {"mix": -1, "selector": selected, "strips": [],
                    "raw_range": [raw_lo, raw_hi],
                    "silence_raw": silence_raw,
                    "noise_floor_raw": noise_floor_raw}
        strips = []
        for channel in range(count):
            off = base + channel * stride
            if off < 0 or off >= len(state):
                break
            raw = state[off]
            strips.append({"ch": channel, "raw": raw,
                           "silence": _meter_is_silent(
                               raw, silence_raw, noise_floor_raw, direction)})
        return {"mix": mix, "selector": selected, "strips": strips,
                "raw_range": [raw_lo, raw_hi],
                "silence_raw": silence_raw,
                "noise_floor_raw": noise_floor_raw}

    def _parse_output_meters(self, state):
        """Parse profile-declared physical-output lanes from the 0x73 state.

        Zen Go's six lanes are observed/provisional rather than calibrated,
        but they are useful in the WebUI and are kept explicitly tagged as
        such.  Profiles without these mappings simply return no output meter
        payload.
        """
        spec = self.profile["frame"].get("state_report", {})
        mappings = [item for item in spec.get("meter_mappings", []) or []
                    if isinstance(item, dict)
                    and item.get("target") == "physical_output"]
        if not mappings:
            return None
        outputs = {}
        ranges = []
        for mapping in mappings:
            try:
                target = proto._as_int(mapping["target_index"])
                lane = proto._as_int(mapping["lane"])
                offset = _state_report_offset(
                    self.profile, spec, mapping, "payload_offset",
                    "snapshot_payload")
                raw_range = mapping.get("raw_range", [0, 96])
                raw_lo, raw_hi = (proto._as_int(raw_range[0]),
                                  proto._as_int(raw_range[1]))
                silence_raw = proto._as_int(
                    mapping.get("silence_raw", raw_hi))
                noise_floor_raw = (proto._as_int(mapping["noise_floor_raw"])
                                   if mapping.get("noise_floor_raw") is not None
                                   else None)
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if offset < 0 or offset >= len(state) or lane < 0:
                continue
            raw = state[offset]
            output = outputs.setdefault(target, {
                "bus": target,
                "name": proto.bus_key(self.profile, target),
                "label": proto.bus_name(self.profile, target),
                "status": mapping.get("status", ""),
                "lanes": [],
            })
            while len(output["lanes"]) <= lane:
                output["lanes"].append(None)
            output["lanes"][lane] = {
                "lane": lane,
                "raw": raw,
                "silence": _meter_is_silent(
                    raw, silence_raw, noise_floor_raw,
                    mapping.get("direction", "inverted")),
            }
            ranges.append((raw_lo, raw_hi))
        if not outputs:
            return None
        raw_range = list(ranges[0]) if ranges else [0, 96]
        return {
            "outputs": [outputs[key] for key in sorted(outputs)],
            "raw_range": raw_range,
            "silence_raw": raw_range[1],
            "provisional": True,
        }

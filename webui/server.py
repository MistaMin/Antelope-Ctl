#!/usr/bin/env python3
"""
antelope-ctl web UI.

A thin local daemon: one background thread owns the HID device, keeps an
in-memory state snapshot, and pushes it to the browser over SSE.
Commands from the browser are queued and executed by that same thread, so
there is exactly one writer/reader on the node.

Two kinds of state:

  * FAST  -- the free-running 0x73 state report, including visible meters,
             parsed every loop and streamed at ~25 Hz over SSE (channels,
             buses, brightness, meters). The 0x75 bank is retained only for
             lower-rate diagnostics.
  * SLOW  -- routing (readback cat 0x03), virtual mixer (cat 0x04),
             profile-confirmed Gazelle Reverb and Surround state, and
             profile-declared nested readback records, refreshed incrementally
             on connect and on a slow timer. Writes use and update their
             serialized caches, querying first if not populated yet. The
             snapshot carries a monotonic `rb_ver`; the browser refetches the
             slow APIs when it bumps.

Readback queries use protocol.build_readback_query and the active profile's
authorized bounds. See PROTOCOL.md §4a for the firmware behavior and AGENTS.md
for device-transaction instructions.

Run:  pip install -r requirements.txt  &&  python3 server.py
Then: http://127.0.0.1:8714
"""
import asyncio
import glob
import json
import math
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from antelope import protocol as proto
from antelope.catalog import load_catalog
from antelope.transport import list_connected_hid
from webui.device_ui import features_for
from webui.afx_catalog import preview_catalog, effect_choices
from webui.afx_test import MemoryCatTest
from webui.afx_inserts import InsertRack
from webui import device as device_core
from webui.device import (  # noqa: F401  (some are used by tests through this module)
    _mixer_surface_packet,
    _surround_global_contract,
    _structured_readback_targets,
    STRUCTURED_READBACK_SAFE_STATUSES,
)

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StrictFloat, StrictInt
import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, "..", "profiles")
DEFAULT_PROFILE = os.path.join(PROFILE_DIR, "orion_studio_sc.json")


def _profile_device_ids(path):
    """(vid, pid) as ints from a profile's device block, or None if absent."""
    try:
        with open(path) as f:
            dev = json.load(f).get("device", {})
    except (OSError, ValueError):
        return None
    vid, pid = dev.get("vid"), dev.get("pid")
    if vid is None or pid is None:
        return None
    try:
        return (int(vid, 16) if isinstance(vid, str) else int(vid),
                int(pid, 16) if isinstance(pid, str) else int(pid))
    except (TypeError, ValueError):
        return None


def resolve_profile():
    """Pick the device profile. ANTELOPE_PROFILE (path or profiles/ basename)
    always wins. Otherwise match a connected HID node against profiles/*.json
    by device vid/pid; fall back to the Orion Studio III profile when nothing
    is plugged in, nothing matches, or the host can't be scanned."""
    env = os.environ.get("ANTELOPE_PROFILE")
    if env:
        if not os.path.exists(env):
            cand = os.path.join(PROFILE_DIR, env if env.endswith(".json") else env + ".json")
            if os.path.exists(cand):
                return cand, "ANTELOPE_PROFILE"
        return env, "ANTELOPE_PROFILE"

    connected = list_connected_hid()
    if connected:
        for path in sorted(glob.glob(os.path.join(PROFILE_DIR, "*.json"))):
            ids = _profile_device_ids(path)
            if ids and ids in connected:
                return path, "autodetected"
    return DEFAULT_PROFILE, "default (no match)" if connected else "default (no scan)"


PROFILE_PATH, _profile_why = resolve_profile()
PROFILE = proto.load_profile(PROFILE_PATH)
UI_FEATURES = features_for(PROFILE_PATH, PROFILE)
print(f"[antelope-ctl] profile: {os.path.basename(PROFILE_PATH)} "
      f"-- {PROFILE.get('device', {}).get('name', '?')} ({_profile_why})",
      file=sys.stderr, flush=True)

def _routing_ui_spec(dest):
    """Return the presentation spec for a logical routing destination."""
    feature = UI_FEATURES.get("routing", {})
    if not isinstance(feature, dict) or not feature.get("enabled"):
        return None
    for item in feature.get("destinations", []) or []:
        try:
            if proto._as_int(item.get("id")) == int(dest):
                return item
        except (AttributeError, TypeError, ValueError):
            continue
    return None


def _routing_ui_destinations():
    """Return logical destinations, or None when the shared raw view applies."""
    feature = UI_FEATURES.get("routing", {})
    if not isinstance(feature, dict) or not feature.get("enabled"):
        return None
    # An absent/empty logical list means the feature wants the profile's raw
    # addressable destination map.  A non-empty list is a logical view such as
    # Zen Go's four mirrored records represented as one mixer-input map.
    destinations = feature.get("destinations")
    return list(destinations) if isinstance(destinations, list) and destinations else None


def _routing_channel_count(profile, dest):
    """Channel count for a UI destination, falling back to the wire map."""
    item = _routing_ui_spec(dest)
    if item is not None:
        return int(item["channels"])
    return proto.route_dest_channels(profile, dest)


def _routing_refusal(dest, channel, target):
    """Why this change to a UI destination is refused before it is queued, or None:
    a source the profile does not list for one of its groups, or a locked mixer strip."""
    groups = _routing_write_destinations(dest)
    strict = PROFILE.get("mixer", {}).get("input_routing", {})
    if strict.get("strict") and set(groups) & set(strict.get("destinations", [])) \
            and channel in strict.get("locked_channels", []):
        return "that strip takes its source from another routing group on this device"
    for group in groups:
        refusal = proto.route_source_refusal(PROFILE, group, *target)
        if refusal:
            return refusal
    return None


def _routing_write_destinations(dest):
    """Physical records that implement one logical UI destination."""
    item = _routing_ui_spec(dest)
    if item is None:
        return [int(dest)]
    return [int(value) for value in item.get("write_destinations", [dest])]


class Device(device_core.Device):
    """The device actor with this server's logical routing view (UI_FEATURES)."""

    def _routing_write_destinations(self, dest):
        return _routing_write_destinations(dest)


try:
    DEV = Device(PROFILE)
except (KeyError, ValueError, TypeError) as _e:
    # Device.__init__ only reads the profile dict (no I/O). Many non-Orion
    # profiles are still stubs -- an autodetected one that can't build a
    # Device would crash the daemon, so fall back to the default and let the
    # user finish that JSON. An explicit ANTELOPE_PROFILE is left to fail.
    if _profile_why != "autodetected" or os.path.abspath(PROFILE_PATH) == os.path.abspath(DEFAULT_PROFILE):
        raise
    print(f"[antelope-ctl] {os.path.basename(PROFILE_PATH)} is incomplete "
          f"({type(_e).__name__}: {_e}) -- falling back to the default profile; "
          f"finish that JSON to use the detected device",
          file=sys.stderr, flush=True)
    PROFILE_PATH, _profile_why = DEFAULT_PROFILE, "default (detected profile incomplete)"
    PROFILE = proto.load_profile(PROFILE_PATH)
    DEV = Device(PROFILE)

AFX_TEST = MemoryCatTest(DEV)
AFX_INSERTS = InsertRack(DEV)

# ---------------------------------------------------------------- HTTP / SSE

app = FastAPI(title="antelope-ctl webui")
# A page on another site can point its own name at 127.0.0.1 (DNS rebinding);
# the Host header check (port ignored) refuses those requests with 400.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
@app.exception_handler(proto.ConstraintError)
def _constraint_refused(request, error):
    """A profile bound refused the request: that is the caller's error, not a server fault."""
    return JSONResponse({"ok": False, "error": str(error)}, status_code=400)


app.mount("/webui/assets", StaticFiles(directory=os.path.join(HERE, "assets")),
          name="webui-assets")
app.mount("/webui/static", StaticFiles(directory=os.path.join(HERE, "static")),
          name="webui-static")


class Gain(BaseModel):
    channel: int
    db: int

class Toggle(BaseModel):
    channel: int
    param: str          # "phantom" | "phase_invert"
    on: bool

class Mode(BaseModel):
    channel: int
    mode: str           # "mic" | "line" | "hiz" | "direct"

class Link(BaseModel):
    pair: int           # 0-based, pair N = channels 2N & 2N+1
    enabled: bool

class DigGain(BaseModel):
    channel: int        # ADAT 0-15 / S-PDIF 0=L 1=R
    db: int

class DigLink(BaseModel):
    pair: int           # ADAT: 0-7 (ch 2N & 2N+1). S-PDIF: only 0 (L+R)
    enabled: bool

class Trim(BaseModel):
    target: int         # Orion: 0 = Monitor A, 1 = Monitor B, 2 = Line Out. Zen Go: 0 = Monitor
    value: int          # 0..6

class LineTrim(BaseModel):
    linked: bool
    trims: list[int]    # Line Out 1-4, 0..6 each (Discrete 4)

class BusToggle(BaseModel):
    bus: int
    param: str          # "dim" | "mute" | "mono"
    on: bool

class GlobalToggle(BaseModel):
    on: bool

class EmuMic(BaseModel):
    channel: int
    enabled: bool
    pattern: int | None = None   # model 0: 0..100 morph (0 omni / 50 card / 100 fig-8)
    model: int | None = None     # 0 = EdgeDuo raw
    swap: bool | None = None

class Scalar(BaseModel):
    value: int

class MixerSelect(BaseModel):
    mix: int                  # 0-based mix index; profile may have no selector

class MixerSource(BaseModel):
    mix: int                  # 0-based mixer tab containing the selector
    channel: int              # 0-based mixer input strip
    source: str               # profile-provided source option key

class Bus(BaseModel):
    bus: int
    level: int

class Route(BaseModel):
    dest: int
    channel: int              # 0-based output channel within the group
    kind: str                 # "preamp" | "compplay" | ... | "mix1".."mix4" | "spdif" | "mute"
    number: int | str | None = None   # 1-based for numbered banks, "L"/"R" for stereo

class RouteChange(BaseModel):
    channel: int
    kind: str
    number: int | str | None = None

class RouteBatch(BaseModel):
    dest: int
    changes: list[RouteChange]

class MixStrip(BaseModel):
    mix: int                  # 0-based
    channel: int              # profile-defined slot (Orion master is slot 0)
    fader: int | None = None  # profile-defined dB attenuation (sign ignored)
    pan: int | None = None    # profile-defined panorama range
    send: int | None = None   # profile-defined Gazelle Reverb send range
    send_pan: int | None = None   # Zen Go: the send has its own pan
    mute: bool | None = None
    solo: bool | None = None

class MixSolo(BaseModel):
    mix: int
    channel: int
    on: bool


class AuraVerbChange(BaseModel):
    mix: int = 0
    param: str | None = None
    value: int | None = None
    enabled: bool | None = None


class ReverbReturn(BaseModel):
    bus: int                  # 0 = Monitor/HP1, 1 = HP2 on the Zen Go
    level: int                # raw attenuation
    mute: bool


class SurroundGlobalChange(BaseModel):
    format: str | None = None
    delay_ms: float | None = None
    level_db: float | None = None
    eq_position: str | None = None


class SurroundEQChange(BaseModel):
    speaker: int
    band: int                  # 0-based EQ band index
    parameter: str             # frequency | q | gain | mode
    value: float


class SurroundEQPointChange(BaseModel):
    """The two EQ coordinates changed together by graph-point dragging."""
    speaker: int
    band: int                  # 0-based EQ band index
    frequency: float
    gain: float


class SurroundBassChange(BaseModel):
    channel: int               # 0-based Bass Management block slot
    field: str                 # one profile-declared block/global field
    value: float | int | bool | str


class SurroundSpeakerChange(BaseModel):
    speaker: int               # 0-based speaker/readback index
    field: str                 # delay_ms | level_db | phase_invert | bypass
    value: float | int | bool


class SurroundEQReset(BaseModel):
    speaker: int               # 0-based currently displayed speaker


class MixerLink(BaseModel):
    """A virtual-mixer stereo pair scoped to one mix."""
    pair: int
    enabled: bool
    mix: int = 0


def _bad(msg):
    return JSONResponse({"ok": False, "error": msg}, status_code=400)


def _surround_raw_value(value, *, step, zero, bounds, label):
    """Convert a display-unit surround value to its profile-defined raw step."""
    try:
        display = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a finite number")
    if not math.isfinite(display):
        raise ValueError(f"{label} must be a finite number")
    raw_float = display / step + zero
    raw = int(round(raw_float))
    if not math.isclose(raw_float, raw, abs_tol=1e-6):
        raise ValueError(f"{label} must use {step:g}-unit steps")
    lo, hi = (int(item) for item in bounds)
    if not lo <= raw <= hi:
        low = (lo - zero) * step
        high = (hi - zero) * step
        raise ValueError(f"{label} outside {low:g}..{high:g}")
    return raw


def _surround_eq_raw_value(profile, parameter, value):
    """Convert one UI EQ value to the profile-defined raw field value."""
    contract = profile.get("runtime_contracts", {}).get(
        "surround_speaker_eq", {}) or {}
    try:
        display = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{parameter} must be a finite number")
    if not math.isfinite(display):
        raise ValueError(f"{parameter} must be a finite number")

    if parameter == "frequency":
        raw_float = display
        field = "frequency"
        bounds = contract["frequency_range"]
        label = "frequency"
    elif parameter == "q":
        raw_float = display * 100
        field = "q_raw"
        bounds = contract["q_raw_range"]
        label = "Q"
    elif parameter == "gain":
        raw_float = display * 100
        field = "gain_raw"
        bounds = contract["gain_raw_range"]
        label = "gain"
    elif parameter == "mode":
        raw_float = display
        field = "mode"
        bounds = contract.get("mode_range", [0, 255])
        label = "mode"
    else:
        raise ValueError(
            "parameter must be one of frequency, q, gain, or mode")

    raw = int(round(raw_float))
    if not math.isclose(raw_float, raw, abs_tol=1e-6):
        raise ValueError(f"{label} has too many decimal places")
    lo, hi = (int(item) for item in bounds)
    if not lo <= raw <= hi:
        if parameter == "q":
            low, high = lo / 100, hi / 100
        elif parameter == "gain":
            low, high = lo / 100, hi / 100
        else:
            low, high = lo, hi
        raise ValueError(f"{label} outside {low:g}..{high:g}")
    return raw, field


def _surround_eq_reset_values(profile):
    """Return the profile-defined reset preset in raw wire units."""
    contract = profile.get("runtime_contracts", {}).get(
        "surround_speaker_eq", {}) or {}
    preset = contract.get("reset_preset", {}) or {}
    frequencies = preset.get("frequency_hz")
    if not isinstance(frequencies, list):
        raise ValueError("profile has no surround EQ reset frequency preset")
    band_count = int(contract.get("band_count", 0))
    if len(frequencies) != band_count:
        raise ValueError(
            f"surround EQ reset needs {band_count} frequencies, "
            f"got {len(frequencies)}")
    raw_frequencies = [
        _surround_eq_raw_value(profile, "frequency", value)[0]
        for value in frequencies
    ]
    q_raw = _surround_eq_raw_value(profile, "q", preset["q"])[0]
    gain_raw = _surround_eq_raw_value(profile, "gain", preset["gain_db"])[0]
    return raw_frequencies, q_raw, gain_raw


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/profile")
def api_profile():
    return {**PROFILE, "webui": {
        "routing": DEV.routing_available,
        "mixer": DEV.mixer_available,
        "surround": DEV.surround_available,
        "mixes": DEV.n_mixes if DEV.mixer_available else 0,
        "mix_channels": DEV.mix_channels if DEV.mixer_available else 0,
        "features": UI_FEATURES,
    }}


_MIC_MODELS_PATH = os.path.join(os.path.dirname(PROFILE_PATH), "mic_models.json")

@app.get("/api/mic_models")
def api_mic_models():
    """The emuMic model catalogue (profiles/mic_models.json). Account-bound --
    it's one account's snapshot, and the id->model mapping may not match the
    user's own pack set. Returns {} if the file isn't present."""
    try:
        with open(_MIC_MODELS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@app.get("/api/state")
def api_state():
    snap, ver = DEV.get()
    return {"version": ver, **snap}


@app.get("/api/readbacks")
def api_readbacks():
    """Profile-driven nested readback state for the diagnostics panel.

    This is intentionally read-only.  The device thread polls only layouts
    with capture-confirmed outer indices; schema-only layouts are returned so
    the UI can explain why a new capture is still required.
    """
    return DEV.structured_readbacks_json()


@app.get("/api/afx/catalog")
def api_afx_catalog():
    """Local control previews, independent of loaded slots or device ownership."""
    if PROFILE.get("afx", {}).get("catalog") != "afx_effects.json":
        return preview_catalog({}, os.path.basename(PROFILE_PATH))
    try:
        catalog = load_catalog(os.path.join(PROFILE_DIR, "afx_effects.json"))
    except (OSError, ValueError):
        catalog = {}
    return preview_catalog(catalog, os.path.basename(PROFILE_PATH))


class InsertChange(BaseModel):
    channel: StrictInt
    operation: str            # load | remove | move
    slot: StrictInt | None = None
    type: StrictInt | None = None
    source: StrictInt | None = None   # move: the slot the effect comes from


@app.get("/api/afx-inserts")
def api_afx_inserts():
    """Insert racks read from the device, plus what this profile may edit."""
    return AFX_INSERTS.state()


@app.post("/api/afx-inserts")
def api_afx_inserts_change(change: InsertChange):
    """Load, remove or move one effect: whole-rack write, verified by readback."""
    try:
        return AFX_INSERTS.change(change.channel, change.operation,
                                  slot=change.slot, type_id=change.type,
                                  source=change.source)
    except (ValueError, RuntimeError, KeyError, proto.ConstraintError) as error:
        return _bad(str(error))


class InsertParameter(BaseModel):
    channel: StrictInt
    slot: StrictInt
    key: str
    value: StrictInt | StrictFloat   # float only for an f32 field; the codec refuses it elsewhere


@app.get("/api/afx-inserts/parameters")
def api_afx_insert_parameters(channel: int, slot: int):
    """Mapped settings of the effect in one rack slot, read from the device."""
    try:
        return AFX_INSERTS.parameters(channel, slot)
    except (ValueError, RuntimeError, KeyError, proto.ConstraintError) as error:
        return _bad(str(error))


@app.post("/api/afx-inserts/parameters")
def api_afx_insert_parameter_set(change: InsertParameter):
    """Change one mapped setting: whole-block write, verified by readback."""
    try:
        return AFX_INSERTS.set_parameter(change.channel, change.slot, change.key, change.value)
    except (ValueError, RuntimeError, KeyError, proto.ConstraintError) as error:
        return _bad(str(error))


class AfxChainTestChange(BaseModel):
    channel: StrictInt = 0
    operation: str
    slot: StrictInt
    source: StrictInt | None = None
    effect_id: str = 'memory_brigade'


class AfxLinkTestChange(BaseModel):
    pair: StrictInt
    enabled: bool


class AfxParameterTestChange(BaseModel):
    channel: StrictInt = 0
    instance: StrictInt
    values: dict[str, StrictInt]


@app.get("/api/afx/memorycat-test")
def api_afx_memorycat_test(channel: int = 0, refresh: bool = False):
    state = AFX_TEST.state()
    if refresh and state.get('available') and state.get('online'):
        try:
            state = AFX_TEST.refresh(channel)
        except (ValueError, RuntimeError) as error:
            return _bad(str(error))
    try:
        catalog = load_catalog(os.path.join(PROFILE_DIR, 'afx_effects.json'))
    except (OSError, ValueError):
        catalog = {}
    return {**state, 'effects': effect_choices(catalog, os.path.basename(PROFILE_PATH), PROFILE)}


@app.post("/api/afx/memorycat-test/chain")
def api_afx_memorycat_chain(change: AfxChainTestChange):
    try:
        return AFX_TEST.change_chain(change.operation, change.slot, change.source, change.effect_id,
                                     channel=change.channel)
    except (ValueError, RuntimeError) as error:
        return _bad(str(error))


@app.post("/api/afx/memorycat-test/parameters")
def api_afx_memorycat_parameters(change: AfxParameterTestChange):
    try:
        return AFX_TEST.change_parameters(change.instance, change.values, channel=change.channel)
    except (ValueError, RuntimeError) as error:
        return _bad(str(error))


@app.post("/api/afx/memorycat-test/unlink")
def api_afx_memorycat_unlink():
    try:
        return AFX_TEST.unlink_pilot()
    except (ValueError, RuntimeError) as error:
        return _bad(str(error))


@app.post('/api/afx/link')
def api_afx_link(change: AfxLinkTestChange):
    try:
        return AFX_TEST.set_link(change.pair, change.enabled)
    except (ValueError, RuntimeError) as error:
        return _bad(str(error))


@app.get("/api/surround")
def api_surround():
    return DEV.surround_json()


@app.post("/api/surround/global")
def api_surround_global(change: SurroundGlobalChange):
    if not DEV.surround_available:
        return _bad("surround global state is not safely mapped for this profile")
    if change.format is not None:
        if (change.delay_ms is not None or change.level_db is not None
                or change.eq_position is not None):
            return _bad(
                "format changes cannot be combined with delay, level, or "
                "EQ position")
        format_name = str(change.format).strip()
        format_write = DEV.surround_json()["write"].get("format", {})
        option = next((item for item in format_write.get("options", [])
                       if item.get("name") == format_name), None)
        if not format_write.get("enabled"):
            return _bad("surround format writes are not enabled for this profile")
        if option is None or not option.get("writable"):
            return _bad(
                f"surround format {format_name!r} is read-only; "
                "higher layouts require the guarded self-test")

        def do(t):
            body = DEV._surround_global_body_for_write(t)
            packet = proto.build_surround_global_format_command(
                PROFILE, body, format_name)
            t.write(packet)
            DEV._cache_surround_global_packet(packet)

        DEV.submit(do)
        return {"ok": True, "queued": True, "format": format_name}
    if change.eq_position is not None:
        if change.delay_ms is not None or change.level_db is not None:
            return _bad(
                "EQ position cannot be combined with delay or level")
        position_write = DEV.surround_json()["write"].get(
            "eq_position", {})
        if not position_write.get("enabled"):
            return _bad(
                "Surround EQ-position writes are not authorized for this "
                "readback")
        position = str(change.eq_position).strip().lower()
        if position not in position_write.get("fields", []):
            return _bad(
                f"Surround EQ position {position!r} is not a writable option")

        with DEV._lock:
            cached_body = DEV.surround_global_raw
        if cached_body is not None:
            try:
                proto.build_surround_global_eq_position_command(
                    PROFILE, cached_body, position,
                    allow_experimental=True)
            except (KeyError, TypeError, ValueError, proto.ConstraintError) as exc:
                return _bad(str(exc))

        def do(t):
            body = DEV._surround_global_body_for_write(t)
            packet = proto.build_surround_global_eq_position_command(
                PROFILE, body, position, allow_experimental=True)
            t.write(packet)
            DEV._cache_surround_global_packet(packet)

        DEV.submit(do)
        return {
            "ok": True,
            "queued": True,
            "experimental": bool(position_write.get("experimental", False)),
            "eq_position": position,
        }
    if change.delay_ms is None and change.level_db is None:
        return _bad("provide a surround delay or level")
    if not DEV.surround_json()["write"]["enabled"]:
        return _bad(
            "surround global write requires a verified 2.0 readback")

    contract = DEV.surround_contract or {}
    try:
        delay_step = float(contract.get("delay_step_ms", 0.1))
        level_step = float(contract.get("level_step_db", 0.1))
        level_zero = int(contract.get("level_zero_raw", 600))
        delay_raw = (_surround_raw_value(
            change.delay_ms, step=delay_step, zero=0,
            bounds=contract["delay_range"], label="surround delay")
                      if change.delay_ms is not None else None)
        level_raw = (_surround_raw_value(
            change.level_db, step=level_step, zero=level_zero,
            bounds=contract["level_range"], label="surround level")
                     if change.level_db is not None else None)
    except (KeyError, TypeError, ValueError) as e:
        return _bad(str(e))

    def do(t):
        body = DEV._surround_global_body_for_write(t)
        packet = proto.build_surround_global_command(
            PROFILE, body, global_delay=delay_raw, global_level=level_raw)
        t.write(packet)
        DEV._cache_surround_global_packet(packet)

    DEV.submit(do)
    return {
        "ok": True,
        "delay_ms": None if delay_raw is None else delay_raw * delay_step,
        "level_db": None if level_raw is None
        else (level_raw - level_zero) * level_step,
    }


@app.post("/api/surround/bass")
def api_surround_bass(change: SurroundBassChange):
    """Queue one bounded Bass Management field write.

    A global filter-type field still carries a channel slot in the API for a
    uniform control shape; its profile scope makes the builder update only
    the global header bit, never a channel block.
    """
    if not DEV.surround_available:
        return _bad("surround global state is not safely mapped for this profile")
    bass_write = DEV.surround_json()["write"].get("bass", {})
    if not bass_write.get("enabled"):
        return _bad(
            "Bass Management writes are available only for a fresh supported "
            "surround readback")
    field = str(change.field).strip().lower()
    if field not in bass_write.get("fields", []):
        return _bad(f"Bass Management field {field!r} is not writable")
    block_count = int(bass_write.get("block_count", 0))
    if not 0 <= change.channel < block_count:
        return _bad(
            f"Bass Management channel {change.channel} out of range "
            f"0..{block_count - 1}")

    with DEV._lock:
        cached_body = DEV.surround_global_raw
    if cached_body is not None:
        try:
            proto.build_surround_global_bass_command(
                PROFILE, cached_body, change.channel, field, change.value,
                allow_experimental=True)
        except (KeyError, TypeError, ValueError, proto.ConstraintError) as exc:
            return _bad(str(exc))

    def do(t):
        body = DEV._surround_global_body_for_write(t)
        packet = proto.build_surround_global_bass_command(
            PROFILE, body, change.channel, field, change.value,
            allow_experimental=True)
        t.write(packet)
        DEV._cache_surround_global_packet(packet)
        try:
            DEV._surround_global_body_for_write(t)
        except RuntimeError as exc:
            print(f"[surround] post-write Bass Management readback: {exc!r}",
                  file=sys.stderr, flush=True)

    DEV.submit(do)
    return {
        "ok": True,
        "queued": True,
        "experimental": bool(bass_write.get("experimental", False)),
        "channel": change.channel,
        "field": field,
        "value": change.value,
    }


@app.post("/api/surround/speaker")
def api_surround_speaker(change: SurroundSpeakerChange):
    """Queue one bounded per-speaker monitor control write.

    Delay, level, and phase-invert use a fresh category-0x1a record and the
    complete 0x87 speaker frame.  Bypass is a bit in the global per-speaker
    mask, so it uses a fresh category-0x1b record and the complete 0xab frame.
    Both paths perform a second fresh read after the write when possible.
    """
    if not DEV.surround_available:
        return _bad("surround state is not safely mapped for this profile")
    speaker_write = DEV.surround_json()["write"].get("speaker_head", {})
    bypass_write = DEV.surround_json()["write"].get("speaker_bypass", {})
    field = str(change.field).strip().lower()
    use_bypass = field in bypass_write.get("fields", [])
    if use_bypass:
        if not bypass_write.get("enabled"):
            return _bad(
                "per-speaker bypass writes are not authorized for this "
                "surround format")
    elif not speaker_write.get("enabled"):
        return _bad(
            "per-speaker delay/level/phase writes are not authorized for this "
            "surround format")
    if not use_bypass and field not in speaker_write.get("fields", []):
        return _bad(f"per-speaker field {field!r} is not writable")
    speaker_count = (int(bypass_write.get("speaker_count", 0))
                     if use_bypass else DEV.surround_speaker_count)
    if not 0 <= change.speaker < speaker_count:
        return _bad(
            f"speaker {change.speaker} out of range "
            f"0..{speaker_count - 1}")

    if use_bypass:
        try:
            requested = proto._surround_boolean_value(
                change.value, f"per-speaker {field}")
        except (TypeError, ValueError) as exc:
            return _bad(str(exc))

        with DEV._lock:
            cached_body = DEV.surround_global_raw
        if cached_body is not None:
            try:
                proto.build_surround_global_speaker_mask_command(
                    PROFILE, cached_body, change.speaker, field, requested,
                    allow_experimental=True)
            except (KeyError, TypeError, ValueError, proto.ConstraintError) as exc:
                return _bad(str(exc))

        def do(t):
            body = DEV._surround_global_body_for_write(t)
            packet = proto.build_surround_global_speaker_mask_command(
                PROFILE, body, change.speaker, field, requested,
                allow_experimental=True)
            t.write(packet)
            DEV._cache_surround_global_packet(packet)
            try:
                DEV._surround_global_body_for_write(t)
            except RuntimeError as exc:
                print(f"[surround] post-write speaker bypass readback: {exc!r}",
                      file=sys.stderr, flush=True)

        DEV.submit(do)
        return {
            "ok": True,
            "queued": True,
            "experimental": bool(bypass_write.get("experimental", False)),
            "speaker": change.speaker,
            "field": field,
            "value": requested,
        }

    controls = speaker_write.get("controls", {})
    control = controls.get(field, {}) if isinstance(controls, dict) else {}
    if control.get("boolean"):
        try:
            requested = proto._surround_boolean_value(
                change.value, f"per-speaker {field}")
        except (TypeError, ValueError) as exc:
            return _bad(str(exc))
    else:
        try:
            requested = float(change.value)
            minimum, maximum = (float(value) for value in control["range"])
        except (TypeError, ValueError, KeyError):
            return _bad(f"invalid value for per-speaker field {field}")
        if not math.isfinite(requested) or not minimum <= requested <= maximum:
            return _bad(
                f"{field} outside {minimum:g}..{maximum:g}")

    def do(t):
        body = DEV._surround_speaker_body_for_write(t, change.speaker)
        packet = proto.build_surround_speaker_head_command(
            PROFILE, body, change.speaker, field, change.value,
            allow_experimental=True)
        t.write(packet)
        DEV._cache_surround_speaker_packet(change.speaker, packet)
        try:
            DEV._surround_speaker_body_for_write(t, change.speaker)
        except RuntimeError as exc:
            print(f"[surround] post-write speaker readback: {exc!r}",
                  file=sys.stderr, flush=True)

    DEV.submit(do)
    return {
        "ok": True,
        "queued": True,
        "experimental": bool(speaker_write.get("experimental", False)),
        "speaker": change.speaker,
        "field": field,
        "value": change.value,
    }


@app.post("/api/surround/eq")
def api_surround_eq(change: SurroundEQChange):
    """Queue one bounded per-speaker EQ field write."""
    if not DEV.surround_available:
        return _bad("surround state is not safely mapped for this profile")
    eq_write = DEV.surround_json()["write"].get("eq", {})
    if not eq_write.get("enabled"):
        return _bad("surround EQ writes are not authorized for this profile")
    parameter = str(change.parameter).strip().lower()
    if not 0 <= change.speaker < DEV.surround_speaker_count:
        return _bad(
            f"speaker {change.speaker} out of range "
            f"0..{DEV.surround_speaker_count - 1}")
    band_count = int(eq_write.get("band_count", 16))
    if not 0 <= change.band < band_count:
        return _bad(f"band {change.band} out of range 0..{band_count - 1}")
    try:
        raw_value, field = _surround_eq_raw_value(
            PROFILE, parameter, change.value)
    except (KeyError, TypeError, ValueError) as exc:
        return _bad(str(exc))

    def do(t):
        body = DEV._surround_speaker_body_for_write(t, change.speaker)
        packet = proto.build_surround_speaker_eq_command(
            PROFILE, body, change.speaker, change.band,
            {field: raw_value}, allow_experimental=True)
        t.write(packet)
        DEV._cache_surround_speaker_packet(change.speaker, packet)

    DEV.submit(do)
    return {
        "ok": True,
        "queued": True,
        "experimental": bool(eq_write.get("experimental", False)),
        "speaker": change.speaker,
        "band": change.band,
        "parameter": parameter,
        "value": change.value,
    }


@app.post("/api/surround/eq/point")
def api_surround_eq_point(change: SurroundEQPointChange):
    """Queue the bounded frequency and gain update from one EQ graph drag."""
    if not DEV.surround_available:
        return _bad("surround state is not safely mapped for this profile")
    eq_write = DEV.surround_json()["write"].get("eq", {})
    if not eq_write.get("enabled"):
        return _bad("surround EQ writes are not authorized for this profile")
    if not 0 <= change.speaker < DEV.surround_speaker_count:
        return _bad(
            f"speaker {change.speaker} out of range "
            f"0..{DEV.surround_speaker_count - 1}")
    band_count = int(eq_write.get("band_count", 16))
    if not 0 <= change.band < band_count:
        return _bad(f"band {change.band} out of range 0..{band_count - 1}")
    try:
        frequency_raw, frequency_field = _surround_eq_raw_value(
            PROFILE, "frequency", change.frequency)
        gain_raw, gain_field = _surround_eq_raw_value(
            PROFILE, "gain", change.gain)
    except (KeyError, TypeError, ValueError) as exc:
        return _bad(str(exc))

    def do(t):
        body = DEV._surround_speaker_body_for_write(t, change.speaker)
        packet = proto.build_surround_speaker_eq_command(
            PROFILE, body, change.speaker, change.band,
            {frequency_field: frequency_raw, gain_field: gain_raw},
            allow_experimental=True)
        t.write(packet)
        DEV._cache_surround_speaker_packet(change.speaker, packet)

    DEV.submit(do)
    return {
        "ok": True,
        "queued": True,
        "experimental": bool(eq_write.get("experimental", False)),
        "speaker": change.speaker,
        "band": change.band,
        "frequency": change.frequency,
        "gain": change.gain,
    }


@app.post("/api/surround/eq/reset")
def api_surround_eq_reset(change: SurroundEQReset):
    """Queue a complete profile-defined EQ reset for one speaker."""
    if not DEV.surround_available:
        return _bad("surround state is not safely mapped for this profile")
    eq_write = DEV.surround_json()["write"].get("eq", {})
    if not eq_write.get("enabled"):
        return _bad("surround EQ writes are not authorized for this profile")
    if not 0 <= change.speaker < DEV.surround_speaker_count:
        return _bad(
            f"speaker {change.speaker} out of range "
            f"0..{DEV.surround_speaker_count - 1}")
    try:
        frequencies, q_raw, gain_raw = _surround_eq_reset_values(PROFILE)
    except (KeyError, TypeError, ValueError) as exc:
        return _bad(str(exc))

    def do(t):
        body = DEV._surround_speaker_body_for_write(t, change.speaker)
        packet = proto.build_surround_speaker_eq_reset_command(
            PROFILE, body, change.speaker, frequencies, q_raw, gain_raw,
            allow_experimental=True)
        t.write(packet)
        DEV._cache_surround_speaker_packet(change.speaker, packet)

    DEV.submit(do)
    reset = eq_write.get("reset", {})
    return {
        "ok": True,
        "queued": True,
        "experimental": True,
        "speaker": change.speaker,
        "frequency_hz": reset.get("frequency_hz", frequencies),
        "q": reset.get("q", q_raw / 100),
        "gain_db": reset.get("gain_db", gain_raw / 100),
    }


def _gain_outer_bounds():
    """Widest gain span the profile declares, across modes. The page clamps
    per mode; this stops a stray request writing a value no mode allows."""
    gain = PROFILE.get("params", {}).get("gain", {})
    ranges = list((gain.get("per_mode_range") or gain.get("range_by_mode") or {}).values())
    ranges.append(gain.get("runtime_range"))
    ranges = [r for r in ranges if isinstance(r, (list, tuple)) and len(r) == 2]
    if not ranges:
        return None
    return min(r[0] for r in ranges), max(r[1] for r in ranges)


@app.post("/api/gain")
def api_gain(g: Gain):
    proto.check_target(PROFILE, g.channel, "input")
    bounds = _gain_outer_bounds()
    if bounds and not bounds[0] <= g.db <= bounds[1]:
        return _bad(f"gain {g.db} dB is outside this device's range {bounds[0]}..{bounds[1]}")
    DEV.submit(lambda t: t.write(proto.build_command(PROFILE, "gain", g.channel, g.db)))
    return {"ok": True}


@app.post("/api/toggle")
def api_toggle(t: Toggle):
    if t.param not in ("phantom", "phase_invert"):
        return _bad("bad param")
    if t.param not in PROFILE.get("params", {}):
        return _bad("not mapped for this device")
    proto.check_target(PROFILE, t.channel, "input")
    val = 1 if t.on else 0
    DEV.submit(lambda tr: tr.write(proto.build_command(PROFILE, t.param, t.channel, val)))
    return {"ok": True}


def _input_link_readback_target(profile, domain, pair):
    """Return a bounded, profile-declared link table for this input pair."""
    spec = profile.get("frame", {}).get("link_command", {}).get("readback")
    if not isinstance(spec, dict) or str(spec.get("status", "")).lower() not in \
            STRUCTURED_READBACK_SAFE_STATUSES:
        return None

    # A post-write diagnostic target need not be an authoritative mapping
    # from a returned byte to one input domain. Confirmed mappings are separate.
    pair_counts = spec.get("post_write_pair_counts", spec.get("pair_counts"))
    if not isinstance(pair_counts, dict):
        return None
    tables = [{
        "category": spec.get("category"),
        "index": spec.get("index"),
        "record_count": spec.get("record_count"),
        "pair_mappings": {
            name: {"pair_start": 0, "record_start": 0, "pair_count": count}
            for name, count in pair_counts.items()
        },
        "status": spec.get("status"),
    }]
    additional_tables = spec.get("additional_tables", [])
    if not isinstance(additional_tables, list):
        return None
    tables.extend(additional_tables)

    for table in tables:
        if not isinstance(table, dict):
            continue
        status = str(table.get("status", "")).strip().lower()
        if status not in STRUCTURED_READBACK_SAFE_STATUSES | {"schema-backed"}:
            continue
        try:
            category = proto._as_int(table["category"])
            index = proto._as_int(table["index"])
            count = int(table["record_count"])
            pair_mappings = table.get("pair_mappings")
            if not isinstance(pair_mappings, dict):
                continue
            mapping = pair_mappings.get(domain)
            if not isinstance(mapping, dict):
                continue
            pair_start = int(mapping.get("pair_start", 0))
            record_start = int(mapping.get("record_start", 0))
            pair_count = int(mapping["pair_count"])
            layout = proto.readback_record_layout(
                profile, category, index, kind="link_table")
            domain_pairs = int(profile.get(
                "channels" if domain == "preamp" else domain, {}
            ).get("link_pairs", {}).get("count", 0))
            if not (pair_count > 0
                    and 0 <= pair_start <= pair < pair_start + pair_count <= domain_pairs
                    and 0 <= record_start
                    and record_start + pair_count <= count
                    and layout is not None
                    and int(layout["record_count"]) == count
                    and (category, index) in _structured_readback_targets(profile)):
                continue
        except (KeyError, TypeError, ValueError, proto.ConstraintError):
            continue
        return category, index
    return None


def _queue_input_link_write(packet, domain, pair):
    target = _input_link_readback_target(PROFILE, domain, pair)

    def do(transport):
        transport.write(packet)
        if target is None:
            return
        # This query follows the write in the same HID worker. A successful
        # response wakes the browser even if the device kept the old value.
        observed = DEV._refresh_readback_one(transport, *target)
        if observed is not None:
            with DEV._lock:
                DEV.link_rb_ver += 1

    DEV.submit(do)


@app.post("/api/link")
def api_link(l: Link):
    """Engage/disengage a preamp-pair link and refresh confirmed readback."""
    npairs = int(PROFILE["channels"].get("link_pairs", {}).get("count", 0))
    if not (0 <= l.pair < npairs):
        return _bad(f"pair {l.pair} out of range 0..{npairs - 1}")
    try:
        pkt = proto.build_link_command(
            PROFILE, l.pair, l.enabled, space=proto.input_link_space(PROFILE, "preamp"))
    except (KeyError, proto.ConstraintError) as e:                # noqa: BLE001
        return _bad(str(e))
    _queue_input_link_write(pkt, "preamp", l.pair)
    return {"ok": True}


def _dig_gain(space, param, rng, g: "DigGain"):
    """Shared ADAT / S-PDIF gain write. Both are int8-dB SET_PARAM params
    (adat_gain 0x5b / spdif_gain 0x5c) on their own address space; neither
    has a status byte. No constraints.* bounds are defined for these indices,
    so clamp the channel here before it hits the wire."""
    n = DEV.n_adat if space == "adat" else DEV.n_spdif
    if not (0 <= g.channel < n):
        return _bad(f"{space} channel {g.channel} out of range 0..{n - 1}")
    lo, hi = rng
    db = max(lo, min(hi, g.db))
    DEV.submit(lambda t: t.write(proto.build_command(PROFILE, param, g.channel, db)))
    return {"ok": True}


@app.post("/api/adat-gain")
def api_adat_gain(g: DigGain):
    return _dig_gain("adat", "adat_gain", proto.adat_gain_range(PROFILE), g)


@app.post("/api/spdif-gain")
def api_spdif_gain(g: DigGain):
    rng = PROFILE["params"].get("spdif_gain", {}).get("range", [-128, 127])
    return _dig_gain("spdif", "spdif_gain", rng, g)


def _dig_link(space_name, npairs, l: "DigLink"):
    """SET_LINK for ADAT/S-PDIF, refreshing a confirmed readback when mapped."""
    if not (0 <= l.pair < npairs):
        return _bad(f"{space_name} pair {l.pair} out of range 0..{npairs - 1}")
    try:
        pkt = proto.build_link_command(
            PROFILE, l.pair, l.enabled, space=proto.input_link_space(PROFILE, space_name))
    except (KeyError, proto.ConstraintError) as e:                # noqa: BLE001
        return _bad(str(e))
    _queue_input_link_write(pkt, space_name, l.pair)
    return {"ok": True}


@app.post("/api/adat-link")
def api_adat_link(l: DigLink):
    n = int(PROFILE.get("adat", {}).get("link_pairs", {}).get("count", 0))
    return _dig_link("adat", n, l)


@app.post("/api/spdif-link")
def api_spdif_link(l: DigLink):
    n = int(PROFILE.get("spdif", {}).get("link_pairs", {}).get("count", 0))
    return _dig_link("spdif", n, l)


@app.post("/api/output-trim")
def api_output_trim(t: Trim):
    """Settings-tab output-trim selector, 7 positions 0..6. Orion: param 0x4b,
    targets 0/1/2 = Monitor A / B / Line Out. Zen Go: its own frame, target 0
    = Monitor. Reads back in state_report.output_trim_block."""
    try:
        packet = proto.build_output_trim_command(
            PROFILE, t.target, max(0, min(6, t.value)))
    except (KeyError, ValueError, proto.ConstraintError) as e:
        return _bad(str(e))
    DEV.submit(lambda tr: tr.write(packet))
    return {"ok": True}


@app.post("/api/line-trim")
def api_line_trim(t: LineTrim):
    """Discrete 4 Line Out 1-4 trims (0..6 = 20..14 dBu) and their one link:
    one whole frame, as the vendor panel sends it. Read back from cat 0x0e
    (GET /api/readbacks, layout line_out_trims). Settings panel: Line Out trim."""
    try:
        packet = proto.build_line_trim_command(PROFILE, t.linked, t.trims)
    except (KeyError, ValueError, proto.ConstraintError) as e:
        return _bad(str(e))
    DEV.submit(lambda tr: tr.write(packet))
    return {"ok": True}


@app.post("/api/bus-toggle")
def api_bus_toggle(t: BusToggle):
    """bus_dim / bus_mute / bus_mono (SET_PARAM 0x66/0x48/0x69 on Zen Go;
    Orion uses 0x68 for dim), bus id at
    the channel offset. Confirmed for monitor buses; bus_mute also confirmed on
    the line output (bus 3). dim/mono may not apply to line/reamp."""
    pname = {"dim": "bus_dim", "mute": "bus_mute", "mono": "bus_mono"}.get(t.param)
    if pname is None:
        return _bad("bad param -- dim|mute|mono")
    try:
        proto.check_target(PROFILE, t.bus, "bus")
        packet = proto.build_command(PROFILE, pname, t.bus, 1 if t.on else 0)
    except (KeyError, ValueError, proto.ConstraintError) as e:
        return _bad(str(e))
    # Zen Go: dim on and dim off both clear the mute bit. Discrete 4: unknown.
    # So dim/mono on a protected muted (or unread) bus is refused.
    state = next((b for b in DEV.get()[0].get("buses", [])
                  if b.get("bus") == t.bus), None)
    if proto.bus_flag_may_release_mute(PROFILE, t.bus, pname, state):
        return _bad(f"{proto.bus_name(PROFILE, t.bus)} is muted or unread: "
                    f"{t.param} is refused so it cannot release the mute")
    DEV.submit(lambda tr, packet=packet: tr.write(packet))
    return {"ok": True}


@app.post("/api/dc-coupling")
def api_dc_coupling(t: GlobalToggle):
    """Submit DC-coupling on/off (param 0x26, SET_GLOBAL 0x12).
    Device readback is 0x73 state-report byte 93 bit 0; this endpoint currently
    submits the requested value while the WebUI keeps an optimistic display
    state."""
    DEV.submit(lambda tr: tr.write(proto.build_global_command(PROFILE, "dc_coupling", 1 if t.on else 0)))
    return {"ok": True}


@app.post("/api/emumic")
def api_emumic(e: EmuMic):
    """emuMic / mic-modeling DSP toggle (SET_MIC_MODELING, 0x17/0xe5).
    Preamps 5-12 (the EMU button is Mic-mode-gated). Current state is exposed
    by the profile-declared category-0x16 nested readback; the write path has
    not yet been verified by a write/readback round-trip, so the controls keep
    their local optimistic state. This endpoint does NOT do the Launcher's
    side effects (auto 48V, pair link)."""
    fr = PROFILE["frame"].get("micmodeling_command")
    if not fr:
        return _bad("this device has no mic modeling")
    chans = fr.get("channels") or []
    if e.channel not in chans:
        return _bad(f"channel {e.channel} has no EMU button (channels {chans})")
    kw = {}
    if e.pattern is not None: kw["pattern"] = e.pattern
    if e.model is not None:   kw["model"] = e.model
    if e.swap is not None:    kw["swap"] = e.swap
    try:
        pkt = proto.build_micmodeling_command(PROFILE, e.channel, e.enabled, **kw)
    except (KeyError, ValueError, proto.ConstraintError) as ex:   # noqa: BLE001
        return _bad(str(ex))
    DEV.submit(lambda t: t.write(pkt))
    return {"ok": True}


@app.post("/api/mode")
def api_mode(m: Mode):
    proto.check_target(PROFILE, m.channel, "input")
    try:
        val = proto.mode_value(PROFILE, m.mode)
        proto.check_enum(PROFILE, "input_mode_allowed_values", val, "input_mode")
    except Exception as e:                                       # noqa: BLE001
        return _bad(str(e))
    hiz = PROFILE.get("channels", {}).get("hiz_channels")
    if m.mode.lower() == "hiz" and hiz is not None and m.channel not in hiz:
        return _bad(f"Hi-Z is not confirmed on channel {m.channel} (only {hiz})")
    DEV.submit(lambda t: t.write(proto.build_command(PROFILE, "input_mode", m.channel, val)))
    return {"ok": True}


@app.post("/api/sample-rate")
def api_sample_rate(s: Scalar):
    """Device sample rate (SET_GLOBAL 0x12 / param 0x03), enum index 0..6.
    Reads back in the 0x73 stream as sample_rate_idx. DISRUPTIVE -- the device
    drops audio and re-locks its clock (~1 s). The Orion IGNORES this write
    while the host holds the USB audio interface streaming, and also while the
    clock source is USB (it defers to the host clock) -- see
    params.sample_rate.notes; the browser surfaces that when the write doesn't
    land."""
    vals = PROFILE["params"].get("sample_rate", {}).get("values", {})
    if not vals:
        return _bad("this profile has no params.sample_rate")
    if str(s.value) not in vals:
        return _bad(f"sample-rate index {s.value} not in {sorted(int(k) for k in vals)}")
    DEV.submit(lambda t: t.write(proto.build_global_command(PROFILE, "sample_rate", s.value)))
    return {"ok": True}


@app.post("/api/clock-source")
def api_clock_source(s: Scalar):
    """Device clock source (SET_GLOBAL 0x12 / param 0x04), enum index 0..6
    (0 = Oven ... 6 = USB, see params.clock_source.values). Reads back in the
    0x73 stream as clock_source_idx. DISRUPTIVE -- selecting a source that is
    not present/locked unlocks the device clock. Same host-streaming caveat as
    the sample rate."""
    vals = PROFILE["params"].get("clock_source", {}).get("values", {})
    if not vals:
        return _bad("this profile has no params.clock_source")
    if str(s.value) not in vals:
        return _bad(f"clock-source index {s.value} not in {sorted(int(k) for k in vals)}")
    DEV.submit(lambda t: t.write(proto.build_global_command(PROFILE, "clock_source", s.value)))
    return {"ok": True}


@app.post("/api/brightness")
def api_brightness(s: Scalar):
    v = max(0, min(100, s.value))
    DEV.submit(lambda t: t.write(proto.build_global_command(PROFILE, "screen_brightness", v)))
    return {"ok": True}


@app.post("/api/pan-law")
def api_pan_law(s: Scalar):
    """Mixer panning law (SET_GLOBAL), an index into params.pan_law.values.
    The index order differs per device. Settings panel: Pan law."""
    if str(s.value) not in PROFILE["params"].get("pan_law", {}).get("values", {}):
        return _bad(f"pan-law index {s.value} is not declared by this profile")
    DEV.submit(lambda t: t.write(proto.build_global_command(PROFILE, "pan_law", s.value)))
    return {"ok": True}


@app.post("/api/oscillator")
def api_oscillator(s: Scalar):
    """The two test oscillators: one packed byte (params.oscillator.value_bits)
    holding both frequencies, the level and both mutes. Bits outside
    params.oscillator.value_mask were never seen and are refused. The Zen Go
    reports the byte back in the state stream as `oscillator`."""
    spec = PROFILE["params"].get("oscillator", {})
    if spec.get("value_mask") is None:
        return _bad("this profile has no captured oscillator bits")
    if not 0 <= s.value <= 255 or s.value & ~proto._as_int(spec["value_mask"]):
        return _bad(f"oscillator value {s.value:#04x} uses bits outside {spec['value_mask']}")
    DEV.submit(lambda t: t.write(proto.build_global_command(PROFILE, "oscillator", s.value)))
    return {"ok": True}


@app.post("/api/bus")
def api_bus(b: Bus):
    proto.check_target(PROFILE, b.bus, "bus")
    lo, hi = proto.bus_level_range(PROFILE)
    v = max(lo, min(hi, b.level))
    DEV.submit(lambda t: t.write(proto.build_command(PROFILE, "bus_level", b.bus, v)))
    return {"ok": True}


# -- routing ---------------------------------------------------------------

@app.get("/api/routing")
def api_routing():
    rc = PROFILE["frame"].get("routing_command", {})
    dc = rc.get("destination_channels", {})
    addr = rc.get("addressable_destinations", {})
    stereo = set(rc.get("stereo_destinations", []))
    logical = _routing_ui_destinations()
    current = DEV.routing_json()
    if logical is None:
        dests = [
            {"id": int(k), "name": addr.get(k, f"dest{k}"),
             "label": proto.route_destination_label(PROFILE, int(k)),
             "channels": int(v), "stereo": k in stereo}
            for k, v in sorted(dc.items(), key=lambda x: int(x[0]))
        ]
    else:
        dests = [{
            "id": int(item["id"]),
            "name": item.get("name", f"dest{item['id']}"),
            "label": item.get("label") or proto.route_destination_label(
                PROFILE, int(item["id"])),
            "channels": int(item["channels"]),
            "stereo": bool(item.get("stereo", False)),
            "note": item.get("note", ""),
        } for item in logical]
        visible = {str(item["id"]) for item in logical}
        current = {key: value for key, value in current.items()
                   if key in visible}
    sources = proto.route_source_options(PROFILE)
    listed = rc.get("destination_sources", {})
    for d in dests:   # source kinds this destination takes (absent = all)
        group = str(_routing_write_destinations(d["id"])[0])
        if group in listed:
            d["sources"] = listed[group]
    return {"dests": dests, "sources": sources, "current": current}


@app.post("/api/route")
def api_route(r: Route):
    if not DEV.routing_available:
        return _bad("routing readback is not safely mapped for this profile")
    dc = PROFILE["frame"].get("routing_command", {}).get("destination_channels", {})
    try:
        nch = _routing_channel_count(PROFILE, r.dest)
    except (KeyError, TypeError, ValueError):
        return _bad(f"unknown routing destination {r.dest}")
    if not 0 <= r.channel < nch:
        return _bad(f"channel {r.channel} out of range 0..{nch - 1} for dest {r.dest}")
    try:
        tgt = proto.route_mute_source(PROFILE) if r.kind == "mute" \
            else proto.resolve_route_source(PROFILE, r.kind, r.number)
    except ValueError as e:
        return _bad(str(e))
    refusal = _routing_refusal(r.dest, r.channel, tgt)
    if refusal:
        return _bad(refusal)

    def do(t, dest=r.dest, ch=r.channel, tgt=tgt):
        DEV._set_routing_source(t, dest, ch, tgt)

    DEV.submit(do)
    return {"ok": True}


@app.post("/api/route-batch")
def api_route_batch(b: RouteBatch):
    """Several route changes for ONE destination group in a single
    read-modify-write (one 0x74 readback + one 0x53 write) -- for the matrix
    group ops (1:1, mute all) and, later, patchbay group drops. Same
    'refuse to write blind' guard as /api/route."""
    if not DEV.routing_available:
        return _bad("routing readback is not safely mapped for this profile")
    dc = PROFILE["frame"].get("routing_command", {}).get("destination_channels", {})
    try:
        nch = _routing_channel_count(PROFILE, b.dest)
    except (KeyError, TypeError, ValueError):
        return _bad(f"unknown routing destination {b.dest}")
    if not b.changes:
        return _bad("no changes")
    resolved = []
    for c in b.changes:
        if not 0 <= c.channel < nch:
            return _bad(f"channel {c.channel} out of range 0..{nch - 1} for dest {b.dest}")
        try:
            tgt = proto.route_mute_source(PROFILE) if c.kind == "mute" \
                else proto.resolve_route_source(PROFILE, c.kind, c.number)
        except ValueError as e:                                  # noqa: BLE001
            return _bad(str(e))
        refusal = _routing_refusal(b.dest, c.channel, tgt)
        if refusal:
            return _bad(refusal)
        resolved.append((c.channel, tgt))

    def do(t, dest=b.dest, changes=resolved):
        DEV._set_routing_batch(t, dest, changes)

    DEV.submit(do)
    return {"ok": True, "n": len(resolved)}


# -- virtual mixer -------------------------------------------------------

@app.post("/api/mixer-source")
def api_mixer_source(s: MixerSource):
    """Update one slot in the Zen Go's shared mixer input map.

    The browser sends an option key from the profile-derived feature list;
    the server resolves that key back to a bank/index pair and refuses any
    source that is not declared by the active profile.
    """
    if not DEV.mixer_source_available:
        return _bad("mixer input routing is not safely mapped for this profile")
    if not 0 <= s.mix < DEV.n_mixes:
        return _bad(f"mix {s.mix} out of range 0..{DEV.n_mixes - 1}")
    if not 0 <= s.channel < DEV.mix_channels:
        return _bad(f"mixer channel {s.channel} out of range 0..{DEV.mix_channels - 1}")
    feature = UI_FEATURES.get("mixer_sources", {})
    option = next((item for item in feature.get("options", [])
                   if item.get("key") == s.source), None)
    if option is None:
        return _bad(f"unknown mixer source option {s.source!r}")
    if s.channel in feature.get("locked_channels", []) \
            or option.get("selectable") is False:
        return _bad("that strip or source is not captured as a mixer-source "
                    "write on this device")
    try:
        target = (proto._as_int(option["bank"]),
                  proto._as_int(option["index"]))
    except (KeyError, TypeError, ValueError) as e:
        return _bad(f"invalid mixer source option: {e}")
    DEV.submit(lambda t: DEV._set_mixer_source(t, s.channel, target))
    return {"ok": True, "mix": s.mix, "channel": s.channel,
            "source": s.source}

@app.post("/api/mixer-select")
def api_mixer_select(s: MixerSelect):
    """Select a profile-defined mixer surface or meter bank.

    This is a UI selection, not a routing change. Orion profiles use the
    legacy ``frame.state_report.mixer_window_selection`` selector; profiles
    such as Zen Go can instead declare a confirmed ``mixer.surface_selection``
    command with a per-mix value map.
    """
    if not DEV.mixer_available:
        return _bad("mixer readback is not safely mapped for this profile")
    if not 0 <= s.mix < DEV.n_mixes:
        return _bad(f"mix {s.mix} out of range 0..{DEV.n_mixes - 1}")
    try:
        surface_packet = _mixer_surface_packet(PROFILE, s.mix)
    except (KeyError, TypeError, ValueError, proto.ConstraintError) as e:
        return _bad(str(e))
    if surface_packet is not None:
        pkt, value, target = surface_packet
        DEV.submit(lambda t: t.write(pkt))
        return {"ok": True, "mix": s.mix, "selected": True,
                "selector": value, "target": target}
    selector = PROFILE["frame"].get("state_report", {}).get("mixer_window_selection", {})
    if not selector:
        # Some devices expose each mix's controls without the Orion's
        # selector-gated meter bank.  The tab still changes locally; there is
        # simply no selector command to send for that profile.
        return {"ok": True, "mix": s.mix, "selected": False}
    try:
        param_id = selector["param_id"]
        param_id = int(param_id, 0) if isinstance(param_id, str) else int(param_id)
        target = int(selector.get("target", 1))
        pkt = proto.build_raw_command(PROFILE, param_id, target, s.mix)
    except (KeyError, TypeError, ValueError, proto.ConstraintError) as e:
        return _bad(str(e))
    DEV.submit(lambda t: t.write(pkt))
    return {"ok": True, "mix": s.mix}


@app.post("/api/mix-link")
def api_mix_link(link: MixerLink):
    """Toggle one virtual-mixer pair (SET_LINK space 3).

    Linked value mirroring is Launcher-side behavior, so the browser owns
    that companion state. Profiles may additionally declare a complete,
    capture-confirmed link bitmap for reconnect-time state seeding.
    """
    if not DEV.mixer_available:
        return _bad("mixer readback is not safely mapped for this profile")
    if not 0 <= link.mix < DEV.n_mixes:
        return _bad(f"mix {link.mix} out of range 0..{DEV.n_mixes - 1}")
    pairs = DEV.mix_channels // 2
    if not 0 <= link.pair < pairs:
        return _bad(f"pair {link.pair} out of range 0..{pairs - 1}")
    try:
        wire_pair = proto.mixer_link_pair_index(PROFILE, link.mix, link.pair)
        pkts = proto.build_mixer_link_commands(
            PROFILE, link.mix, link.pair, link.enabled)
    except (KeyError, proto.ConstraintError) as e:                # noqa: BLE001
        return _bad(str(e))
    DEV.submit(lambda t: [t.write(pkt) for pkt in pkts])
    return {"ok": True, "mix": link.mix, "pair": link.pair,
            "wire_pair": wire_pair, "enabled": link.enabled}

@app.post("/api/mix-solo")
def api_mix_solo(s: MixSolo):
    """Apply a mix-wide solo, matching the Launcher's host-side behavior.

    One or more channels may be soloed together. While any solo is active,
    non-solo channels are muted; when the last Solo is released, restore the
    flags that existed before the first Solo instead of blindly unmuting.
    """
    if not DEV.mixer_available:
        return _bad("mixer readback is not safely mapped for this profile")
    if not 0 <= s.mix < DEV.n_mixes:
        return _bad(f"mix {s.mix} out of range 0..{DEV.n_mixes - 1}")
    first_channel = 1 if DEV.mixer_has_master else 0
    last_channel = DEV.mix_channels if DEV.mixer_has_master else DEV.mix_channels - 1
    if not first_channel <= s.channel <= last_channel:
        if DEV.mixer_has_master:
            return _bad(
                f"channel {s.channel} out of range {first_channel}..{last_channel}; "
                "master has no Solo")
        return _bad(
            f"channel {s.channel} out of range {first_channel}..{last_channel}")

    def do(t, m=s.mix, ch=s.channel, on=s.on):
        slots = DEV._mixer_record_for_write(t, m)
        n_slots = DEV.mix_channels + int(DEV.mixer_has_master)
        if len(slots) < n_slots:
            raise RuntimeError(
                f"mixer {m} returned {len(slots)} slots; expected {n_slots}")

        def solo_update(active):
            # A mix master is deliberately outside Solo: its strip has only
            # fader and mute, and muting it would silence every soloed input.
            return [(bool(slot["mute"]), bool(slot["solo"]))
                    if DEV.mixer_has_master and idx == 0
                    else (idx not in active, idx in active)
                    for idx, slot in enumerate(slots[:n_slots])]

        if on:
            group = DEV.solo_state.get(m)
            if group is None:
                group = {
                    "restore": [
                        (bool(slot["mute"]), bool(slot["solo"]))
                        for slot in slots[:n_slots]
                    ],
                    "active": set(),
                }
                DEV.solo_state[m] = group
            group["active"].add(ch)
            active = group["active"]
            updated = solo_update(active)
        else:
            group = DEV.solo_state.get(m)
            if group is None:
                updated = [(False, False) for _ in range(n_slots)]
            else:
                group["active"].discard(ch)
                active = group["active"]
                if active:
                    updated = solo_update(active)
                else:
                    updated = group["restore"]
                    DEV.solo_state.pop(m, None)

        for idx, slot in enumerate(slots[:n_slots]):
            mute, solo = updated[idx]
            t.write(proto.build_mix_command(
                PROFILE, m, idx, slot["fader"], slot["pan"], slot["send"],
                mute, solo))
            slots[idx] = {**slot, "mute": mute, "solo": solo}
        DEV._cache_mixer(m, slots)

    DEV.submit(do)
    return {"ok": True, "mix": s.mix, "channel": s.channel, "on": s.on}


@app.get("/api/auraverb")
def api_auraverb():
    command = PROFILE["frame"].get("auraverb_command", {})
    param_names = list(command.get("param_offsets", {}))
    lo, hi = command.get("param_range", [0, 100])
    return {
        "available": bool(UI_FEATURES.get("auraverb", {}).get("enabled")
                           and DEV.auraverb_available),
        "mix": UI_FEATURES.get("auraverb", {}).get("mix", 0),
        "params": param_names,
        "range": [lo, hi],
        "ranges": command.get("param_ranges", {}),
        "current": DEV.auraverb_json(),
        "returns": _reverb_returns(),
    }


def _reverb_returns():
    """Reverb return level + mute per output mix (Zen Go), from the readback;
    None where the profile has no return control or nothing was read yet."""
    spec = PROFILE["params"].get("reverb_return")
    if not spec:
        return None
    rb = spec["runtime_readback"]
    with DEV._lock:
        rows = DEV.structured.get((proto._as_int(rb["category"]), proto._as_int(rb["index"])))
    mute = proto._as_int(spec["mute_bit"])
    return rows and [{"bus": int(bus), "level": rows[int(bus)]["value"] & ~mute,
                      "mute": bool(rows[int(bus)]["value"] & mute)}
                     for bus in spec["targets"]]


@app.post("/api/reverb-return")
def api_reverb_return(r: ReverbReturn):
    """One reverb return: level and mute travel in the same byte, so both are
    given (read the current pair from GET /api/auraverb `returns`)."""
    spec = PROFILE["params"].get("reverb_return")
    if not spec:
        return _bad("this profile has no reverb return control")
    lo, hi = spec["range"]
    if str(r.bus) not in spec["targets"] or not lo <= r.level <= hi:
        return _bad(f"reverb return {r.bus} / level {r.level} outside the captured range {lo}..{hi}")
    value = r.level | (proto._as_int(spec["mute_bit"]) if r.mute else 0)
    try:
        packet = proto.build_command(PROFILE, "reverb_return", r.bus, value)
    except (KeyError, ValueError, proto.ConstraintError) as e:
        return _bad(str(e))
    DEV.submit(lambda t: t.write(packet))
    return {"ok": True}


@app.post("/api/auraverb")
def api_auraverb_change(change: AuraVerbChange):
    spec = UI_FEATURES.get("auraverb", {})
    command = PROFILE["frame"].get("auraverb_command", {})
    if not spec.get("enabled") or not DEV.auraverb_available:
        return _bad("Gazelle Reverb is not safely mapped for this profile")
    if change.mix != int(spec.get("mix", 0)):
        return _bad("only the profile-confirmed Gazelle Reverb mix is available")
    if (change.param is None) != (change.value is None):
        return _bad("param and value must be supplied together")
    if change.param is None and change.enabled is None:
        return _bad("provide a Gazelle Reverb parameter or enabled state")
    names = command.get("param_offsets", {})
    if change.param is not None and change.param not in names:
        return _bad(f"unknown Gazelle Reverb parameter {change.param!r}")
    lo, hi = command.get("param_ranges", {}).get(
        change.param, command.get("param_range", [0, 100]))
    if change.value is not None and not lo <= change.value <= hi:
        return _bad(f"Gazelle Reverb value {change.value} outside {lo}..{hi}")

    def do(t):
        records = DEV._auraverb_record_for_write(t)
        mix = int(spec.get("mix", 0))
        if mix >= len(records):
            raise RuntimeError(f"Gazelle Reverb readback has no Mix {mix + 1} record")
        current = records[mix]
        params = dict(current.get("params", {}))
        if set(params) != set(names):
            raise RuntimeError("Gazelle Reverb readback is incomplete -- not writing blind")
        enabled = current.get("enabled")
        if enabled is None:
            raise RuntimeError("Gazelle Reverb enabled state is unknown -- not writing blind")
        if change.param is not None:
            params[change.param] = change.value
        if change.enabled is not None:
            enabled = bool(change.enabled)
        packet = proto.build_auraverb_command(
            PROFILE, params, enabled, mix=mix)
        t.write(packet)
        records[mix] = {**current, "params": params, "enabled": enabled}
        DEV._cache_auraverb(records)

    DEV.submit(do)
    return {"ok": True, "mix": change.mix,
            "param": change.param, "value": change.value,
            "enabled": change.enabled}


@app.get("/api/mixer")
def api_mixer():
    p = PROFILE.get("params", {})
    mixer = PROFILE.get("mixer", {})
    send = p.get("mix_send", {})
    fader = p.get("mix_fader", {}).get("range",
                                        mixer.get("fader", {}).get("range", [0, 90]))
    pan = p.get("mix_pan", {}).get("range",
                                    mixer.get("pan", {}).get("range_deg", [-30, 30]))
    send_range = send.get("range", [0, 96])
    return {
        "available": DEV.mixer_available,
        "n_mixes": DEV.n_mixes,
        "channels_per_mix": DEV.mix_channels,
        "has_master": DEV.mixer_has_master,
        "has_send": proto.mix_has_send(PROFILE) or "reverb_send_command" in PROFILE["frame"],
        "send_mixes": [int(m) for m in send.get("mix_indices", [])],
        "send_master": bool(send.get("include_master", False)),
        # None = every pair is writable (profiles without mixer.link_write)
        "link_pairs": (mixer.get("link_write") or {}).get("pairs_by_mix"),
        "ranges": {
            "fader": fader,
            "pan": pan,
            "send": send_range,
        },
        "current": DEV.mixer_json(),
    }


@app.post("/api/mix")
def api_mix(s: MixStrip):
    if not DEV.mixer_available:
        return _bad("mixer readback is not safely mapped for this profile")
    if not 0 <= s.mix < DEV.n_mixes:
        return _bad(f"mix {s.mix} out of range 0..{DEV.n_mixes - 1}")
    first_channel = 0
    last_channel = DEV.mix_channels if DEV.mixer_has_master else DEV.mix_channels - 1
    if not first_channel <= s.channel <= last_channel:
        label = "0 = master" if DEV.mixer_has_master else "no master strip"
        return _bad(f"channel {s.channel} out of range {first_channel}..{last_channel} ({label})")
    # Zen Go: the send (level + its own pan) is a separate frame from the strip.
    send_frame = "reverb_send_command" in PROFILE["frame"]
    has_send = proto.mix_has_send(PROFILE) or send_frame
    mixer = PROFILE.get("mixer", {})
    f_lo, f_hi = PROFILE.get("params", {}).get("mix_fader", {}).get(
        "range", mixer.get("fader", {}).get("range", [0, 90]))
    p_lo, p_hi = PROFILE.get("params", {}).get("mix_pan", {}).get(
        "range", mixer.get("pan", {}).get("range_deg", [-30, 30]))
    send_spec = PROFILE.get("params", {}).get("mix_send", {})
    s_lo, s_hi = send_spec.get("range", [0, 96])
    send_mixes = {int(m) for m in send_spec.get("mix_indices", [])}
    send_allowed = has_send and s.mix in send_mixes \
        and (not DEV.mixer_has_master or s.channel != 0
             or bool(send_spec.get("include_master", False)))
    if (s.send is not None or s.send_pan is not None) and not send_allowed:
        return _bad("this mixer strip has no Gazelle Reverb send")
    if s.send_pan is not None and not send_frame:
        return _bad("this device has no Gazelle Reverb send pan")

    def do(t, m=s.mix, ch=s.channel):
        slots = DEV._mixer_record_for_write(t, m)
        if ch >= len(slots):
            raise RuntimeError(
                f"mixer {m} returned {len(slots)} slots; channel {ch} is unavailable")
        cur = slots[ch]
        fader = cur["fader"] if s.fader is None else max(f_lo, min(f_hi, abs(s.fader)))
        pan = cur["pan"] if s.pan is None else max(p_lo, min(p_hi, s.pan))
        send = cur["send"] if s.send is None else max(s_lo, min(s_hi, s.send))
        mute = cur["mute"] if s.mute is None else bool(s.mute)
        solo = cur["solo"] if s.solo is None else bool(s.solo)
        send_change = s.send is not None or s.send_pan is not None
        extra = {}
        if send_frame and send_change:
            send_pan = cur["send_pan"] if s.send_pan is None else max(p_lo, min(p_hi, s.send_pan))
            t.write(proto.build_reverb_send_command(PROFILE, ch, send, send_pan))
            extra = {"send_pan": send_pan}
        if not send_frame or any(v is not None for v in (s.fader, s.pan, s.mute, s.solo)):
            # a send-only change on the Zen Go leaves the strip frame unsent
            t.write(proto.build_mix_command(PROFILE, m, ch, fader, pan,
                                            send if has_send else 0, mute, solo))
        slots[ch] = {**cur, "fader": fader, "pan": pan, "send": send,
                     "mute": mute, "solo": solo, **extra}
        DEV._cache_mixer(m, slots)

    DEV.submit(do)
    return {"ok": True}


@app.get("/api/stream")
async def stream():
    """Server-Sent Events: one JSON snapshot per line whenever the version
    bumps, ~25 Hz. EventSource on the browser side reconnects on its own."""
    async def gen():
        last = -1
        # prime the stream so a just-connected client gets state immediately
        while True:
            snap, ver = DEV.get()
            if ver != last:
                last = ver
                yield f"data: {json.dumps({'version': ver, **snap})}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.04)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _install_terminal_shutdown(server, device):
    """Make Uvicorn's terminal signal path stop the device service first.

    Uvicorn installs its own SIGINT/SIGTERM handlers inside ``Server.run``.
    Wrapping ``handle_exit`` keeps that behavior (including the second-Ctrl+C
    force-exit path) while also waking the HID worker immediately.  The
    wrapper is intentionally small because it runs in Python's signal context;
    the bounded worker join remains in ``run_server``'s ``finally`` block.
    """
    original = getattr(server, "handle_exit", None)
    shutdown_requested = threading.Event()

    def handle_exit(sig, frame):
        if not shutdown_requested.is_set():
            shutdown_requested.set()
            device.request_stop()
        original(sig, frame)

    if original is None:
        return server
    server.handle_exit = handle_exit
    return server


def run_server():
    """Run the local WebUI and make terminal shutdown deterministic."""
    DEV.start()
    try:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=int(os.environ.get("ANTELOPE_PORT", 8714)),
            log_level="warning",
            timeout_graceful_shutdown=2.0,
        )
        server = _install_terminal_shutdown(uvicorn.Server(config), DEV)
        server.run()
    except KeyboardInterrupt:
        # Uvicorn re-raises the captured SIGINT after restoring the terminal
        # handlers.  Treat that normal Ctrl+C path as a clean exit.
        pass
    finally:
        # This is idempotent and also covers startup/runtime exceptions that
        # never pass through the terminal signal handler.
        DEV.stop()


if __name__ == "__main__":
    run_server()

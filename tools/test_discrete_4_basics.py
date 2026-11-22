"""Discrete 4 (0xa270) preamp, output, mixer and strip-source frames.

Every expected payload is a frame the vendor panel sent in the d4_basics
captures of 2026-11-17 (hashes in profiles/discrete_4_a270.json) or in the
vendor panel run of 2026-11-22 (d4_panel/live.log); the readback bodies are the
vendor's own connect walks (d4_panel/start_state.txt, markers_state.txt). No
device is opened.
"""
import copy
import json
import os
import types
import unittest
from unittest import mock

from antelope import protocol
from tools.test_meter_sources import load_server_without_web_or_hid_boundaries
from webui.device_ui import features_for

server = load_server_without_web_or_hid_boundaries()   # no web stack, no HID scan

PROFILES = os.path.join(os.path.dirname(__file__), '..', 'profiles')


def load(name):
    with open(os.path.join(PROFILES, name + '.json')) as source:
        return json.load(source)


def payload(packet):
    return packet[16:].rstrip(b'\0').hex(' ')


# category 0x03 bodies from the connect walk: <group> then 32 (type, index)
STRIPS = '04 00 04 01 04 02 04 03 01 00 01 01 03 00 03 01 ' \
         '02 00 02 01 02 02 02 03 02 04 02 05 02 06 02 07'
TAIL = {6: ' 00 00' * 16, 10: ' 09 00' * 16, 11: ' 09 00' * 16,
        12: ' 09 00' * 16, 13: ' 09 00' * 16}
# the other destination groups as read at the start of the 2026-11-22 run
OUTPUT_GROUPS = {0: '00 00 00 01 01 02 01 03', 7: '00 00 00 01 00 02 00 03 01 00 01 01 03 00 03 01',
                 8: '00 00 00 01', 9: '00 00 00 01 00 02 00 03'}
GROUPS = [6, 10, 11, 12, 13]
# category 0x04 body from the same walk: 133 bytes, identical for all mixes
# bytes 0-47 of the first 0x73 report of the 2026-11-22 run
START_S73 = ('73' + ' 00' * 15 + ' 08 08 02 05 30 00 bb 80 00 00 ff 08 2b 00 18 00 1e 00 22 00 1e 00'
             ' 00 00 06 06 00 00 01 01 01 01')
MIX_BODY = bytes.fromhex(
    '00 20' + ' 5a 20' * 4 + ' 00 02 00 3e' * 14 + ' 60 00 20' + ' 60 20' * 4
    + ' 60 02 60 3e' * 14)


def routing_body(group, strips=STRIPS):
    if group in OUTPUT_GROUPS:
        return bytes([group]) + bytes.fromhex(OUTPUT_GROUPS[group])
    return bytes([group]) + bytes.fromhex(strips + TAIL[group])


def response(category, index, body):
    head = bytes([0x75, 0, 0, 0, 0x10, 0, 0, 0]) \
        + category.to_bytes(4, 'little') + index.to_bytes(4, 'little')
    return (head + body).ljust(320, b'\0')


class FakeTransport:
    """Answers routing readbacks from the walk and records every write."""

    def __init__(self):
        self.written = []

    def query(self, request, match, timeout=None):
        reply = response(request[8], request[12], routing_body(request[12]))
        return reply if match(reply) else None

    def write(self, packet):
        self.written.append(packet)


class Discrete4BasicsTests(unittest.TestCase):
    def setUp(self):
        self.profile = load('discrete_4_a270')

    def check(self, packet, opcode, expected):
        self.assertEqual(len(packet), 320)
        self.assertEqual(packet[:16].hex(' '),
                         f'70 00 00 00 {opcode:02x}' + ' 00' * 11)
        self.assertEqual(payload(packet), expected)

    def test_set_param_frames_match_capture(self):
        cases = [('gain', 0, 7, '50 00 07'), ('gain', 1, 6, '50 01 06'),
                 ('input_mode', 0, 2, '4f 00 02'), ('input_mode', 0, 1, '4f 00 01'),
                 ('phase_invert', 0, 1, '52 00 01'),
                 ('bus_level', 0, 0x2c, '47 00 2c'), ('bus_level', 1, 0x19, '47 01 19'),
                 ('bus_mute', 0, 1, '48 00 01'), ('bus_mute', 1, 1, '48 01 01'),
                 # dim: Monitor (bus 0) is id 4 and Headphone 1 (bus 1) is id 0
                 ('bus_dim', 0, 1, '66 04 01'), ('bus_dim', 1, 1, '66 00 01'),
                 # 2026-11-22: Hi-Z on preamp 2, phase on preamps 2-4, Headphones 2-4, Line Out
                 ('input_mode', 1, 2, '4f 01 02'), ('input_mode', 3, 0, '4f 03'),
                 ('phase_invert', 1, 1, '52 01 01'), ('phase_invert', 3, 1, '52 03 01'),
                 ('gain', 2, -12, '50 02 f4'), ('gain', 2, 65, '50 02 41'),
                 ('bus_level', 2, 0x1e, '47 02 1e'), ('bus_mute', 4, 1, '48 04 01'),
                 ('bus_dim', 2, 1, '66 01 01'), ('bus_dim', 3, 1, '66 02 01'), ('bus_dim', 4, 1, '66 03 01'),
                 ('bus_level', 5, 0x60, '47 05 60'), ('bus_mute', 5, 1, '48 05 01'),
                 ('reverb_return', 0, 0xda, '68 00 da')]
        for param, target, value, expected in cases:
            self.check(protocol.build_command(self.profile, param, target, value),
                       0x13, expected)
        self.check(protocol.build_command(self.profile, 'input_mode', 0, 0), 0x13, '4f')
        self.check(protocol.build_command(self.profile, 'bus_dim', 1, 0), 0x13, '66')

    def test_link_frames_match_capture(self):
        space = protocol.input_link_space(self.profile, 'preamp')
        self.check(protocol.build_link_command(self.profile, 0, True, space=space),
                   0x14, 'a2 00 00 01')
        on = protocol.build_mixer_link_commands(self.profile, 0, 0, True)
        self.assertEqual([payload(p) for p in on], ['a2 04 00 01', 'a2 03 00 01'])
        off = protocol.build_mixer_link_commands(self.profile, 0, 0, False)
        self.assertEqual([payload(p) for p in off], ['a2 04', 'a2 03'])
        # 2026-11-22: preamps 3+4, mix 0 strips 3+4 and 15+16, HEADPHONES 2 strips 7+8
        self.check(protocol.build_link_command(self.profile, 1, True, space=space),
                   0x14, 'a2 00 01 01')
        for mix, pair, frames in [(0, 1, ['a2 04 01 01', 'a2 03 01 01']), (0, 7, ['a2 03 07 01']),
                                  (1, 3, ['a2 03 13 01'])]:
            self.assertEqual([payload(p) for p in protocol.build_mixer_link_commands(
                self.profile, mix, pair, True)], frames)

    def test_mixer_frames_match_capture(self):
        # (mix, 0-based strip, fader, pan, mute, solo) -> captured payload
        cases = [(0, 0, 90, 0, True, False, 'd4 04 00 01 5a 60'),
                 (0, 0, 90, 0, False, False, 'd4 04 00 01 5a 20'),
                 (0, 0, 90, 0, False, True, 'd4 04 00 01 5a a0'),
                 (0, 0, 90, 1, False, False, 'd4 04 00 01 5a 21'),
                 (0, 0, 88, 0, False, False, 'd4 04 00 01 58 20'),
                 (0, 1, 90, 0, True, False, 'd4 04 00 02 5a 60'),
                 (0, 4, 0, -30, True, False, 'd4 04 00 05 00 42'),
                 (0, 4, 0, -30, False, False, 'd4 04 00 05 00 02'),
                 (0, 7, 0, 30, True, False, 'd4 04 00 08 00 7e'),
                 (1, 0, 90, 0, True, False, 'd4 04 01 01 5a 60'),
                 (2, 0, 90, 0, True, False, 'd4 04 02 01 5a 60'),
                 (3, 0, 90, 0, False, False, 'd4 04 03 01 5a 20')]
        for mix, strip, fader, pan, mute, solo, expected in cases:
            self.check(protocol.build_mix_command(
                self.profile, mix, strip, fader, pan, 0, mute, solo), 0x16, expected)

    def test_mixer_readback_matches_the_frames_the_panel_sent(self):
        slots = protocol.parse_mixer_record(self.profile, MIX_BODY)
        self.assertEqual(len(slots), 16)
        for strip, fader, pan in [(0, 90, 0), (1, 90, 0), (4, 0, -30), (7, 0, 30)]:
            self.assertEqual((slots[strip]['fader'], slots[strip]['pan']), (fader, pan))
            self.assertFalse(slots[strip]['mute'] or slots[strip]['solo'])
        # a read strip written back unchanged is the panel's "mute off" frame
        s = slots[4]
        self.check(protocol.build_mix_command(
            self.profile, 0, 4, s['fader'], s['pan'], 0, s['mute'], s['solo']),
            0x16, 'd4 04 00 05 00 02')

    def test_state_report_offsets(self):
        # first 0x73 report of the 2026-11-22 run, then the phase / mode bytes it moved to
        report = bytearray(bytes.fromhex(START_S73).ljust(320, b'\0'))
        state = protocol.parse_state(self.profile, bytes(report), 0)
        self.assertEqual((state['gain'], state['input_mode'], state['phase_invert']), (6, 1, 0))
        report[45] = 0x40                      # 52 01 01 in Mic mode: 45:00>40
        report[44] = 0x02                      # 4f 00 02: 44:01>02
        state = protocol.parse_state(self.profile, bytes(report), 1)
        self.assertEqual((state['input_mode'], state['phase_invert']), (0, 1))
        self.assertEqual(protocol.mode_name(self.profile, protocol.parse_state(
            self.profile, bytes(report), 0)['input_mode']), 'HiZ')
        self.assertNotIn('phantom', state)     # 48 V has no known bit: absent, never a made-up "off"
        report[29], report[31] = 0x03, 0x02    # Monitor muted + dimmed (66 04 01 while muted), HP1 dimmed
        monitor = protocol.parse_bus_state(self.profile, bytes(report), 0)
        self.assertEqual((monitor['level'], monitor['mute'], monitor['dim']), (0x2b, 1, 1))
        hp1 = protocol.parse_bus_state(self.profile, bytes(report), 1)
        self.assertEqual((hp1['level'], hp1['mute'], hp1['dim']), (0x18, 0, 1))
        device = server.Device(self.profile)
        channels = device._parse_channels(bytes(report))
        self.assertEqual([(c['mode'], c['phase_invert'], c['unread']) for c in channels[:2]],
                         [('HiZ', False, ['phantom']), ('Mic', True, ['phantom'])])
        buses = device._parse_buses(bytes(report))
        self.assertEqual([(b['bus'], b['level']) for b in buses],
                         [(0, 0x2b), (1, 0x18), (2, 0x1e), (3, 0x22), (4, 0x1e), (5, 0x00)])
        self.assertEqual(buses[5]['name'], 'line_out')   # shown in the Settings panel

    def test_strip_source_change_resends_the_five_captured_frames(self):
        device = server.Device(self.profile)
        self.assertTrue(device.mixer_available and device.mixer_source_available)
        self.assertEqual((device.n_mixes, device.mix_channels, device.bus_ids),
                         (4, 16, [0, 1, 2, 3, 4, 5]))
        transport = FakeTransport()
        emu2 = protocol.resolve_route_source(self.profile, 'emumic', 2)
        play1 = protocol.resolve_route_source(self.profile, 'compplay', 1)
        self.assertEqual((emu2, play1), ((0x0b, 1), (0x01, 0)))
        device._set_mixer_source(transport, 4, emu2)     # strip 5, as captured
        device._set_mixer_source(transport, 4, play1)    # and back
        for step, source in enumerate(('0b 01', '01 00')):
            strips = STRIPS[:24] + source + STRIPS[29:]
            for n, group in enumerate(GROUPS):
                self.check(transport.written[step * 5 + n], 0x53,
                           (f'd3 41 {group:02x} ' + strips + TAIL[group])
                           .rstrip('0 ').strip())
        self.assertEqual(len(transport.written), 10)

    def test_strip_source_refusals(self):
        device = server.Device(self.profile)
        transport = FakeTransport()
        play1 = (0x01, 0)
        with self.assertRaises(RuntimeError):      # strips 1-4 use group 09
            device._set_mixer_source(transport, 0, play1)
        with self.assertRaises(RuntimeError):      # beyond the 16 strips
            device._set_mixer_source(transport, 16, play1)
        with self.assertRaises(RuntimeError):      # rack channel: display only
            device._set_mixer_source(transport, 4, (0x04, 0))
        with self.assertRaises(RuntimeError):      # one mirror alone
            device._set_routing_changes(transport, [6], [(4, play1)])
        with self.assertRaises(RuntimeError):      # a loopback into the mixer map
            device._set_mixer_source(transport, 7, (0x05, 0))
        self.assertEqual(transport.written, [])
        feature = features_for('discrete_4_a270.json', self.profile)['mixer_sources']
        self.assertTrue(feature['writable'])
        selectable = {o['key'] for o in feature['options'] if o.get('selectable') is not False}
        self.assertEqual(selectable, {f'{kind}:{n}' for kind, count in (
            ('preamp', 4), ('compplay', 16), ('adat', 8), ('spdif', 2), ('osc', 2), ('emumic', 4))
            for n in range(1, count + 1)} | {'mute:0'})

    def test_out_of_range_targets_are_refused(self):
        refused = protocol.ConstraintError
        for target, space in [(4, 'input'), (-1, 'input'), (6, 'bus'), (-1, 'bus')]:
            with self.assertRaises(refused):
                protocol.check_target(self.profile, target, space)
        for mix, strip in [(4, 0), (-1, 0), (0, 16), (0, -1)]:
            with self.assertRaises(refused):
                protocol.build_mix_command(self.profile, mix, strip, 0, 0, 0)
        for mix, pair in [(0, 2), (0, 8), (1, 0), (2, 3), (4, 0)]:
            with self.assertRaises(refused):
                protocol.build_mixer_link_commands(self.profile, mix, pair, True)
        for category, index in [(0x04, 4), (0x03, 14), (0x0b, 1), (0x0b, 2)]:
            with self.assertRaises(refused):
                protocol.build_readback_query(self.profile, category, index)
        with self.assertRaises(refused):           # the Zen Go's chain-write opcode
            protocol.check_opcode(self.profile, 0x23)
        with self.assertRaises(KeyError):          # 48 V is not mapped
            protocol.build_command(self.profile, 'phantom', 0, 1)
        with self.assertRaises(ValueError):
            protocol.resolve_route_source(self.profile, 'compplay', 17)
        # levels are clamped, not wrapped
        self.check(protocol.build_mix_command(self.profile, 0, 0, 200, 99, 0),
                   0x16, 'd4 04 00 01 5a 3e')
        self.assertEqual(protocol.gain_range(self.profile, 'Mic'), (-12, 65))   # swept 2026-11-22
        self.assertEqual(protocol.gain_range(self.profile, 'HiZ'), (0, 20))     # not captured
        self.assertEqual(protocol.bus_level_range(self.profile), (0, 96))

    def test_headphones_2_4_and_line_out_are_confirmed_buses(self):
        self.assertEqual(protocol.channel_space_bounds(self.profile, 'bus'), [0, 1, 2, 3, 4, 5])
        with self.assertRaises(protocol.ConstraintError):   # the Line Out has no dim
            protocol.build_command(self.profile, 'bus_dim', 5, 1)
        flipped = copy.deepcopy(self.profile)                # an unconfirmed entry is still refused
        flipped['buses']['known']['2']['status'] = 'unconfirmed'
        self.assertEqual(protocol.channel_space_bounds(flipped, 'bus'), [0, 1, 3, 4, 5])
        with self.assertRaises(protocol.ConstraintError):
            protocol.build_command(flipped, 'bus_dim', 2, 1)

    def test_safety_constraints(self):
        constraints = self.profile['constraints']
        self.assertEqual(protocol.protected_mute_buses(self.profile), [0])
        self.assertEqual(constraints['min_write_interval_ms'], 250)
        self.assertEqual(constraints['allowed_opcodes'],
                         ['0x12', '0x13', '0x14', '0x16', '0x1d', '0x53', '0x54', '0x5b'])
        self.assertNotIn('phantom', self.profile['params'])
        self.assertEqual(self.profile['channels']['hiz_channels'], [0, 1])

    def test_other_devices_keep_their_frames(self):
        zen = load('zen_go_sc')
        self.assertEqual(payload(protocol.build_command(zen, 'bus_dim', 2, 1)), '66 02 01')
        # strip byte = strip number (vendor panel 2026-11-21: strip 9 of the HP2 page)
        self.assertEqual(payload(protocol.build_mix_command(zen, 1, 8, 2, -30, 0)),
                         'd4 04 01 09 02 02')
        self.assertEqual([payload(p) for p in
                          protocol.build_mixer_link_commands(zen, 1, 2, True)],
                         ['a2 03 12 01'])
        self.assertEqual(protocol.channel_space_bounds(zen, 'bus'), [0, 1, 2])
        self.assertEqual(protocol.protected_mute_buses(zen), [0, 1, 2])


# Vendor panel run of 2026-11-22: every payload below is a frame the panel sent
# (d4_panel/live.log), every body a reply the device gave (start / marker walks).
MARK_MIX0 = ('00 20' + ' 5a 20' * 4 + ' 00 02 00 3e' * 14         # cat 04 index 0, marker reopen
             + ' da 00 20 60 20 5b 20 60 20 60 20' + ' 60 02 60 3e' * 14)
START_REVERB = '00 51 64 00 64 0b 0d 18 42 32 01 ff' + ' 00' * 10 + ' ff' + ' 00' * 10 + ' ff'


def panel_payload(packet):
    return f'{packet[4]:02x} | ' + packet[16:].rstrip(b'\0').hex(' ')


class Discrete4PanelTests(unittest.TestCase):
    def setUp(self):
        """A Discrete 4 server whose commands run at once on a recording transport."""
        self.profile = load('discrete_4_a270')
        self.device = server.Device(self.profile)
        self.sent = []
        transport = types.SimpleNamespace(write=self.sent.append, query=lambda *args, **kw: None)
        patches = [
            mock.patch.object(server, 'PROFILE', self.profile),
            mock.patch.object(server, 'UI_FEATURES',
                              features_for('discrete_4_a270.json', self.profile)),
            mock.patch.object(server, 'DEV', self.device),
            mock.patch.object(self.device, 'submit', side_effect=lambda fn: fn(transport)),
            mock.patch.object(server, '_bad', side_effect=lambda msg: {'ok': False, 'error': msg}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.device.mixer = {mix: protocol.parse_mixer_record(self.profile, bytes.fromhex(MARK_MIX0))
                             for mix in range(4)}
        self.device.auraverb = {0: protocol.parse_auraverb_record(
            self.profile, bytes.fromhex(START_REVERB))}
        self.device.routing = {group: protocol.parse_routing_record(
            self.profile, routing_body(group).ljust(80, b'\0'))[1] for group in [0, 7, 8, 9] + GROUPS}

    def call(self, api, **fields):
        del self.sent[:]
        result = api(types.SimpleNamespace(**fields))
        return result, [panel_payload(packet) for packet in self.sent]

    def refused(self, api, **fields):
        result, sent = self.call(api, **fields)
        self.assertEqual((result['ok'], sent), (False, []), fields)

    def test_features_the_existing_pages_show(self):
        features = features_for('discrete_4_a270.json', self.profile)
        self.assertEqual([d['id'] for d in features['routing']['destinations']], [6, 0, 7, 8, 9])
        self.assertTrue(features['routing']['writable'])
        self.assertTrue(features['auraverb']['enabled'] and features['settings']['enabled'])
        self.assertTrue(self.device.auraverb_available)

    def test_dim_keeps_the_mute_on_this_device(self):
        # 66 04 01 on a muted Monitor: 29:01>03; 66 04 00: 03>01 (all five buses alike)
        report = bytearray(bytes.fromhex(START_S73).ljust(320, b'\0'))
        report[29] = 0x01
        self.device.snapshot = {'online': True, 'buses': self.device._parse_buses(bytes(report))}
        toggle = server.api_bus_toggle
        self.assertEqual(self.call(toggle, bus=0, param='dim', on=True)[1], ['13 | 66 04 01'])
        self.assertEqual(self.call(toggle, bus=0, param='dim', on=False)[1], ['13 | 66 04'])
        self.assertEqual(self.call(toggle, bus=4, param='dim', on=True)[1], ['13 | 66 03 01'])
        self.assertFalse(protocol.bus_flag_may_release_mute(self.profile, 0, 'bus_dim', {'mute': True}))
        self.assertEqual(protocol.protected_mute_buses(self.profile), [0])
        self.assertEqual(self.call(toggle, bus=5, param='mute', on=True)[1], ['13 | 48 05 01'])
        self.refused(toggle, bus=5, param='dim', on=True)        # the Line Out has no dim
        self.assertEqual(self.call(server.api_bus, bus=5, level=0x60)[1], ['13 | 47 05 60'])
        self.assertEqual(self.call(server.api_bus, bus=3, level=0x22)[1], ['13 | 47 03 22'])

    def test_hiz_only_on_preamps_1_and_2(self):
        self.assertEqual(self.call(server.api_mode, channel=1, mode='hiz')[1], ['13 | 4f 01 02'])
        self.refused(server.api_mode, channel=2, mode='hiz')
        self.refused(server.api_mode, channel=3, mode='hiz')
        self.assertEqual(self.call(server.api_mode, channel=3, mode='mic')[1], ['13 | 4f 03'])
        self.assertEqual(self.call(server.api_toggle, channel=2, param='phase_invert', on=True)[1],
                         ['13 | 52 02 01'])

    def test_link_readbacks_from_the_marker_reopen(self):
        preamps = protocol.parse_readback_records(self.profile, bytes.fromhex('00 01').ljust(8, b'\0'), 0x0b, 0)
        self.assertEqual([r['linked'] for r in preamps[:2]], [False, True])     # preamps 3+4 linked
        mixer = protocol.parse_readback_records(self.profile, bytes.fromhex(
            '00 01' + ' 00' * 17 + ' 01').ljust(24, b'\0'), 0x0b, 3)
        self.assertEqual([r['record_index'] for r in mixer if r['linked']], [1, 19])   # mix 0 pair 1, mix 1 pair 3
        self.assertEqual(self.call(server.api_link, pair=1, enabled=True)[1], ['14 | a2 00 01 01'])
        self.refused(server.api_link, pair=2, enabled=True)

    def test_gazelle_reverb_send_and_return(self):
        change = server.api_auraverb_change
        base = dict(mix=0, param=None, value=None, enabled=None)
        self.assertEqual(self.call(change, **{**base, 'param': 'color', 'value': 99})[1],
                         ['1d | da 0b 00 51 63 00 64 0b 0d 18 42 32 01'])
        self.call(change, **{**base, 'param': 'color', 'value': 100})
        self.assertEqual(self.call(change, **{**base, 'enabled': False})[1],
                         ['1d | da 0b 00 51 64 00 64 0b 0d 18 42 32'])
        self.refused(change, **{**base, 'param': 'reverb_level', 'value': 0})
        mix = server.api_mix
        none = dict(fader=None, pan=None, send=None, send_pan=None, mute=None, solo=None)
        self.assertEqual(self.call(mix, **{**none, 'mix': 0, 'channel': 1, 'send': 0x5b})[1],
                         ['16 | e9 04 00 02 5b 20'])
        self.assertEqual(self.call(mix, **{**none, 'mix': 0, 'channel': 0, 'send_pan': 1})[1],
                         ['16 | e9 04 00 01 60 21'])
        self.refused(mix, **{**none, 'mix': 1, 'channel': 0, 'send': 0})       # sends are on mix 0
        slots = protocol.parse_mixer_record(self.profile, bytes.fromhex(MARK_MIX0))
        self.assertEqual((slots[1]['send'], slots[1]['send_pan']), (0x5b, 0))
        back = server.api_reverb_return
        self.assertEqual(self.call(back, bus=0, level=0x5a, mute=True)[1], ['13 | 68 00 da'])
        self.assertEqual(self.call(back, bus=0, level=0x60, mute=False)[1], ['13 | 68 00 60'])  # restore value, not captured as a write
        self.refused(back, bus=1, level=0x60, mute=False)                       # HP2-4 returns: not captured
        self.device.structured[(0x17, 0)] = protocol.parse_readback_records(
            self.profile, bytes.fromhex('da 60 60 60'), 0x17, 0)
        self.assertEqual(server.api_auraverb()['returns'], [{'bus': 0, 'level': 0x5a, 'mute': True}])

    def test_settings_frames_and_state_bytes(self):
        self.assertEqual(self.call(server.api_output_trim, target=0, value=6)[1], ['54 | cb 42 00 01 06'])
        self.assertEqual(self.call(server.api_output_trim, target=0, value=0)[1], ['54 | cb 42 00 01'])
        self.assertEqual(self.call(server.api_pan_law, value=3)[1], ['12 | 24 03'])
        self.refused(server.api_pan_law, value=4)
        self.assertEqual(self.call(server.api_oscillator, value=0x34)[1], ['12 | 0a 34'])
        self.assertEqual(self.call(server.api_oscillator, value=0xb0)[1], ['12 | 0a b0'])
        self.refused(server.api_oscillator, value=0x32)                           # bit 0x02 never seen
        self.assertEqual(self.call(server.api_brightness, value=0x64)[1], ['12 | 0e 64'])
        report = bytearray(bytes.fromhex(START_S73).ljust(320, b'\0'))
        report[25] = 0x18                                                         # cb 42 00 01 06: 25:14>18
        self.assertEqual(self.device._parse_output_trim(bytes(report)), {'monitor_a': 6})
        self.assertEqual(protocol.parse_state_scalar(self.profile, bytes(report), 'oscillator_byte_offset'), 0x30)
        self.assertEqual(protocol.parse_state_scalar(
            self.profile, bytes(report), 'screen_brightness_byte_offset'), 0xff)   # raw, as read at the start
        self.assertEqual(protocol.parse_readback_records(self.profile, b'\x03', 0x12, 0)[0]['index'], 3)

    def test_line_out_trims_are_one_whole_frame(self):
        trim = server.api_line_trim
        self.assertEqual(self.call(trim, linked=False, trims=[0, 0, 6, 0])[1],
                         ['54 | cb 42 01 01 00 00 00 00 06'])
        self.assertEqual(self.call(trim, linked=False, trims=[0, 0, 5, 1])[1],
                         ['54 | cb 42 01 01 00 00 00 00 05 00 01'])
        self.assertEqual(self.call(trim, linked=True, trims=[1, 1, 1, 1])[1],
                         ['54 | cb 42 01 00 01 00 01 00 01 00 01'])
        for trims in ([0, 0, 7, 0], [0, 0, 6], [-1, 0, 0, 0]):
            self.refused(trim, linked=False, trims=trims)
        state = protocol.parse_readback_records(self.profile, bytes.fromhex(
            '01 00 01 00 01 00 01 00 01'), 0x0e, 0)[0]                             # marker reopen
        self.assertEqual([state[k] for k in ('link', 'trim_1', 'trim_2', 'trim_3', 'trim_4')],
                         [0, 1, 1, 1, 1])
        start = protocol.parse_readback_records(self.profile, bytes.fromhex(
            '01 01 00 00 00 00 06').ljust(9, b'\0'), 0x0e, 0)[0]
        self.assertEqual([start[k] for k in ('link', 'trim_1', 'trim_2', 'trim_3', 'trim_4')],
                         [1, 0, 0, 6, 0])

    def test_output_and_rack_input_routing_write_one_group_each(self):
        route = server.api_route
        cases = [(0, 1, 'compplay', 16, 'd3 41 00 00 00 01 0f 01 02 01 03'),     # Line Out 2
                 (0, 1, 'emumic', 4, 'd3 41 00 00 00 0b 03 01 02 01 03'),
                 (0, 1, 'osc', 2, 'd3 41 00 00 00 0a 01 01 02 01 03'),
                 (0, 0, 'mute', None, 'd3 41 00 09 00 00 01 01 02 01 03'),
                 (7, 0, 'mute', None, 'd3 41 07 09 00 00 01 00 02 00 03 01 00 01 01 03 00 03 01'),
                 (8, 1, 'compplay', 1, 'd3 41 08 00 00 01'),
                 (9, 0, 'loopback_hp1', 1, 'd3 41 09 05 00 00 01 00 02 00 03'),    # Record 1
                 (9, 0, 'emumic', 1, 'd3 41 09 0b 00 00 01 00 02 00 03')]
        for dest, channel, kind, number, frame in cases:
            self.device.routing = {g: protocol.parse_routing_record(
                self.profile, routing_body(g).ljust(80, b'\0'))[1] for g in [0, 7, 8, 9] + GROUPS}
            self.assertEqual(self.call(route, dest=dest, channel=channel, kind=kind, number=number)[1],
                             ['53 | ' + frame], (dest, kind))
        for dest, channel, kind, number in [(0, 0, 'loopback_hp1', 1), (8, 0, 'afx', 1), (9, 0, 'afx', 1),
                                            (6, 7, 'loopback_hp2', 2), (6, 0, 'preamp', 1)]:
            self.refused(route, dest=dest, channel=channel, kind=kind, number=number)
        # strip 5 = PREAMP 4: the five mirrored frames the panel sent
        sent = self.call(route, dest=6, channel=4, kind='preamp', number=4)[1]
        strips = STRIPS[:24] + '00 03' + STRIPS[29:]
        self.assertEqual(sent, [('53 | ' + f'd3 41 {g:02x} ' + strips + TAIL[g]).rstrip('0 ') for g in GROUPS])
        listed = {d['id']: d.get('sources') for d in server.api_routing()['dests']}
        self.assertIn('loopback_hp2', listed[9])
        self.assertNotIn('loopback_hp2', listed[0])


if __name__ == '__main__':
    unittest.main()

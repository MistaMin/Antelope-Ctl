"""Discrete 4 (0xa270) preamp, output, mixer and strip-source frames.

Every expected payload is a frame the vendor panel sent in the d4_basics
captures of 2026-11-17 (hashes in profiles/discrete_4_a270.json); the readback
bodies are the vendor's own connect walk. No device is opened.
"""
import copy
import json
import os
import unittest

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
GROUPS = [6, 10, 11, 12, 13]
# category 0x04 body from the same walk: 133 bytes, identical for all mixes
MIX_BODY = bytes.fromhex(
    '00 20' + ' 5a 20' * 4 + ' 00 02 00 3e' * 14 + ' 60 00 20' + ' 60 20' * 4
    + ' 60 02 60 3e' * 14)


def routing_body(group, strips=STRIPS):
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
                 ('bus_dim', 0, 1, '66 04 01'), ('bus_dim', 1, 1, '66 00 01')]
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
        report = bytearray(320)
        report[0] = 0x73
        report[28:38] = bytes.fromhex('2b 02 18 01 1e 00 22 00 1e 00')
        report[40:48] = bytes.fromhex('06 07 00 00 02 01 01 01')
        state = protocol.parse_state(self.profile, bytes(report), 0)
        self.assertEqual((state['gain'], state['input_mode']), (6, 2))
        self.assertEqual(protocol.mode_name(self.profile, state['input_mode']), 'HiZ')
        # phase and 48 V have no known byte: absent, never a made-up "off"
        self.assertNotIn('phase_invert', state)
        self.assertNotIn('phantom', state)
        self.assertEqual(protocol.parse_state(self.profile, bytes(report), 1)['gain'], 7)
        monitor = protocol.parse_bus_state(self.profile, bytes(report), 0)
        self.assertEqual((monitor['level'], monitor['mute'], monitor['dim']), (0x2b, 0, 1))
        hp1 = protocol.parse_bus_state(self.profile, bytes(report), 1)
        self.assertEqual((hp1['level'], hp1['mute'], hp1['dim']), (0x18, 1, 0))
        device = server.Device(self.profile)
        channel = device._parse_channels(bytes(report))[0]
        self.assertEqual((channel['mode'], channel['unread']),
                         ('HiZ', ['phantom', 'phase_invert']))
        self.assertEqual([b['bus'] for b in device._parse_buses(bytes(report))], [0, 1])

    def test_strip_source_change_resends_the_five_captured_frames(self):
        device = server.Device(self.profile)
        self.assertTrue(device.mixer_available and device.mixer_source_available)
        self.assertEqual((device.n_mixes, device.mix_channels, device.bus_ids),
                         (4, 16, [0, 1]))
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
        with self.assertRaises(RuntimeError):      # group 09 is not mapped
            device._set_routing_changes(transport, [9], [(0, play1)])
        self.assertEqual(transport.written, [])
        feature = features_for('discrete_4_a270.json', self.profile)['mixer_sources']
        self.assertTrue(feature['writable'])
        selectable = {o['key'] for o in feature['options'] if o.get('selectable') is not False}
        self.assertEqual(selectable, {
            'compplay:1', 'compplay:2', 'adat:1', 'adat:2', 'adat:3', 'adat:4',
            'adat:5', 'adat:6', 'adat:7', 'adat:8', 'spdif:1', 'spdif:2',
            'emumic:1', 'emumic:2', 'mute:0'})

    def test_out_of_range_targets_are_refused(self):
        refused = protocol.ConstraintError
        for target, space in [(4, 'input'), (-1, 'input'), (5, 'bus'), (2, 'bus')]:
            with self.assertRaises(refused):
                protocol.check_target(self.profile, target, space)
        for mix, strip in [(4, 0), (-1, 0), (0, 16), (0, -1)]:
            with self.assertRaises(refused):
                protocol.build_mix_command(self.profile, mix, strip, 0, 0, 0)
        for mix, pair in [(0, 1), (0, 8), (1, 0), (4, 0)]:
            with self.assertRaises(refused):
                protocol.build_mixer_link_commands(self.profile, mix, pair, True)
        for category, index in [(0x04, 4), (0x03, 14), (0x0b, 1), (0x0b, 2)]:
            with self.assertRaises(refused):
                protocol.build_readback_query(self.profile, category, index)
        with self.assertRaises(refused):           # SET_GLOBAL is not captured
            protocol.check_opcode(self.profile, 0x12)
        with self.assertRaises(KeyError):          # 48 V is not mapped
            protocol.build_command(self.profile, 'phantom', 0, 1)
        with self.assertRaises(ValueError):
            protocol.resolve_route_source(self.profile, 'compplay', 3)
        # levels are clamped, not wrapped
        self.check(protocol.build_mix_command(self.profile, 0, 0, 200, 99, 0),
                   0x16, 'd4 04 00 01 5a 3e')
        self.assertEqual(protocol.gain_range(self.profile, 'Mic'), (0, 20))
        self.assertEqual(protocol.bus_level_range(self.profile), (0, 96))

    def test_unconfirmed_headphone_bus_is_refused_until_one_line_flips_it(self):
        self.assertEqual(protocol.channel_space_bounds(self.profile, 'bus'), [0, 1])
        for param in ('bus_dim',):
            with self.assertRaises(protocol.ConstraintError):
                protocol.build_command(self.profile, param, 2, 1)
        flipped = copy.deepcopy(self.profile)
        flipped['buses']['known']['2']['status'] = 'confirmed'
        self.assertEqual(protocol.channel_space_bounds(flipped, 'bus'), [0, 1, 2])
        protocol.check_target(flipped, 2, 'bus')
        self.check(protocol.build_command(flipped, 'bus_dim', 2, 1), 0x13, '66 01 01')
        self.check(protocol.build_command(flipped, 'bus_mute', 2, 1), 0x13, '48 02 01')
        report = bytearray(320)
        report[32:34] = bytes.fromhex('1e 02')
        hp2 = protocol.parse_bus_state(flipped, bytes(report), 2)
        self.assertEqual((hp2['level'], hp2['dim']), (0x1e, 1))

    def test_safety_constraints(self):
        constraints = self.profile['constraints']
        self.assertEqual(protocol.protected_mute_buses(self.profile), [0])
        self.assertEqual(constraints['min_write_interval_ms'], 250)
        self.assertEqual(constraints['allowed_opcodes'],
                         ['0x13', '0x14', '0x16', '0x53', '0x5b'])
        self.assertNotIn('phantom', self.profile['params'])
        self.assertEqual(self.profile['channels']['hiz_channels'], [0])

    def test_other_devices_keep_their_frames(self):
        zen = load('zen_go_sc')
        self.assertEqual(payload(protocol.build_command(zen, 'bus_dim', 2, 1)), '66 02 01')
        self.assertEqual(payload(protocol.build_mix_command(zen, 1, 3, 12, 0, 0)),
                         'd4 04 01 03 0c 20')
        self.assertEqual([payload(p) for p in
                          protocol.build_mixer_link_commands(zen, 1, 2, True)],
                         ['a2 03 12 01'])
        self.assertEqual(protocol.channel_space_bounds(zen, 'bus'), [0, 1, 2])
        self.assertEqual(protocol.protected_mute_buses(zen), [])


if __name__ == '__main__':
    unittest.main()

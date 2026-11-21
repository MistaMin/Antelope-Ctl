"""Insert-rack chain writes rebuilt from the captured vendor frames."""
import json
import os
import threading
import unittest

from antelope import protocol
from webui.afx_inserts import InsertRack

PROFILES = os.path.join(os.path.dirname(__file__), '..', 'profiles')


def load(name):
    with open(os.path.join(PROFILES, name + '.json')) as source:
        return json.load(source)


def payload(packet):
    return packet[16:].rstrip(b'\0').hex(' ')


CLEARQ_DEFAULTS = ('1e 00 47 00 00 00 00 | c8 00 47 00 00 00 02 | e8 03 47 00 00 00 02 | '
                   '88 13 47 00 00 00 02 | 98 3a 47 00 00 00 01')


class FakeDevice:
    """Enough of a Discrete 4 for InsertRack: racks, free instances, ClearQ's per-band store."""

    def __init__(self, profile):
        self.profile, self.snapshot, self.structured, self.rb_ver = profile, {'online': True}, {}, 0
        self._lock, self._transport = threading.RLock(), self
        self.racks, self.written, self.deaf = {}, [], False
        self.clearq = bytearray(36 * 8)

    def submit(self, work):
        work(self)

    def write(self, packet):
        self.written.append(packet)
        if self.deaf:
            return
        if packet[16] == 0xd7:
            self.racks[packet[18]] = packet[19:35]
        elif packet[16] == 0xd5 and packet[18] == 1:   # one band: <inst> <band> <7 bytes>
            start = packet[19] * 36 + packet[20] * 7
            self.clearq[start:start + 7] = packet[21:28]

    def query(self, request, accept, timeout):
        category, index = request[8], request[12]
        body = {0x19: bytes(self.racks.get(index, b'')), 0x15: bytes([1, 8, 3, 1, 4, 1]), 0x0b: b'',
                0x07: bytes(self.clearq) if index == 0 else bytes([1]) + bytes(16)}[category]
        reply = (bytes([0x75]) + bytes(7) + bytes([category, 0, 0, 0]) + request[12:16] + body).ljust(320, b'\0')
        return reply if accept(reply) else None


class InsertChainTests(unittest.TestCase):
    def test_zen_go_frames_match_capture(self):
        profile = load('zen_go_sc')
        cases = [([(27, 1)], 'd7 11 01 1b 01'),
                 ([(27, 1), (39, 0)], 'd7 11 01 1b 01 27'),
                 ([(27, 1), (39, 0), (51, 0)], 'd7 11 01 1b 01 27 00 33'),
                 ([(51, 0)], 'd7 11 01 33'),
                 ([], 'd7 11 01')]
        for slots, expected in cases:
            packet = protocol.build_afx_insert_chain(profile, 1, slots)
            self.assertEqual(len(packet), 320)
            self.assertEqual(packet[:5].hex(' '), '70 00 00 00 23')
            self.assertEqual(payload(packet), expected)

    def test_discrete_4_frames_match_capture(self):
        profile = load('discrete_4_a270')
        cases = [(0, [(27, 0)], 'd7 49 00 1b'),
                 (0, [(27, 1)], 'd7 49 00 1b 01'),
                 (1, [(27, 0), (39, 0), (51, 0)], 'd7 49 01 1b 00 27 00 33'),
                 (1, [], 'd7 49 01'),
                 (0, [], 'd7 49')]
        for strip, slots, expected in cases:
            packet = protocol.build_afx_insert_chain(profile, strip, slots)
            self.assertEqual(packet[:5].hex(' '), '70 00 00 00 5b')
            self.assertEqual(payload(packet), expected)

    def test_contract_is_enforced(self):
        profile = load('discrete_4_a270')
        writes = profile['afx_inserts']['writes']
        refused = [(max(writes['strips']) + 1, [(27, 0)]),   # unverified strip
                   (0, [(73, 0)]),                            # unidentified effect
                   (0, [(27, 0), (27, 0)]),                   # repeated instance
                   (0, [(27, 0)] * 0 + [(27, i) for i in range(writes['max_slots'] + 1)])]
        for strip, slots in refused:
            with self.assertRaises(protocol.ConstraintError):
                protocol.build_afx_insert_chain(profile, strip, slots)

    def test_discrete_4_powergate_parameter_frames_match_capture(self):
        profile = load('discrete_4_a270')
        effect = protocol.afx_parameter_effect(profile, 39)
        defaults = bytes.fromhex('5a0064003200000000')
        self.assertEqual(protocol.afx_block_get(effect, defaults),
                         {'threshold': 90, 'range': 0, 'attack': 100, 'decay': 50, 'hold': 0, 'gain': 0})
        cases = [('threshold', 91, '27 00 5b 00 64 00 32 00 00 00 00'),
                 ('attack', 110, '27 00 5a 00 6e 00 32 00 00 00 00'),
                 ('hold', 40, '27 00 5a 00 64 00 32 00 28 00 00'),
                 ('range', 1, '27 00 5a 01 64 00 32 00 00 00 00'),
                 ('decay', 60, '27 00 5a 00 64 00 3c 00 00 00 00'),
                 ('gain', 1, '27 00 5a 00 64 00 32 00 00 00 01')]
        for key, value, expected in cases:
            packet = protocol.build_afx_parameter_write(
                profile, 39, 0, protocol.afx_block_set(effect, defaults, key, value))
            self.assertEqual(packet[:5].hex(' '), '70 00 00 00 1d')
            self.assertEqual(packet[16:18].hex(' '), 'd5 0b')
            self.assertEqual(packet[18:29].hex(' '), expected)
        self.assertEqual(protocol.afx_block_get(
            effect, protocol.afx_block_set(effect, defaults, 'gain', -24))['gain'], -24)
        for key, value in (('gain', 13), ('attack', 0), ('hold', 5001), ('unknown', 1)):
            with self.assertRaises(protocol.ConstraintError):
                protocol.afx_block_set(effect, defaults, key, value)
        self.assertEqual(protocol.build_afx_state_query(profile, 39)[:16].hex(' '),
                         '74 00 00 00 10 00 00 00 07 00 00 00 27 00 00 00')
        self.assertIsNone(protocol.afx_parameter_effect(profile, 4))  # cabinet: not mapped

    def test_discrete_4_effect_maps_are_consistent(self):
        profile = load('discrete_4_a270')
        effects = profile['afx_inserts']['parameters']['effects']
        self.assertEqual(len(effects), 47)
        for kind, effect in effects.items():
            length = effect['block_length']
            if effect.get('editable') is False:   # read not captured: no read layout may be claimed
                self.assertNotIn('read_stride', effect, kind)
            else:
                self.assertGreater(effect['read_stride'], length, kind)
            keys = [field['key'] for field in effect['fields']]
            self.assertEqual(len(keys), len(set(keys)), kind)
            taken = set()
            for field in effect['fields']:
                width, signed = protocol._AFX_FIELD_TYPES[field['type']]
                span = set(range(field['offset'], field['offset'] + width))
                self.assertFalse(span & taken, f'{kind} {field["key"]} overlaps')
                self.assertLessEqual(max(span), length - 1, kind)
                taken |= span
                low = -(1 << (8 * width - 1)) if signed else 0
                self.assertLessEqual(low, field['min'], kind)
                self.assertLess(field['min'], field['max'], kind)
                self.assertLess(field['max'], (1 << (8 * width - (1 if signed else 0))), kind)
            self.assertIn(kind, profile['afx_inserts']['writes']['effects'])
        # PowerFFC's times are 32-bit: the block the vendor panel sent, release changed.
        ffc = effects['2']
        block = bytes.fromhex('102700001027000 0ffff64000000001800 00'.replace(' ', ''))
        self.assertEqual(protocol.afx_block_get(ffc, block)['release'], 10000)
        changed = protocol.afx_block_set(ffc, block, 'release', 600000)
        self.assertEqual(changed[4:8].hex(' '), 'c0 27 09 00')
        packet = protocol.build_afx_parameter_write(profile, 2, 0, changed)
        self.assertEqual(packet[4], 0x14 + 0x12)
        self.assertEqual(packet[16:20].hex(' '), 'd5 14 02 00')

    def test_float_fields_round_trip_and_refuse_bad_values(self):
        zen = load('zen_go_sc')
        effect = protocol.afx_parameter_effect(zen, 75)   # Instinct: 26 little-endian float32 values
        half, zero = '0000003f', '00000000'
        # instinct_probe.pcapng: the block before the first control was moved.
        block = bytes.fromhex(half * 4 + '0000c842' + half + zero + half + '0000c442' + half * 2 + '0000803f'
                              + zero * 3 + half * 4 + '0000c442' + half * 3 + zero + half * 2)
        values = protocol.afx_block_get(effect, block)
        self.assertEqual((values['punch'], values['boost_freq'], values['filter_freq'], values['centre_knob']),
                         (0.5, 100.0, 98.0, 0.0))
        changed = protocol.afx_block_set(effect, block, 'punch', 0.51)
        self.assertEqual((changed[:4].hex(), changed[4:]), ('5c8f023f', block[4:]))   # the frame the panel sent
        # What is read back (0.50999999...) encodes to the same bytes, and an int is taken as a float.
        self.assertEqual(protocol.afx_block_set(effect, block, 'punch', protocol.afx_block_get(effect, changed)['punch']), changed)
        self.assertEqual(protocol.afx_block_set(effect, block, 'volume', 1)[100:].hex(), '0000803f')
        packet = protocol.build_afx_parameter_write(zen, 75, 0, changed)
        self.assertEqual((packet[4], packet[16:24].hex(' ')), (0x7c, 'd5 6a 4b 00 5c 8f 02 3f'))
        # float32(0.01) is below the double 0.01: the lower limit itself must still be in range.
        lowest = protocol.afx_block_set(effect, block, 'filter_q', 0.01)
        protocol.build_afx_parameter_write(zen, 75, 0, lowest)
        for key, value in (('punch', 1.01), ('punch', -0.01), ('filter_q', 0.009), ('punch', float('nan')),
                           ('punch', float('inf')), ('punch', 1e40), ('punch', '0.5'), ('punch', True)):
            with self.assertRaises(protocol.ConstraintError, msg=(key, value)):
                protocol.afx_block_set(effect, block, key, value)
        with self.assertRaises(protocol.ConstraintError):   # a block holding an out-of-range float is not sent
            protocol.build_afx_parameter_write(zen, 75, 0, bytes.fromhex('0000c07f') + block[4:])
        gate = protocol.afx_parameter_effect(load('discrete_4_a270'), 39)
        with self.assertRaises(protocol.ConstraintError):   # integer fields still refuse a float
            protocol.afx_block_set(gate, bytes.fromhex('5a0064003200000000'), 'threshold', 91.0)

    def test_zen_go_shares_the_discrete_4_effect_maps(self):
        zen, d4 = load('zen_go_sc'), load('discrete_4_a270')
        effects = zen['afx_inserts']['parameters']['effects']
        self.assertEqual(len(effects), 69)
        own = {'53', '54', '59', '63', '64', '65', '66', '67', '68', '69', '70', '71', '73', '74', '75', '77', '78',
               '80', '82', '83', '85', '86'}  # Zen-Go-only effects
        self.assertEqual(protocol.afx_block_get(effects['54'], bytes.fromhex('21a00014320132'))['feedback'], -96)
        for kind, effect in effects.items():
            if kind in own:
                self.assertEqual((effect['read_offset'], effect['read_stride']), (1, effect['block_length'] + 1), kind)
                continue
            # The Discrete 4's fields come first; switches clicked only on the Zen Go follow.
            shared = d4['afx_inserts']['parameters']['effects'][kind]['fields']
            self.assertEqual(effect['fields'][:len(shared)], shared, kind)
            taken = [byte for field in effect['fields'] for byte in
                     range(field['offset'], field['offset'] + protocol._AFX_FIELD_TYPES[field['type']][0])]
            self.assertEqual(len(taken), len(set(taken)), kind)
            self.assertEqual(effect.get('read_offset'), 0 if kind == '1' else 1)
        ratio = next(field for field in effects['9']['fields'] if field['key'] == 'ratio')
        self.assertEqual([value for value in range(-1, 10) if protocol.afx_field_in_range(ratio, value)], [0, 1, 2, 4, 8])
        self.assertEqual(protocol.afx_block_set(effects['3'], bytes(16), 'bright', 1)[10], 1)  # amp: readable since 2026-11-21
        # The Zen Go's read is tagged and its sub-command differs; the write frame is the same.
        self.assertEqual(protocol.build_afx_state_query(zen, 29)[:16].hex(' '),
                         '74 00 00 00 11 00 00 00 07 00 00 00 1d 00 00 80')
        packet = protocol.build_afx_parameter_write(zen, 29, 0, bytes.fromhex('523232'))
        self.assertEqual((packet[4], packet[16:23].hex(' ')), (0x17, 'd5 05 1d 00 52 32 32'))

    def test_clearq_band_frames_match_capture(self):
        d4, zen = load('discrete_4_a270'), load('zen_go_sc')
        effect = protocol.afx_parameter_effect(d4, 1)
        # Read body the vendor panel got back right after loading ClearQ (d4_afx_two_effects, frame 68405).
        instance = bytes.fromhex(CLEARQ_DEFAULTS.replace('|', '')) + b'\x01'
        defaults = (instance * 8)[36:71]   # instance 1 of the reply
        self.assertEqual(protocol.afx_block_get(effect, defaults),
                         {'b1_freq': 30, 'b2_q': 71, 'b2_gain': 0, 'b3_freq': 1000, 'b3_q': 71, 'b3_gain': 0})
        # The Zen Go owner's ClearQ (panels_connect_walk, frame 28225): band 1 at 125 Hz, shape 4.
        owner = bytes.fromhex('7d004700000004' + CLEARQ_DEFAULTS.replace('|', '').replace(' ', '')[14:])
        self.assertEqual(protocol.afx_block_get(effect, owner)['b1_freq'], 125)
        # Sweep frames of clearq_sweep.pcapng: one knob, one band, one frame.
        swept = protocol.afx_block_set(effect, protocol.afx_block_set(effect, defaults, 'b3_freq', 125), 'b3_q', 50)
        cases = [(defaults, 'b3_freq', 3000, '01 00 02 b8 0b 47 00 00 00 02'),
                 (defaults, 'b1_freq', 50, '01 00 00 32 00 47 00 00 00 00'),
                 (defaults, 'b2_q', 221, '01 00 01 c8 00 dd 00 00 00 02'),
                 (swept, 'b3_gain', -2400, '01 00 02 7d 00 32 00 a0 f6 02'),
                 (swept, 'b3_gain', 1200, '01 00 02 7d 00 32 00 b0 04 02')]
        for before, key, value, expected in cases:
            frames = protocol.build_afx_parameter_writes(
                d4, 1, 0, protocol.afx_block_set(effect, before, key, value), before)
            self.assertEqual(len(frames), 1, key)
            self.assertEqual(frames[0][:5].hex(' '), '70 00 00 00 1c')
            self.assertEqual(frames[0][16:28].hex(' '), 'd5 0a ' + expected)
        # Without a previous block every band is sent, in band order; the owner's shape byte survives.
        frames = protocol.build_afx_parameter_writes(zen, 1, 0, owner)
        self.assertEqual([frame[16:28].hex(' ') for frame in frames][:2],
                         ['d5 0a 01 00 00 7d 00 47 00 00 00 04', 'd5 0a 01 00 01 c8 00 47 00 00 00 02'])
        self.assertEqual(len(frames), 5)
        self.assertEqual(protocol.build_afx_parameter_writes(d4, 1, 0, defaults, defaults), [])
        for key, value in (('b1_freq', 801), ('b2_gain', 1201), ('b3_q', 49), ('b4_freq', 5000), ('b1_gain', 0)):
            with self.assertRaises(protocol.ConstraintError):
                protocol.afx_block_set(effect, defaults, key, value)
        with self.assertRaises(protocol.ConstraintError):   # five frames, not one
            protocol.build_afx_parameter_write(d4, 1, 0, defaults)
        with self.assertRaises(protocol.ConstraintError):   # a write block is not the read image
            protocol.build_afx_parameter_writes(d4, 1, 0, defaults[:8])
        # The reads the vendor panels sent: index 0 on the Discrete 4, sub-command 0x10 on the Zen Go.
        self.assertEqual(protocol.build_afx_state_query(d4, 1)[:16].hex(' '),
                         '74 00 00 00 10 00 00 00 07 00 00 00 00 00 00 00')
        self.assertEqual(protocol.build_afx_state_query(zen, 1)[:16].hex(' '),
                         '74 00 00 00 10 00 00 00 07 00 00 00 01 00 00 80')
        reply = bytes.fromhex('75 00 00 00 40 01 00 00 07 00 00 00 01 00 00 80') + owner + b'\x01'
        self.assertTrue(protocol.is_afx_state_response(zen, reply.ljust(320, b'\0'), 1))
        self.assertFalse(protocol.is_afx_state_response(zen, reply.ljust(320, b'\0'), 27))

    def test_load_defaults_match_capture(self):
        d4, zen = load('discrete_4_a270'), load('zen_go_sc')
        bands = ['00 1e 00 47 00 00 00 00', '01 c8 00 47 00 00 00 02', '02 e8 03 47 00 00 00 02',
                 '03 88 13 47 00 00 00 02', '04 98 3a 47 00 00 00 01']
        # clearq_probe.pcapng frames 6-10 (instance 0); zengo_batch_load.pcapng frames 18-22 (instance 1).
        for profile, instance in ((d4, 0), (zen, 1)):
            frames, block = protocol.afx_load_defaults(profile, 1, instance)
            self.assertEqual([frame[16:28].hex(' ') for frame in frames],
                             [f'd5 0a 01 {instance:02x} {band}' for band in bands])
            self.assertTrue(all(frame[4] == 0x1c and not any(frame[28:]) for frame in frames))
            self.assertEqual(block.hex(' '), CLEARQ_DEFAULTS.replace(' | ', ' '))
        # Guitar amp: amp_darkface_65_probe.pcapng and zengo_batch_load.pcapng frame 323, the same block.
        for profile in (d4, zen):
            frames, block = protocol.afx_load_defaults(profile, 3, 0)
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0][:5].hex(' '), '70 00 00 00 24')
            self.assertEqual(payload(frames[0]), 'd5 12 03 00 00 46 3e 3f 00 44 00 32 59 00 01')
            # Discrete 4: no captured read, nothing to compare with. Zen Go: read captured 2026-11-21.
            self.assertEqual(block, None if profile is d4 else bytes.fromhex('00463e3f004400325900010000000000'))
            self.assertEqual(profile['afx_inserts']['writes']['effects']['3']['instances'], [0])
        self.assertEqual(protocol.afx_load_defaults(d4, 39, 0), ([], None))   # PowerGate: chain write only

    def test_guitar_amp_is_not_editable(self):
        profile = load('discrete_4_a270')
        effect = protocol.afx_parameter_effect(profile, 3)
        block = bytes.fromhex(effect['defaults'])
        # The mapping itself is the captured one (amp_darkface_65_sweep: model 0x0a, output 0xd0)...
        changed = protocol.afx_block_set(effect, protocol.afx_block_set(effect, block, 'model', 10), 'output', -48)
        self.assertEqual(changed.hex(' '), '0a 46 3e 3f 00 44 00 32 59 00 01 00 00 00 00 d0')
        # ...but no frame is built for it until its read is confirmed.
        with self.assertRaises(protocol.ConstraintError):
            protocol.build_afx_parameter_writes(profile, 3, 0, changed)
        for key, value in (('model', 11), ('output', 13), ('volume', 50)):
            with self.assertRaises(protocol.ConstraintError):
                protocol.afx_block_set(effect, block, key, value)

    def test_rack_loads_and_edits_clearq_like_the_vendor_panel(self):
        device = FakeDevice(load('discrete_4_a270'))
        rack = InsertRack(device)
        state = rack.change(0, 'load', type_id=1)
        self.assertEqual(state['racks']['0'][0]['type'], 1)
        # clearq_probe.pcapng frames 5-10: the chain write, then the five default bands.
        self.assertEqual([payload(packet) for packet in device.written],
                         ['d7 49 00 01', 'd5 0a 01 00 00 1e 00 47', 'd5 0a 01 00 01 c8 00 47 00 00 00 02',
                          'd5 0a 01 00 02 e8 03 47 00 00 00 02', 'd5 0a 01 00 03 88 13 47 00 00 00 02',
                          'd5 0a 01 00 04 98 3a 47 00 00 00 01'])
        self.assertIn(1, state['mapped_types'])
        self.assertNotIn(3, state['mapped_types'])
        shown = rack.parameters(0, 0)
        self.assertEqual([field['key'] for field in shown['fields']],
                         ['b1_freq', 'b2_q', 'b2_gain', 'b3_freq', 'b3_q', 'b3_gain'])
        del device.written[:]
        result = rack.set_parameter(0, 0, 'b3_freq', 3000)
        self.assertEqual([payload(packet) for packet in device.written], ['d5 0a 01 00 02 b8 0b 47 00 00 00 02'])
        self.assertEqual({field['key']: field['value'] for field in result['fields']}['b3_freq'], 3000)
        with self.assertRaises(ValueError):
            rack.set_parameter(0, 0, 'b3_freq', 9000)
        # A device that does not take the write turns editing off.
        device.deaf = True
        with self.assertRaises(RuntimeError):
            rack.set_parameter(0, 0, 'b2_gain', 100)
        self.assertFalse(rack.state()['writes_enabled'])

    def test_rack_loads_the_amp_but_never_edits_it(self):
        device = FakeDevice(load('discrete_4_a270'))
        rack = InsertRack(device)
        rack.change(1, 'load', type_id=3)
        self.assertEqual([payload(packet) for packet in device.written],
                         ['d7 49 01 03', 'd5 12 03 00 00 46 3e 3f 00 44 00 32 59 00 01'])
        del device.written[:]
        with self.assertRaises(ValueError):
            rack.set_parameter(1, 0, 'output', 2)
        self.assertEqual(device.written, [])
        self.assertEqual(rack.parameters(1, 0)['fields'], [])   # raw reply only
        with self.assertRaises(RuntimeError):                    # instance 0 is taken, 1 is not verified
            rack.change(0, 'load', type_id=3)
        with self.assertRaises(ValueError):                      # cabinet: impulse-response data, not loadable
            rack.change(0, 'load', type_id=4)

    def test_orion_has_no_insert_contract(self):
        self.assertIsNone(protocol.afx_insert_writes(load('orion_studio_sc')))


if __name__ == '__main__':
    unittest.main()

"""Insert-rack chain writes rebuilt from the captured vendor frames."""
import json
import os
import unittest

from antelope import protocol

PROFILES = os.path.join(os.path.dirname(__file__), '..', 'profiles')


def load(name):
    with open(os.path.join(PROFILES, name + '.json')) as source:
        return json.load(source)


def payload(packet):
    return packet[16:].rstrip(b'\0').hex(' ')


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
        self.assertIsNone(protocol.afx_parameter_effect(profile, 1))  # ClearQ: not mapped

    def test_discrete_4_effect_maps_are_consistent(self):
        profile = load('discrete_4_a270')
        effects = profile['afx_inserts']['parameters']['effects']
        self.assertEqual(len(effects), 45)
        for kind, effect in effects.items():
            length = effect['block_length']
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

    def test_orion_has_no_insert_contract(self):
        self.assertIsNone(protocol.afx_insert_writes(load('orion_studio_sc')))


if __name__ == '__main__':
    unittest.main()

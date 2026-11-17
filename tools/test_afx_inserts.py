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

    def test_orion_has_no_insert_contract(self):
        self.assertIsNone(protocol.afx_insert_writes(load('orion_studio_sc')))


if __name__ == '__main__':
    unittest.main()

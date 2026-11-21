"""Offline tests for profile-declared nested readback records."""
import json
from pathlib import Path
import unittest

from antelope import protocol


class ReadbackRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.profile = json.loads((root / 'profiles/orion_studio_sc.json').read_text())
        cls.zen = json.loads((root / 'profiles/zen_go_sc.json').read_text())

    def test_link_table_layouts_are_indexed_by_link_space(self):
        expected = {0: ('preamp', 6),
                    1: ('adat', 8),
                    2: ('spdif', 1),
                    3: ('mixer', 64), 4: ('afx', 32)}
        for index, (name, count) in expected.items():
            with self.subTest(index=index):
                layout = protocol.readback_record_layout(self.profile, 0x0b, index)
                self.assertEqual(layout['name'], name)
                self.assertEqual(layout['record_count'], count)
                body = bytes((i & 1 for i in range(count))) + bytes(304 - count)
                records = protocol.parse_link_table(self.profile, body, 0x0b, index)
                self.assertEqual(len(records), count)
                self.assertEqual([r['linked'] for r in records],
                                 [i & 1 for i in range(count)])

    def test_mic_emulation_records(self):
        body = b''.join(bytes((i, i + 1, i & 1, i + 2)) for i in range(8))
        records = protocol.parse_mic_emulations(self.profile, body + bytes(304 - len(body)),
                                                0x16, 0)
        self.assertEqual(len(records), 8)
        self.assertEqual(records[0]['target'], 0)
        self.assertEqual(records[3]['emu_model'], 4)
        self.assertEqual(records[7]['ch_swap'], 1)
        self.assertEqual(records[7]['pattern'], 9)

    def test_afx_strip_order_records(self):
        body = b''.join(bytes((0, 0)) if i == 0 else bytes((i, i + 0x40))
                         for i in range(8))
        records = protocol.parse_afx_strip_order(self.profile,
                                                 body + bytes(304 - len(body)),
                                                 0x19, 63)
        self.assertEqual(len(records), 8)
        self.assertEqual(records[0]['raw'], b'\x00\x00')
        self.assertEqual((records[4]['type'], records[4]['inst']), (4, 0x44))
        self.assertIsNone(protocol.readback_record_layout(self.profile, 0x19, 64))

    def test_afx_instance_count_tables(self):
        body = b''.join(bytes((i & 0xff, (i * 2) & 0xff)) for i in range(91))
        records = protocol.parse_afx_instance_table(self.profile,
                                                     body + bytes(304 - len(body)),
                                                     0x15, 0)
        self.assertEqual(len(records), 91)
        self.assertEqual(records[12]['type_id'], 12)
        self.assertEqual(records[12]['inst_count'], 24)

        available = protocol.parse_afx_instance_table(
            self.profile, bytes(180) + bytes(124), 0x0c, 0)
        self.assertEqual(len(available), 90)

    def test_short_nested_record_is_rejected(self):
        with self.assertRaises(ValueError):
            protocol.parse_mic_emulations(self.profile, bytes(31), 0x16, 0)

    def test_schema_only_outer_indices_are_not_queryable(self):
        self.assertEqual(protocol.readback_record_layout_indices(
            self.profile, 0x19), list(range(64)))
        self.assertEqual(protocol.readback_record_layout_indices(
            self.profile, 0x0c), [])
        self.assertEqual(protocol.readback_record_layout_indices(
            self.profile, 0x0c, safe_only=False), [0, 1])
        with self.assertRaises(protocol.ConstraintError):
            protocol.build_readback_query(self.profile, 0x0c, 0)
        with self.assertRaises(protocol.ConstraintError):
            protocol.build_readback_query(self.profile, 0x0b, -1)

    def test_sibling_profiles_do_not_inherit_orion_layouts(self):
        self.assertIsNone(protocol.readback_record_layout(self.zen, 0x0b, 0))

    def test_zen_go_reverb_and_mixer_records_from_the_panel_marker_pass(self):
        zen = self.zen
        # Replies of 2026-11-21 with strip 15 send 2a, strip 16 send 21 / send pan 19,
        # mix 0 strip 16 at fader 11 muted with pan 17, return 0 at 33 muted, return 1 at 44.
        mixer = bytes.fromhex(
            '00 20 5a 20 5a 20 00 02 00 3e 00 02 00 3e 5a 02 5a 3e' + ' 00 02 00 3e' * 3
            + ' 00 02 11 57' + ' 00 02 00 3e' * 8 + ' b3 00 20 60 20 60 20' + ' 60 02 60 3e' * 6
            + ' 2a 02 21 19' + ' 60 02 60 3e' * 8)
        slots = protocol.parse_mixer_record(zen, mixer)
        self.assertEqual(len(slots), 16)
        last = slots[15]
        self.assertEqual((last['fader'], last['pan'], last['mute'], last['solo']), (0x11, -9, True, False))
        self.assertEqual((last['send'], last['send_pan']), (0x21, -7))
        self.assertEqual((slots[14]['send'], slots[14]['send_pan']), (0x2a, -30))
        self.assertEqual((slots[0]['fader'], slots[0]['send'], slots[0]['send_pan']), (0x5a, 0x60, 0))
        sends = protocol.parse_readback_records(zen, bytes.fromhex(
            '00 20 60 20 60 20' + ' 60 02 60 3e' * 6 + ' 2a 02 21 19' + ' 60 02 60 3e' * 8), 0x18, 0)
        self.assertEqual([(row['send'], row['pan']) for row in sends[15:17]], [(0x2a, 0x02), (0x21, 0x19)])
        returns = protocol.parse_readback_records(zen, bytes.fromhex('b3 44 60 60'), 0x17, 0)
        self.assertEqual([row['value'] for row in returns], [0xb3, 0x44, 0x60, 0x60])
        self.assertEqual(protocol.parse_readback_records(zen, b'\x02', 0x12, 0)[0]['index'], 2)
        reverb = protocol.parse_auraverb_record(zen, bytes.fromhex(
            '00 1f 4d 07 64 2c 3a 39 11 46 00 ff' + ' 51 64 00 64 0b 0d 18 42 32 00 ff' * 2
            + ' 51 64 00 64 0b 0d 18 42 32'))[0]
        self.assertEqual(reverb['params'], {
            'room_size': 0x1f, 'color': 0x4d, 'pre_delay': 0x07, 'early_reflection_gain': 0x2c,
            'late_reflection_delay': 0x3a, 'richness': 0x39, 'reverb_time': 0x11, 'reverb_level': 0x46})
        self.assertEqual((reverb['enabled'], reverb['wet']), (False, 100))
        self.assertTrue(protocol.auraverb_readback_available(zen))
        for category, index in ((0x0a, 1), (0x17, 1), (0x18, 1), (0x12, 1), (0x03, 2)):
            with self.assertRaises(protocol.ConstraintError):
                protocol.build_readback_query(zen, category, index)
        self.assertEqual(protocol.build_readback_query(zen, 0x03, 4)[:16].hex(' '),
                         '74 00 00 00 10 00 00 00 03 00 00 00 04 00 00 00')


if __name__ == '__main__':
    unittest.main()

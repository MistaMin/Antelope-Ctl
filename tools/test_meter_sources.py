"""Offline meter-source checks. Run: python3 -m unittest tools.test_meter_sources."""
import copy
import importlib
import json
from pathlib import Path
import signal
import sys
import threading
import types
import unittest
from unittest import mock

from antelope import cli
from antelope import protocol
from antelope import transport


def load_server_without_web_or_hid_boundaries():
    class FakeFastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            return lambda function: function

        post = exception_handler = get

        def mount(self, *args, **kwargs):
            return None

        def add_middleware(self, middleware, **options):
            self.middleware = (middleware, options)

    fastapi = types.ModuleType('fastapi')
    fastapi.FastAPI = FakeFastAPI
    trustedhost = types.ModuleType('fastapi.middleware.trustedhost')
    trustedhost.TrustedHostMiddleware = 'TrustedHostMiddleware'
    responses = types.ModuleType('fastapi.responses')
    responses.FileResponse = object
    responses.JSONResponse = object
    responses.StreamingResponse = object
    staticfiles = types.ModuleType('fastapi.staticfiles')
    class FakeStaticFiles:
        def __init__(self, *args, **kwargs):
            pass
    staticfiles.StaticFiles = FakeStaticFiles
    pydantic = types.ModuleType('pydantic')
    pydantic.BaseModel = object
    pydantic.StrictInt = int
    pydantic.StrictFloat = float
    uvicorn = types.ModuleType('uvicorn')
    with mock.patch.dict(sys.modules, {
        'fastapi': fastapi,
        'fastapi.middleware.trustedhost': trustedhost,
        'fastapi.responses': responses,
        'fastapi.staticfiles': staticfiles,
        'pydantic': pydantic,
        'uvicorn': uvicorn,
    }), mock.patch.object(transport, 'list_connected_hid', return_value=set()):
        return importlib.import_module('webui.server')


class MeterSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'profiles/orion_studio_sc.json'
        cls.profile = json.loads(path.read_text())
        zen_path = Path(__file__).resolve().parents[1] / 'profiles/zen_go_sc.json'
        cls.zen_profile = json.loads(zen_path.read_text())
        cls.server = load_server_without_web_or_hid_boundaries()

    def test_orion_state_bank_preserves_raw_endpoints_without_meter_report_calibration(self):
        source, magic, base = protocol.channel_meter_source_details(self.profile)
        self.assertEqual((source, magic, base), ('state_report', 0x73, 221))

        report = bytearray(233)
        report[221] = 0
        report[232] = 96
        self.assertEqual(protocol.parse_channel_meter(self.profile, report, 0, base), 0)
        self.assertEqual(protocol.parse_channel_meter(self.profile, report, 11, base), 96)
        self.assertIsNone(protocol.raw_to_db(self.profile, 0, source))
        self.assertIsNone(protocol.meter_led(self.profile, None, source))
        self.assertEqual(cli._meter_bar(0, self.profile, source_frame=source), '########')
        self.assertEqual(cli._meter_bar(96, self.profile, source_frame=source), '........')

    def test_orion_bus_silent_endpoint_marker_is_not_reported_as_definite_mute(self):
        report = bytearray(320)
        report[28] = 96
        report[29] = 0x04

        state = protocol.parse_bus_state(self.profile, report, 0)

        self.assertEqual(state['level'], 96)
        self.assertEqual(state['status_raw'], 0x04)
        self.assertFalse(state['mute'])
        self.assertTrue(state['mute_ambiguous'])

        report[28] = 95
        state = protocol.parse_bus_state(self.profile, report, 0)
        self.assertTrue(state['mute'])
        self.assertFalse(state['mute_ambiguous'])

    def test_orion_bus_level_endpoint_contract_is_attenuation(self):
        self.assertIn('level_encoding', self.profile['buses'])
        encoding = self.profile['buses']['level_encoding']
        self.assertIn('raw 0 = 0 dB maximum/unity', encoding)
        self.assertIn('raw 96 (0x60) = -inf/silent', encoding)
        self.assertIn('0x47', encoding)

    def test_web_samples_keep_channel_one_activity_and_channel_twelve_silence_uncalibrated(self):
        device = self.server.Device.__new__(self.server.Device)
        device.profile = self.profile
        device.n_ch = 12
        report = bytearray(233)
        report[221] = 0
        report[232] = 96

        samples = device._parse_meters(report)
        self.assertEqual(len(samples), 12)
        self.assertEqual(samples[0], {
            'raw': 0, 'db': None, 'clip': None, 'silence': False,
        })
        self.assertEqual(samples[11], {
            'raw': 96, 'db': None, 'clip': None, 'silence': True,
        })

    def test_surround_eq_reset_uses_the_profile_preset(self):
        frequencies, q_raw, gain_raw = self.server._surround_eq_reset_values(
            self.profile)
        self.assertEqual(frequencies, [30, 45, 90, 160, 350, 650, 1100, 1700,
                                       2500, 3500, 4750, 6250, 8250, 10750,
                                       13000, 15000])
        self.assertEqual(q_raw, 71)
        self.assertEqual(gain_raw, 0)

    def test_surround_json_enables_20_bass_and_speaker_head_writes(self):
        device = self.server.Device.__new__(self.server.Device)
        device.profile = self.profile
        device.surround_contract = self.server._surround_global_contract(
            self.profile)
        device.surround_available = True
        device.surround_speaker_count = 16
        device._lock = threading.Lock()
        global_body = bytearray(151)
        global_body[0] = 0x02             # 2.0, no bass-management flag
        global_body[1] = 0x9F             # no-LFE sentinel
        global_body[4:6] = (600).to_bytes(2, 'little')
        global_body[13:23] = protocol.pack_surround_channel_order(
            [1, 3], 10)
        device.surround_global_raw = bytes(global_body)
        device.surround_global = protocol.parse_surround_global_record(
            self.profile, global_body)
        speaker_body = bytearray(116)
        speaker_body[0:2] = (100).to_bytes(2, 'little')
        speaker_body[2:4] = (0x8000 | 635).to_bytes(2, 'little')
        speaker = protocol.parse_surround_speaker_eq_record(
            self.profile, speaker_body)
        device.surround_speakers = {0: speaker, 1: speaker}

        payload = device.surround_json()

        self.assertTrue(payload['write']['bass']['enabled'])
        self.assertEqual(payload['write']['bass']['formats'], ['2.0', '2.1'])
        self.assertEqual(payload['write']['bass']['filter_types'], {
            'hp': 'Butterworth', 'lp': 'Butterworth'})
        self.assertEqual(
            [item['label'] for item in payload['write']['bass']
             ['filter_type_values']['hp_filter_type']],
            ['Butterworth', 'Linkwitz-Riley'])
        self.assertTrue(payload['write']['speaker_head']['enabled'])
        self.assertEqual(payload['write']['speaker_head']['fields'],
                         ['delay_ms', 'level_db', 'phase_invert'])
        self.assertEqual(
            payload['write']['speaker_head']['controls']['delay_ms']['range'],
            [0.6, 100.6])
        self.assertEqual(
            payload['speakers'][0]['head']['level_db'], 3.5)
        self.assertTrue(payload['speakers'][0]['head']['phase_invert'])
        self.assertTrue(payload['write']['speaker_bypass']['enabled'])
        self.assertEqual(payload['write']['speaker_bypass']['fields'], ['bypass'])
        self.assertTrue(payload['speakers'][0]['bypass'])

    def test_terminal_shutdown_stops_device_before_uvicorn_exit(self):
        class FakeServer:
            def __init__(self):
                self.should_exit = False
                self.calls = []

            def handle_exit(self, sig, frame):
                self.calls.append((sig, frame))
                self.should_exit = True

        fake_server = FakeServer()
        device = mock.Mock()
        wrapped = self.server._install_terminal_shutdown(fake_server, device)

        wrapped.handle_exit(signal.SIGINT, None)

        device.request_stop.assert_called_once_with()
        self.assertEqual(fake_server.calls, [(signal.SIGINT, None)])
        self.assertTrue(fake_server.should_exit)

        # A repeated Ctrl+C keeps Uvicorn's force-exit behavior but does not
        # enqueue a second device shutdown request.
        wrapped.handle_exit(signal.SIGINT, None)
        device.request_stop.assert_called_once_with()
        self.assertEqual(len(fake_server.calls), 2)

    def test_orion_selected_mixer_strip_meters_follow_the_window_selector(self):
        device = self.server.Device.__new__(self.server.Device)
        device.profile = self.profile
        report = bytearray(320)
        report[122] = 2                 # Mix 3, target-1 selector
        report[157] = 0                 # strip 1
        report[188] = 96                # strip 32

        meters = device._parse_mixer_meters(report)
        self.assertEqual(meters['mix'], 2)
        self.assertEqual(meters['raw_range'], [0, 96])
        self.assertEqual(len(meters['strips']), 32)
        self.assertEqual(meters['strips'][0],
                         {'ch': 1, 'raw': 0, 'silence': False})
        self.assertEqual(meters['strips'][-1],
                         {'ch': 32, 'raw': 96, 'silence': True})

    def test_orion_state_bank_truncation_does_not_invent_channel_twelve(self):
        _, _, base = protocol.channel_meter_source_details(self.profile)
        report = bytearray(232)
        report[221] = 48
        self.assertEqual(protocol.parse_channel_meter(self.profile, report, 0, base), 48)
        with self.assertRaisesRegex(ValueError, 'channel 11'):
            protocol.parse_channel_meter(self.profile, report, 11, base)

        device = self.server.Device.__new__(self.server.Device)
        device.profile = self.profile
        device.n_ch = 12
        self.assertEqual(len(device._parse_meters(report)), 11)

    def test_webui_structured_readbacks_only_schedule_safe_outer_indices(self):
        device = self.server.Device.__new__(self.server.Device)
        device.profile = self.profile
        device.structured = {
            (0x0b, 0): [{'record_index': 0, 'raw': b'\x01', 'linked': 1}],
            (0x16, 0): [{'record_index': 0, 'raw': b'\x00\x02\x00\x04',
                         'target': 0, 'emu_model': 2, 'ch_swap': 0,
                         'pattern': 4}],
        }
        device._lock = threading.Lock()
        payload = device.structured_readbacks_json()
        by_name = {layout['name']: layout for layout in payload['layouts']}
        self.assertEqual(by_name['preamp']['current']['0'][0]['linked'], 1)
        self.assertEqual(by_name['preamp']['current']['0'][0]['raw'], '01')
        self.assertFalse(by_name['available']['safe'])
        self.assertTrue(by_name['available']['capture_required'])
        targets = set(self.server._structured_readback_targets(self.profile))
        self.assertIn((0x0b, 0), targets)
        self.assertIn((0x19, 63), targets)
        self.assertNotIn((0x0c, 0), targets)

    def test_input_link_write_refreshes_the_bounded_diagnostic_table(self):
        self.assertEqual(self.server._input_link_readback_target(
            self.profile, 'preamp', 3), (0x0b, 0))
        self.assertEqual(self.server._input_link_readback_target(
            self.profile, 'adat', 3), (0x0b, 1))
        self.assertEqual(self.server._input_link_readback_target(
            self.profile, 'adat', 6), (0x0b, 1))
        self.assertEqual(self.server._input_link_readback_target(
            self.profile, 'spdif', 0), (0x0b, 2))
        profile = copy.deepcopy(self.profile)
        profile['frame']['link_command']['readback'] = {
            'status': 'capture-confirmed', 'category': '0x0b', 'index': 0,
            'record_count': 6, 'pair_counts': {'preamp': 6, 'adat': 6},
            'additional_tables': [{
                'status': 'schema-backed', 'category': '0x0b', 'index': 1,
                'record_count': 8,
                'pair_mappings': {
                    'adat': {'pair_start': 6, 'record_start': 6, 'pair_count': 2},
                },
            }],
        }
        self.assertEqual(self.server._input_link_readback_target(
            profile, 'preamp', 3), (0x0b, 0))
        self.assertEqual(self.server._input_link_readback_target(
            profile, 'adat', 3), (0x0b, 0))
        self.assertEqual(self.server._input_link_readback_target(
            profile, 'adat', 6), (0x0b, 1))
        self.assertEqual(self.server._input_link_readback_target(
            profile, 'adat', 7), (0x0b, 1))
        self.assertIsNone(self.server._input_link_readback_target(
            profile, 'adat', 8))

        device = self.server.Device(profile)
        fake_transport = types.SimpleNamespace(write=mock.Mock())
        with mock.patch.object(self.server, 'PROFILE', profile), \
                mock.patch.object(self.server, 'DEV', device), \
                mock.patch.object(device, 'submit',
                                  side_effect=lambda fn: fn(fake_transport)), \
                mock.patch.object(device, '_refresh_readback_one',
                                  return_value=False) as refresh:
            self.server._queue_input_link_write(b'link', 'adat', 3)
            fake_transport.write.assert_called_once_with(b'link')
            refresh.assert_called_once_with(fake_transport, 0x0b, 0)
            self.assertEqual(device.link_rb_ver, 1)

            fake_transport.write.reset_mock()
            refresh.reset_mock()
            self.server._queue_input_link_write(b'tail', 'adat', 6)
            fake_transport.write.assert_called_once_with(b'tail')
            refresh.assert_called_once_with(fake_transport, 0x0b, 1)
            self.assertEqual(device.link_rb_ver, 2)

            fake_transport.write.reset_mock()
            refresh.reset_mock()
            self.server._queue_input_link_write(b'unmapped', 'adat', 8)
            fake_transport.write.assert_called_once_with(b'unmapped')
            refresh.assert_not_called()
            self.assertEqual(device.link_rb_ver, 2)

            refresh.reset_mock(return_value=True)
            refresh.return_value = None  # a missing response must not publish stale data
            self.server._queue_input_link_write(b'no-response', 'preamp', 3)
            self.assertEqual(device.link_rb_ver, 2)

    def test_meter_report_source_keeps_existing_curve_and_clip_behavior(self):
        profile = copy.deepcopy(self.profile)
        profile['frame']['state_report'].pop('channel_meter_base_offset')
        source, magic, base = protocol.channel_meter_source_details(profile)
        self.assertEqual((source, magic, base), ('meter_report', 0x75, 32))
        self.assertEqual(protocol.raw_to_db(profile, 0, source), 0.0)
        self.assertEqual(protocol.raw_to_db(profile, 96, source), -60.0)
        self.assertIn('CLIP', cli._meter_bar(0, profile, source_frame=source))
        self.assertNotIn('CLIP', cli._meter_bar(96, profile, source_frame=source))

    def test_zen_go_surface_selected_mixer_strip_meters(self):
        device = self.server.Device.__new__(self.server.Device)
        device.profile = self.zen_profile
        report = bytearray(320)
        report[122] = 0x0f              # Monitor / HP1 surface, Mix 1
        report[158] = 0
        report[159] = 24
        report[173] = 96

        meters = device._parse_mixer_meters(report)
        self.assertEqual(meters['mix'], 0)
        self.assertEqual(meters['selector'], 0x0f)
        self.assertEqual(meters['raw_range'], [0, 96])
        self.assertEqual(len(meters['strips']), 16)
        self.assertEqual(meters['strips'][0], {'ch': 0, 'raw': 0, 'silence': False})
        self.assertEqual(meters['strips'][1], {'ch': 1, 'raw': 24, 'silence': False})
        self.assertEqual(meters['strips'][-1], {'ch': 15, 'raw': 96, 'silence': True})

        report[122] = 0x0c              # HP2 surface, Mix 2
        self.assertEqual(device._parse_mixer_meters(report)['mix'], 1)

    def test_zen_go_preamp_candidates_are_exposed_as_raw_only_input_meters(self):
        device = self.server.Device.__new__(self.server.Device)
        device.profile = self.zen_profile
        device.n_ch = 2
        report = bytearray(320)
        report[0xce + 0x10] = 12
        report[0xcf + 0x10] = 96

        samples = device._parse_meters(report)
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0], {
            'raw': 12, 'db': None, 'clip': None, 'silence': False,
        })
        self.assertEqual(samples[1], {
            'raw': 96, 'db': None, 'clip': None, 'silence': True,
        })

    def test_zen_go_mixer_idle_floor_suppresses_first_strip_stub(self):
        device = self.server.Device.__new__(self.server.Device)
        device.profile = self.zen_profile
        report = bytearray(320)
        report[122] = 0x0f
        report[158] = 84       # captured idle/noise floor
        report[159] = 83       # just below the declared floor

        meters = device._parse_mixer_meters(report)
        self.assertEqual(meters['noise_floor_raw'], 84)
        self.assertTrue(meters['strips'][0]['silence'])
        self.assertFalse(meters['strips'][1]['silence'])

    def test_zen_go_provisional_output_lanes_are_profile_driven(self):
        device = self.server.Device.__new__(self.server.Device)
        device.profile = self.zen_profile
        report = bytearray(320)
        report[234:240] = bytes((20, 96, 30, 40, 50, 96))

        meters = device._parse_output_meters(report)
        self.assertTrue(meters['provisional'])
        self.assertEqual(meters['raw_range'], [0, 96])
        self.assertEqual([item['bus'] for item in meters['outputs']], [0, 1, 2])
        self.assertEqual(meters['outputs'][0]['name'], 'Monitor')
        self.assertEqual(meters['outputs'][0]['lanes'][0]['raw'], 20)
        self.assertTrue(meters['outputs'][0]['lanes'][1]['silence'])
        self.assertEqual(meters['outputs'][2]['lanes'][0]['raw'], 50)

    def test_zen_go_routing_is_one_logical_sixteen_strip_destination(self):
        path = Path(__file__).resolve().parents[1] / 'profiles/zen_go_sc.json'
        features = self.server.features_for(str(path), self.zen_profile)
        routing = features['routing']
        self.assertTrue(routing['writable'])
        # the mixer map first, then Record 1-8, S/PDIF out and the rack inputs
        self.assertEqual([item['id'] for item in routing['destinations']], [6, 3, 4, 5])
        self.assertEqual([item['write_destinations'] for item in routing['destinations'][1:]],
                         [[3], [4], [5]])
        self.assertEqual(routing['destinations'][0]['id'], 6)
        self.assertEqual(routing['destinations'][0]['channels'], 16)
        self.assertEqual(routing['destinations'][0]['write_destinations'],
                         [6, 7, 8, 9])

    def test_zen_go_routing_api_hides_mirrored_and_unrelated_records(self):
        path = Path(__file__).resolve().parents[1] / 'profiles/zen_go_sc.json'
        features = self.server.features_for(str(path), self.zen_profile)
        fake_device = mock.Mock()
        fake_device.routing_json.return_value = {
            str(dest): [] for dest in (3, 4, 5, 6, 7, 8, 9)
        }
        with mock.patch.object(self.server, 'PROFILE', self.zen_profile), \
                mock.patch.object(self.server, 'UI_FEATURES', features), \
                mock.patch.object(self.server, 'DEV', fake_device):
            payload = self.server.api_routing()
        self.assertEqual([item['id'] for item in payload['dests']], [6, 3, 4, 5])
        self.assertEqual(set(payload['current']), {'3', '4', '5', '6'})   # mirrors 7-9 stay hidden
        self.assertNotIn('loopback_hp1', payload['dests'][3]['sources'])

    def test_zen_go_logical_route_writes_all_four_mirrors(self):
        class FakeTransport:
            def __init__(self):
                self.writes = []

            def write(self, packet):
                self.writes.append(packet)

        path = Path(__file__).resolve().parents[1] / 'profiles/zen_go_sc.json'
        features = self.server.features_for(str(path), self.zen_profile)
        device = self.server.Device(self.zen_profile)
        for dest in (6, 7, 8, 9):
            device.routing[dest] = [(0x03, index) for index in range(32)]
        transport_obj = FakeTransport()
        with mock.patch.object(self.server, 'UI_FEATURES', features):
            device._set_routing_source(transport_obj, 6, 4, (0x00, 0))

        self.assertEqual([packet[18] for packet in transport_obj.writes],
                         [6, 7, 8, 9])
        self.assertEqual([packet[27:29] for packet in transport_obj.writes],
                         [bytes((0x00, 0))] * 4)

    def test_device_stop_closes_transport_and_wakes_worker(self):
        class FakeTransport:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        device = self.server.Device(self.zen_profile)
        transport_obj = FakeTransport()
        device._transport = transport_obj
        worker_done = threading.Event()

        def worker():
            device.cmds.get(timeout=1.0)
            worker_done.set()

        device._t = threading.Thread(target=worker)
        device._t.start()
        device.stop(timeout=1.0)
        self.assertTrue(device._stop.is_set())
        self.assertTrue(transport_obj.closed)
        self.assertTrue(worker_done.is_set())
        self.assertFalse(device._t.is_alive())

    def test_zen_go_surface_selector_uses_profile_command(self):
        packet, value, target = self.server._mixer_surface_packet(self.zen_profile, 1)
        self.assertEqual((value, target), (0x0c, 0))
        self.assertEqual(packet[0], 0x70)
        self.assertEqual(packet[4], 0x13)
        self.assertEqual(packet[16:19], bytes((0x49, 0, 0x0c)))
        self.assertIsNone(self.server._mixer_surface_packet(self.zen_profile, 2))


# Zen Go vendor panel run of 2026-11-21: every payload below is a frame the
# panel sent, every body a reply the device gave (zen_panel/live.log).
ZEN_MIX0 = ('00 20 5a 20 5a 20 00 02 00 3e 00 02 00 3e 5a 02 5a 3e' + ' 00 02 00 3e' * 12
            + ' 60 00 20 60 20 60 20' + ' 60 02 60 3e' * 15)         # cat 04 index 0 at connect
ZEN_REVERB = '00 51 64 00 64 0b 0d 18 42 32 01 ff' + ' 51 64 00 64 0b 0d 18 42 32 00 ff' * 2
ZEN_ROUTES = {3: '03 03 00 03 01 01 00 01 01 02 00 02 01 08 00 08 00',
              4: '04 02 00 02 01', 5: '05 00 00 00 01 01 00 01 01'}
ZEN_ROUTES.update({dest: f'{dest:02x} 00 00 00 01 03 02 03 03 02 00 02 01 0a 00 0a 01'
                         + ' 08 00' * 24 for dest in (6, 7, 8, 9)})


def zen_payload(packet):
    return f'{packet[4]:02x} | ' + packet[16:].rstrip(b'\0').hex(' ')


class ZenGoPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = Path(__file__).resolve().parents[1] / 'profiles/zen_go_sc.json'
        cls.zen = json.loads(cls.path.read_text())
        cls.profile_orion = json.loads(cls.path.with_name('orion_studio_sc.json').read_text())
        cls.server = load_server_without_web_or_hid_boundaries()

    def setUp(self):
        """A Zen Go server whose commands run at once on a recording transport."""
        server, zen = self.server, self.zen
        self.device = server.Device(zen)
        self.sent = []
        transport_obj = types.SimpleNamespace(write=self.sent.append)
        patches = [
            mock.patch.object(server, 'PROFILE', zen),
            mock.patch.object(server, 'UI_FEATURES', server.features_for(str(self.path), zen)),
            mock.patch.object(server, 'DEV', self.device),
            mock.patch.object(self.device, 'submit', side_effect=lambda fn: fn(transport_obj)),
            mock.patch.object(server, '_bad', side_effect=lambda msg: {'ok': False, 'error': msg}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.device.mixer = {mix: protocol.parse_mixer_record(zen, bytes.fromhex(ZEN_MIX0))
                             for mix in (0, 1)}
        self.device.auraverb = {0: protocol.parse_auraverb_record(zen, bytes.fromhex(ZEN_REVERB))}
        self.device.routing = {dest: protocol.parse_routing_record(
            zen, bytes.fromhex(body).ljust(80, b'\0'))[1] for dest, body in ZEN_ROUTES.items()}

    def call(self, api, **fields):
        del self.sent[:]
        result = api(types.SimpleNamespace(**fields))
        return result, [zen_payload(packet) for packet in self.sent]

    def state(self, **bytes_at):
        report = bytearray(320)
        report[0] = 0x73
        for offset, value in bytes_at.items():
            report[int(offset[1:])] = value
        self.device.snapshot = {'online': True, 'buses': self.device._parse_buses(bytes(report))}
        return bytes(report)

    def test_output_status_is_a_bit_field_and_dim_cannot_release_mute(self):
        zen = self.zen
        # 29:02>03 (mute after dim), 29:01>09 (muted, trim index 1), 31:01>02 (dim replaced mute)
        for raw, mute, dim in ((0x03, 1, 1), (0x09, 1, 0), (0x31, 1, 0), (0x02, 0, 1), (0x00, 0, 0)):
            state = protocol.parse_bus_state(zen, self.state(o29=raw), 0)
            self.assertEqual((state['mute'], state['dim']), (mute, dim), hex(raw))
        self.assertEqual(protocol.protected_mute_buses(zen), [0, 1, 2])
        toggle = self.server.api_bus_toggle
        for raw in (0x01, 0x03, 0x09):               # muted: dim on AND dim off would un-mute
            self.state(o29=raw, o31=raw & 3, o33=raw & 3)
            for bus in (0, 1, 2):
                for on in (True, False):
                    result, sent = self.call(toggle, bus=bus, param='dim', on=on)
                    self.assertEqual((result['ok'], sent), (False, []))
            self.assertEqual(self.call(toggle, bus=0, param='mono', on=True)[1], [])
        self.device.snapshot = {'online': True}       # state not read yet: refused too
        self.assertEqual(self.call(toggle, bus=0, param='dim', on=False)[1], [])
        self.state(o29=0x02)                          # dimmed, not muted: 66 00 00 as captured
        self.assertEqual(self.call(toggle, bus=0, param='dim', on=False)[1], ['13 | 66'])
        self.assertEqual(self.call(toggle, bus=1, param='dim', on=True)[1], ['13 | 66 01 01'])
        self.state(o29=0x03)                          # an explicit mute write is always allowed
        self.assertEqual(self.call(toggle, bus=0, param='mute', on=False)[1], ['13 | 48'])
        self.assertEqual(self.call(self.server.api_bus, bus=2, level=2)[1], ['13 | 47 02 02'])
        # the rule is the shared helper the CLI uses as well
        self.assertTrue(protocol.bus_flag_may_release_mute(zen, 1, 'bus_dim', {'mute': True}))
        self.assertTrue(protocol.bus_flag_may_release_mute(zen, 1, 'bus_dim', None))
        self.assertFalse(protocol.bus_flag_may_release_mute(zen, 1, 'bus_dim', {'mute': False}))
        self.assertFalse(protocol.bus_flag_may_release_mute(zen, 1, 'bus_mute', {'mute': True}))
        written = []
        hid = types.SimpleNamespace(write=written.append,
                                    read_one=lambda magic, timeout: self.state(o31=0x03))
        args = types.SimpleNamespace(bus='1', state='off', timeout=0.1, force=False)
        with mock.patch.object(cli, 'get_transport', return_value=hid), \
                mock.patch.object(cli.time, 'sleep'):
            with self.assertRaises(SystemExit):          # CLI set-bus-dim hp1 off while muted
                cli.cmd_set_bus_dim(args, zen)
            self.assertEqual(written, [])
            cli.cmd_set_bus_mute(args, zen)              # the explicit un-mute still goes out
        self.assertEqual([zen_payload(packet) for packet in written], ['13 | 48 01'])

    def test_mixer_strip_byte_is_the_strip_number(self):
        mix = self.server.api_mix
        none = dict(fader=None, pan=None, send=None, send_pan=None, mute=None, solo=None)
        # HP2 page strip 16: pan to 0x3c then S, strip 9: fader 2; MONITOR page strip 16: fader 1
        self.assertEqual(self.call(mix, **{**none, 'mix': 1, 'channel': 15, 'pan': 28, 'solo': True})[1],
                         ['16 | d4 04 01 10 00 bc'])
        self.assertEqual(self.call(mix, **{**none, 'mix': 1, 'channel': 8, 'fader': 2})[1],
                         ['16 | d4 04 01 09 02 02'])
        self.assertEqual(self.call(mix, **{**none, 'mix': 0, 'channel': 15, 'fader': 1})[1],
                         ['16 | d4 04 00 10 01 3e'])

    def test_reverb_send_is_its_own_frame(self):
        mix = self.server.api_mix
        none = dict(fader=None, pan=None, send=None, send_pan=None, mute=None, solo=None)
        self.assertTrue(self.server.api_mixer()['has_send'])
        self.assertEqual(self.server.api_mixer()['send_mixes'], [0])
        # strip 16 send one notch up (pan kept from the readback), then the marker values
        self.assertEqual(self.call(mix, **{**none, 'mix': 0, 'channel': 15, 'send': 0x5f})[1],
                         ['16 | e9 04 00 10 5f 3e'])
        self.assertEqual(self.call(mix, **{**none, 'mix': 0, 'channel': 15, 'send': 0x21, 'send_pan': -7})[1],
                         ['16 | e9 04 00 10 21 19'])
        self.assertEqual(self.call(mix, **{**none, 'mix': 0, 'channel': 14, 'send': 0x2a})[1],
                         ['16 | e9 04 00 0f 2a 02'])
        self.assertEqual(self.device.mixer_json()['0'][15]['send'], 0x21)
        self.assertEqual(self.device.mixer_json()['0'][15]['send_pan'], -7)
        # a strip change and a send change together are two frames; mix 1 has no send
        self.assertEqual(self.call(mix, **{**none, 'mix': 0, 'channel': 15, 'send': 0x60, 'fader': 1})[1],
                         ['16 | e9 04 00 10 60 19', '16 | d4 04 00 10 01 3e'])
        result, sent = self.call(mix, **{**none, 'mix': 1, 'channel': 15, 'send': 0})
        self.assertEqual((result['ok'], sent), (False, []))
        for send, pan in ((97, 0), (-1, 0), (0, 31)):
            with self.assertRaises(protocol.ConstraintError):
                protocol.build_reverb_send_command(self.zen, 0, send, pan)
        with self.assertRaises(protocol.ConstraintError):
            protocol.build_reverb_send_command(self.zen, 16, 0, 0)

    def test_gazelle_reverb_frames_and_ranges(self):
        change = self.server.api_auraverb_change
        info = self.server.api_auraverb()
        self.assertTrue(info['available'])
        self.assertEqual(info['ranges']['pre_delay'], [0, 150])
        self.assertEqual(info['current'][0]['params']['room_size'], 0x51)
        base = dict(mix=0, param=None, value=None, enabled=None)
        self.assertEqual(self.call(change, **{**base, 'param': 'color', 'value': 99})[1],
                         ['1d | da 0b 00 51 63 00 64 0b 0d 18 42 32 01'])
        self.assertEqual(self.call(change, **{**base, 'param': 'pre_delay', 'value': 1})[1],
                         ['1d | da 0b 00 51 63 01 64 0b 0d 18 42 32 01'])
        self.assertEqual(self.call(change, **{**base, 'param': 'pre_delay', 'value': 0})[1],
                         ['1d | da 0b 00 51 63 00 64 0b 0d 18 42 32 01'])
        self.call(change, **{**base, 'param': 'color', 'value': 100})
        self.assertEqual(self.call(change, **{**base, 'enabled': False})[1],
                         ['1d | da 0b 00 51 64 00 64 0b 0d 18 42 32'])
        self.assertEqual(self.call(change, **{**base, 'param': 'late_reflection_delay', 'value': 150})[1],
                         ['1d | da 0b 00 51 64 00 64 0b 96 18 42 32'])
        for param, value in (('pre_delay', 151), ('reverb_level', 0), ('color', 101)):
            result, sent = self.call(change, **{**base, 'param': param, 'value': value})
            self.assertEqual((result['ok'], sent), (False, []), param)

    def test_reverb_returns(self):
        back = self.server.api_reverb_return
        self.assertEqual(self.call(back, bus=0, level=0x5a, mute=True)[1], ['13 | 68 00 da'])
        self.assertEqual(self.call(back, bus=0, level=0x58, mute=False)[1], ['13 | 68 00 58'])
        self.assertEqual(self.call(back, bus=1, level=0x5a, mute=True)[1], ['13 | 68 01 da'])
        self.assertEqual(self.call(back, bus=1, level=0, mute=False)[1], ['13 | 68 01'])
        for bus, level in ((2, 0), (0, 97), (0, -1)):
            result, sent = self.call(back, bus=bus, level=level, mute=False)
            self.assertEqual((result['ok'], sent), (False, []))
        self.assertIsNone(self.server.api_auraverb()['returns'])       # not read yet
        self.device.structured[(0x17, 0)] = protocol.parse_readback_records(
            self.zen, bytes.fromhex('b3 44 60 60'), 0x17, 0)            # marker pass
        self.assertEqual(self.server.api_auraverb()['returns'],
                         [{'bus': 0, 'level': 0x33, 'mute': True},
                          {'bus': 1, 'level': 0x44, 'mute': False}])

    def test_settings_frames_and_state_bytes(self):
        server = self.server
        self.assertEqual(self.call(server.api_output_trim, target=0, value=6)[1], ['54 | cb 42 00 01 06'])
        self.assertEqual(self.call(server.api_output_trim, target=0, value=0)[1], ['54 | cb 42 00 01'])
        self.assertEqual(self.call(server.api_output_trim, target=1, value=0)[1], [])   # Orion-only target
        self.assertEqual(self.call(server.api_pan_law, value=3)[1], ['12 | 24 03'])
        self.assertEqual(self.call(server.api_pan_law, value=4)[1], [])
        self.assertEqual(self.zen['params']['pan_law']['values']['3'], '-4.5 dB')
        self.assertEqual(self.call(server.api_oscillator, value=0x44)[1], ['12 | 0a 44'])
        self.assertEqual(self.call(server.api_oscillator, value=0x34)[1], ['12 | 0a 34'])
        self.assertEqual(self.call(server.api_oscillator, value=0x06)[1], [])           # bit 0x02 never seen
        self.assertEqual(self.call(server.api_brightness, value=0x2b)[1], ['12 | 0e 2b'])
        # 25:14>18 with 29:29>31 (trim 14 dBu, Monitor muted), 26 = brightness, 20 = oscillator
        report = self.state(o20=0x04, o25=0x18, o26=0x23, o29=0x31)
        self.assertEqual(self.device._parse_output_trim(report), {'monitor_a': 6})
        self.assertEqual(protocol.parse_state_scalar(self.zen, report, 'screen_brightness_byte_offset'), 0x23)
        self.assertEqual(protocol.parse_state_scalar(self.zen, report, 'oscillator_byte_offset'), 0x04)
        self.assertTrue(self.device._parse_buses(report)[0]['mute'])
        with self.assertRaises(protocol.ConstraintError):
            protocol.build_output_trim_command(self.zen, 0, 7)

    def test_record_spdif_and_rack_inputs_write_one_group_each(self):
        route = self.server.api_route
        # Record 8 -> LOOPBACK HP 1/MON 2: the panel's own frame; groups 6-9 are not touched
        self.device.routing[3] = self.device.routing[6][:8]
        self.assertEqual(self.call(route, dest=3, channel=7, kind='loopback_hp1', number=2)[1],
                         ['53 | d3 41 03 00 00 00 01 03 02 03 03 02 00 02 01 0a 00 04 01'])
        self.assertEqual(self.call(route, dest=3, channel=7, kind='loopback_hp2', number=1)[1],
                         ['53 | d3 41 03 00 00 00 01 03 02 03 03 02 00 02 01 0a 00 05'])
        # an ordinary source on Record 5 is still ONE frame (the panel sent five)
        self.assertEqual(self.call(route, dest=3, channel=4, kind='spdif', number='R')[1],
                         ['53 | d3 41 03 00 00 00 01 03 02 03 03 02 01 02 01 0a 00 05'])
        self.assertEqual(self.call(route, dest=3, channel=0, kind='afx', number=1)[1][0][:19],
                         '53 | d3 41 03 03 00')
        self.assertEqual(self.call(route, dest=4, channel=1, kind='compplay', number=8)[1],
                         ['53 | d3 41 04 02 00 01 07'])
        self.assertEqual(self.call(route, dest=4, channel=0, kind='emumic', number=1)[1],
                         ['53 | d3 41 04 0a 00 01 07'])
        self.assertEqual(self.call(route, dest=5, channel=3, kind='compplay', number=3)[1],
                         ['53 | d3 41 05 00 00 00 01 01 00 01 02'])
        refused = [(5, 0, 'loopback_hp1', 1), (5, 0, 'afx', 1), (4, 0, 'afx', 1),
                   (6, 5, 'loopback_hp2', 1), (6, 5, 'afx', 1), (3, 8, 'preamp', 1)]
        for dest, channel, kind, number in refused:
            result, sent = self.call(route, dest=dest, channel=channel, kind=kind, number=number)
            self.assertEqual((result['ok'], sent), (False, []), (dest, kind))
        result, sent = self.call(route, dest=6, channel=0, kind='preamp', number=2)
        self.assertEqual((result['ok'], sent), (False, []))          # strips 1-4: their source is group 5
        with self.assertRaises(RuntimeError):                         # and the worker refuses it as well
            self.device._set_routing_source(types.SimpleNamespace(write=self.sent.append), 6, 0, (0, 1))
        self.assertEqual(self.sent, [])
        self.assertIn('LOOPBACK HP 2 1', protocol.route_source_refusal(self.zen, 5, 0x05, 0))
        self.assertIsNone(protocol.route_source_refusal(self.zen, 5, 0x08, 0))    # mute
        self.assertIsNone(protocol.route_source_refusal(self.profile_orion, 0, 0x06, 0))
        sent = self.call(route, dest=6, channel=15, kind='compplay', number=8)[1]   # strip 16, as captured
        self.assertEqual([frame[:13] for frame in sent],
                         [f'53 | d3 41 {dest:02x}' for dest in (6, 7, 8, 9)])
        self.assertTrue(all(frame[14:].startswith('00 00 00 01 03 02 03 03 02 00 02 01 0a 00 0a 01'
                                                  + ' 08 00' * 7 + ' 01 07') for frame in sent))


if __name__ == '__main__':
    unittest.main()

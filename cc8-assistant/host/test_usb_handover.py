"""Bus evidence requires ordered USB registry and host route samples."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from observe import protect_window

from usb_handover import CheckFailure, Handover, main, run, topology

ROOT = Path(__file__).resolve().parents[2]


def bus(*devices, hub=2):
    data = {
        'IORegistryEntryID': 1,
        'IORegistryEntryChildren': [
            {'IORegistryEntryID': hub, 'IORegistryEntryChildren': list(devices)}
        ],
    }
    return topology(plistlib.dumps(data))


def cc8(entry=3, serial='fixture-private-serial', location=41, product=0x201C):
    return {
        'IORegistryEntryID': entry,
        'USB Product Name': 'AILABS_S6',
        'IOObjectClass': 'IOUSBHostDevice',
        'USB Serial Number': serial,
        'locationID': location,
        'idVendor': 0x0E8D,
        'idProduct': product,
    }


def status(route='usb', *, switches=None):
    return {
        'runtime_instance': 'fixture-private-runtime',
        'bridge_version': '0.14.20',
        'model_ready': True,
        'connected': route is not None,
        'transport': route or 'none',
        'microphone_active': False,
        'calibrating': False,
        'playing': False,
        'mic_frames': 120,
        'render_frames': 240,
    }, {
        'transport': route or 'none',
        'usb_ready': route == 'usb',
        'wifi_ready': True,
        'usb_test_suppressed': False,
        'transport_switches': (0 if route == 'usb' else 1) if switches is None else switches,
    }


class WitnessTests(unittest.TestCase):
    def ready(self):
        check = Handover()
        check.sample(bus(cc8()), *status(), 0)
        return check

    def test_waits_for_independent_usb_absence_wifi_and_new_usb_registration(self):
        check = self.ready()
        self.assertFalse(check.sample(bus(), *status('usb'), 1))
        self.assertFalse(check.sample(bus(), *status(None), 3))
        self.assertFalse(check.sample(bus(), *status('wifi', switches=2), 10))
        self.assertFalse(check.sample(bus(cc8(4)), *status('wifi', switches=2), 12))
        self.assertTrue(check.sample(bus(cc8(4)), *status(switches=3), 16))
        self.assertEqual(
            check.rows,
            [
                {
                    'usb_absent_s': 1,
                    'wifi_selected_s': 10,
                    'usb_returned_s': 12,
                    'usb_selected_s': 16,
                    'route_switches': 3,
                }
            ],
        )
        self.assertIsNone(check.current)
        self.assertFalse(check.sample(bus(cc8(4)), *status(switches=3), 18))
        self.assertFalse(check.sample(bus(), *status('wifi', switches=4), 19))
        self.assertTrue(check.sample(bus(cc8(5)), *status(switches=5), 21))
        self.assertEqual(len(check.rows), 2)

    def test_software_switch_never_counts_as_bus_detach(self):
        check = self.ready()
        with self.assertRaisesRegex(CheckFailure, 'route_switched_without_bus_edge'):
            check.sample(bus(cc8()), *status('wifi'), 1)
        check = self.ready()
        h, t = status('wifi')
        with self.assertRaisesRegex(CheckFailure, 'usb_software_suppression'):
            check.sample(bus(cc8()), h, {**t, 'usb_test_suppressed': True}, 3)
        self.assertFalse(check.rows)

    def test_complete_software_outage_cannot_count_as_a_physical_edge(self):
        for flag in (True, 0, None):
            with self.subTest(flag=flag):
                h, t = status('wifi')
                with self.assertRaisesRegex(CheckFailure, 'usb_software_suppression'):
                    self.ready().sample(bus(), h, {**t, 'all_routes_test_suppressed': flag}, 2)

    def test_wrong_port_device_hub_or_missed_edge_is_never_counted(self):
        for replacement, reason in (
            (bus(cc8(4, serial='other')), 'usb_device_or_port_changed'),
            (bus(cc8(4, location=42)), 'usb_device_or_port_changed'),
            (bus(hub=5), 'usb_upstream_topology_changed'),
            (bus(cc8(4)), 'usb_detach_not_observed'),
            (bus({**cc8(4), 'USB Product Name': 'other'}), 'usb_port_has_other_device'),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(CheckFailure, reason):
                self.ready().sample(replacement, *status(), 1)

    def test_replug_without_wifi_evidence_or_new_registry_entry_fails(self):
        check = self.ready()
        h, t = status('wifi')
        self.assertFalse(check.sample(bus(), h, {**t, 'transport': 'usb'}, 1))
        with self.assertRaisesRegex(CheckFailure, 'usb_returned_before_wifi_observed'):
            check.sample(bus(cc8(4)), *status(), 2)
        check = self.ready()
        check.sample(bus(), *status('wifi'), 1)
        with self.assertRaisesRegex(CheckFailure, 'usb_new_registration_not_observed'):
            check.sample(bus(cc8()), *status(), 2)

    def test_missing_wifi_or_usb_fails_within_fixed_transition_window(self):
        check = self.ready()
        check.sample(bus(), *status(None), 1)
        with self.assertRaisesRegex(CheckFailure, 'wifi_takeover_not_observed'):
            check.sample(bus(), *status('wifi'), 32)
        check = self.ready()
        check.sample(bus(), *status('wifi'), 1)
        check.sample(bus(cc8(4)), *status('wifi'), 2)
        with self.assertRaisesRegex(CheckFailure, 'usb_priority_not_restored'):
            check.sample(bus(cc8(4)), *status('usb'), 33)

    def test_wifi_dropout_after_takeover_is_not_hidden_by_replug(self):
        check = self.ready()
        check.sample(bus(), *status('wifi'), 1)
        with self.assertRaisesRegex(CheckFailure, 'wifi_route_lost_while_usb_absent'):
            check.sample(bus(), *status(None), 2)

    def test_runtime_change_or_audio_activity_stops_observation(self):
        h, t = status('wifi')
        for changes, reason in (
            ({'microphone_active': True}, 'runtime_audio_not_idle'),
            ({'playing': True}, 'runtime_audio_not_idle'),
            ({'mic_frames': 121}, 'microphone_frames_changed'),
            ({'runtime_instance': 'different'}, 'runtime_changed'),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(CheckFailure, reason):
                self.ready().sample(bus(), {**h, **changes}, t, 2)

    def test_playback_between_idle_polls_invalidates_evidence(self):
        check = self.ready()
        h, t = status('wifi')
        with self.assertRaisesRegex(CheckFailure, 'render_frames_changed'):
            check.sample(bus(), {**h, 'render_frames': 241}, t, 1)
        self.assertFalse(check.rows)

    def test_backup_drop_after_usb_reappears_cannot_be_hidden(self):
        check = self.ready()
        check.sample(bus(), *status('wifi'), 1)
        with self.assertRaisesRegex(CheckFailure, 'backup_route_lost_before_usb_reselected'):
            check.sample(bus(cc8(4)), *status(None, switches=2), 2)
        self.assertFalse(check.rows)

    def test_usb_return_requires_paired_wifi_to_remain_ready(self):
        check = self.ready()
        check.sample(bus(), *status('wifi'), 1)
        health, transport = status(switches=2)
        transport['wifi_ready'] = False
        with self.assertRaisesRegex(CheckFailure, 'wifi_backup_not_ready_after_usb'):
            check.sample(bus(cc8(4)), health, transport, 2)
        self.assertFalse(check.rows)

    def test_missing_backup_between_cycles_breaks_continuous_witness(self):
        health, transport = status()
        transport['wifi_ready'] = False
        with self.assertRaisesRegex(CheckFailure, 'idle_usb_or_wifi_not_ready'):
            self.ready().sample(bus(cc8()), health, transport, 1)

    def test_extra_switches_between_samples_cannot_pass(self):
        check = self.ready()
        check.sample(bus(), *status('wifi'), 1)
        with self.assertRaisesRegex(CheckFailure, 'unexpected_route_switches'):
            check.sample(bus(cc8(4)), *status(switches=4), 2)
        self.assertFalse(check.rows)

    def test_unrecognized_and_duplicate_devices_fail_closed(self):
        with self.assertRaisesRegex(CheckFailure, 'usb_device_identity_unconfirmed'):
            bus(cc8(product=0x2000))
        with self.assertRaisesRegex(CheckFailure, 'usb_device_identity_ambiguous'):
            bus(cc8(), cc8(4))

    def test_runner_is_read_only_and_report_contains_no_usb_or_runtime_identifiers(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(
                runtime_root=Path(temporary),
                output=Path(temporary) / 'result.json',
                seconds=20,
                cycles=1,
            )
            samples = iter(
                [
                    (bus(cc8()), status()),
                    (bus(), status('wifi')),
                    (bus(cc8(4)), status(switches=2)),
                ]
            )
            seen = []
            now = [0.0]
            sample = None
            bus_reads = 0

            def read_bus(timeout):
                nonlocal sample, bus_reads
                if bus_reads % 2 == 0:
                    sample = next(samples)
                bus_reads += 1
                return sample[0]

            def request(path, *, operation, timeout):
                seen.append(operation)
                return sample[1][{'health': 0, 'transport_status': 1}[operation]]

            def sleep(seconds):
                now[0] += seconds

            result = run(
                args,
                read_bus=read_bus,
                request=request,
                sleep=sleep,
                clock=lambda: now[0],
                wall=lambda: now[0],
            )
            self.assertEqual(result, 0)
            self.assertEqual(seen, ['health', 'transport_status', 'health'] * 3)
            data = args.output.read_text()
            self.assertNotIn('fixture-private-', data)
            report = json.loads(data)
            self.assertFalse(report['manual_cable_action_asserted'])
            self.assertFalse(report['full_p0_acceptance'])
            self.assertEqual(len(report['completed_cycles']), 1)
            self.assertEqual(args.output.stat().st_mode & 0o777, 0o600)

    def test_bus_or_route_edge_crossing_probe_window_never_qualifies_wifi(self):
        for crossing in ('bus', 'route'):
            with self.subTest(crossing=crossing), tempfile.TemporaryDirectory() as temporary:
                args = SimpleNamespace(
                    runtime_root=Path(temporary),
                    output=Path(temporary) / 'edge.json',
                    seconds=20,
                    cycles=1,
                )
                plugged, absent, returned = bus(cc8()), bus(), bus(cc8(4))
                after_edge = returned if crossing == 'bus' else absent
                samples = iter([plugged, plugged, absent, after_edge, returned, returned])
                usb_health, usb_transport = status()
                wifi_health, wifi_transport = status('wifi')
                before_edge = wifi_health if crossing == 'bus' else usb_health
                reports = iter(
                    [
                        usb_health,
                        usb_transport,
                        usb_health,
                        before_edge,
                        wifi_transport,
                        wifi_health,
                        usb_health,
                        usb_transport,
                        usb_health,
                    ]
                )
                now = [0.0]

                def sleep(seconds):
                    now[0] += seconds

                result = run(
                    args,
                    read_bus=lambda timeout: next(samples),
                    request=lambda path, operation, timeout: next(reports),
                    clock=lambda: now[0],
                    wall=lambda: now[0],
                    sleep=sleep,
                )
                self.assertEqual(result, 2)
                report = json.loads(args.output.read_text())
                self.assertEqual(report['unstable_bus_polls'], int(crossing == 'bus'))
                self.assertEqual(report['unstable_route_polls'], int(crossing == 'route'))
                self.assertEqual(report['reason'], 'usb_detach_not_observed')
                self.assertFalse(report['completed_cycles'])

    def test_replaced_backend_cannot_supply_last_transport_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(
                runtime_root=Path(temporary),
                output=Path(temporary) / 'replacement.json',
                seconds=20,
                cycles=1,
            )
            plugged, absent, returned = bus(cc8()), bus(), bus(cc8(4))
            buses = iter([plugged, plugged, absent, absent, returned, returned])
            samples = (status(), status('wifi'), status(switches=2))
            reads = [0, 0, 0]
            now = [0.0]

            def request(path, operation, timeout):
                index = int(now[0] * 2)
                health, transport = samples[index]
                if operation == 'transport_status':
                    return transport
                reads[index] += 1
                if index == 2 and reads[index] == 2:
                    return {**health, 'runtime_instance': 'PRIVATE_NEW_INSTANCE'}
                return health

            def sleep(seconds):
                now[0] += seconds

            result = run(
                args,
                read_bus=lambda timeout: next(buses),
                request=request,
                clock=lambda: now[0],
                wall=lambda: now[0],
                sleep=sleep,
            )
            self.assertEqual(result, 2)
            report = json.loads(args.output.read_text())
            self.assertEqual(report['reason'], 'runtime_changed')
            self.assertFalse(report['completed_cycles'])
            self.assertNotIn('PRIVATE_NEW_INSTANCE', args.output.read_text())

    def test_wall_clock_gap_cannot_count_as_a_continuous_round(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(
                runtime_root=Path(temporary),
                output=Path(temporary) / 'gap.json',
                seconds=20,
                cycles=1,
            )
            wall = iter([100, 100, 107])
            h, t = status()
            result = run(
                args,
                read_bus=lambda timeout: bus(cc8()),
                clock=lambda: 0,
                request=lambda path, operation, timeout: h if operation == 'health' else t,
                wall=lambda: next(wall),
                sleep=lambda seconds: None,
            )
            self.assertEqual(result, 2)
            report = json.loads(args.output.read_text())
            self.assertEqual(report['reason'], 'observation_poll_gap')
            self.assertFalse(report['completed_cycles'])

    def test_managed_usb_witness_shares_the_runtime_automation_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root / 'device-access/private/voice-runtime'
            directory.mkdir(parents=True, mode=0o700)
            address = directory / 'announce.sock'
            output = root / 'result.json'
            args = ['--runtime-root', str(root), '--output', str(output), '--execute']
            with (
                protect_window(address),
                patch('usb_handover.sys.platform', 'darwin'),
                patch('usb_handover.run') as witness,
                redirect_stderr(io.StringIO()) as error,
            ):
                self.assertEqual(main(args), 2)
            witness.assert_not_called()
            self.assertIn('automation', error.getvalue())
            self.assertFalse(output.exists())

            def run_with_lock(_args):
                with self.assertRaises(BlockingIOError), protect_window(address):
                    self.fail('observation started during the USB witness')
                return 0

            with (
                patch('usb_handover.sys.platform', 'darwin'),
                patch('usb_handover.run', side_effect=run_with_lock) as witness,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(args), 0)
            witness.assert_called_once()
            with protect_window(address) as acquired:
                self.assertTrue(acquired)

    def test_minimal_path_plan_never_needs_runtime_or_mac(self):
        result = subprocess.run(
            [
                sys.executable,
                '-S',
                str(ROOT / 'cc8'),
                'watch-handover',
                '--runtime-root',
                '/nonexistent',
                '--cycles',
                '2',
            ],
            env={'PATH': '/usr/bin:/bin'},
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout)['cycles'], 2)
        self.assertFalse(json.loads(result.stdout)['execute'])

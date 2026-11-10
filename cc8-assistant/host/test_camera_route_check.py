"""Exercise the real route-check state machine with clocks/routes, never real media."""

from copy import deepcopy
import threading
import unittest

from camera_route_check import CameraRouteRun, CameraCheckFailure, CASES


class Simulation:
    def __init__(self, fault=None):
        self.now, self.generation = 0.0, 10
        self.fault = fault
        self.active = self.microphone = False
        self.until, self.route, self.outage = 0, 'usb', None
        self.probes, self.operations = [], []

    def sleep(self, seconds):
        self.now += seconds

    def request(self, payload):
        self.operations.append(deepcopy(payload))
        if self.outage is not None and self.now >= self.until:
            self.route = 'usb'
        if payload == {'operation': 'health'}:
            return dict(
                camera_metadata_only_supported=self.fault != 'old_backend',
                health_owner_version=1,
                runtime_instance=('b' if self.fault == 'owner' and self.probes else 'a') * 32,
                runtime_process_id=100,
                session_generation=self.generation,
                microphone_active=self.microphone,
                playing=False,
                calibrating=False,
                text_request_active=False,
                voice_asr_attempts=True if self.fault == 'counter' else 0,
                mic_frames=0,
                render_frames=0,
                desktop_snapshots_captured=1 if self.fault == 'early' and self.probes else 0,
                desktop_snapshot_active=self.active,
                desktop_snapshot_generation=self.generation if self.active else None,
                vision_active=self.active,
                connected=self.route != 'none',
                transport='wifi'
                if self.fault == 'backend_route'
                and self.outage is not None
                and self.now >= self.until + 1
                else self.route,
            )
        if payload == {'operation': 'transport_status'}:
            suppressed = self.outage is not None and self.now < self.until
            return dict(
                usb_ready=not suppressed,
                wifi_ready=self.route != 'none',
                usb_test_suppressed=suppressed,
                all_routes_test_suppressed=suppressed and self.outage == 'all',
            )
        if payload.get('operation') == 'camera_route_interrupt':
            assert (
                payload['expected_instance'] == 'a' * 32
                and payload['expected_generation'] == self.generation
            )
            self.outage = 'all' if payload['route'] == 'all_routes_lost' else 'wifi'
            self.until = self.now + (10 if self.outage == 'all' else 12)
            self.route = 'none' if self.outage == 'all' else 'wifi'
            if self.fault != 'not_cancelled':
                self.active = False
                self.generation += 1
                probe = self.probes[-1]
                probe.result = dict(
                    status='interrupted', request_id=probe.ident, metadata_only=True
                )
                if self.fault == 'receipt':
                    probe.result['request_id'] = 'f' * 32
                probe.done.set()
            if self.fault == 'interrupt':
                raise KeyboardInterrupt
            return dict(ok=True)
        raise AssertionError('Unplanned operation')

    def status(self):
        opened = self.active and self.fault != 'no_preview'
        if self.outage is not None and self.now >= self.until and self.fault == 'reopen':
            opened = True
        if self.active and self.fault == 'slow_read':
            self.now += 8
        return (
            dict(
                foreground=self.fault != 'home',
                microphone_active=self.microphone,
                vision_active=self.active,
                connected=self.route != 'none',
                transport='usb' if self.fault == 'device_route' else self.route,
            ),
            dict(
                camera_open=opened,
                camera_captures=int(
                    self.fault == 'late_frame'
                    and self.outage is not None
                    and self.now >= self.until
                ),
                camera_preview_elapsed_ms=3500 if self.fault == 'late' else 100,
                camera_preview_hold_ms=5000,
            ),
        )

    def probe(self, instance, generation):
        assert instance == 'a' * 32 and generation == self.generation
        simulation = self

        class Probe:
            def __init__(self):
                self.ident = f'{len(simulation.probes) + 1:032x}'
                self.done, self.result, self.error = threading.Event(), None, None
                self.closed = False

            def start(self):
                simulation.probes.append(self)
                simulation.generation += 1
                simulation.active = True
                if simulation.fault == 'microphone':
                    simulation.microphone = True
                if simulation.fault in ('early', 'permission'):
                    simulation.active = False
                    self.result = dict(
                        status='completed' if simulation.fault == 'early' else 'failed',
                        request_id=self.ident,
                        metadata_only=True,
                    )
                    self.done.set()

            def close(self):
                self.closed = True
                simulation.active = False
                self.done.set()

        return Probe()

    def run(self):
        return CameraRouteRun(
            self, self.request, self.probe, clock=lambda: self.now, sleep=self.sleep
        )


class CameraRouteTests(unittest.TestCase):
    def test_both_real_control_flow_cases_require_preview_cancel_receipt_release_and_restore(self):
        simulation = Simulation()
        snapshots = []
        value = simulation.run().run(checkpoint=snapshots.append)
        self.assertEqual(value['state'], 'passed')
        self.assertEqual([row['case'] for row in value['cases']], list(CASES))
        for row in value['cases']:
            self.assertEqual(row['state'], 'passed')
            for key in (
                'cleanup_verified',
                'camera_released',
                'matched_interruption_receipt',
                'usb_priority_restored',
                'counters_unchanged',
            ):
                self.assertTrue(row[key])
            self.assertEqual(row['images_returned'], 0)
            self.assertGreaterEqual(row['reconnect_observed_seconds'], 3)
        self.assertTrue(all(probe.closed for probe in simulation.probes))
        self.assertFalse(simulation.active or simulation.microphone)
        self.assertTrue(value['real_camera_test_performed'])
        self.assertFalse(value['full_p0_acceptance'] or value['physical_usb_unplug_tested'])
        self.assertFalse(value['images_exported'] or value['images_saved'])
        self.assertEqual(snapshots[-1]['state'], 'passed')

    def test_each_failure_stops_the_batch_without_retake_or_model_operations(self):
        for fault in (
            'old_backend',
            'counter',
            'owner',
            'home',
            'microphone',
            'receipt',
            'early',
            'permission',
            'late',
            'no_preview',
            'slow_read',
            'not_cancelled',
            'reopen',
            'late_frame',
            'device_route',
            'backend_route',
        ):
            with self.subTest(fault=fault):
                simulation = Simulation(fault)
                value = simulation.run().run()
                self.assertEqual(value['state'], 'failed')
                self.assertEqual(len(value['cases']), 1)
                self.assertLessEqual(len(simulation.probes), 1)
                self.assertTrue(all(probe.closed for probe in simulation.probes))
                self.assertFalse(value['images_saved'] or value['images_exported'])
                self.assertTrue(
                    all(
                        'text' not in row and row.get('operation') != 'device_control'
                        for row in simulation.operations
                    )
                )
                if fault in ('old_backend', 'counter', 'home'):
                    self.assertEqual(simulation.probes, [])
                    self.assertFalse(value['real_camera_test_performed'])
                if fault == 'microphone':
                    self.assertTrue(
                        simulation.microphone
                    )  # Never stop an owner's unrelated capture.

    def test_operator_interrupt_cancels_owned_preview_waits_for_routes_and_saves_receipt(self):
        simulation = Simulation('interrupt')
        receipts = []
        value = simulation.run().run(checkpoint=receipts.append)
        self.assertEqual(value['state'], 'interrupted')
        self.assertEqual(len(simulation.probes), 1)
        self.assertTrue(simulation.probes[0].closed)
        self.assertFalse(simulation.active)
        self.assertEqual(simulation.route, 'usb')
        self.assertTrue(value['cases'][0]['cleanup_verified'])
        self.assertTrue(value['cases'][0]['outage_requested'])
        self.assertEqual(receipts[-1]['state'], 'interrupted')

    def test_case_selection_is_explicit_bounded_and_one_shot(self):
        for cases in ((), ['wifi_handover'], ('unknown',), (CASES[0], CASES[0])):
            with self.subTest(cases=cases):
                simulation = Simulation()
                with self.assertRaises(CameraCheckFailure):
                    simulation.run().run(cases)
                self.assertEqual(simulation.operations, [])
        simulation = Simulation()
        runner = simulation.run()
        self.assertEqual(runner.run((CASES[1],))['state'], 'passed')
        with self.assertRaises(CameraCheckFailure):
            runner.run()

"""Automation fails closed and restores capture; fixtures never contact hardware."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from device_smoke import CheckFailure, Device, SmokeRun, main, objects, rpc


class DeviceAutomationTests(unittest.TestCase):
    def test_missing_speech_probe_voice_aborts_before_ui_or_capture(self):
        run, device, health, now = self.fixture(capture=True, standby=True, acoustic=True)
        device.prepare_speech_probe.side_effect = ValueError('unavailable_voice')
        result = run.run()
        self.assertEqual(result['state'], 'failed')
        device.open.assert_not_called()
        device.speak_probe.assert_not_called()

    def test_speech_probe_resolves_actual_installed_voice_before_say(self):
        device = Device.__new__(Device)
        with (
            patch('device_smoke.resolve_voice', return_value='Tingting (Chinese CN)') as resolve,
            patch('device_smoke.subprocess.run') as command,
        ):
            device.speak_probe()
            resolve.assert_called_once_with('Tingting')
            self.assertEqual(command.call_args.args[0][1:3], ['-v', 'Tingting (Chinese CN)'])
        with (
            patch('device_smoke.resolve_voice', side_effect=ValueError),
            patch('device_smoke.subprocess.run') as command,
        ):
            with self.assertRaises(ValueError):
                device.speak_probe()
            command.assert_not_called()

    def fixture(self, **options):
        now = [0.0]
        device = Mock()
        device.status.return_value = (
            {
                'foreground': True,
                'microphone_active': False,
                'vision_active': False,
                'transport': 'usb',
            },
            {'camera_open': False, 'wake_checked': True},
        )
        health = {
            'ok': True,
            'runtime_instance': 'same-instance',
            'connected': True,
            'microphone_active': False,
            'calibrating': False,
            'playing': False,
            'transport': 'usb',
            'mic_frames': 100,
        }

        def request(p):
            if p == {'operation': 'health'}:
                return dict(health)
            if p == {'operation': 'transport_status'}:
                return {'ok': True, 'usb_ready': True, 'wifi_ready': True}
            raise AssertionError('Unexpected operation')

        def sleep(seconds):
            now[0] += seconds

        run = SmokeRun(device, request, clock=lambda: now[0], sleep=sleep, **options)
        return run, device, health, now

    def test_dry_plan_in_minimal_path_has_no_device_or_model_side_effect(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / 'not-created'
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / 'cc8'),
                    'verify-device',
                    '--runtime-root',
                    str(missing),
                    '--capture',
                    '--standby',
                    '--acoustic',
                ],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertFalse(plan['execute'])
            self.assertEqual(len(plan['checks']), 9)
            self.assertFalse(missing.exists())
        with patch('device_smoke.Device') as device, patch('device_smoke.rpc') as query:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--runtime-root', '/nonexistent']), 0)
            device.assert_not_called()
            query.assert_not_called()

    def test_capture_options_cannot_be_enabled_implicitly(self):
        for flags in (['--standby'], ['--acoustic'], ['--capture', '--acoustic']):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit), patch('device_smoke.Device') as device:
                    main(['--runtime-root', '/unused', *flags])
                device.assert_not_called()

    def test_preflight_transport_errors_are_redacted_by_cli(self):
        import device_smoke
        from release_info import RELEASE

        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                'device_smoke.rpc',
                return_value={
                    'bridge_version': RELEASE.backend_version,
                    'protocol_version': RELEASE.protocol_version,
                },
            ),
            patch('device_smoke.private_directory', return_value=Path(folder)),
            patch('device_smoke.Device', side_effect=RuntimeError('PRIVATE_DEVICE_OUTPUT')),
            redirect_stderr(io.StringIO()) as error,
        ):
            with self.assertRaises(SystemExit) as stopped:
                main(['--runtime-root', str(device_smoke.ROOT), '--execute'])
            self.assertEqual(stopped.exception.code, 2)
            self.assertNotIn('PRIVATE_DEVICE_OUTPUT', error.getvalue())

    def test_preflight_busy_owner_or_camera_never_mutates_device(self):
        for camera in (False, True):
            run, device, health, _ = self.fixture()
            if camera:
                d, ui = device.status.return_value
                ui['camera_open'] = True
            else:
                health['microphone_active'] = True
            result = run.run()
            self.assertEqual(result['state'], 'failed')
            device.open.assert_not_called()
            device.home.assert_not_called()

    def test_replaced_runtime_does_not_pass_checks(self):
        run, _, health, _ = self.fixture()
        run.preflight()
        health['runtime_instance'] = 'different-instance'
        with self.assertRaisesRegex(CheckFailure, 'runtime_restarted'):
            run.health()

    def test_device_version_mismatch_is_rejected_before_mutation(self):
        run, device, _, _ = self.fixture(expected_apk=('0.8.8', 33))
        device.version.return_value = ('0.8.6', 31)
        result = run.run()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['checks'][0]['reason'], 'device_source_version_mismatch')
        device.open.assert_not_called()
        device.home.assert_not_called()

    def test_maintenance_open_cannot_hide_auto_started_capture(self):
        run, device, health, _ = self.fixture()
        run.preflight()

        def opened(capture):
            self.assertFalse(capture)
            health['microphone_active'] = True

        device.open.side_effect = opened
        with self.assertRaises(CheckFailure):
            run.maintenance_open()

    def test_home_requires_stopped_capture_and_stable_frame_count(self):
        run, device, health, _ = self.fixture()
        run.preflight()
        run.home()
        device.home.assert_called_once()
        original_sleep = run.sleep

        def advancing_frames(seconds):
            original_sleep(seconds)
            health['mic_frames'] += 160

        run.sleep = advancing_frames
        with self.assertRaisesRegex(CheckFailure, 'frames_increasing'):
            run.home()

    def test_disabled_capture_plan_never_opens_recording(self):
        run, device, _, _ = self.fixture()
        run.playback = Mock()
        run.handover = Mock()
        result = run.run()
        self.assertEqual(result['state'], 'passed')
        self.assertTrue(result['cleanup_microphone_off_verified'])
        self.assertTrue(all(call.args == (False,) for call in device.open.call_args_list))
        self.assertEqual(len(result['checks']), 4)
        self.assertFalse(result['physical_usb_unplug_tested'])
        self.assertFalse(result['full_p0_acceptance'])

    def test_failure_and_keyboard_interrupt_cleanup_without_recording_retry(self):
        for error in (RuntimeError('PRIVATE_PROVIDER_BODY'), KeyboardInterrupt()):
            run, device, _, _ = self.fixture(capture=True)

            def failure():
                run.device.open(True)
                raise error

            run.playback = failure
            result = run.run()
            self.assertIn(result['state'], ('failed', 'interrupted'))
            self.assertTrue(result['cleanup_microphone_off_verified'])
            device.home.assert_called_once()
            self.assertEqual(device.open.call_args_list[-1].args, (False,))
            self.assertEqual(sum(call.args == (True,) for call in device.open.call_args_list), 1)
            self.assertNotIn('PRIVATE_PROVIDER_BODY', json.dumps(result))

    def test_failed_cleanup_cannot_be_reported_as_pass(self):
        run, device, _, _ = self.fixture()
        run.playback = Mock(side_effect=CheckFailure('injected_failure'))
        device.home.side_effect = OSError('PRIVATE_BODY')
        result = run.run()
        self.assertEqual(result['state'], 'cleanup_unconfirmed')
        self.assertFalse(result['cleanup_microphone_off_verified'])
        self.assertNotIn('PRIVATE_BODY', json.dumps(result))

    def test_playout_requires_fresh_completed_receipt_not_old_success(self):
        for fresh in (False, True):
            run, device, health, _ = self.fixture()
            row = {
                'request_id': 'old',
                'origin': 'announcement',
                'outcome': 'completed',
                'stages': {'playout_ack': 1, 'playback_done': 2},
            }
            count = [0]

            def request(p):
                if p.get('operation') == 'diagnostics':
                    count[0] += 1
                    return {
                        'requests': [row]
                        + ([{**row, 'request_id': 'new'}] if count[0] > 1 and fresh else [])
                    }
                if 'text' in p:
                    return {'status': 'completed'}
                return dict(health)

            run.request = request
            # UI polling must also be observed, so even a fresh receipt alone
            # cannot establish the caption/layout checks.
            with self.assertRaises(CheckFailure):
                run.playback()

    def test_route_switch_cannot_rearm_microphone(self):
        run, _, health, _ = self.fixture()
        run.preflight()

        def request(p):
            if p.get('operation') == 'transport_test':
                health.update(transport='wifi', microphone_active=True)
                return {'ok': True}
            return dict(health)

        run.request = request
        with self.assertRaisesRegex(CheckFailure, 'handover_started_capture'):
            run.handover()

    def test_standby_cannot_accept_an_already_black_screen_as_15_minutes(self):
        run, device, _, _ = self.fixture(capture=True, standby=True)
        run.start_capture = Mock()
        run.health = lambda: {'microphone_active': True, 'connected': True}
        d, ui = device.status.return_value
        ui['screen_sleeping'] = True
        with self.assertRaisesRegex(CheckFailure, 'too_early'):
            run.idle()

    def test_playback_checks_ui_samples_and_fresh_receipt(self):
        run, device, health, _ = self.fixture()
        health['playing'] = True
        device.status.return_value[1].update(
            caption_progress_overlap=False, halo_status_overlap=False, caption_line_visible=True
        )
        released = threading.Event()
        count = [0]

        def request(p):
            if 'text' in p:
                self.assertTrue(released.wait(2))
                return {'status': 'completed'}
            if p.get('operation') == 'diagnostics':
                count[0] += 1
                return {
                    'requests': []
                    if count[0] == 1
                    else [
                        {
                            'request_id': 'fresh',
                            'origin': 'announcement',
                            'outcome': 'completed',
                            'stages': {'playout_ack': 10, 'playback_done': 20},
                        }
                    ]
                }
            return dict(health)

        run.request = request

        def sleep(seconds):
            released.set()
            time.sleep(0.001)

        run.sleep = sleep
        run.playback()
        device.status.assert_called()

    def test_real_idle_waits_full_duration(self):
        run, device, health, now = self.fixture(capture=True, standby=True)
        run.start_capture = Mock()
        health['microphone_active'] = True
        original = device.status.return_value
        device.status.side_effect = lambda: (
            dict(original[0]),
            {**original[1], 'screen_sleeping': now[0] >= 900},
        )
        run.idle()
        self.assertGreaterEqual(now[0], 900)
        self.assertLess(now[0], 906)
        self.assertFalse(run.report('passed')['full_p0_acceptance'])

    def test_idle_timeout_is_bounded(self):
        run, _, health, now = self.fixture(capture=True, standby=True)
        run.start_capture = Mock()
        health['microphone_active'] = True
        with self.assertRaisesRegex(CheckFailure, 'idle_black_screen_timeout'):
            run.idle()
        self.assertGreaterEqual(now[0], 970)
        self.assertLessEqual(now[0], 975)

    def test_acoustic_probe_needs_idle_capture_before_speaking(self):
        run, device, _, _ = self.fixture(capture=True, standby=True, acoustic=True)
        with self.assertRaises(CheckFailure):
            run.acoustic_probe()
        device.speak_probe.assert_not_called()

    def test_dumpsys_missing_or_duplicate_receipt_is_not_success(self):
        for text in ('other output', 'HUD_UI_STATUS {}\nHUD_UI_STATUS {}'):
            with self.assertRaises(CheckFailure):
                objects(text, 'HUD_UI_STATUS ')
        result = objects('PRIVATE_CAPTION\nHUD_UI_STATUS {"camera_open":false}', 'HUD_UI_STATUS ')
        self.assertEqual(result, {'camera_open': False})

    def test_ipc_rejects_camera_and_arbitrary_tool_routes_before_connect(self):
        for request in (
            {'operation': 'camera_snapshot'},
            {'operation': 'text_request', 'text': 'PRIVATE'},
            {'operation': 'device_control', 'arguments': {'action': 'start'}},
        ):
            with patch('device_smoke.socket.socket') as connection:
                with self.assertRaises(CheckFailure):
                    rpc(Path('/nonexistent'), request)
                connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()

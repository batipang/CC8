"""Verify speech preparation ordering and cleanup in the actual smoke runner."""

import json
import unittest
from unittest.mock import Mock

import test_device_smoke as fixture


class PreparedInterruptionTests(unittest.TestCase):
    def fixture(self, **options):
        result = fixture.DeviceAutomationTests().fixture(
            capture=True, synthetic_barge=True, **options
        )
        result[1].status.return_value[0]['test_interruption_transcript_supported'] = True
        return result

    def test_input_is_prepared_before_open_and_freed_after_success_or_later_failure(self):
        for fail in (None, 'maintenance', 'capture'):
            with self.subTest(fail=fail):
                run, device, _, _ = self.fixture()
                events = []
                prepare = device.prepare_interruption_probe.side_effect
                device.prepare_interruption_probe.side_effect = lambda **kw: (
                    events.append('prepare'),
                    prepare(**kw),
                )[1]
                device.close_speech_probe.side_effect = lambda: events.append('close')
                for name in (
                    'maintenance_open',
                    'playback',
                    'handover',
                    'start_capture',
                    'acoustic_probe',
                    'interruption_probe',
                    'home',
                ):

                    def operation(*, name=name, **kwargs):
                        events.append(name)
                        if (fail == 'maintenance' and name == 'maintenance_open') or (
                            fail == 'capture' and name == 'start_capture'
                        ):
                            raise RuntimeError('private-body-must-not-be-reported')

                    setattr(run, name, Mock(side_effect=operation))
                run.cleanup = Mock(side_effect=lambda: events.append('capture-cleanup'))
                report = run.run()
                self.assertEqual(report['state'], 'passed' if fail is None else 'failed')
                self.assertEqual(events[0], 'prepare')
                self.assertEqual(events[-2:], ['capture-cleanup', 'close'])
                self.assertIs(report['prepared_interruption_input_cleaned'], True)
                self.assertEqual(report['prepared_interruption_input']['kind'], 'barge')
                report['prepared_interruption_input']['kind'] = 'changed'
                self.assertEqual(
                    run.report('failed')['prepared_interruption_input']['kind'], 'barge'
                )
                self.assertNotIn('private-body', json.dumps(report))

    def test_preparation_failure_is_cleaned_without_opening_or_stopping_owner_capture(self):
        run, device, _, _ = self.fixture()
        device.prepare_interruption_probe.side_effect = RuntimeError('private-error')
        report = run.run()
        self.assertEqual(report['state'], 'failed')
        device.open.assert_not_called()
        device.home.assert_not_called()
        device.close_speech_probe.assert_called_once_with()
        self.assertIs(report['prepared_interruption_input_cleaned'], True)
        self.assertIsNone(report['prepared_interruption_input'])
        self.assertNotIn('private-error', json.dumps(report))

    def test_state_changed_while_synthesizing_is_not_overridden_by_maintenance(self):
        for change in (
            'microphone',
            'playback',
            'foreground',
            'camera',
            'task',
            'runtime',
            'wake',
            'backup',
        ):
            with self.subTest(change=change):
                run, device, health, _ = self.fixture()
                prepare = device.prepare_interruption_probe.side_effect

                def changed(**kwargs):
                    value = prepare(**kwargs)
                    if change == 'microphone':
                        health['microphone_active'] = True
                    elif change == 'playback':
                        health['playing'] = True
                    elif change == 'runtime':
                        health['runtime_instance'] = 'different'
                    elif change == 'foreground':
                        device.status.return_value[0]['foreground'] = False
                    elif change == 'camera':
                        device.status.return_value[1]['camera_open'] = True
                    elif change == 'wake':
                        device.status.return_value[1]['wake_checked'] = False
                    elif change == 'backup':
                        original = run.request
                        run.request = lambda value: (
                            {'ok': True, 'usb_ready': True, 'wifi_ready': False}
                            if value == {'operation': 'transport_status'}
                            else original(value)
                        )
                    elif change == 'task':
                        original = run.request
                        run.request = lambda value: (
                            {'requests': [{'outcome': None}]}
                            if value == {'operation': 'diagnostics'}
                            else original(value)
                        )
                    return value

                device.prepare_interruption_probe.side_effect = changed
                report = run.run()
                self.assertNotEqual(report['state'], 'passed')
                device.open.assert_not_called()
                device.home.assert_not_called()
                device.speak_probe.assert_not_called()
                device.close_speech_probe.assert_called_once_with()
                self.assertIs(report['prepared_interruption_input_cleaned'], True)

    def test_cleanup_failure_never_suppresses_the_owned_capture_cleanup(self):
        for part in ('capture', 'file'):
            with self.subTest(part=part):
                run, device, _, _ = self.fixture()
                run.maintenance_open = Mock(side_effect=RuntimeError('stop-before-capture'))
                run.cleanup = Mock(
                    side_effect=OSError('capture-close-error') if part == 'capture' else None
                )
                if part == 'file':
                    device.close_speech_probe.side_effect = OSError('file-close-error')
                report = run.run()
                self.assertEqual(report['state'], 'cleanup_unconfirmed')
                run.cleanup.assert_called_once_with()
                device.close_speech_probe.assert_called_once_with()
                self.assertIs(report['prepared_interruption_input_cleaned'], part != 'file')
                self.assertNotIn('close-error', json.dumps(report))

    def test_owner_apk_changed_during_preparation_is_not_reopened(self):
        run, device, _, _ = self.fixture()
        run.expected_apk = ('0.9.37', 74)
        device.version.return_value = run.expected_apk
        device.status.return_value[0]['test_playback_stop_supported'] = True
        prepare = device.prepare_interruption_probe.side_effect

        def changed(**kwargs):
            device.version.return_value = ('0.9.38', 75)
            return prepare(**kwargs)

        device.prepare_interruption_probe.side_effect = changed
        report = run.run()
        self.assertEqual(report['checks'][0]['reason'], 'device_source_version_mismatch')
        device.open.assert_not_called()
        device.home.assert_not_called()
        device.close_speech_probe.assert_called_once_with()

    def test_invalid_or_content_bearing_receipt_is_rejected_before_ui(self):
        for field, value in (
            ('text', 'private-content'),
            ('kind', 'local_stop'),
            ('acoustic_playback_confirmed', True),
        ):
            with self.subTest(field=field):
                run, device, _, _ = self.fixture()
                prepare = device.prepare_interruption_probe.side_effect
                device.prepare_interruption_probe.side_effect = lambda **kw: {
                    **prepare(**kw),
                    field: value,
                }
                report = run.run()
                self.assertEqual(report['state'], 'failed')
                device.open.assert_not_called()
                device.close_speech_probe.assert_called_once_with()
                self.assertNotIn('private-content', json.dumps(report))


if __name__ == '__main__':
    unittest.main()

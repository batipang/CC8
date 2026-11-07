"""Active-format proof rejects preferences, declared fallback and malformed reports."""

import json
import io
from contextlib import redirect_stderr
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from capture_frontend_evidence import summarize, valid_report
from device_smoke import CheckFailure, SmokeRun, main
import test_device_smoke as smoke_tests
import test_audit_device_suite as audit_tests
from audit_device_suite import content_free_phase, audit


def active(mode='paired', fallback=None):
    device = {
        'microphone_active': True,
        'capture_frontend': mode,
        'paired_capture_version': 1,
        'capture_sample_rate': 48000 if mode == 'paired' else 16000,
        'capture_channels': 4 if mode == 'paired' else 1,
        'capture_clock_evidence_supported': mode == 'mono',
    }
    if fallback is not None:
        device['capture_fallback_reason'] = fallback
    return device, {'microphone_active': True, 'capture_frontend': mode}


class CaptureFrontendEvidenceTests(unittest.TestCase):
    def test_actual_formats_and_declared_fallback_remain_distinct(self):
        for mode, fallback in [
            ('mono', None),
            ('paired', None),
            ('mono', 'paired_format_unavailable'),
        ]:
            with self.subTest(mode=mode, fallback=fallback):
                evidence = summarize(*active(mode, fallback))
                self.assertEqual(evidence['status'], 'observed')
                self.assertEqual(evidence['frontend'], mode)
                self.assertEqual(evidence['fallback_reason'], fallback)
                self.assertTrue(valid_report({'capture_frontend_evidence': evidence}))
                self.assertTrue(
                    valid_report(
                        {'expected_capture_frontend': mode, 'capture_frontend_evidence': evidence}
                    )
                )
                other = 'mono' if mode == 'paired' else 'paired'
                self.assertFalse(
                    valid_report(
                        {'expected_capture_frontend': other, 'capture_frontend_evidence': evidence}
                    )
                )

    def test_missing_idle_mismatched_or_malformed_fields_never_prove_format(self):
        self.assertEqual(
            summarize({}, {}), {'status': 'unavailable', 'reason': 'legacy_metadata_missing'}
        )
        for device, host in ((active()[0], {}), ({}, active('mono')[1])):
            self.assertEqual(
                summarize(device, host),
                {'status': 'unavailable', 'reason': 'legacy_metadata_missing'},
            )
        for key, value in (
            ('microphone_active', False),
            ('capture_frontend', 'none'),
            ('capture_frontend', True),
            ('capture_frontend', {'api_key': 'PRIVATE-SENTINEL'}),
            ('paired_capture_version', True),
            ('capture_sample_rate', True),
            ('capture_sample_rate', 16000),
            ('capture_channels', 1),
            ('capture_channels', 4.0),
            ('capture_fallback_reason', 'platform_api'),
            ('capture_fallback_reason', 'PRIVATE-SENTINEL'),
        ):
            with self.subTest(key=key, value_type=type(value).__name__):
                d, h = active()
                d[key] = value
                result = summarize(d, h)
                self.assertEqual(result['status'], 'unconfirmed')
                self.assertNotIn('PRIVATE-SENTINEL', json.dumps(result))
                self.assertFalse(valid_report({'capture_frontend_evidence': result}))
        d, h = active()
        h['capture_frontend'] = 'mono'
        self.assertEqual(summarize(d, h)['reason'], 'frontend_not_confirmed')
        h['capture_frontend'] = 'paired'
        h['microphone_active'] = False
        self.assertEqual(summarize(d, h)['reason'], 'capture_not_active')

    def test_external_audit_expectation_requires_all_phases_and_preserves_legacy(self):
        for make in (audit_tests.saved_suite, audit_tests.saved_english_pair):
            data = make()
            self.assertTrue(audit(data)['verified'])
            self.assertFalse(audit(data, expect_capture_frontend='paired')['verified'])
            for phase in data['phases']:
                phase['report'].update(
                    expected_capture_frontend='paired',
                    capture_frontend_evidence=summarize(*active()),
                )
            self.assertTrue(audit(data, expect_capture_frontend='paired')['verified'])
            data['phases'][-1]['report'].update(
                expected_capture_frontend='mono',
                capture_frontend_evidence=summarize(*active('mono')),
            )
            result = audit(data, expect_capture_frontend='paired')
            self.assertFalse(result['verified'])
            self.assertEqual(
                result.get('reason', result.get('failure', {}).get('reason')),
                'capture_frontend_expectation_mismatch',
            )
        with self.assertRaises(ValueError):
            audit(audit_tests.saved_suite(), expect_capture_frontend=True)

    def test_offline_audit_cannot_upgrade_legacy_or_inconsistent_receipts(self):
        base = {
            'schema': 1,
            'capture_tests_enabled': True,
            'raw_audio_saved': False,
            'contains_conversation_content': False,
            'automation_source_sha256': 'a' * 64,
            'speech_probe_source_sha256': 'b' * 64,
        }
        self.assertTrue(content_free_phase(base))
        self.assertFalse(content_free_phase({**base, 'expected_capture_frontend': 'paired'}))
        evidence = summarize(*active())
        report = {
            **base,
            'expected_capture_frontend': 'paired',
            'capture_frontend_evidence': evidence,
        }
        self.assertTrue(content_free_phase(report))
        for key, value in (
            ('capture_channels', True),
            ('capture_sample_rate', 48000.0),
            ('fallback_reason', 'platform_api'),
            ('capture_clock_supported', 1),
            ('frontend', 'mono'),
            ('extra', 'PRIVATE-SENTINEL'),
        ):
            with self.subTest(key=key):
                self.assertFalse(
                    content_free_phase(
                        {**report, 'capture_frontend_evidence': {**evidence, key: value}}
                    )
                )
        self.assertFalse(valid_report({'expected_capture_frontend': True}))

    def fixture(self, **options):
        return smoke_tests.DeviceAutomationTests.fixture(self, **options)

    def capture(self, run, device, health, mode='paired', fallback=None):
        d, ui = device.status.return_value
        d.update(active(mode, fallback)[0])
        d.update(
            test_capture_limit_ms=run.capture_limit_ms,
            test_capture_remaining_ms=run.capture_limit_ms,
        )
        health.update(active(mode, fallback)[1], local_wake_ready=True)
        return d, ui

    def test_start_requires_actual_paired_receipt_and_retains_mono_failure(self):
        run, device, health, _ = self.fixture(capture=True, expected_capture_frontend='paired')
        self.capture(run, device, health, 'mono', 'paired_format_unavailable')
        with self.assertRaisesRegex(CheckFailure, 'capture_frontend_expectation_mismatch'):
            run.start_capture()
        proof = run.report('failed')['capture_frontend_evidence']
        self.assertEqual(proof['frontend'], 'mono')
        self.assertEqual(proof['fallback_reason'], 'paired_format_unavailable')
        self.assertEqual(run.capture_start_failure['device_microphone_active'], True)

    def test_rechecks_detect_handover_changes_and_reports_own_their_copy(self):
        run, device, health, _ = self.fixture(capture=True, expected_capture_frontend='paired')
        d, _ = self.capture(run, device, health)
        run.start_capture()
        self.assertEqual(run.report('passed')['capture_frontend_evidence']['frontend'], 'paired')
        snapshot = run.report('passed')
        snapshot['capture_frontend_evidence']['frontend'] = 'mono'
        self.assertEqual(run.capture_frontend_evidence['frontend'], 'paired')
        run.capture_status()
        d['capture_channels'] = 1
        with self.assertRaisesRegex(CheckFailure, 'capture_frontend_not_confirmed'):
            run.capture_status()

    def test_old_peer_fails_before_ui_for_explicit_format_but_keeps_legacy_compatibility(self):
        run, device, _, _ = self.fixture(capture=True, expected_capture_frontend='paired')
        result = run.run()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['checks'][0]['reason'], 'capture_frontend_evidence_unsupported')
        device.open.assert_not_called()
        device.home.assert_not_called()
        legacy, device, health, _ = self.fixture(capture=True)
        d, _ = device.status.return_value
        d.update(
            microphone_active=True,
            test_capture_limit_ms=legacy.capture_limit_ms,
            test_capture_remaining_ms=legacy.capture_limit_ms,
        )
        health.update(microphone_active=True, local_wake_ready=True)
        health['capture_frontend'] = 'mono'
        legacy.start_capture()
        self.assertEqual(legacy.capture_frontend_evidence['status'], 'unavailable')

    def test_format_cannot_change_during_the_same_bounded_test_without_an_expectation(self):
        run, device, health, _ = self.fixture(capture=True)
        d, _ = self.capture(run, device, health)
        run.start_capture()
        d.update(active('mono')[0])
        health.update(active('mono')[1])
        with self.assertRaisesRegex(CheckFailure, 'capture_frontend_changed_during_test'):
            run.capture_status()

    def test_bad_expectations_fail_without_hardware_or_ipc(self):
        device, request = Mock(), Mock()
        for capture, mode in ((False, 'paired'), (True, True), (True, []), (True, 'auto')):
            with self.subTest(capture=capture, mode=mode), self.assertRaises(ValueError):
                SmokeRun(device, request, capture=capture, expected_capture_frontend=mode)
        self.assertFalse(device.mock_calls)
        self.assertFalse(request.mock_calls)
        errors = io.StringIO()
        with (
            patch('device_smoke.Device') as hardware,
            patch('device_smoke.rpc') as ipc,
            redirect_stderr(errors),
        ):
            with self.assertRaises(SystemExit):
                main(['--runtime-root', '/never-open', '--expect-capture-frontend', 'paired'])
        self.assertIn('--expect-capture-frontend requires --capture', errors.getvalue())
        hardware.assert_not_called()
        ipc.assert_not_called()

    def test_minimal_path_plan_selects_expectation_without_opening_runtime(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / 'missing-runtime'
            for options in ([], ['--p0-device-suite']):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(root / 'cc8'),
                        'verify-device',
                        '--runtime-root',
                        str(missing),
                        '--capture',
                        '--expect-capture-frontend',
                        'paired',
                        *options,
                    ],
                    env={'PATH': '/usr/bin:/bin'},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = json.loads(result.stdout)
                self.assertFalse(plan['execute'])
                self.assertEqual(plan['expected_capture_frontend'], 'paired')
                self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()

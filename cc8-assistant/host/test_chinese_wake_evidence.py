"""Fixed-source ownership, real smoke flow and independent receipt verification."""

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from audit_device_suite import audit
from chinese_wake_evidence import COUNTERS, counter_delta, run_fixed_standby, snapshot
from device_smoke import CheckFailure, SmokeRun
from mac_voice import SpeechFixtureCleanupError
from test_audit_device_suite import saved_suite
import test_device_smoke as smoke_fixtures
from test_wake_input_probe import metadata
import device_smoke
from release_info import RELEASE


class ChineseWakeEvidenceTests(unittest.TestCase):
    def test_standby_plan_is_side_effect_free_and_missing_counters_stop_before_device(self):
        out = io.StringIO()
        with (
            patch('device_smoke.Device') as device,
            patch('chinese_wake_evidence.PreparedSpeech') as prepare,
            redirect_stdout(out),
        ):
            self.assertEqual(
                device_smoke.main(['--runtime-root', '/unused', '--capture', '--standby-suite']), 0
            )
        self.assertTrue(json.loads(out.getvalue())['shared_prepared_chinese_input'])
        device.assert_not_called()
        prepare.assert_not_called()
        health = dict(
            bridge_version=RELEASE.backend_version, protocol_version=RELEASE.protocol_version
        )
        with (
            patch('device_smoke.rpc', return_value=health),
            patch('device_smoke.Device') as device,
            patch('chinese_wake_evidence.PreparedSpeech') as prepare,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            device_smoke.main(
                [
                    '--runtime-root',
                    str(device_smoke.ROOT),
                    '--capture',
                    '--standby-suite',
                    '--execute',
                ]
            )
        self.assertEqual(error.exception.code, 2)
        device.assert_not_called()
        prepare.assert_not_called()

    def test_boundary_copy_unknown_and_reset_are_not_reported_as_zero(self):
        health = dict.fromkeys(COUNTERS, 4)
        before = snapshot(health)
        health[COUNTERS[0]] += 3
        self.assertEqual(before, dict.fromkeys(COUNTERS, 4))
        self.assertEqual(
            counter_delta(before, health),
            {'status': 'observed', **dict.fromkeys(COUNTERS, 0), COUNTERS[0]: 3},
        )
        self.assertEqual(counter_delta(before, {}), {'status': 'unavailable'})
        self.assertEqual(counter_delta(None, health), {'status': 'unavailable'})
        self.assertEqual(counter_delta(before, dict.fromkeys(COUNTERS, 0)), {'status': 'invalid'})
        for invalid in (True, -1, 2**63, 'private detail'):
            self.assertIsNone(snapshot({**health, COUNTERS[0]: invalid}))

    def test_actual_smoke_uses_owned_fixture_and_records_only_boundary_deltas(self):
        helper = smoke_fixtures.DeviceAutomationTests()
        run, device, health, _ = helper.acoustic_fixture(
            lambda _: [helper.acoustic_row()], awake=True
        )
        health.update(dict.fromkeys(COUNTERS, 7))
        spoken = device.speak_probe.side_effect

        def play():
            spoken(question=run.qa_question)
            health.update({key: health[key] + 1 for key in COUNTERS[:3]})

        fixture = Mock(metadata=metadata(), play=Mock(side_effect=play))
        run.chinese_fixture = fixture
        run.chinese_fixture_metadata = metadata()
        run.acoustic_probe(awake=True)
        fixture.play.assert_called_once_with()
        device.speak_probe.assert_not_called()
        report = run.report('passed')
        self.assertTrue(report['chinese_fixture_played'])
        self.assertEqual(report['prepared_chinese_input'], metadata())
        self.assertEqual(
            report['acoustic_evidence']['chinese_wake_counters'],
            {'status': 'observed', **dict.fromkeys(COUNTERS[:3], 1), COUNTERS[3]: 0},
        )

    def test_fixture_replacement_is_rejected_before_any_playback(self):
        helper = smoke_fixtures.DeviceAutomationTests()
        run, device, _, _ = helper.acoustic_fixture(lambda _: [], awake=True)
        run.chinese_fixture_metadata = metadata()
        run.chinese_fixture = Mock(metadata={**metadata(), 'pcm_sha256': 'a' * 64})
        with self.assertRaisesRegex(CheckFailure, 'chinese_stimulus_identity_changed'):
            run.acoustic_probe(awake=True)
        run.chinese_fixture.play.assert_not_called()
        device.speak_probe.assert_not_called()

    def test_failed_wake_keeps_actual_zero_deltas_instead_of_cumulative_counts(self):
        helper = smoke_fixtures.DeviceAutomationTests()
        run, _, health, _ = helper.acoustic_fixture(lambda _: [], awake=True)
        health.update(dict.fromkeys(COUNTERS, 11))
        with self.assertRaisesRegex(CheckFailure, 'synthetic_acoustic_chain_not_confirmed'):
            run.acoustic_probe(awake=True)
        self.assertEqual(
            run.acoustic_evidence['chinese_wake_counters'],
            {'status': 'observed', **dict.fromkeys(COUNTERS, 0)},
        )

    def test_wrapper_rejects_missing_playback_receipt_and_wrong_phase_source(self):
        for row in (
            {'chinese_fixture_played': False, 'prepared_chinese_input': metadata()},
            {
                'chinese_fixture_played': True,
                'prepared_chinese_input': {**metadata(), 'pcm_sha256': 'a' * 64},
            },
        ):
            fixture = Mock(metadata=metadata())
            suite = Mock(
                return_value={'state': 'passed', 'phases': [{'state': 'passed', 'report': row}] * 3}
            )
            with patch('chinese_wake_evidence.PreparedSpeech', return_value=fixture):
                report = run_fixed_standby(Mock(), Mock(), suite)
            self.assertEqual(report['state'], 'failed')
            self.assertEqual(report['reason'], 'chinese_stimulus_identity_unconfirmed')
            self.assertTrue(report['prepared_chinese_input_cleaned'])
            fixture.close.assert_called_once()

    def test_constructor_refuses_wrong_language_question_or_unbounded_capture(self):
        for options in (
            {'capture': False, 'acoustic': True},
            {'capture': True, 'acoustic': True, 'english_wake': True},
            {'capture': True, 'acoustic': True, 'wake_input_shape': True},
            {'capture': True, 'acoustic': True, 'qa_question': 'different'},
            {'capture': True},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                SmokeRun(Mock(), Mock(), chinese_fixture=Mock(metadata=metadata()), **options)

    def test_suite_shares_one_fixture_and_preserves_failed_third_without_retry(self):
        for terminal in ('passed', 'failed', 'interrupted'):
            with self.subTest(terminal=terminal):
                fixture = Mock(metadata=metadata())
                calls = []
                reports = []

                def make_run(options, update, *, chinese_fixture):
                    calls.append(chinese_fixture)
                    return SimpleNamespace(
                        run=lambda: {
                            'prepared_chinese_input': chinese_fixture.metadata,
                            'chinese_fixture_played': True,
                        }
                    )

                def suite(make, save, *, standby_only):
                    self.assertTrue(standby_only)
                    phases = []
                    for index in range(3):
                        result = make({'capture', 'standby', 'acoustic'}, save).run()
                        phases.append(
                            {'state': 'passed' if index < 2 else terminal, 'report': result}
                        )
                    value = dict(state=terminal, phases=phases)
                    save(value)
                    if terminal == 'interrupted':
                        raise KeyboardInterrupt
                    return value

                with patch('chinese_wake_evidence.PreparedSpeech', return_value=fixture) as prepare:
                    report = run_fixed_standby(
                        make_run, lambda v: reports.append(deepcopy(v)), suite
                    )
                prepare.assert_called_once_with('Tingting', '大黄蜂，一加一等于几？')
                self.assertEqual(report['state'], terminal)
                self.assertEqual(len(calls), 3)
                self.assertTrue(all(value is fixture for value in calls))
                fixture.close.assert_called_once_with()
                self.assertTrue(report['prepared_chinese_input_cleaned'])
                self.assertEqual(reports[-1], report)

    def test_constructor_and_cleanup_failures_preserve_fixture_owner(self):
        for failed_constructor in (False, True):
            with self.subTest(failed_constructor=failed_constructor):
                fixture = Mock(metadata=metadata(), close=Mock(side_effect=OSError('private')))
                kwargs = (
                    {'side_effect': SpeechFixtureCleanupError(fixture)}
                    if failed_constructor
                    else {'return_value': fixture}
                )
                suite = Mock(return_value={'state': 'passed', 'phases': []})
                with patch('chinese_wake_evidence.PreparedSpeech', **kwargs):
                    report = run_fixed_standby(Mock(), Mock(), suite)
                self.assertEqual(report['state'], 'cleanup_unconfirmed')
                self.assertFalse(report['prepared_chinese_input_cleaned'])
                self.assertNotIn('private', str(report))
                fixture.close.assert_called_once_with()
                if failed_constructor:
                    suite.assert_not_called()

    def modern_report(self):
        report = saved_suite(standby_only=True)
        report.update(
            backend_version='0.15.60',
            prepared_chinese_input=metadata(),
            prepared_chinese_input_cleaned=True,
        )
        for phase in report['phases']:
            row = phase['report']
            row.update(prepared_chinese_input=metadata(), chinese_fixture_played=True)
            row['acoustic_evidence']['chinese_wake_counters'] = {
                'status': 'observed',
                **dict.fromkeys(COUNTERS, 0),
            }
        return report

    def test_independent_audit_requires_same_source_cleanup_and_measured_counters(self):
        report = self.modern_report()
        checked = audit(report)
        self.assertTrue(checked['verified'])
        self.assertTrue(checked['same_chinese_stimulus_verified'])
        variants = []
        value = deepcopy(report)
        del value['prepared_chinese_input']
        variants.append(value)
        value = deepcopy(report)
        value['prepared_chinese_input_cleaned'] = False
        variants.append(value)
        value = deepcopy(report)
        value['phases'][1]['report']['chinese_fixture_played'] = False
        variants.append(value)
        value = deepcopy(report)
        value['phases'][1]['report']['prepared_chinese_input']['pcm_sha256'] = 'a' * 64
        variants.append(value)
        value = deepcopy(report)
        value['phases'][1]['report']['acoustic_evidence']['chinese_wake_counters'][COUNTERS[0]] = (
            True
        )
        variants.append(value)
        for invalid in variants:
            self.assertFalse(audit(invalid)['verified'])
        value = deepcopy(report)
        value['backend_version'] = '0.15.59'
        value['phases'][1]['report']['acoustic_evidence']['chinese_wake_counters'] = {
            'status': 'unavailable'
        }
        self.assertFalse(audit(value)['verified'])
        legacy = saved_suite(standby_only=True)
        checked = audit(legacy)
        self.assertTrue(checked['verified'])
        self.assertFalse(checked['same_chinese_stimulus_verified'])


if __name__ == '__main__':
    unittest.main()

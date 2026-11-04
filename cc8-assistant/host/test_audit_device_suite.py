"""Offline audits require recorded evidence, not the saved summary's verdict."""

import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from audit_device_suite import audit
from device_smoke import (
    ACOUSTIC_RESPONSE_METRIC,
    FAST_STOP_DEVICE_METRIC,
    QA_PLAYOUT_METRIC,
    QA_STAGE_METRICS,
    STOP_VISUAL_METRIC,
    planned_checks,
    run_device_suite,
)


def saved_suite(*, qa=False, extended=False, scenario='qa'):
    index = 0

    def make_run(options, _checkpoint, **_kwargs):
        nonlocal index
        index += 1
        response = 10 * index
        report = {
            'schema': 1,
            'state': 'passed',
            'automation_source_sha256': 'a' * 64,
            'speech_probe_source_sha256': 'b' * 64,
            'expected_apk_version': '0.9.34',
            'expected_apk_code': 71,
            'capture_tests_enabled': True,
            'cleanup_microphone_off_verified': True,
            'raw_audio_saved': False,
            'contains_conversation_content': False,
            'elapsed_seconds': 960 if 'standby' in options else 60,
            'checks': [{'name': n, 'state': 'passed'} for n in planned_checks(options)],
        }
        if 'standby' in options:
            report['standby_evidence'] = {
                'last_awake_seconds': 896,
                'black_screen_poll_started_seconds': 901,
                'first_black_screen_seconds': 902,
            }
        if 'synthetic_stop' in options:
            report['barge_evidence'] = {
                'device_stop_applied': True,
                'announcement_cancelled': True,
                'stimulus_played': True,
                'fast_stop_detections': 1,
                'stop_cloud_asr_attempts': 0,
                'admitted_voice_requests': 0,
                'post_playback_quiet_seconds': 20,
                FAST_STOP_DEVICE_METRIC: 140,
                STOP_VISUAL_METRIC: 33,
            }
        if {'awake_dialogue', 'acoustic'} & options:
            report['acoustic_evidence'] = {
                'recognized_stimulus_matches': True,
                'post_playback_quiet_seconds': 20,
                'response_playout_timing': {
                    'status': 'observed',
                    'utterance_end_ms': 80,
                    'confirmed_playout_ack_ms': 80 + response,
                    'confirmed_playback_done_ms': 100 + response,
                    QA_PLAYOUT_METRIC: response,
                },
                'latency_scenario': scenario,
                'qa_stage_latencies_ms': dict.fromkeys(QA_STAGE_METRICS, response),
                'response_acoustic_timing': {
                    'status': 'observed',
                    ACOUSTIC_RESPONSE_METRIC: response,
                },
            }
        return SimpleNamespace(
            source_sha256='a' * 64, speech_source_sha256='b' * 64, run=lambda: report
        )

    data = run_device_suite(
        make_run,
        lambda _: None,
        qa_latency=qa,
        response_acoustic=qa,
        extended_voice=extended,
        latency_scenario=scenario,
    )
    backend = '0.14.60' if scenario == 'qa' else '0.14.61'
    return {**data, 'backend_version': backend, 'protocol_version': 2}


class AuditDeviceSuiteTests(unittest.TestCase):
    def test_fixed_and_extended_batches_recheck_every_phase(self):
        for extended, count in ((False, 6), (True, 7)):
            with self.subTest(extended=extended):
                result = audit(
                    saved_suite(extended=extended), expect_backend='0.14.60', expect_apk_code=71
                )
                self.assertTrue(result['verified'])
                self.assertEqual(result['completed_phases'], count)
                self.assertFalse(result['full_p0_acceptance'])

    def test_qa_latency_is_recomputed_instead_of_reusing_saved_percentiles(self):
        data = saved_suite(qa=True)
        data['latencies_ms'] = {ACOUSTIC_RESPONSE_METRIC: {'samples': 1, 'p95': 0}}
        reviewed = audit(data)
        self.assertTrue(reviewed['verified'])
        self.assertEqual(
            reviewed['latencies_ms'][ACOUSTIC_RESPONSE_METRIC],
            {'samples': 30, 'p50': 155, 'p95': 285.5},
        )

    def test_routed_latency_rejects_a_mixed_corpus_or_receipt(self):
        data = saved_suite(qa=True, scenario='web')
        reviewed = audit(data)
        self.assertTrue(reviewed['verified'])
        self.assertEqual(reviewed['latency_scenario'], 'web')
        with self.assertRaisesRegex(ValueError, 'Invalid suite mode'):
            audit({**data, 'backend_version': '0.14.60'})
        data['latency_scenario'] = 'tool'
        with self.assertRaisesRegex(ValueError, 'Invalid suite mode'):
            audit(data)
        data['latency_scenario'] = 'web'
        evidence = data['phases'][1]['report']['acoustic_evidence']
        for route in ('tool', None):
            with self.subTest(route=route):
                evidence['latency_scenario'] = route
                reviewed = audit(data)
                self.assertFalse(reviewed['verified'])
                self.assertEqual(reviewed['reason'], 'suite_latency_scenario_unconfirmed')
                self.assertEqual(reviewed['completed_phases'], 1)

    def test_claims_cannot_cover_missing_evidence_or_mix_runs(self):
        for case, reason in (
            ('unfinished', 'report_not_completed'),
            ('phase_plan', 'phase_plan_mismatch'),
            ('short_standby', 'standby_duration_unconfirmed'),
            ('wrong_duration', 'dialogue_timing_unconfirmed'),
            ('mixed_source', 'suite_source_changed'),
            ('mixed_apk', 'apk_identity_mismatch'),
            ('missing_visuals', 'suite_synthetic_stop_evidence_incomplete'),
            ('missing_cleanup', 'suite_phase_evidence_incomplete'),
        ):
            with self.subTest(case=case):
                data = saved_suite(extended=True)
                phases = data['phases']
                if case == 'unfinished':
                    data['state'] = 'running'
                elif case == 'phase_plan':
                    phases[1]['expected_checks'].pop()
                elif case == 'short_standby':
                    phases[-1]['report']['standby_evidence'][
                        'black_screen_poll_started_seconds'
                    ] = 899
                elif case == 'wrong_duration':
                    phases[3]['report']['acoustic_evidence']['response_playout_timing'][
                        QA_PLAYOUT_METRIC
                    ] = 1
                elif case == 'mixed_source':
                    phases[-1]['report']['automation_source_sha256'] = 'c' * 64
                elif case == 'mixed_apk':
                    phases[-1]['report']['expected_apk_code'] = 70
                elif case == 'missing_visuals':
                    phases[2]['report']['barge_evidence'].pop(STOP_VISUAL_METRIC)
                else:
                    phases[1]['report']['cleanup_microphone_off_verified'] = False
                result = audit(data)
                self.assertFalse(result['verified'])
                self.assertEqual(result['reason'], reason)

    def test_real_cli_reads_private_report_and_leaves_it_unchanged(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / 'suite.json'
            data = saved_suite()
            data['phases'][0]['report']['unrecognized_value'] = 'PRIVATE_NOT_IN_OUTPUT'
            encoded = json.dumps(data)
            report.write_text(encoded)
            report.chmod(0o600)
            cmd = [str(root / 'cc8'), 'audit-device-suite', str(report), '--expect-apk-code', '71']
            result = subprocess.run(
                cmd, env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('PRIVATE_NOT_IN_OUTPUT', result.stdout)
            self.assertTrue(json.loads(result.stdout)['verified'])
            self.assertEqual(report.read_text(), encoded)
            report.chmod(0o644)
            bad = subprocess.run(
                cmd, env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(report.read_text(), encoded)

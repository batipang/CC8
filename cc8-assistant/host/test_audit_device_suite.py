"""Offline audits require recorded evidence, not the saved summary's verdict."""

import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from audit_device_suite import audit
from copy import deepcopy
from diagnostics import VAD_ONSET_PAUSE
from device_smoke import (
    ACOUSTIC_RESPONSE_METRIC,
    BARGE_DEVICE_METRIC,
    FAST_STOP_DEVICE_METRIC,
    QA_PLAYOUT_METRIC,
    QA_STAGE_METRICS,
    STOP_VISUAL_METRIC,
    planned_checks,
    run_device_suite,
)


class PreparedInputAuditTests(unittest.TestCase):
    def test_individual_known_speech_scope_cannot_be_relabelled_as_a_batch(self):
        for field, value in (
            ('known_speech_evidence_required', True),
            ('known_speech_evidence_required', 1),
            ('known_speech_evidence', {'text': 'private'}),
        ):
            with self.subTest(field=field):
                data = saved_suite(interruption=True)
                data['phases'][0]['report'][field] = value
                result = audit(data)
                self.assertFalse(result['verified'])
                self.assertEqual(result['reason'], 'unexpected_known_speech_scope')

    def test_new_input_metadata_requires_valid_shape_scope_and_cleanup(self):
        from test_prepared_speech import metadata

        original = saved_suite(interruption=True)
        self.assertIs(audit(original)['verified'], True)
        for change in ('valid', 'text', 'kind', 'physical_claim', 'no_cleanup', 'wrong_scope'):
            with self.subTest(change=change):
                data = deepcopy(original)
                report = data['phases'][0]['report']
                report['prepared_interruption_input'] = {
                    **metadata(),
                    'kind': 'barge',
                    'voice_family': 'Tingting',
                }
                report['prepared_interruption_input_cleaned'] = True
                value = report['prepared_interruption_input']
                if change == 'text':
                    value['text'] = 'private-content'
                elif change == 'kind':
                    value['kind'] = 'local_stop'
                elif change == 'physical_claim':
                    value['acoustic_playback_confirmed'] = True
                elif change == 'no_cleanup':
                    report['prepared_interruption_input_cleaned'] = False
                elif change == 'wrong_scope':
                    data = saved_suite(qa=True)
                    data['phases'][0]['report'].update(report)
                reviewed = audit(data)
                self.assertIs(reviewed['verified'], change == 'valid')
                if change != 'valid':
                    self.assertEqual(reviewed['reason'], 'prepared_input_evidence_invalid')


def saved_suite(
    *,
    qa=False,
    extended=False,
    scenario='qa',
    interruption=False,
    fail_at=None,
    echo_soak=False,
    background=None,
    acoustic_coverage=False,
    missing_acoustic=(),
):
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
        timing = {
            'status': 'observed',
            'utterance_end_ms': 80,
            'confirmed_playout_ack_ms': 80 + response,
            'confirmed_playback_done_ms': 100 + response,
            QA_PLAYOUT_METRIC: response,
        }
        if 'echo_soak' in options:
            pauses = int(index == 2 or fail_at == index)
            report['elapsed_seconds'] = 955
            report['echo_soak_evidence'] = {
                'elapsed_seconds': 902,
                'completed_announcements': 39,
                'render_samples': 12300000,
                'pause_requests': pauses,
                'device_pauses': pauses,
                'device_resumes': pauses,
                'admitted_voice_requests': 0,
            }
        if background is not None:
            report['echo_soak_evidence'].update(
                background=background, background_observed_seconds=902
            )
        if 'synthetic_barge' in options:
            report['barge_evidence'] = {
                'device_stop_applied': True,
                'announcement_cancelled': True,
                'stimulus_played': True,
                'replacement_completed': True,
                'transcript_matches_stimulus': True,
                'admitted_voice_requests': 1,
                'post_playback_quiet_seconds': 20,
                BARGE_DEVICE_METRIC: 110,
                'response_playout_timing': timing if index != fail_at else {'status': 'missing'},
            }
        if {'awake_dialogue', 'acoustic'} & options:
            report['acoustic_evidence'] = {
                'recognized_stimulus_matches': True,
                'admitted_voice_requests': 1,
                'pending_voice_candidates': 0,
                'unresumed_rejected_pauses': 0,
                'post_playback_quiet_seconds': 20,
                'response_playout_timing': timing,
                'latency_scenario': scenario,
                'latency_route_completed': scenario != 'qa',
                'qa_stage_latencies_ms': dict.fromkeys(QA_STAGE_METRICS, response),
                'response_acoustic_timing': {
                    'status': 'observed',
                    ACOUSTIC_RESPONSE_METRIC: response,
                },
            }
            if index in missing_acoustic:
                report['acoustic_evidence']['response_acoustic_timing'] = {
                    'status': 'unconfirmed',
                    'reason': 'response_prefix_alignment_mismatch',
                }
        return SimpleNamespace(
            source_sha256='a' * 64, speech_source_sha256='b' * 64, run=lambda: report
        )

    data = run_device_suite(
        make_run,
        lambda _: None,
        qa_latency=qa,
        response_acoustic=qa,
        response_acoustic_coverage=acoustic_coverage,
        extended_voice=extended,
        latency_scenario=scenario,
        interruption=interruption,
        echo_soak=echo_soak,
        background=background,
    )
    backend = '0.14.60' if scenario == 'qa' else '0.14.62'
    if echo_soak:
        backend = '0.14.63'
    return {**data, 'backend_version': backend, 'protocol_version': 2}


class AuditDeviceSuiteTests(unittest.TestCase):
    def test_acoustic_coverage_keeps_only_observed_samples_and_remains_unverified(self):
        data = saved_suite(qa=True, acoustic_coverage=True, missing_acoustic=(2, 15))
        self.assertEqual(data['state'], 'acoustic_incomplete')
        self.assertTrue(all(phase['state'] == 'passed' for phase in data['phases']))
        self.assertEqual(data['latencies_ms'][QA_PLAYOUT_METRIC]['samples'], 30)
        self.assertEqual(data['latencies_ms'][ACOUSTIC_RESPONSE_METRIC]['samples'], 28)
        data['response_acoustic_unconfirmed'] = {}
        result = audit(data)
        self.assertFalse(result['verified'])
        self.assertFalse(result['full_p0_acceptance'])
        self.assertEqual(result['completed_phases'], 30)
        self.assertEqual(result['reason'], 'suite_qa_acoustic_coverage_incomplete')
        self.assertEqual(result['latencies_ms'][ACOUSTIC_RESPONSE_METRIC]['samples'], 28)
        self.assertEqual(
            result['response_acoustic_unconfirmed'], {'response_prefix_alignment_mismatch': 2}
        )
        strict = saved_suite(qa=True, missing_acoustic=(2, 15))
        self.assertEqual(strict['state'], 'failed')
        self.assertEqual(strict['phases'][2]['state'], 'not_run')

    def test_acoustic_coverage_requires_valid_phase_and_same_generation_receipt(self):
        for field, value in (
            ('cleanup_microphone_off_verified', False),
            ('pending_voice_candidates', 1),
            (
                'response_acoustic_timing',
                {'status': 'unconfirmed', 'reason': 'generation_mismatch'},
            ),
            (
                'response_acoustic_timing',
                {
                    'status': 'unconfirmed',
                    'reason': 'response_prefix_alignment_mismatch',
                    ACOUSTIC_RESPONSE_METRIC: 1,
                },
            ),
        ):
            with self.subTest(field=field, value=value):
                data = saved_suite(qa=True, acoustic_coverage=True, missing_acoustic=(2,))
                row = data['phases'][1]['report']
                if field in row:
                    row[field] = value
                else:
                    row['acoustic_evidence'][field] = value
                result = audit(data)
                self.assertFalse(result['verified'])
                self.assertEqual(result['completed_phases'], 1)
                self.assertEqual(result['phase'], 'qa_02')
                self.assertEqual(result['response_acoustic_unconfirmed'], {})
        data = saved_suite(qa=True, acoustic_coverage=True)
        self.assertTrue(audit(data)['verified'])
        data['response_acoustic_evidence'] = False
        with self.assertRaisesRegex(ValueError, 'Invalid suite mode'):
            audit(data)

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

    def test_qa_audit_recomputes_optional_dot_stages_and_rejects_unknown_metrics(self):
        data = saved_suite(qa=True)
        name = 'dot_first_delta_ms'
        data['latencies_ms'][name] = {'samples': 30, 'p95': 0}
        self.assertEqual(
            audit(data)['latencies_ms'][name], {'samples': 0, 'p50': None, 'p95': None}
        )
        phases = data['phases']
        first = phases[0]['report']['acoustic_evidence']['qa_stage_latencies_ms']
        first[name] = 5
        verified = audit(data)
        self.assertTrue(verified['verified'])
        self.assertEqual(verified['latencies_ms'][name], {'samples': 1, 'p50': 5, 'p95': 5})
        second = phases[1]['report']['acoustic_evidence']['qa_stage_latencies_ms']
        for key, value in ((name, True), (name, float('nan')), ('provider_body', 'private text')):
            with self.subTest(key=key, value=value):
                second[key] = value
                reviewed = audit(data)
                self.assertFalse(reviewed['verified'])
                self.assertEqual(reviewed['completed_phases'], 1)
                self.assertEqual(reviewed['reason'], 'suite_qa_stage_timing_incomplete')
                self.assertEqual(reviewed['latencies_ms'][name]['samples'], 1)
                second.pop(key)

    def test_qa_audit_recounts_math_answer_evidence_for_completed_rounds(self):
        data = saved_suite(qa=True)
        data['math_answers'] = {'matched': 30}
        first, second, third, fourth = [
            phase['report']['acoustic_evidence'] for phase in data['phases'][:4]
        ]
        first['math_answer_status'] = 'matched'
        second['math_answer_status'] = 'mismatch'
        third['math_answer_status'] = 'unconfirmed'
        self.assertEqual(
            audit(data)['math_answers'],
            {'matched': 1, 'mismatch': 1, 'unconfirmed': 1, 'unreported': 27},
        )
        fourth['math_answer_status'] = 'private-body'
        reviewed = audit(data)
        self.assertFalse(reviewed['verified'])
        self.assertEqual(reviewed['completed_phases'], 3)
        self.assertEqual(reviewed['reason'], 'suite_math_answer_evidence_invalid')
        self.assertEqual(
            reviewed['math_answers'],
            {'matched': 1, 'mismatch': 1, 'unconfirmed': 1, 'unreported': 0},
        )
        self.assertNotIn('private-body', json.dumps(reviewed))
        self.assertNotIn('math_answers', audit(saved_suite(qa=True, scenario='web')))

    def test_routed_latency_rejects_a_mixed_corpus_or_receipt(self):
        data = saved_suite(qa=True, scenario='web')
        reviewed = audit(data)
        self.assertTrue(reviewed['verified'])
        self.assertEqual(reviewed['latency_scenario'], 'web')
        with self.assertRaisesRegex(ValueError, 'Invalid suite mode'):
            audit({**data, 'backend_version': '0.14.61'})
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
        evidence['latency_scenario'] = 'web'
        del evidence['latency_route_completed']
        reviewed = audit(data)
        self.assertFalse(reviewed['verified'])
        self.assertEqual(reviewed['reason'], 'suite_latency_route_incomplete')

    def test_partial_qa_replays_only_completed_prefix(self):
        for state in ('running', 'failed'):
            with self.subTest(state=state):
                data = saved_suite(qa=True)
                data['state'] = state
                data['phases'][2]['state'] = state
                data['latencies_ms'] = {QA_PLAYOUT_METRIC: {'samples': 30, 'p95': 0}}

                reviewed = audit(data)
                self.assertFalse(reviewed['verified'])
                self.assertFalse(reviewed['full_p0_acceptance'])
                self.assertEqual(reviewed['expected_phases'], 30)
                self.assertEqual(reviewed['completed_phases'], 2)
                self.assertEqual(reviewed['phase'], 'qa_03')
                self.assertEqual(reviewed['reason'], 'phase_not_completed')
                self.assertEqual(
                    reviewed['latencies_ms'][QA_PLAYOUT_METRIC],
                    {'samples': 2, 'p50': 15, 'p95': 19.5},
                )

    def test_partial_audit_stops_at_earlier_invalid_evidence(self):
        data = saved_suite(qa=True)
        data['state'] = 'running'
        data['phases'][3]['state'] = 'running'
        data['phases'][2]['report']['raw_audio_saved'] = True
        data['phases'][1]['report']['automation_source_sha256'] = 'c' * 64

        reviewed = audit(data)
        self.assertFalse(reviewed['verified'])
        self.assertEqual(reviewed['completed_phases'], 1)
        self.assertEqual(reviewed['phase'], 'qa_02')
        self.assertEqual(reviewed['reason'], 'suite_source_changed')
        self.assertEqual(reviewed['latencies_ms'][QA_PLAYOUT_METRIC]['samples'], 1)

        data['phases'][1]['report']['automation_source_sha256'] = 'a' * 64
        reviewed = audit(data)
        self.assertEqual(reviewed['completed_phases'], 2)
        self.assertEqual(reviewed['phase'], 'qa_03')
        self.assertEqual(reviewed['reason'], 'phase_provenance_incomplete')
        self.assertEqual(reviewed['latencies_ms'][QA_PLAYOUT_METRIC]['samples'], 2)

    def test_partial_audit_retains_only_the_known_precheck_failure(self):
        data = saved_suite(qa=True)
        data['state'] = 'failed'
        phase = data['phases'][2]
        phase.pop('report')
        phase.update(state='failed', reason='suite_source_changed')
        reviewed = audit(data)
        self.assertFalse(reviewed['verified'])
        self.assertEqual(reviewed['completed_phases'], 2)
        self.assertEqual(reviewed['phase'], 'qa_03')
        self.assertEqual(reviewed['reason'], 'phase_not_completed')
        self.assertEqual(reviewed['recorded_failure'], 'suite_source_changed')

        phase['reason'] = 'private-backend-message'
        self.assertNotIn('recorded_failure', audit(data))
        phase.update(state='running', reason='suite_source_changed')
        self.assertNotIn('recorded_failure', audit(data))

    def test_self_playback_soak_recomputes_coverage_and_uses_one_pause_budget(self):
        data = saved_suite(echo_soak=True)
        data['echo_soak_totals'] = {'pause_requests': 0}
        reviewed = audit(data)
        self.assertTrue(reviewed['verified'])
        self.assertEqual(reviewed['echo_soak_totals']['elapsed_seconds'], 1804)
        self.assertEqual(reviewed['echo_soak_totals']['pause_requests'], 1)
        bad = saved_suite(echo_soak=True, fail_at=1)
        self.assertEqual(bad['state'], 'failed')
        self.assertEqual(bad['phases'][1]['reason'], 'suite_echo_soak_false_pause_limit')
        data['phases'][0]['report']['echo_soak_evidence']['elapsed_seconds'] = 899
        self.assertEqual(audit(data)['reason'], 'suite_echo_soak_evidence_incomplete')

    def test_background_report_requires_matching_fixture_and_full_player_coverage(self):
        background = {'kind': 'tv', 'sha256': 'c' * 64}
        data = saved_suite(echo_soak=True, background=background)
        reviewed = audit(data)
        self.assertTrue(reviewed['verified'])
        self.assertEqual(reviewed['background'], background)
        evidence = data['phases'][0]['report']['echo_soak_evidence']
        evidence['background_observed_seconds'] = 1
        self.assertEqual(audit(data)['reason'], 'suite_echo_soak_evidence_incomplete')
        evidence['background_observed_seconds'] = 902
        evidence['background'] = {**background, 'kind': 'low-noise'}
        self.assertEqual(audit(data)['reason'], 'suite_echo_soak_evidence_incomplete')

    def test_interruption_audit_recomputes_separate_metrics_and_rejects_incomplete_pair(self):
        data = saved_suite(interruption=True)
        self.assertEqual(data['state'], 'passed')
        data['latencies_ms'] = {}
        reviewed = audit(data, expect_backend='0.14.60', expect_apk_code=71)
        self.assertTrue(reviewed['verified'])
        self.assertEqual(reviewed['completed_phases'], 30)
        self.assertEqual(
            reviewed['latencies_ms'],
            {
                BARGE_DEVICE_METRIC: {'samples': 15, 'p50': 110, 'p95': 110},
                FAST_STOP_DEVICE_METRIC: {'samples': 15, 'p50': 140, 'p95': 140},
                STOP_VISUAL_METRIC: {'samples': 15, 'p50': 33, 'p95': 33},
            },
        )
        first = data['phases'][0]['report']['barge_evidence']
        first[VAD_ONSET_PAUSE] = 260
        self.assertEqual(
            audit(data)['latencies_ms'][VAD_ONSET_PAUSE],
            {
                'samples': 1,
                'p50': 260,
                'p95': 260,
            },
        )
        first[VAD_ONSET_PAUSE] = 100
        self.assertEqual(audit(data)['reason'], 'suite_synthetic_barge_evidence_incomplete')
        del first[VAD_ONSET_PAUSE]
        timing = data['phases'][2]['report']['barge_evidence']['response_playout_timing']
        timing['confirmed_playback_done_ms'] = 0
        reviewed = audit(data)
        self.assertEqual(reviewed['completed_phases'], 2)
        self.assertEqual(reviewed['reason'], 'suite_synthetic_barge_evidence_incomplete')

    def test_interruption_batch_stops_without_retry_and_preserves_complete_samples(self):
        data = saved_suite(interruption=True, fail_at=3)
        self.assertEqual(data['state'], 'failed')
        self.assertEqual(
            [phase['state'] for phase in data['phases']],
            ['passed', 'passed', 'failed'] + ['not_run'] * 27,
        )
        self.assertEqual(
            {name: stats['samples'] for name, stats in data['latencies_ms'].items()},
            dict.fromkeys((BARGE_DEVICE_METRIC, FAST_STOP_DEVICE_METRIC, STOP_VISUAL_METRIC), 1),
        )

    def test_strict_render_overlap_is_rechecked_from_every_interruption_phase(self):
        data = saved_suite(interruption=True)
        self.assertTrue(audit(data)['verified'])
        data.update(require_render_overlap=True, backend_version='0.14.83')
        self.assertFalse(audit(data)['verified'])
        for phase in data['phases'][::2]:
            phase['report']['require_render_overlap'] = True
            phase['report']['barge_evidence']['credible_render_overlap_frames'] = 3
        reviewed = audit(data)
        self.assertTrue(reviewed['verified'])
        self.assertTrue(reviewed['require_render_overlap'])
        report = data['phases'][2]['report']
        for invalid in (None, 0, -1, True, 1.5, 2001, '3'):
            with self.subTest(value=invalid):
                report['barge_evidence']['credible_render_overlap_frames'] = invalid
                reviewed = audit(data)
                self.assertFalse(reviewed['verified'])
                self.assertEqual(reviewed['completed_phases'], 2)
        report['barge_evidence']['credible_render_overlap_frames'] = 3
        report['require_render_overlap'] = False
        self.assertFalse(audit(data)['verified'])
        data['require_render_overlap'] = 'true'
        with self.assertRaises(ValueError):
            audit(data)

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
            data['state'] = 'running'
            data['phases'][1]['state'] = 'running'
            encoded = json.dumps(data)
            report.write_text(encoded)
            partial = subprocess.run(
                cmd, env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(partial.returncode, 2)
            self.assertNotIn('PRIVATE_NOT_IN_OUTPUT', partial.stdout)
            self.assertEqual(json.loads(partial.stdout)['completed_phases'], 1)
            self.assertEqual(report.read_text(), encoded)
            report.chmod(0o644)
            bad = subprocess.run(
                cmd, env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(report.read_text(), encoded)

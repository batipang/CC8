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
    AWAKE_DIALOGUE_CHECK,
    BARGE_DEVICE_METRIC,
    FAST_STOP_DEVICE_METRIC,
    QA_PLAYOUT_METRIC,
    QA_STAGE_METRICS,
    SmokeRun,
    STOP_VISUAL_METRIC,
    planned_checks,
    run_device_suite,
)
from qa_speech import SCENARIO_QUESTIONS


class PreparedInputAuditTests(unittest.TestCase):
    def test_individual_known_speech_scope_cannot_be_relabelled_as_a_batch(self):
        for field, value in (
            ('known_speech_evidence_required', True),
            ('known_speech_evidence_required', 1),
            ('known_speech_room_model_enabled', True),
            ('known_speech_room_model_enabled', 'false'),
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
    legacy_echo_soak=False,
    background=None,
    acoustic_coverage=False,
    missing_acoustic=(),
    standby_only=False,
    missing_standby_quiet=None,
    fixture_bound=False,
    misplaced_fixture=None,
    tool_coverage=False,
    tool_misses=(),
    tool_miss_reason='read_only_missing',
):
    index = 0

    def make_run(options, _checkpoint, **kwargs):
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
        if fixture_bound:
            case = SCENARIO_QUESTIONS[scenario].index(kwargs['qa_question']) + 1
            report['qa_fixture'] = {
                'scenario': scenario,
                'case': case - 1 if case == misplaced_fixture else case,
            }
        if 'standby' in options:
            report['standby_evidence'] = {
                'last_awake_seconds': 896,
                'black_screen_poll_started_seconds': 901,
                'first_black_screen_seconds': 902,
            }
            if standby_only and index != missing_standby_quiet:
                report['standby_evidence']['quiet_counter_deltas'] = {
                    'local_wake_count': 0,
                    'voice_asr_attempts': 0,
                    'render_frames': 0,
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
            echo_elapsed = 902 if legacy_echo_soak else 602
            report['elapsed_seconds'] = echo_elapsed + 53
            report['echo_soak_evidence'] = {
                'elapsed_seconds': echo_elapsed,
                'completed_announcements': 39,
                'render_samples': 12300000,
                'pause_requests': pauses,
                'device_pauses': pauses,
                'device_resumes': pauses,
                'admitted_voice_requests': 0,
            }
        if background is not None:
            report['echo_soak_evidence'].update(
                background=background,
                background_observed_seconds=report['echo_soak_evidence']['elapsed_seconds'],
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
            if tool_coverage or tool_misses:
                report['acoustic_evidence'].update(
                    tool_sample_guarded=True, rejected_voice_gates={'tool_sample_guard': 0}
                )
            if index in tool_misses:
                report['state'] = 'failed'
                awake = planned_checks(options).index(AWAKE_DIALOGUE_CHECK)
                report['checks'] = report['checks'][: awake + 1]
                report['checks'][-1].update(
                    state='failed', reason='tool_sample_transcript_not_confirmed'
                )
                report['acoustic_evidence'] = {
                    'tool_sample_guarded': True,
                    'tool_sample_rejections': {tool_miss_reason: 1},
                    'rejected_voice_gates': {'tool_sample_guard': 1},
                    'sole_tool_rejection_input': {'asr_input_samples': 98720},
                    'new_voice_requests': 1,
                    'admitted_voice_requests': 0,
                    'rejected_voice_candidates': 1,
                    'pending_voice_candidates': 0,
                    'unclassified_terminal_candidates': 0,
                    'rejected_pause_count': 0,
                    'completed_requests': 0,
                    'cancelled_requests': 0,
                    'completed_tasks': 0,
                    'stage_counts': {
                        'wake_detected': 1,
                        'asr_start': 1,
                        'asr_end': 1,
                        'brain_start': 0,
                        'playout_ack': 0,
                        'playback_done': 0,
                    },
                }
        return SimpleNamespace(
            source_sha256='a' * 64, speech_source_sha256='b' * 64, run=lambda: report
        )

    data = run_device_suite(
        make_run,
        lambda _: None,
        qa_latency=qa,
        qa_fixture_bound=fixture_bound,
        tool_rejection_coverage=tool_coverage,
        response_acoustic=qa,
        response_acoustic_coverage=acoustic_coverage,
        extended_voice=extended,
        latency_scenario=scenario,
        interruption=interruption,
        echo_soak=echo_soak,
        background=background,
        legacy_echo_soak=legacy_echo_soak,
        standby_only=standby_only,
    )
    backend = '0.14.60' if scenario == 'qa' else '0.14.62'
    if tool_coverage or tool_misses:
        backend = '0.15.28'
    if echo_soak:
        backend = '0.14.63'
    return {**data, 'backend_version': backend, 'protocol_version': 2}


class AuditDeviceSuiteTests(unittest.TestCase):
    def test_tool_coverage_counts_empty_asr_without_latency_or_success(self):
        observed = saved_suite(
            qa=True,
            scenario='tool',
            fixture_bound=True,
            tool_coverage=True,
            tool_misses=(2,),
            tool_miss_reason='empty',
        )
        self.assertEqual(observed['phases'][1]['state'], 'guard_rejected')
        checked = audit(observed, expect_apk_code=71)
        self.assertFalse(checked['verified'])
        self.assertEqual(checked['reason'], 'suite_tool_transcript_coverage_incomplete')
        self.assertEqual(checked['tool_transcript_misses'], 1)
        self.assertEqual(checked['latencies_ms'][QA_PLAYOUT_METRIC]['samples'], 29)

    def test_tool_coverage_counts_guarded_misses_without_latency_or_success(self):
        options = dict(qa=True, scenario='tool', fixture_bound=True, tool_misses=(2, 9))
        strict = saved_suite(**options)
        self.assertEqual(strict['state'], 'failed')
        self.assertEqual(strict['phases'][2]['state'], 'not_run')
        self.assertEqual(audit(strict)['recorded_failure'], 'tool_sample_transcript_not_confirmed')

        observed = saved_suite(**options, tool_coverage=True)
        self.assertEqual(observed['state'], 'tool_transcript_incomplete')
        self.assertEqual(observed['tool_transcript_misses'], 2)
        self.assertEqual(observed['phases'][1]['state'], 'guard_rejected')
        self.assertEqual(observed['phases'][-1]['state'], 'passed')
        checked = audit(observed, expect_apk_code=71)
        self.assertFalse(checked['verified'])
        self.assertEqual(checked['reason'], 'suite_tool_transcript_coverage_incomplete')
        self.assertEqual(checked['completed_phases'], 28)
        self.assertEqual(checked['tool_transcript_misses'], 2)
        self.assertEqual(checked['latencies_ms'][QA_PLAYOUT_METRIC]['samples'], 28)

    def test_tool_coverage_stops_auditing_at_unconfirmed_guard_rejection(self):
        original = saved_suite(
            qa=True, scenario='tool', fixture_bound=True, tool_misses=(2, 9), tool_coverage=True
        )
        for variant in ('cleanup', 'admitted', 'dispatch', 'pending', 'gates', 'old_source', 'apk'):
            with self.subTest(variant=variant):
                data = deepcopy(original)
                report = data['phases'][8]['report']
                evidence = report['acoustic_evidence']
                if variant == 'cleanup':
                    report['cleanup_microphone_off_verified'] = False
                elif variant == 'admitted':
                    evidence['admitted_voice_requests'] = 1
                elif variant == 'dispatch':
                    evidence['stage_counts']['brain_start'] = 1
                elif variant == 'pending':
                    evidence['pending_voice_candidates'] = 1
                elif variant == 'gates':
                    evidence['rejected_voice_gates']['wake_closed'] = 1
                elif variant == 'old_source':
                    report['automation_source_sha256'] = 'c' * 64
                else:
                    report['expected_apk_code'] += 1
                result = audit(data, expect_apk_code=71)
                self.assertFalse(result['verified'])
                self.assertEqual(result['phase'], 'qa_09')
                self.assertEqual(result['completed_phases'], 7)
                self.assertEqual(result['tool_transcript_misses'], 1)

    def test_bound_qa_report_rejects_a_reused_or_misplaced_fixture(self):
        original = saved_suite(qa=True, fixture_bound=True)
        self.assertTrue(audit(original, require_qa_fixture_binding=True)['verified'])

        for variant in ('reused', 'wrong_scenario', 'missing', 'boolean'):
            with self.subTest(variant=variant):
                data = deepcopy(original)
                first, second = data['phases'][:2]
                if variant == 'reused':
                    second['report'] = deepcopy(first['report'])
                elif variant == 'wrong_scenario':
                    second['report']['qa_fixture']['scenario'] = 'web'
                elif variant == 'missing':
                    del second['report']['qa_fixture']
                else:
                    second['report']['qa_fixture']['case'] = True
                result = audit(data)
                self.assertFalse(result['verified'])
                self.assertEqual(result['completed_phases'], 1)
                self.assertEqual(result['phase'], 'qa_02')
                self.assertEqual(result['reason'], 'suite_qa_fixture_mismatch')

    def test_qa_fixture_binding_distinguishes_legacy_reports_and_stops_the_producer(self):
        old = saved_suite(qa=True)
        result = audit(old)
        self.assertTrue(result['verified'])
        self.assertFalse(result['qa_fixture_binding_required'])
        result = audit(old, require_qa_fixture_binding=True)
        self.assertFalse(result['verified'])
        self.assertEqual(result['reason'], 'suite_qa_fixture_binding_missing')

        data = saved_suite(qa=True, fixture_bound=True, misplaced_fixture=2)
        self.assertEqual(data['state'], 'failed')
        self.assertEqual(data['phases'][1]['reason'], 'suite_qa_fixture_mismatch')
        self.assertEqual(data['phases'][2]['state'], 'not_run')
        self.assertEqual(audit(data)['completed_phases'], 1)

        data = saved_suite(qa=True, fixture_bound=True)
        del data['qa_fixture_bound']
        self.assertEqual(audit(data)['reason'], 'suite_qa_fixture_mismatch')

    def test_real_report_uses_the_selected_qa_fixture_without_text(self):
        question = SCENARIO_QUESTIONS['tool'][6]
        smoke = SmokeRun(
            None, None, latency_sample=True, latency_scenario='tool', qa_question=question
        )
        report = smoke.report('failed')
        self.assertEqual(report['qa_fixture'], {'scenario': 'tool', 'case': 7})
        self.assertNotIn(question, json.dumps(report, ensure_ascii=False))

    def test_standby_batch_always_requires_quiet_and_stops_at_the_first_miss(self):
        complete = saved_suite(standby_only=True)
        self.assertEqual(complete['scope'], 'fixed_standby_suite')
        self.assertEqual(complete['state'], 'passed')
        reviewed = audit(complete)
        self.assertTrue(reviewed['verified'])
        self.assertEqual(reviewed['completed_phases'], 3)
        self.assertTrue(reviewed['require_standby_quiet'])
        self.assertFalse(reviewed['full_p0_acceptance'])

        incomplete = saved_suite(standby_only=True, missing_standby_quiet=2)
        self.assertEqual(incomplete['state'], 'failed')
        self.assertEqual(
            [p['state'] for p in incomplete['phases']], ['passed', 'failed', 'not_run']
        )
        self.assertEqual(incomplete['phases'][1]['reason'], 'standby_quiet_counters_unconfirmed')

        complete['phases'][1]['report']['standby_evidence'].pop('quiet_counter_deltas')
        reviewed = audit(complete)
        self.assertFalse(reviewed['verified'])
        self.assertEqual(reviewed['completed_phases'], 1)
        self.assertEqual(reviewed['reason'], 'standby_quiet_counters_unconfirmed')

    def test_standby_audit_rejects_nonquiet_or_malformed_optional_counts(self):
        zero = {'local_wake_count': 0, 'voice_asr_attempts': 0, 'render_frames': 0}
        for value, verified in (
            (zero, True),
            ({**zero, 'voice_asr_attempts': 1}, False),
            ({**zero, 'local_wake_count': False}, False),
            ({'render_frames': 0}, False),
            ({**zero, 'extra': 0}, False),
            (None, False),
        ):
            with self.subTest(value=value):
                data = saved_suite()
                data['phases'][-1]['report']['standby_evidence']['quiet_counter_deltas'] = value
                result = audit(data)
                self.assertIs(result['verified'], verified)
                if not verified:
                    self.assertEqual(result['reason'], 'standby_quiet_counters_unconfirmed')

    def test_strict_standby_audit_requires_counts_in_each_round(self):
        data = saved_suite()
        self.assertTrue(audit(data)['verified'])
        zero = {'local_wake_count': 0, 'voice_asr_attempts': 0, 'render_frames': 0}
        for phase in data['phases'][-3:]:
            reviewed = audit(data, require_standby_quiet=True)
            self.assertFalse(reviewed['verified'])
            self.assertEqual(reviewed['phase'], phase['name'])
            self.assertEqual(reviewed['reason'], 'standby_quiet_counters_unconfirmed')
            phase['report']['standby_evidence']['quiet_counter_deltas'] = zero
        self.assertTrue(audit(data, require_standby_quiet=True)['verified'])

    def test_strict_standby_audit_rejects_other_suite_scopes(self):
        with self.assertRaisesRegex(ValueError, 'Standby quiet evidence requires a standby suite'):
            audit(saved_suite(qa=True), require_standby_quiet=True)

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

    def test_tool_guard_evidence_is_required_for_current_batches_only(self):
        data = saved_suite(qa=True, scenario='tool')
        self.assertTrue(audit(data)['verified'])
        self.assertIsNot(audit(data).get('tool_sample_guard_required'), True)
        data['backend_version'] = '0.15.4'
        reviewed = audit(data)
        self.assertFalse(reviewed['verified'])
        self.assertEqual(reviewed['reason'], 'phase_tool_sample_guard_unconfirmed')
        for phase in data['phases']:
            phase['report']['acoustic_evidence'].update(
                tool_sample_guarded=True, rejected_voice_gates={'tool_sample_guard': 0}
            )
        reviewed = audit(data)
        self.assertTrue(reviewed['verified'])
        self.assertTrue(reviewed['tool_sample_guard_required'])
        evidence = data['phases'][1]['report']['acoustic_evidence']
        for field, value in (
            ('tool_sample_guarded', 1),
            ('admitted_voice_requests', True),
            ('admitted_voice_requests', 2),
            ('rejected_voice_gates', {}),
            ('rejected_voice_gates', {'tool_sample_guard': False}),
            ('rejected_voice_gates', {'tool_sample_guard': 1}),
        ):
            with self.subTest(field=field, value=value):
                original = evidence[field]
                evidence[field] = value
                reviewed = audit(data)
                self.assertFalse(reviewed['verified'])
                self.assertEqual(reviewed['reason'], 'phase_tool_sample_guard_unconfirmed')
                self.assertEqual(reviewed['completed_phases'], 1)
                evidence[field] = original

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

    def test_partial_tool_audit_exposes_only_allowlisted_dialogue_failures(self):
        for reason, variant, expected in (
            ('tool_sample_extra_voice_candidate', None, 'tool_sample_extra_voice_candidate'),
            ('tool_sample_transcript_not_confirmed', None, 'tool_sample_transcript_not_confirmed'),
            ('tool_sample_guard_not_confirmed', None, 'tool_sample_guard_not_confirmed'),
            ('PRIVATE_ERROR', None, None),
            ('tool_sample_guard_not_confirmed', 'wrong_check', None),
            ('tool_sample_guard_not_confirmed', 'wrong_prefix', None),
            ('tool_sample_guard_not_confirmed', 'mixed_source', None),
            ('tool_sample_guard_not_confirmed', 'mixed_apk_code', None),
            ('tool_sample_guard_not_confirmed', 'mixed_apk_version', None),
            ('tool_sample_guard_not_confirmed', 'untrusted_report', None),
            ('tool_sample_guard_not_confirmed', 'cleanup_unconfirmed', None),
            ('tool_sample_guard_not_confirmed', 'cleanup_flag_false', None),
        ):
            with self.subTest(reason=reason, variant=variant):
                data = saved_suite(qa=True, scenario='tool')
                data.update(state='failed', backend_version='0.15.15')
                data['phases'][0]['report']['acoustic_evidence'].update(
                    tool_sample_guarded=True, rejected_voice_gates={'tool_sample_guard': 0}
                )
                phase = data['phases'][1]
                phase['state'] = 'failed'
                report = phase['report']
                report['state'] = 'failed'
                checks = report['checks']
                index = phase['expected_checks'].index(AWAKE_DIALOGUE_CHECK)
                del checks[index + 1 :]
                checks[-1].update(state='failed', reason=reason)
                if variant == 'wrong_check':
                    checks[-1]['name'] = 'preflight'
                elif variant == 'wrong_prefix':
                    checks[0]['state'] = 'running'
                elif variant == 'mixed_source':
                    report['automation_source_sha256'] = 'c' * 64
                elif variant == 'mixed_apk_code':
                    report['expected_apk_code'] += 1
                elif variant == 'mixed_apk_version':
                    report['expected_apk_version'] = '0.9.42'
                elif variant == 'untrusted_report':
                    report['raw_audio_saved'] = True
                elif variant == 'cleanup_unconfirmed':
                    report['state'] = 'cleanup_unconfirmed'
                elif variant == 'cleanup_flag_false':
                    report['cleanup_microphone_off_verified'] = False
                reviewed = audit(data)
                self.assertFalse(reviewed['verified'])
                self.assertEqual(reviewed['completed_phases'], 1)
                self.assertEqual(reviewed['phase'], 'qa_02')
                self.assertEqual(reviewed['reason'], 'phase_not_completed')
                self.assertEqual(reviewed.get('recorded_failure'), expected)
                self.assertNotIn('PRIVATE_ERROR', json.dumps(reviewed))

    def test_first_tool_failure_requires_valid_apk_identity_and_requested_code(self):
        data = saved_suite(qa=True, scenario='tool')
        data.update(state='failed', backend_version='0.15.15')
        phase = data['phases'][0]
        phase['state'] = 'failed'
        report = phase['report']
        report['state'] = 'failed'
        checks = report['checks']
        del checks[phase['expected_checks'].index(AWAKE_DIALOGUE_CHECK) + 1 :]
        checks[-1].update(state='failed', reason='tool_sample_transcript_not_confirmed')

        self.assertEqual(
            audit(data, expect_apk_code=71).get('recorded_failure'),
            'tool_sample_transcript_not_confirmed',
        )
        for version, code, expected in (
            ('0.9.34', 71, 79),
            ('0.9.34', True, None),
            ('0.9.34', None, None),
            ('invalid', 71, None),
        ):
            with self.subTest(version=version, code=code, expected=expected):
                report.update(expected_apk_version=version, expected_apk_code=code)
                result = audit(data, expect_apk_code=expected)
                self.assertFalse(result['verified'])
                self.assertEqual(result['completed_phases'], 0)
                self.assertEqual(result['phase'], 'qa_01')
                self.assertNotIn('recorded_failure', result)

    def test_self_playback_soak_recomputes_coverage_and_uses_one_pause_budget(self):
        data = saved_suite(echo_soak=True)
        data['echo_soak_totals'] = {'pause_requests': 0}
        reviewed = audit(data)
        self.assertTrue(reviewed['verified'])
        self.assertEqual(reviewed['echo_soak_totals']['elapsed_seconds'], 1806)
        self.assertEqual(reviewed['completed_phases'], 3)
        self.assertEqual(reviewed['self_playback_seconds_per_phase'], 600)
        self.assertEqual(reviewed['echo_soak_totals']['pause_requests'], 1)
        bad = saved_suite(echo_soak=True, fail_at=1)
        self.assertEqual(bad['state'], 'failed')
        self.assertEqual(bad['phases'][1]['reason'], 'suite_echo_soak_false_pause_limit')
        bad_third = saved_suite(echo_soak=True, fail_at=3)
        self.assertEqual(bad_third['state'], 'failed')
        self.assertEqual(bad_third['phases'][2]['reason'], 'suite_echo_soak_false_pause_limit')
        data['phases'][0]['report']['echo_soak_evidence']['elapsed_seconds'] = 599
        self.assertEqual(audit(data)['reason'], 'suite_echo_soak_evidence_incomplete')

    def test_legacy_soak_uses_its_two_nine_hundred_second_windows(self):
        data = saved_suite(echo_soak=True, legacy_echo_soak=True)
        self.assertNotIn('self_playback_seconds_per_phase', data)
        reviewed = audit(data)
        self.assertTrue(reviewed['verified'])
        self.assertEqual(reviewed['completed_phases'], 2)
        self.assertEqual(reviewed['self_playback_seconds_per_phase'], 900)
        self.assertEqual(reviewed['echo_soak_totals']['elapsed_seconds'], 1804)
        data['phases'][0]['report']['echo_soak_evidence']['elapsed_seconds'] = 899
        self.assertEqual(audit(data)['reason'], 'suite_echo_soak_evidence_incomplete')

    def test_soak_rejects_shortened_plan_and_observation_larger_than_its_phase(self):
        data = saved_suite(echo_soak=True)
        with self.assertRaisesRegex(ValueError, 'suite length'):
            audit({**data, 'phases': data['phases'][:2]})
        for duration in (0, 600.0, None, 900):
            with self.subTest(duration=duration), self.assertRaisesRegex(ValueError, 'suite mode'):
                audit({**data, 'self_playback_seconds_per_phase': duration})
        data['phases'][1]['report']['elapsed_seconds'] = 601
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
        evidence['background_observed_seconds'] = evidence['elapsed_seconds']
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
            strict = subprocess.run(
                [*cmd, '--require-standby-quiet'],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(strict.returncode, 2)
            self.assertEqual(
                json.loads(strict.stdout)['reason'], 'standby_quiet_counters_unconfirmed'
            )
            self.assertNotIn('PRIVATE_NOT_IN_OUTPUT', strict.stdout)
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

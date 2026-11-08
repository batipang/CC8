"""Fixed single-case selection and content-free, per-candidate miss coverage."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import unittest
from device_smoke import main, safe_tool_transcript_miss
from test_audit_device_suite import saved_suite


def candidate(gate):
    guarded = gate == 'tool_sample_guard'
    return dict(
        origin='voice',
        gate=gate,
        outcome='rejected',
        generation=None,
        turn_id=None,
        task_outcome=None,
        error=None,
        asr_input_samples=64000 if guarded else None,
        tool_sample_rejection='read_only_missing' if guarded else None,
        stages=dict(utterance_end=10, wake_detected=15, asr_start=20, asr_end=25)
        if guarded
        else dict(voice_detected=1, utterance_end=2),
    )


def coverage_report():
    data = saved_suite(
        qa=True, scenario='tool', fixture_bound=True, tool_misses=(2,), tool_coverage=True
    )
    phase = data['phases'][1]
    r = phase['report']
    e = r['acoustic_evidence']
    e.pop('sole_tool_rejection_input')
    e['stage_counts'].update(pause_request=0, pause_ack=0, resume_ack=0)
    e.update(
        new_voice_requests=2,
        rejected_voice_candidates=2,
        rejected_voice_gates={'tool_sample_guard': 1, 'wake_closed': 1},
        tool_rejection_context=dict(
            version=1,
            guarded=candidate('tool_sample_guard'),
            closed_candidates=[candidate('wake_closed')],
        ),
    )
    return r, phase['expected_checks']


class CoverageTests(unittest.TestCase):
    def test_closed_pre_asr_candidate_does_not_hide_a_safely_guarded_recognition_miss(self):
        report, checks = coverage_report()
        self.assertTrue(safe_tool_transcript_miss(report, checks))

    def test_extra_actual_recognition_or_admission_stays_hard_failure(self):
        for change in ('asr', 'accepted'):
            report, checks = coverage_report()
            e = report['acoustic_evidence']
            if change == 'asr':
                e['stage_counts']['asr_start'] = 2
            else:
                e['admitted_voice_requests'] = 1
            self.assertFalse(safe_tool_transcript_miss(report, checks))


class SingleCaseTests(unittest.TestCase):
    def plan(self, *flags):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            try:
                code = main(['--runtime-root', '/nonexistent-cc8-qa-case', *flags])
            except SystemExit as error:
                code = error.code
        return code, output.getvalue()

    def test_public_web_case_has_one_bounded_check_sequence_and_declared_corpus(self):
        code, text = self.plan(
            '--capture',
            '--awake-dialogue',
            '--latency-scenario',
            'web',
            '--qa-case',
            '5',
            '--response-acoustic-evidence',
        )
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual(p['scope'], 'fixed_qa_case')
        self.assertEqual((p['latency_scenario'], p['qa_case']), ('web', 5))
        self.assertIn('synthetic_awake_dialogue', p['checks'])
        self.assertNotIn('phases', p)
        self.assertFalse(p['full_p0_acceptance'])
        self.assertFalse(p['execute'])

    def test_invalid_single_modes_fail_before_any_runtime_access(self):
        for flags in (
            ['--qa-case', '0'],
            ['--qa-case', '31'],
            ['--capture', '--qa-case', '1'],
            ['--capture', '--qa-latency-suite', '--qa-case', '1'],
            ['--capture', '--awake-dialogue', '--latency-scenario', 'tool', '--qa-case', '1'],
            ['--capture', '--awake-dialogue', '--qa-case', '1', '--tool-case', '1'],
            ['--capture', '--awake-dialogue', '--qa-case', '1', '--english-wake'],
            ['--capture', '--awake-dialogue', '--qa-case', '1', '--collect-acoustic-coverage'],
        ):
            with self.subTest(flags=flags):
                self.assertEqual(self.plan(*flags)[0], 2)


class ContextProofTests(unittest.TestCase):
    def test_factory_strips_content_and_copies_stage_maps(self):
        from tool_rejection_context import summarize_context

        guarded = candidate('tool_sample_guard')
        closed = candidate('wake_closed')
        guarded.update(request_id='PRIVATE_ID', transcript='PRIVATE_TEXT')
        closed.update(request_id='PRIVATE_OTHER', audio='PRIVATE_AUDIO')
        result = summarize_context([closed, guarded])
        self.assertIsNotNone(result)
        self.assertNotIn('PRIVATE', json.dumps(result))
        closed['stages']['asr_start'] = 3
        self.assertNotIn('asr_start', result['closed_candidates'][0]['stages'])
        self.assertIsNone(summarize_context([closed, guarded]))

    def test_per_record_recognition_effects_unknowns_and_invalid_input_stay_rejected(self):
        from tool_rejection_context import summarize_context

        for role, field, value in (
            ('closed', 'generation', 1),
            ('closed', 'turn_id', 'PRIVATE_TURN'),
            ('closed', 'task_outcome', 'completed'),
            ('closed', 'error', 'error'),
            ('closed', 'asr_input_samples', 64000),
            ('closed', 'gate', 'echo_text'),
            ('closed', 'outcome', 'cancelled'),
            ('closed', 'origin', 'text'),
            ('guarded', 'asr_input_samples', True),
            ('guarded', 'tool_sample_rejection', 'not_open'),
            ('guarded', 'stages', dict(asr_start=3, asr_end=2, utterance_end=1, wake_detected=1)),
            ('closed', 'stages', dict(voice_detected=True, utterance_end=2)),
            ('closed', 'stages', dict(voice_detected=1, utterance_end=float('nan'))),
        ):
            with self.subTest(role=role, field=field):
                g = candidate('tool_sample_guard')
                c = candidate('wake_closed')
                (g if role == 'guarded' else c)[field] = value
                self.assertIsNone(summarize_context([g, c]))
        self.assertIsNone(summarize_context([candidate('tool_sample_guard')] * 2))
        self.assertIsNone(
            summarize_context([candidate('tool_sample_guard')] + [candidate('wake_closed')] * 9)
        )

    def test_report_corruption_cannot_continue_or_invent_success(self):
        from device_smoke import safe_tool_transcript_miss

        for key, value in (
            ('new_voice_requests', 3),
            ('rejected_voice_candidates', 3),
            ('pending_voice_candidates', 1),
            ('unclassified_terminal_candidates', 1),
            ('rejected_pause_count', 1),
            ('completed_tasks', 1),
            ('cancelled_requests', 1),
        ):
            with self.subTest(key=key):
                r, c = coverage_report()
                r['acoustic_evidence'][key] = value
                self.assertFalse(safe_tool_transcript_miss(r, c))
        for changes in (dict(version=True), dict(unknown=0), dict(closed_candidates=[])):
            r, c = coverage_report()
            r['acoustic_evidence']['tool_rejection_context'].update(changes)
            self.assertFalse(safe_tool_transcript_miss(r, c))
        r, c = coverage_report()
        r['acoustic_evidence']['tool_rejection_context']['guarded']['tool_sample_rejection'] = (
            'arithmetic_mismatch'
        )
        self.assertFalse(safe_tool_transcript_miss(r, c))
        r, c = coverage_report()
        r['acoustic_evidence']['stage_counts']['unreported_stage'] = 1
        self.assertFalse(safe_tool_transcript_miss(r, c))
        r, c = coverage_report()
        r['acoustic_evidence']['stage_counts']['pause_ack'] = 1
        self.assertFalse(safe_tool_transcript_miss(r, c))

    def test_batch_recompute_preserves_failed_transcript_without_latency_sample(self):
        from audit_device_suite import audit

        data = saved_suite(
            qa=True, scenario='tool', fixture_bound=True, tool_misses=(2,), tool_coverage=True
        )
        report, checks = coverage_report()
        data['phases'][1]['report'] = report
        result = audit(data)
        self.assertFalse(result['verified'])
        self.assertEqual(result['reason'], 'suite_tool_transcript_coverage_incomplete')
        self.assertEqual(result['tool_transcript_misses'], 1)
        self.assertEqual(
            result['latencies_ms']['utterance_to_confirmed_playout_ack_ms']['samples'], 29
        )
        data['phases'][1]['report']['acoustic_evidence']['tool_rejection_context'][
            'closed_candidates'
        ][0]['stages']['asr_start'] = 3
        result = audit(data)
        self.assertFalse(result['verified'])
        self.assertEqual(result['completed_phases'], 1)

    def test_actual_collection_produces_the_same_strict_witness(self):
        from test_device_smoke import DeviceAutomationTests
        from device_smoke import CheckFailure, SCENARIO_QUESTIONS

        helper = DeviceAutomationTests('test_fixed_tool_execution_binds_case_runtime_and_lock')
        self.addCleanup(helper.doCleanups)
        g = candidate('tool_sample_guard')
        g['request_id'] = 'PRIVATE_GUARD'
        c = candidate('wake_closed')
        c['request_id'] = 'PRIVATE_CLOSED'
        run, device, health, now = helper.acoustic_fixture(lambda _: [c, g], awake=True)
        run.latency_sample = True
        run.latency_scenario = 'tool'
        run.qa_question = SCENARIO_QUESTIONS['tool'][4]
        run.tool_sample_token = 'a' * 32
        with self.assertRaisesRegex(CheckFailure, 'tool_sample_transcript_not_confirmed'):
            run.acoustic_probe(awake=True)
        evidence = run.acoustic_evidence
        self.assertIn('tool_rejection_context', evidence)
        self.assertNotIn('sole_tool_rejection_input', evidence)
        self.assertNotIn('PRIVATE', json.dumps(evidence))
        report, checks = coverage_report()
        report['acoustic_evidence'] = evidence
        self.assertTrue(safe_tool_transcript_miss(report, checks))


class SingleCaseExecutionTests(unittest.TestCase):
    def test_entrypoint_binds_selected_question_and_actual_runtime_while_holding_lock(self):
        from unittest.mock import patch, Mock
        from pathlib import Path
        import tempfile
        import fcntl
        from release_info import RELEASE
        from qa_speech import SCENARIO_QUESTIONS

        health = dict(
            bridge_version=RELEASE.backend_version,
            protocol_version=RELEASE.protocol_version,
            release_id=RELEASE.release_id,
            runtime_instance='PRIVATE_INSTANCE',
            response_acoustic_evidence_supported=True,
        )
        for scenario, index in (('qa', 3), ('web', 5)):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder).resolve()

                def factory(device, request, *, checkpoint, **options):
                    self.assertTrue(options['fixed_qa_case'])
                    self.assertTrue(options['latency_sample'])
                    self.assertFalse(options['fixed_tool_case'])
                    self.assertTrue(options['response_acoustic_evidence'])
                    self.assertEqual(
                        options['qa_question'], SCENARIO_QUESTIONS[scenario][index - 1]
                    )
                    with (directory / 'automation-test.lock').open('rb') as other:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertEqual(request({'operation': 'health'}), health)
                    result = dict(state='passed', checks=[], cleanup_microphone_off_verified=True)
                    checkpoint(result)
                    return Mock(run=Mock(return_value=result))

                with (
                    patch('device_smoke.rpc', return_value=health),
                    patch('device_smoke.load_release', return_value=RELEASE),
                    patch('device_smoke.private_directory', return_value=directory),
                    patch('device_smoke.Device'),
                    patch('device_smoke.SmokeRun', side_effect=factory) as smoke,
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        main(
                            [
                                '--runtime-root',
                                str(directory),
                                '--capture',
                                '--awake-dialogue',
                                '--latency-scenario',
                                scenario,
                                '--qa-case',
                                str(index),
                                '--response-acoustic-evidence',
                                '--execute',
                            ]
                        ),
                        0,
                    )
                smoke.assert_called_once()
                self.assertNotIn(
                    'PRIVATE_INSTANCE', next(directory.glob('device-*.json')).read_text()
                )

    def test_single_scope_and_fixture_cannot_be_relabelled_as_a_batch(self):
        from device_smoke import SmokeRun, SCENARIO_QUESTIONS, qa_fixture_matches

        run = SmokeRun(
            None,
            None,
            capture=True,
            awake_dialogue=True,
            latency_sample=True,
            fixed_qa_case=True,
            latency_scenario='web',
            qa_question=SCENARIO_QUESTIONS['web'][4],
        )
        report = run.report('failed')
        self.assertEqual(report['scope'], 'fixed_qa_case')
        self.assertEqual(report['qa_fixture'], dict(scenario='web', case=5))
        self.assertFalse(qa_fixture_matches(report, 'web', 5))
        self.assertFalse(report['full_p0_acceptance'])


if __name__ == '__main__':
    unittest.main()

"""The declared acceptance plan must match the real runner's branch ordering."""

from itertools import product
import unittest

from device_smoke import SmokeRun, planned_checks
from qa_speech import SCENARIO_QUESTIONS


class RecordedRun(SmokeRun):
    """Exercise actual run control flow without executing any device operation."""

    def check(self, name, action, **_):
        self.checks.append({'name': name, 'state': 'passed'})
        return {}

    def cleanup(self):
        self.cleanup_verified = True

    def report(self, state):
        return {'state': state, 'checks': self.checks}


class PlannedCheckTests(unittest.TestCase):
    def assert_plan(self, options, *, stop_route='local', tool_comparison=False):
        kwargs = {key: True for key in options}
        if tool_comparison:
            kwargs.update(
                tool_local_compare=True,
                latency_sample=True,
                latency_scenario='tool',
                qa_question=SCENARIO_QUESTIONS['tool'][0],
            )
        run = RecordedRun(None, None, stop_route=stop_route, **kwargs)
        result = run.run()
        self.assertEqual(result['state'], 'passed')
        self.assertTrue(run.cleanup_verified)
        self.assertEqual(
            planned_checks(options, stop_route=stop_route, tool_comparison=tool_comparison),
            [row['name'] for row in result['checks']],
        )

    def test_focused_standby_latency_plan_matches_real_seven_checks(self):
        self.assert_plan({'capture', 'standby', 'acoustic', 'latency_sample'})

    def test_capture_latency_and_observation_combinations(self):
        core = ('capture', 'latency_sample', 'standby', 'acoustic', 'awake_dialogue')
        for selected in product((False, True), repeat=len(core)):
            options = {name for name, enabled in zip(core, selected) if enabled}
            with self.subTest(options=sorted(options)):
                self.assert_plan(options)

    def test_latency_keeps_requested_evidence_and_interruption_order(self):
        extras = (
            'capture_clock_evidence',
            'playback_evidence',
            'clock_evidence',
            'leading_acoustic_evidence',
            'active_handover',
            'wake_self_playback',
            'echo_soak',
            'all_route_loss',
        )
        for latency, stop_route, interruption, extra in product(
            (False, True), ('local', 'asr'), (None, 'synthetic_barge', 'synthetic_stop'), extras
        ):
            options = {'capture', 'awake_dialogue', extra}
            if latency:
                options.add('latency_sample')
            if interruption:
                options.add(interruption)
            with self.subTest(options=sorted(options), stop_route=stop_route):
                self.assert_plan(options, stop_route=stop_route)

    def test_tool_comparison_preserves_requested_lifecycle_and_evidence(self):
        for extra in (None, 'standby', 'acoustic', 'capture_clock_evidence', 'all_route_loss'):
            options = {'capture', 'awake_dialogue', 'latency_sample'}
            if extra:
                options.add(extra)
            with self.subTest(extra=extra):
                self.assert_plan(options, tool_comparison=True)

    def test_cli_tool_comparison_implies_latency_without_mutating_options(self):
        options = {'capture', 'awake_dialogue'}
        self.assert_plan(options, tool_comparison=True)
        self.assertEqual(options, {'capture', 'awake_dialogue'})


if __name__ == '__main__':
    unittest.main()

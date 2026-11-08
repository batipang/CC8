"""Real worker/composition checks: a comparison reserves one authorized sample."""

import time
import unittest
from unittest.mock import patch

import numpy as np
import test_wake_pipeline as pipeline_fixture
from qa_speech import SCENARIO_QUESTIONS


class ComparisonUploadBoundaryTests(unittest.TestCase):
    def fixture(self, *, comparison=True):
        harness = pipeline_fixture.WakePipelineTests(methodName='runTest')
        question = SCENARIO_QUESTIONS['tool'][0]
        bridge, _, audio, requests, _, _, _ = harness.session('大黄蜂', '大黄蜂，' + question)
        self.addCleanup(bridge.session.stop)
        token = harness.arm_tool_capture(bridge, engine='sensevoice' if comparison else None)
        return harness, bridge, token, audio, requests

    def terminal(self, bridge, count=1):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows = bridge.diagnostics_report()['requests']
            if len(rows) == count and all(row['outcome'] is not None for row in rows):
                return rows
            time.sleep(0.005)
        self.fail('Recognition did not finish its bounded test input')

    def test_bound_comparison_rejects_before_cloud_or_local_recognition(self):
        harness, bridge, _, audio, requests = self.fixture()
        with patch('tool_asr_compare.compare_local') as local:
            harness.frames(bridge)
            row = self.terminal(bridge)[0]
        self.assertEqual(audio, [])
        local.assert_not_called()
        self.assertEqual(requests, [])
        self.assertEqual(row['tool_sample_rejection'], 'not_open')
        self.assertEqual(row['gate'], 'tool_sample_guard')
        self.assertNotIn('asr_start', row['stages'])

    def test_closed_comparison_does_not_upload_a_followup(self):
        harness, bridge, token, audio, requests = self.fixture()
        self.assertEqual(bridge.session.begin_tool_sample(token), {'status': 'ready'})
        with patch('tool_asr_compare.compare_local', return_value='matched') as local:
            harness.frames(bridge)
            first = self.terminal(bridge)[0]
            self.assertEqual(first['tool_sample_rejection'], 'comparison_only')
            self.assertEqual(len(audio), 1)
            trace = bridge.diagnostics.begin('voice')
            bridge.enqueue_recognition(
                (
                    bridge.session.generation,
                    np.full(16000, 0.02),
                    False,
                    [],
                    {'echo_risk': False, 'local_wake': False, 'captured_at': time.monotonic()},
                    None,
                    trace,
                )
            )
            second = self.terminal(bridge, 2)[1]
        self.assertEqual(len(audio), 1)
        local.assert_called_once()
        self.assertEqual(requests, [])
        self.assertEqual(second['tool_sample_rejection'], 'not_open')
        self.assertNotIn('asr_start', second['stages'])
        self.assertNotIn('brain_start', second['stages'])

    def test_invalid_comparison_capture_is_not_uploaded(self):
        for mismatch, reason in (
            ('capture', 'capture_changed'),
            ('window', 'capture_window'),
            ('route', 'route_changed'),
        ):
            with self.subTest(mismatch=mismatch):
                harness, bridge, token, audio, requests = self.fixture()
                self.assertEqual(bridge.session.begin_tool_sample(token), {'status': 'ready'})
                sample = bridge.session.tool_samples._sample
                if mismatch == 'capture':
                    sample.capture_id = '0' * 32
                elif mismatch == 'window':
                    sample.opened_at = time.monotonic() + 60
                else:
                    bridge.session.transport_kind = 'wifi'
                with patch('tool_asr_compare.compare_local') as local:
                    harness.frames(bridge)
                    row = self.terminal(bridge)[0]
                self.assertEqual(audio, [])
                self.assertEqual(requests, [])
                local.assert_not_called()
                self.assertEqual(row['tool_sample_rejection'], reason)
                self.assertNotIn('asr_start', row['stages'])

    def test_normal_tool_measurement_keeps_its_transcript_check(self):
        harness, bridge, _, audio, requests = self.fixture(comparison=False)
        harness.frames(bridge)
        row = self.terminal(bridge)[0]
        self.assertEqual(len(audio), 1)
        self.assertEqual(requests, [])
        self.assertEqual(row['tool_sample_rejection'], 'not_open')
        self.assertIn('asr_start', row['stages'])
        self.assertIn('asr_end', row['stages'])

"""Real admission and endpoint wiring, deterministic local models and provider ports."""

from contextlib import contextmanager
from dataclasses import replace
import unittest
from unittest.mock import Mock
import numpy as np

from dsp import FRAME, VoiceDetector
from english_wake_candidates import EnglishConfirmation, EnglishWaitingUtterance
import test_wake_utterance as ownership
import test_waiting_utterance_pipeline as pipeline


class EnglishWakeIntegrationTests(unittest.TestCase):
    def test_english_onset_lease_does_not_turn_chinese_receipt_into_english_subclass(self):
        controller, state, detector, _ = self.owner()
        detector.complete_chinese_enabled = True
        detector.complete_recovery_generation = 9
        detector.complete_recovery_commit.side_effect = lambda: controller.hooks.lock
        detector.recheck_chinese.return_value = True
        detector.feed.return_value = '大黄蜂'
        base = controller.waiting_utterance(150)
        self.assertIs(type(base), EnglishWaitingUtterance)
        controller.feed(np.zeros(FRAME), 7, capture_nanos=175)
        ticket = controller.complete_chinese_ticket(base, 200)
        self.assertEqual(ticket.keyword, '大黄蜂')
        self.assertEqual(ticket.model_generation, 9)
        self.assertTrue(controller.recheck_utterance(ticket, np.zeros(FRAME), 7))
        self.assertTrue(state['awake'])
        detector.recheck_english.assert_not_called()

    def owner(self):
        controller, state, detector, metrics = ownership.WakeUtteranceTests().controller()
        detector.complete_english_enabled = True
        detector.english_recovery_generation = 3
        detector.english_recovery_commit.side_effect = lambda: controller.hooks.lock
        detector.recheck_utterance.return_value = False
        detector.recheck_english.return_value = True
        detector.feed.return_value = ''
        detector.english_candidate_hit = True
        return controller, state, detector, metrics

    def receipt(self, controller):
        base = controller.waiting_utterance(150)
        controller.feed(np.zeros(FRAME), 7, capture_nanos=175)
        return controller.complete_english_ticket(base, 200)

    def test_candidate_never_wakes_and_only_completed_confirmed_audio_is_admitted(self):
        controller, state, detector, _ = self.owner()
        ticket = self.receipt(controller)
        self.assertIs(type(ticket), EnglishConfirmation)
        self.assertFalse(state['awake'])
        detector.recheck_english.assert_not_called()
        self.assertTrue(controller.recheck_utterance(ticket, np.zeros(FRAME), 7))
        self.assertTrue(state['awake'])
        detector.recheck_utterance.assert_called_once()
        detector.recheck_english.assert_called_once()
        self.assertFalse(controller.claim_prefix())
        self.assertIsNone(controller.recheck_utterance(ticket, np.zeros(FRAME), 7))

    def test_baseline_positive_survives_absent_restarted_or_forged_confirmation(self):
        for condition in ('absent', 'restarted', 'forged', 'not_ready'):
            controller, state, detector, _ = self.owner()
            ticket = self.receipt(controller)
            if condition == 'absent':
                controller.english_candidates.reset()
            elif condition == 'restarted':
                detector.english_recovery_generation += 1
            elif condition == 'forged':
                ticket = replace(ticket, confirmation_token='f' * 32)
            else:
                detector.complete_english_enabled = False
            detector.recheck_utterance.return_value = True
            with self.subTest(condition=condition):
                self.assertTrue(controller.recheck_utterance(ticket, np.zeros(FRAME), 7))
                detector.recheck_english.assert_not_called()
                self.assertTrue(state['awake'])

    def test_only_valid_current_one_use_receipt_can_try_extra_model(self):
        for condition in (
            'forged',
            'used',
            'model',
            'late_end',
            'risk',
            'stop',
            'generation',
            'gap',
        ):
            controller, state, detector, _ = self.owner()
            ticket = self.receipt(controller)
            if condition == 'forged':
                ticket = replace(ticket, confirmation_token='f' * 32)
            elif condition == 'used':
                self.assertTrue(controller.english_candidates.consume(ticket))
            elif condition == 'model':
                detector.english_recovery_generation += 1
            elif condition == 'late_end':
                ticket = replace(ticket, last_sample_ns=201)
            elif condition == 'risk':
                state['risk'] = True
            elif condition == 'stop':
                state['active'] = False
            elif condition == 'generation':
                state['generation'] += 1
            else:
                controller.reset('capture_gap')
            with self.subTest(condition=condition):
                self.assertIsNot(controller.recheck_utterance(ticket, np.zeros(FRAME), 7), True)
                detector.recheck_english.assert_not_called()
                self.assertFalse(state['awake'])

    def test_model_restart_mid_collection_or_native_feed_cannot_relabel_old_audio(self):
        for phase in ('before_candidate', 'during_candidate', 'after_candidate'):
            controller, _, detector, _ = self.owner()
            base = controller.waiting_utterance(150)
            self.assertIs(type(base), EnglishWaitingUtterance)
            if phase == 'before_candidate':
                detector.english_recovery_generation += 1
            elif phase == 'during_candidate':

                def native(*_args, **_kwargs):
                    detector.english_recovery_generation += 1
                    return ''

                detector.feed.side_effect = native
            controller.feed(np.zeros(FRAME), 7, capture_nanos=175)
            if phase == 'after_candidate':
                detector.english_recovery_generation += 1
            ticket = controller.complete_english_ticket(base, 200)
            with self.subTest(phase=phase):
                self.assertIs(ticket, base)
                self.assertFalse(controller.recheck_utterance(ticket, np.zeros(FRAME), 7))
                detector.recheck_english.assert_not_called()

    def test_model_result_cannot_commit_after_decode_or_commit_guard_changes_owner(self):
        for phase in ('decode', 'commit'):
            for change in ('model', 'stop', 'risk', 'generation', 'gap'):
                controller, state, detector, _ = self.owner()
                ticket = self.receipt(controller)

                def mutate():
                    if change == 'model':
                        detector.english_recovery_generation += 1
                    elif change == 'stop':
                        state['active'] = False
                    elif change == 'risk':
                        state['risk'] = True
                    elif change == 'generation':
                        state['generation'] += 1
                    else:
                        controller.reset('capture_gap')

                if phase == 'decode':

                    def decode(*_):
                        mutate()
                        return True

                    detector.recheck_english.side_effect = decode
                else:

                    @contextmanager
                    def guard():
                        mutate()
                        yield

                    detector.english_recovery_commit.side_effect = guard
                with self.subTest(phase=phase, change=change):
                    self.assertIsNone(controller.recheck_utterance(ticket, np.zeros(FRAME), 7))
                    self.assertFalse(state['awake'])

    def test_negative_unavailable_exception_or_nonboolean_never_admit_cloud(self):
        for answer in (False, None, 1, RuntimeError('test only')):
            controller, state, detector, _ = self.owner()
            ticket = self.receipt(controller)
            if isinstance(answer, Exception):
                detector.recheck_english.side_effect = answer
            else:
                detector.recheck_english.return_value = answer
            self.assertIsNot(controller.recheck_utterance(ticket, np.zeros(FRAME), 7), True)
            self.assertFalse(state['awake'])

    def test_real_pipeline_orders_baseline_confirmation_cloud_and_dot_on_same_audio(self):
        fixture = pipeline.WaitingUtterancePipelineTests()
        b, local, asr, answered, order, questions = fixture.bridge(hit=False)
        local.complete_english_enabled = True
        local.english_recovery_generation = 3
        local.english_candidate_hit = True
        local.recheck_english = lambda audio, current: order.append('confirm') or current()
        fixture.frames(b)
        self.assertTrue(answered.wait(1))
        self.assertTrue(asr.is_set())
        self.assertEqual(order, ['local', 'confirm', 'asr', 'brain'])
        self.assertEqual(questions, ['what is three plus five?'])
        self.assertEqual(fixture.terminal(b)[0]['gate'], 'accepted')
        self.assertEqual(b.metrics['local_wake_english_confirmations'], 1)

    def test_unmatched_or_rejected_candidate_keeps_real_pipeline_cloud_closed(self):
        fixture = pipeline.WaitingUtterancePipelineTests()
        for candidate in (True, False):
            b, local, asr, answered, order, _ = fixture.bridge(hit=False)
            local.complete_english_enabled = True
            local.english_recovery_generation = 3
            local.english_candidate_hit = candidate
            local.recheck_english = Mock(return_value=False)
            fixture.frames(b)
            self.assertEqual(fixture.terminal(b)[0]['gate'], 'wake_closed')
            self.assertFalse(asr.is_set() or answered.is_set() or b.session.awake)
            self.assertEqual(local.recheck_english.call_count, int(candidate))

    def test_capped_quiet_tail_candidate_is_outside_actual_retained_audio(self):
        fixture = pipeline.WaitingUtterancePipelineTests()
        for candidate_frame in (1200, 1240):
            b, local, asr, answered, _, _ = fixture.bridge(hit=False)
            b.capture.endpoint.vad = VoiceDetector()
            local.complete_english_enabled = True
            local.english_recovery_generation = 3
            local.recheck_english = Mock(return_value=True)
            count = 0

            def native(*_args, **_kwargs):
                nonlocal count
                count += 1
                local.english_candidate_hit = count == candidate_frame
                return ''

            local.feed = native
            b.capture.endpoint.near_speech.feed.return_value = (False, False)
            fixture.frames(b, last=50)
            b.capture.endpoint.near_speech.feed.return_value = (True, True)
            fixture.frames(b, first=51, last=1200)
            b.capture.endpoint.near_speech.feed.return_value = (False, False)
            fixture.frames(b, first=1201, last=1280)
            rows = fixture.terminal(b)
            with self.subTest(candidate_frame=candidate_frame):
                self.assertEqual(len(local.rechecked[0]), 1200 * FRAME)
                expected = candidate_frame == 1200
                self.assertEqual(local.recheck_english.call_count, int(expected))
                self.assertEqual(rows[0]['gate'], 'accepted' if expected else 'wake_closed')
                if expected:
                    self.assertTrue(answered.wait(1))
                else:
                    self.assertFalse(asr.is_set() or answered.is_set())


if __name__ == '__main__':
    unittest.main()

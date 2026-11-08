"""Pending local name ownership, late cancellation and exact same-utterance admission."""

from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock
import numpy as np

from dsp import FRAME
import test_wake_controller as fixtures


class CompleteChineseWakeTests(unittest.TestCase):
    def owner(self, result=True):
        fixture = fixtures.WakeControllerTests()
        fixture.setUp()
        d = fixture.detector
        d.complete_chinese_enabled = True
        d.utterance_recheck_supported = True
        d.recheck_utterance = Mock(return_value=False)
        d.recheck_chinese = Mock(return_value=result)
        return fixture

    def ticket(self, f, first=100, hit=150, last=200):
        base = f.controller.waiting_utterance(first)
        self.assertFalse(f.controller.feed(np.zeros(FRAME), 100, capture_nanos=hit))
        return f.controller.complete_chinese_ticket(base, last)

    def test_candidate_does_not_wake_or_touch_until_complete_local_confirmation(self):
        f = self.owner()
        ticket = self.ticket(f)
        self.assertFalse(f.state.awake)
        self.assertEqual(f.touches, 0)
        f.detector.recheck_chinese.assert_not_called()
        self.assertTrue(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
        self.assertTrue(f.state.awake)
        self.assertEqual(f.touches, 1)
        self.assertEqual(f.metrics['local_wake_count'], 1)
        self.assertEqual(f.metrics['local_wake_chinese_recheck_hits'], 1)
        self.assertNotIn('local_wake_english_count', f.metrics)
        self.assertFalse(f.controller.claim_prefix())
        self.assertIsNone(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
        f.detector.recheck_chinese.assert_called_once()

    def test_veto_or_unavailable_does_not_fall_back_to_english_or_live_wake(self):
        for result in (False, None):
            with self.subTest(result=result):
                f = self.owner(result)
                ticket = self.ticket(f)
                self.assertIs(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100), result)
                self.assertFalse(f.state.awake)
                self.assertEqual(f.touches, 0)
                f.detector.recheck_utterance.assert_not_called()

    def test_keyword_before_or_after_interval_cannot_confirm_it(self):
        for first, hit, last in ((100, 99, 200), (100, 201, 200)):
            f = self.owner()
            self.assertIsNone(self.ticket(f, first, hit, last))
            f.detector.recheck_chinese.assert_not_called()

    def test_queued_chinese_receipt_cannot_borrow_a_restarted_model(self):
        f = self.owner()
        f.detector.complete_recovery_generation = 1
        ticket = self.ticket(f)
        f.detector.complete_recovery_generation = 2
        self.assertIsNone(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
        f.detector.recheck_chinese.assert_not_called()
        self.assertFalse(f.state.awake)

    def test_model_restart_during_decode_cannot_authorize_old_audio(self):
        f = self.owner()
        f.detector.complete_recovery_generation = 1
        ticket = self.ticket(f)

        def decode(*_):
            f.detector.complete_recovery_generation = 2
            return True

        f.detector.recheck_chinese.side_effect = decode
        self.assertIsNone(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
        self.assertFalse(f.state.awake)

    def test_chinese_model_recovery_does_not_invalidate_english_review(self):
        f = self.owner()
        ticket = f.controller.waiting_utterance(100)
        f.detector.complete_recovery_generation = 2
        f.detector.recheck_utterance.return_value = True
        self.assertTrue(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
        f.detector.recheck_chinese.assert_not_called()

    def test_awake_poll_maintains_model_without_keyword_input_or_risk_resets(self):
        f = self.owner()
        f.state.awake = True
        f.detector.poll_capture = Mock()
        self.assertFalse(f.controller.feed(np.zeros(FRAME), 100, capture_nanos=150, echo_risk=True))
        f.detector.poll_capture.assert_called_once_with()
        self.assertEqual(f.detector.calls, 0)
        self.assertEqual(f.detector.resets, [])
        f.state.active = False
        f.controller.feed(np.zeros(FRAME), 100, capture_nanos=160)
        f.detector.poll_capture.assert_called_once_with()

    def test_model_recovery_status_waits_for_safe_idle_and_then_clears(self):
        from wake_controller import WAITING, RECOVERING

        f = self.owner()
        f.detector.word = ''
        f.detector.complete_recovery_generation = 2
        f.detector.complete_recovery_snapshot = {'state': 'retry_wait'}
        f.state.busy = True
        f.controller.feed(np.zeros(FRAME), 100, capture_nanos=150)
        self.assertEqual(f.statuses, [])
        f.state.busy = False
        f.controller.feed(np.zeros(FRAME), 100, capture_nanos=160)
        self.assertEqual(f.statuses[-1], ('error', RECOVERING))
        f.controller.feed(np.zeros(FRAME), 100, capture_nanos=170)
        self.assertEqual(len(f.statuses), 1)
        f.detector.complete_recovery_snapshot = {'state': 'ready'}
        f.state.busy = True
        f.controller.feed(np.zeros(FRAME), 100, capture_nanos=180)
        self.assertEqual(len(f.statuses), 1)
        f.state.busy = False
        f.controller.feed(np.zeros(FRAME), 100, capture_nanos=190)
        self.assertEqual(f.statuses[-1], ('listening', WAITING))
        self.assertEqual(f.touches, 0)

    def test_only_current_unavailable_worker_shows_local_recovery_hint(self):
        for exception in (False, True):
            for stale in (False, True):
                with self.subTest(exception=exception, stale=stale):
                    f = self.owner(None)
                    ticket = self.ticket(f)

                    def decode(*args):
                        if stale:
                            f.controller.reset('transport')
                        if exception:
                            raise RuntimeError('test worker failure')
                        return None

                    f.detector.recheck_chinese.side_effect = decode
                    self.assertIsNone(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
                    hints = [s for s in f.statuses if '本地中文唤醒复核暂不可用' in s[1]]
                    self.assertEqual(len(hints), 0 if stale else 1)
                    if not stale:
                        self.assertEqual(hints[0][0], 'error')
                    self.assertFalse(f.state.awake)

    def test_modified_ticket_cannot_consume_the_actual_owners_receipt(self):
        f = self.owner()
        ticket = self.ticket(f)
        self.assertIsNone(
            f.controller.recheck_utterance(
                replace(ticket, first_sample_ns=101), np.zeros(FRAME), 100
            )
        )
        self.assertTrue(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
        f.detector.recheck_chinese.assert_called_once()

    def test_each_capture_reset_invalidates_old_proof(self):
        for reason in ('stop', 'transport', 'capture_gap', 'mode', 'cancel'):
            with self.subTest(reason=reason):
                f = self.owner()
                ticket = self.ticket(f)
                f.controller.reset(reason)
                self.assertIsNone(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
                f.detector.recheck_chinese.assert_not_called()

    def test_decoding_during_reset_cannot_wake_new_state(self):
        for reason in ('cancel', 'transport', 'capture_gap', 'stop'):
            with self.subTest(reason=reason):
                f = self.owner()
                ticket = self.ticket(f)
                f.detector.recheck_chinese.side_effect = lambda *a: (
                    f.controller.reset(reason) or True
                )
                self.assertIsNone(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
                self.assertFalse(f.state.awake)

    def test_completed_playback_risk_invalidates_even_a_later_dequeued_job(self):
        f = self.owner()
        ticket = self.ticket(f)
        f.detector.word = ''
        f.controller.feed(np.zeros(FRAME), 100, capture_nanos=210, echo_risk=True)
        f.controller.feed(np.zeros(FRAME), 100, capture_nanos=220, echo_risk=False)
        self.assertIsNone(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
        f.detector.recheck_chinese.assert_not_called()

    def test_pending_frames_and_issued_jobs_are_bounded(self):
        f = self.owner()
        for stamp in range(32):
            f.controller.feed(np.zeros(FRAME), 100, capture_nanos=stamp)
        self.assertEqual(len(f.controller.pending_chinese), 16)
        tickets = []
        for n in range(12):
            tickets.append(self.ticket(f, 100 + n * 10, 105 + n * 10, 109 + n * 10))
        self.assertEqual(len(f.controller.complete_chinese_jobs), 8)
        self.assertIsNone(f.controller.recheck_utterance(tickets[0], np.zeros(FRAME), 100))
        self.assertFalse(f.state.awake)

    def test_native_decode_and_session_callbacks_do_not_deadlock(self):
        f = self.owner()
        ticket = self.ticket(f)
        entered = threading.Event()
        release = threading.Event()
        result = []

        def decode(pcm, current):
            entered.set()
            self.assertTrue(release.wait(2))
            return True if current() else None

        f.detector.recheck_chinese.side_effect = decode
        t = threading.Thread(
            target=lambda: result.append(
                f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100)
            )
        )
        t.start()
        try:
            self.assertTrue(entered.wait(1))
            f.controller.reset('transport')
        finally:
            release.set()
            t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(result, [None])
        self.assertFalse(f.state.awake)


if __name__ == '__main__':
    unittest.main()

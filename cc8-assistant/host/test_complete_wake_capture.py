"""Actual endpoint bounds, original audio preservation and erased rejected buffers."""

from dataclasses import replace
import unittest
from unittest.mock import Mock, patch
import numpy as np
from complete_wake_capture import CompleteWakeCapture
from dsp import FRAME, MAX_UTTERANCE_FRAMES
from wake_utterance import WaitingUtterance


class CompleteCaptureTests(unittest.TestCase):
    def fixture(self, accept=True):
        capture = CompleteWakeCapture()
        factory = lambda stamp: WaitingUtterance(1, 2, stamp)
        take = Mock(
            side_effect=lambda base, end: (
                replace(base, keyword='大黄蜂', last_sample_ns=end, candidate_token='a' * 32)
                if accept
                else None
            )
        )
        return capture, factory, take

    def test_completed_audio_owns_exact_contiguous_interval_and_input_end(self):
        c, factory, take = self.fixture()
        source = np.full(FRAME, 0.01)
        saved = None
        result = None
        for n in range(1, 141):
            row = c.feed(source, n <= 60, n * 10_000_000, factory, take)
            if n == 20:
                saved = c.vad.audio[0]
            if row is not None:
                result = row
        self.assertIsNotNone(result)
        self.assertEqual(result.ticket.first_sample_ns, 10_000_000)
        self.assertEqual(result.ticket.last_sample_ns, 1_400_000_000)
        self.assertEqual(result.input_end_ns, 610_000_000)
        np.testing.assert_array_equal(result.audio, np.full(140 * FRAME, 0.01))
        self.assertFalse(np.any(saved))
        self.assertTrue(np.all(source == 0.01))
        take.assert_called_once()

    def test_missing_original_owner_erases_completed_audio(self):
        c, factory, take = self.fixture(False)
        owned = []
        concatenate = np.concatenate

        def observe(*args, **kwargs):
            value = concatenate(*args, **kwargs)
            owned.append(value)
            return value

        with patch('complete_wake_capture.np.concatenate', side_effect=observe):
            for n in range(1, 141):
                self.assertIsNone(c.feed(np.ones(FRAME), n <= 60, n * 10_000_000, factory, take))
        self.assertTrue(owned)
        self.assertTrue(all(not np.any(a) for a in owned))

    def test_reset_or_gap_erases_partial_input_without_touching_caller(self):
        for gap in (False, True):
            with self.subTest(gap=gap):
                c, factory, take = self.fixture()
                source = np.ones(FRAME)
                for n in range(1, 31):
                    c.feed(source, True, n * 10_000_000, factory, take)
                saved = c.vad.audio[0]
                if gap:
                    c.feed(source, True, 1_000_000_000, factory, take)
                else:
                    c.reset()
                self.assertFalse(np.any(saved))
                self.assertTrue(np.all(source == 1))
                self.assertIsNone(c.ticket)
                take.assert_not_called()

    def test_overlong_never_dispatches_a_prefix_or_rearms_on_its_tail(self):
        c, factory, take = self.fixture()
        source = np.ones(FRAME)
        saved = None
        for n in range(1, MAX_UTTERANCE_FRAMES + 201):
            self.assertIsNone(c.feed(source, True, n * 10_000_000, factory, take))
            if n == 20:
                saved = c.vad.audio[0]
        self.assertTrue(c.vad.discarding)
        self.assertFalse(np.any(saved))
        take.assert_not_called()
        for n in range(MAX_UTTERANCE_FRAMES + 201, MAX_UTTERANCE_FRAMES + 281):
            self.assertIsNone(c.feed(np.zeros(FRAME), False, n * 10_000_000, factory, take))
        self.assertFalse(c.vad.discarding)
        take.assert_not_called()

    def test_sample_cap_cannot_attest_to_silence_that_was_not_retained(self):
        c, factory, take = self.fixture()
        for n in range(1, MAX_UTTERANCE_FRAMES + 80):
            voiced = n <= MAX_UTTERANCE_FRAMES - 10
            self.assertIsNone(c.feed(np.ones(FRAME), voiced, n * 10_000_000, factory, take))
        take.assert_not_called()

    def test_evicted_waiting_pre_roll_is_erased(self):
        c, factory, take = self.fixture()
        source = np.ones(FRAME)
        c.feed(source, False, 10_000_000, factory, take)
        oldest = c.vad.pre[0]
        for n in range(2, 52):
            c.feed(source, False, n * 10_000_000, factory, take)
        self.assertFalse(np.any(oldest))
        self.assertTrue(np.all(source == 1))


if __name__ == '__main__':
    unittest.main()

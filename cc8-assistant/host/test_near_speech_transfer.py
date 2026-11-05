"""Independent synthetic training, holdout and raw-match controls."""

import unittest

import numpy as np

from near_speech_transfer import analyse, HOLDOUT, CHECK
from playback_evidence import match_window


def fixture(delay=6300):
    rng = np.random.default_rng(192)
    source = rng.normal(0, 0.12, 45318)
    for start, stop in (
        (0, 640),
        (8400, 9200),
        (13900, 15400),
        (17000, 19500),
        (21000, 23200),
        (26600, 27000),
        (29300, 31200),
        (39900, 41000),
    ):
        source[start:stop] *= 0.015
    taps = np.zeros(85)
    taps[[0, 12, 28, 44, 60, 84]] = [0.24, -0.20, 0.21, -0.23, 0.22, 0.21]
    captured = np.r_[np.zeros(delay), np.convolve(source, taps), np.zeros(3200)]
    return source, captured


class NearTransferTests(unittest.TestCase):
    def test_repeatable_room_transfer_can_explain_a_weak_direct_waveform_match(self):
        for delay in (6300, 6301, 16600):
            with self.subTest(delay=delay):
                source, captured = fixture(delay)
                self.assertEqual(
                    match_window(captured[::2], source[640:3840:2])[-1], 'not_observed'
                )
                result = analyse(source, captured)
                self.assertEqual(result['status'], 'validated')
                self.assertEqual(result['held_out_blocks'], ['matched', 'matched'])
                self.assertLessEqual(abs(result['delay_frames'] - delay), 2)

    def test_holdout_cannot_select_alignment_or_learn_an_inverted_later_block(self):
        source, captured = fixture()
        expected = analyse(source, captured)
        second = 6300 + 2 * (HOLDOUT + CHECK)
        captured[second : second + 2 * CHECK] *= -1
        result = analyse(source, captured)
        for name in ('delay_frames', 'envelope_confidence', 'envelope_rival'):
            self.assertEqual(result[name], expected[name])
        self.assertEqual(result['status'], 'held_out_unconfirmed')
        self.assertEqual(result['held_out_blocks'], ['matched', 'shape_mismatch'])

    def test_short_quiet_or_unrelated_input_cannot_validate(self):
        source, captured = fixture()
        self.assertEqual(analyse(source, captured[:10000])['status'], 'coverage_incomplete')
        self.assertEqual(analyse(source * 0, captured)['status'], 'training_below_floor')
        self.assertEqual(analyse(source, captured * 0)['status'], 'alignment_unconfirmed')
        noise = np.random.default_rng(47).normal(0, 0.05, len(captured))
        self.assertEqual(analyse(source, noise)['status'], 'alignment_unconfirmed')

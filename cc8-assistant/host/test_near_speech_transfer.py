"""Independent synthetic training, holdout and raw-match controls."""

import unittest

import numpy as np

from near_speech_transfer import analyse, HOLDOUT, CHECK, LOCAL_RADIUS, local_alignment
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
                self.assertEqual(
                    [row['offset_frames'] for row in result['held_out_alignment']], [0, 0]
                )

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
        self.assertNotIn('offset_frames', result['held_out_alignment'][1])

    def test_simulated_clock_offset_finds_late_peaks_without_changing_holdout_failure(self):
        source, captured = fixture()
        scale = 1.00025
        warped = np.interp(
            np.arange(round(len(captured) / scale)) * scale, np.arange(len(captured)), captured
        )
        result = analyse(source, warped)
        self.assertEqual(result['status'], 'held_out_unconfirmed')
        self.assertEqual(result['held_out_blocks'], ['shape_mismatch', 'shape_mismatch'])
        for row in result['held_out_alignment']:
            self.assertLess(row['offset_frames'], 0)
            self.assertGreater(row['correlation'], 0.8)

    def test_local_peak_requires_full_coverage_an_interior_match_and_uniqueness(self):
        rng = np.random.default_rng(188)
        expected = rng.normal(0, 0.05, CHECK)
        self.assertIsNone(local_alignment(expected, expected, 0))
        capture = np.r_[np.zeros(2 * LOCAL_RADIUS), expected]
        edge = local_alignment(expected, capture, LOCAL_RADIUS)
        self.assertEqual(edge['correlation'], 1.0)
        self.assertNotIn('offset_frames', edge)
        periodic = np.sin(2 * np.pi * np.arange(CHECK + 2 * LOCAL_RADIUS) / 24) * 0.05
        result = local_alignment(
            periodic[LOCAL_RADIUS : LOCAL_RADIUS + CHECK], periodic, LOCAL_RADIUS
        )
        self.assertGreater(result['rival_correlation'], 0.99)
        self.assertNotIn('offset_frames', result)

    def test_partial_final_sample_pair_is_not_counted_as_full_search_coverage(self):
        source, captured = fixture()
        end = 6300 + 2 * (HOLDOUT + 2 * CHECK + LOCAL_RADIUS)
        result = analyse(source, captured[: end - 1])
        self.assertEqual(result['status'], 'validated')
        self.assertEqual(result['held_out_alignment'][0]['offset_frames'], 0)
        self.assertIsNone(result['held_out_alignment'][1])

    def test_short_quiet_or_unrelated_input_cannot_validate(self):
        source, captured = fixture()
        self.assertEqual(analyse(source, captured[:10000])['status'], 'coverage_incomplete')
        self.assertEqual(analyse(source * 0, captured)['status'], 'training_below_floor')
        self.assertEqual(analyse(source, captured * 0)['status'], 'alignment_unconfirmed')
        noise = np.random.default_rng(47).normal(0, 0.05, len(captured))
        self.assertEqual(analyse(source, noise)['status'], 'alignment_unconfirmed')

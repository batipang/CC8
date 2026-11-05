"""Causal train/check separation and unchanged raw near-source evidence gate."""

import unittest
from unittest.mock import Mock, patch

import numpy as np

import causal_speech_model as model
from known_speech_evidence import KnownSpeechWindow
from known_speech_receipt import (
    validate,
    validate_boundary,
    validate_far_preflight,
    validate_transfer,
)
from test_known_speech_evidence import fixture


CUTOFF = 14400
NEAR_START = 16400


def signals(*, near=True):
    rng = np.random.default_rng(927)
    band = np.hanning(17)
    band /= np.linalg.norm(band)
    far = np.convolve(rng.normal(0, 0.3, 48000), band, 'same')
    mic = np.r_[np.zeros(240), np.convolve(far, [0.8, 0.2, -0.1], 'same')[:-240]]
    pcm, _, _ = fixture()
    known = np.frombuffer(pcm, '<i2').astype(float) / 32768
    if near:
        mic[NEAR_START : NEAR_START + len(known)] += known
    return mic, far, known


class CausalSpeechModelTests(unittest.TestCase):
    def run_model(self, mic, far, known):
        result = model.analyse(mic, far, known[:3200], CUTOFF, CUTOFF)
        return validate_transfer(result, len(mic), len(far))

    def test_held_out_transfer_removes_far_without_promoting_a_weak_raw_match(self):
        mic, far, known = signals()
        result = self.run_model(mic, far, known)
        self.assertEqual(result['status'], 'residual_observed')
        self.assertGreater(result['held_out_confidence'], 0.99)
        self.assertLess(result['raw_at_residual_confidence'], 0.55)
        self.assertGreater(result['residual_confidence'], 0.98)
        self.assertEqual(result['residual_window_offset_frames'], NEAR_START)
        self.assertGreater(result['predicted_far_rms'], 0.05)

        pcm, metadata, _ = fixture()
        probe = KnownSpeechWindow(pcm, metadata, 12, 7, timer=Mock())
        self.addCleanup(probe.cancel)
        self.feed(probe, mic, far, 0, CUTOFF)
        boundary = validate_boundary(probe.mark_stimulus())
        self.feed(probe, mic, far, CUTOFF, len(mic))
        held = probe.mic, probe.render, probe.template
        result = validate(probe.read())
        self.assertEqual(result['status'], 'not_observed')
        self.assertEqual(result['far_transfer']['status'], 'residual_observed')
        self.assertEqual(
            result['far_transfer']['pre_stimulus_mic_frames'], boundary['pre_stimulus_mic_frames']
        )
        self.assertFalse(result['physical_double_talk_confirmed'])
        self.assertTrue(all(not np.any(a) for a in held))
        with self.assertRaises(ValueError):
            validate({**probe._receipt('expired'), 'far_transfer': result['far_transfer']})
        result['far_transfer']['status'] = 'forged'
        self.assertEqual(probe.read()['far_transfer']['status'], 'residual_observed')

    @staticmethod
    def feed(probe, mic, far, left, right):
        for offset in range(left, right, 160):
            stamp = 1_000_000_000 + offset * 62500
            probe.reference(12, offset, stamp, far[offset : offset + 160])
            probe.microphone(7, stamp, mic[offset : offset + 160])

    def test_far_preflight_freezes_samples_and_reports_signed_estimated_coverage(self):
        mic, far, _ = signals(near=False)
        pcm, metadata, _ = fixture()
        probe = KnownSpeechWindow(pcm, metadata, 12, 7, timer=Mock())
        self.addCleanup(probe.cancel)
        probe.reference(12, 0, 1_000_000_000, far[:CUTOFF])
        probe.microphone(7, 1_060_000_000, mic[960 : 960 + CUTOFF])
        checked = validate_far_preflight(probe.mark_stimulus(verify_far=True))
        self.assertEqual(checked['transfer']['status'], 'alignment_unconfirmed')
        self.assertEqual(checked['coverage']['estimated_mic_start_relative_to_render_frames'], 960)
        self.assertEqual(checked['coverage']['estimated_overlap_frames'], CUTOFF - 960)
        self.assertGreater(checked['coverage']['overlap_reference_rms'], 0.1)
        checked['coverage']['estimated_overlap_frames'] += 1
        with self.assertRaises(ValueError):
            validate_far_preflight(checked)

    def test_far_preflight_discards_cancelled_result_and_clears_snapshot(self):
        mic, far, _ = signals(near=False)
        pcm, metadata, _ = fixture()
        probe = KnownSpeechWindow(pcm, metadata, 12, 7, timer=Mock())
        self.addCleanup(probe.cancel)
        self.feed(probe, mic, far, 0, CUTOFF)
        analyse = model.analyse
        snapshots = []

        def cancel_while_fitting(captured, reference, *args):
            snapshots.extend((captured, reference))
            self.assertEqual(probe.read()['status'], 'analysing')
            self.feed(probe, mic, far, CUTOFF, CUTOFF + 160)
            self.assertEqual(probe.mic_count, CUTOFF + 160)
            probe.cancel('transport_changed')
            return analyse(captured, reference, *args)

        with patch.object(model, 'analyse', side_effect=cancel_while_fitting):
            self.assertEqual(probe.mark_stimulus(verify_far=True), {'status': 'unavailable'})
        self.assertEqual(probe.read()['status'], 'transport_changed')
        self.assertTrue(all(not np.any(samples) for samples in snapshots))

    def test_silence_near_only_noise_and_far_only_cannot_invent_a_mixed_near_match(self):
        mic, far, known = signals(near=False)
        for case in ('far_only', 'near_only', 'silence', 'noise'):
            with self.subTest(case=case):
                original = mic.copy()
                ref = far.copy()
                if case == 'near_only':
                    original.fill(0)
                    original[NEAR_START : NEAR_START + len(known)] = known
                    ref.fill(0)
                elif case == 'silence':
                    original.fill(0)
                elif case == 'noise':
                    original[:] = np.random.default_rng(9).normal(0, 0.1, len(mic))
                result = self.run_model(original, ref, known)
                self.assertNotEqual(result['status'], 'residual_observed')
                if case == 'far_only':
                    self.assertEqual(result['status'], 'residual_unconfirmed')

    def test_independent_held_out_change_rejects_a_good_training_fit(self):
        mic, far, known = signals()
        # Training remains exact. Only the pre-stimulus held-out transfer changes.
        # Model selection/fitting on the mixed suffix would conceal this failure.
        start = 240 + (model.HALF + model.TRAIN + model.GAP) * 2
        mic[start:CUTOFF] *= -1
        result = model.analyse(mic, far, known[:3200], CUTOFF, CUTOFF)
        self.assertEqual(result['status'], 'held_out_unconfirmed')
        validate_transfer(result, len(mic), len(far))
        self.assertNotIn('residual_confidence', result)

    def test_mixed_samples_do_not_select_alignment_fit_or_held_out_result(self):
        mic, far, known = signals()
        first = self.run_model(mic, far, known)
        changed = mic.copy()
        changed[CUTOFF:] = np.random.default_rng(3).normal(0, 0.2, len(mic) - CUTOFF)
        second = self.run_model(changed, far, known)
        for key in (
            'alignment_confidence',
            'alignment_rival',
            'held_out_confidence',
            'held_out_gain_valid',
        ):
            self.assertEqual(first[key], second[key])
        self.assertEqual(second['status'], 'residual_unconfirmed')

    def test_repeated_reference_and_known_near_are_not_unique_matches(self):
        mic, far, known = signals()
        repeated = np.tile(far[:2000], 24)
        echoed = np.r_[np.zeros(240), repeated[:-240]]
        self.assertEqual(self.run_model(echoed, repeated, known)['status'], 'alignment_unconfirmed')
        mic[32000 : 32000 + len(known)] += known
        result = self.run_model(mic, far, known)
        self.assertEqual(result['status'], 'residual_unconfirmed')
        self.assertGreater(result['residual_rival'], 0.8)

    def test_model_cannot_extrapolate_through_missing_original_reference(self):
        mic, far, known = signals()
        result = self.run_model(mic, far[:CUTOFF], known)
        self.assertEqual(result['status'], 'mixed_window_incomplete')
        self.assertNotIn('residual_confidence', result)

    def test_a_negligible_residual_cannot_be_confirmed_just_from_normalized_correlation(self):
        mic, far, known = signals(near=False)
        far[CUTOFF:] = 0
        mic[CUTOFF:] = 0
        mic[NEAR_START : NEAR_START + len(known)] = known * 0.0001
        result = self.run_model(mic, far, known)
        self.assertEqual(result['status'], 'residual_unconfirmed')
        self.assertLess(result['residual_rms'], 0.002)

    def test_perfect_post_stimulus_reference_cannot_replace_pre_stimulus_alignment(self):
        mic, far, known = signals()
        before = self.run_model(mic, far, known)
        # A later perfect echo would win a non-causal global NCC over the
        # genuine pre-stimulus transfer. It must never move the training origin.
        mic[CUTOFF : CUTOFF * 2] = far[:CUTOFF]
        after = self.run_model(mic, far, known)
        self.assertEqual(before['alignment_confidence'], after['alignment_confidence'])
        self.assertEqual(before['held_out_confidence'], after['held_out_confidence'])

    def test_barrier_is_one_shot_and_rejects_cancelled_or_analysing_owners(self):
        pcm, metadata, _ = fixture()
        probe = KnownSpeechWindow(pcm, metadata, 12, 7, timer=Mock())
        self.addCleanup(probe.cancel)
        self.assertEqual(probe.mark_stimulus(), {'status': 'insufficient_audio'})
        mic, far, _ = signals()
        self.feed(probe, mic, far, 0, CUTOFF)
        self.assertEqual(
            validate_boundary(probe.mark_stimulus())['pre_stimulus_mic_frames'], CUTOFF
        )
        self.feed(probe, mic, far, CUTOFF, CUTOFF + 160)
        self.assertEqual(probe.mark_stimulus(), {'status': 'unavailable'})
        self.assertEqual(probe.stimulus_boundary, (CUTOFF, CUTOFF))
        probe.cancel('expired')
        self.assertEqual(probe.mark_stimulus(), {'status': 'unavailable'})
        self.assertEqual(probe.read()['status'], 'expired')

    def test_receipts_reject_forged_boundary_scores_content_and_physical_claims(self):
        result = self.run_model(*signals())
        for key, value in (
            ('pre_stimulus_mic_frames', 48001),
            ('training_frames', True),
            ('held_out_confidence', 0.7),
            ('alignment_confidence', 0.1),
            ('residual_window_offset_frames', CUTOFF - 2),
            ('residual_rival', float('nan')),
            ('raw_at_residual_confidence', float('inf')),
            ('physical_double_talk_confirmed', True),
            ('pcm', 'private'),
            ('held_out_gain_valid', False),
            ('held_out_polarity_valid', False),
            ('residual_rms', 0.001),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_transfer({**result, key: value}, 48000, 48000)
        for bad in (
            {'status': 'recorded'},
            {
                'status': 'recorded',
                'pre_stimulus_mic_frames': True,
                'pre_stimulus_render_frames': CUTOFF,
            },
        ):
            with self.assertRaises(ValueError):
                validate_boundary(bad)

    def test_model_failure_and_late_cancellation_keep_owner_cleanup(self):
        pcm, metadata, _ = fixture()
        for cancel in (False, True):
            probe = KnownSpeechWindow(pcm, metadata, 12, 7, timer=Mock())
            self.addCleanup(probe.cancel)
            mic, far, _ = signals()
            self.feed(probe, mic, far, 0, CUTOFF)
            probe.mark_stimulus()
            self.feed(probe, mic, far, CUTOFF, len(mic))
            held = probe.mic, probe.render, probe.template
            original_analyse = model.analyse

            def analyse(*args):
                if cancel:
                    probe.cancel('transport_changed')
                    return original_analyse(*args)
                raise RuntimeError('synthetic failure')

            with patch('causal_speech_model.analyse', side_effect=analyse):
                if cancel:
                    self.assertEqual(probe.read()['status'], 'transport_changed')
                else:
                    with self.assertRaises(RuntimeError):
                        probe.read()
            self.assertTrue(all(not np.any(a) for a in held))


if __name__ == '__main__':
    unittest.main()

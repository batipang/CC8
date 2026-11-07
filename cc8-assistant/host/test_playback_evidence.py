"""Public synthetic PCM through the actual observer and capture packet router."""

import json
import queue
import socket
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from announce import AnnouncementServer
from bridge import Bridge
from echo_pipeline import CleanFrame
from playback_evidence import PlaybackEvidence, MIC_LIMIT, PROBE_TEXT
from protocol import Packet, MIC, RENDER, EVENT, SPEAKER, receive
from test_runtime import Fragmented
from test_barge_in import Sink

RATE = 16000
START = 2_000_000_000


def public_signal():
    # Non-repeating deterministic broadband signal, not a user recording.
    rng = np.random.default_rng(608)
    noise = rng.normal(0, 0.12, 16000)
    return np.convolve(noise, [0.2, 0.6, 0.2], mode='same')


def scene(reference, delay=1920, gain=0.3, *, noise=0.001):
    rng = np.random.default_rng(702)
    mic = rng.normal(0, noise, 32000) + 0.04
    start = 3200 + delay
    mic[start : start + len(reference)] += gain * reference
    return mic


def feed(observer, reference, microphone, *, render_first=False):
    def render():
        for frame in range(0, len(reference), 320):
            observer.render(
                101, frame, START + round(frame * 1e9 / RATE), reference[frame : frame + 320]
            )

    if render_first:
        render()
    for frame in range(0, len(microphone), 320):
        stamp = START - 200_000_000 + round(frame * 1e9 / RATE)
        observer.microphone(7, stamp, microphone[frame : frame + 320])
        if not render_first and 3200 <= frame < 3200 + len(reference):
            offset = frame - 3200
            observer.render(
                101, offset, START + round(offset * 1e9 / RATE), reference[offset : offset + 320]
            )


class AcousticEvidenceTests(unittest.TestCase):
    def test_gain_dc_room_filter_and_packet_delivery_order(self):
        reference = public_signal()
        room = np.convolve(reference, [0.8, 0.1, 0.04], mode='full')[: len(reference)]
        for first in (True, False):
            for delay in (0, 1920, 9600):
                with self.subTest(render_first=first, delay=delay):
                    observer = PlaybackEvidence()
                    probe = observer.arm(101, 7)
                    feed(observer, reference, scene(room, delay, -0.4), render_first=first)
                    self.assertTrue(probe.done.is_set())
                    result = probe.result
                    self.assertEqual(result['status'], 'observed')
                    self.assertAlmostEqual(result['matched_window_delay_ms'], delay / 16, delta=1)
                    self.assertEqual(result['leading_acoustic_evidence']['status'], 'observed')
                    self.assertAlmostEqual(
                        result['leading_acoustic_evidence']['capture_minus_render_origin_ms'],
                        delay / 16,
                        delta=1,
                    )
                    self.assertFalse(result['first_audible_onset_measured'])
                    self.assertFalse(result['human_dialogue_latency_measured'])
                    self.assertEqual(observer.mic.size + observer.reference.size, 0)
                    self.assertNotIn('pcm', json.dumps(result))

    def test_first_eligible_reference_blocks_are_independently_corroborated(self):
        reference = public_signal()
        reference[:1280] = 0  # 80 ms of reference pre-roll.
        for render_first in (False, True):
            with self.subTest(render_first=render_first):
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7)
                feed(observer, reference, scene(reference), render_first=render_first)
                leading = probe.result['leading_acoustic_evidence']
                self.assertEqual(leading['status'], 'observed')
                self.assertEqual(leading['reference_block_start_ms'], 80)
                self.assertAlmostEqual(leading['capture_minus_render_origin_ms'], 200, delta=1)
                self.assertEqual(leading['block_ms'], 20)
                self.assertEqual(leading['consecutive_blocks'], 2)
                self.assertGreaterEqual(leading['minimum_correlation'], 0.55)
                self.assertFalse(probe.result['first_audible_onset_measured'])

    def test_later_match_cannot_hide_missing_leading_audio(self):
        reference = public_signal()
        delay = 1920
        start = 3200 + delay
        for offset in (0, 320):
            with self.subTest(missing_block=offset):
                microphone = scene(reference, delay)
                microphone[start + offset : start + offset + 320] = 0
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7)
                feed(observer, reference, microphone)
                self.assertEqual(probe.result['status'], 'observed')
                self.assertEqual(
                    probe.result['leading_acoustic_evidence'],
                    {'status': 'leading_match_unconfirmed'},
                )

    def test_isolated_earlier_reference_burst_is_not_ignored_for_later_success(self):
        reference = public_signal()
        reference[320:1280] = 0
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        feed(observer, reference, scene(reference))
        self.assertEqual(probe.result['status'], 'observed')
        self.assertEqual(
            probe.result['leading_acoustic_evidence'], {'status': 'reference_not_sustained'}
        )

    def test_silence_noise_unrelated_and_periodic_are_not_success(self):
        ref = public_signal()
        tone = 0.15 * np.sin(np.arange(16000) * 2 * np.pi * 400 / RATE)
        cases = (
            (ref, np.zeros(32000), 'not_observed'),
            (ref, scene(np.random.default_rng(4).normal(0, 0.1, 16000)), 'not_observed'),
            (tone, scene(tone), 'ambiguous'),
            (np.zeros(24000), np.zeros(32000), 'insufficient_reference'),
        )
        for reference, microphone, expected in cases:
            with self.subTest(expected=expected):
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7)
                feed(observer, reference, microphone, render_first=True)
                self.assertEqual(probe.result['status'], expected)
                self.assertIsNone(probe.result.get('matched_window_delay_ms'))

    def test_stale_packets_do_not_complete_or_destroy_current_probe(self):
        observer = PlaybackEvidence()
        old = observer.arm(100, 6)
        observer.cancel()
        new = observer.arm(101, 7)
        observer.render(100, 0, START, np.ones(16000))
        observer.microphone(6, START, np.ones(16000))
        observer.cancel('expired', old)
        self.assertFalse(new.done.is_set())
        self.assertEqual(len(observer.mic) + len(observer.reference), 0)
        feed(observer, public_signal(), scene(public_signal()))
        self.assertEqual(new.result['status'], 'observed')
        observer.cancel()
        self.assertEqual(new.result['status'], 'observed')

    def test_deadline_frees_buffers_without_any_more_packets(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7, seconds=0.03)
        samples = np.ones(320)
        observer.microphone(7, START, samples)
        self.assertTrue(probe.done.wait(1))
        self.assertEqual(probe.result['status'], 'expired')
        self.assertEqual(observer.mic.size + observer.reference.size, 0)
        self.assertTrue(np.all(samples == 1))
        for seconds in (0, -1, True, float('nan'), float('inf'), 21):
            with self.assertRaises(ValueError):
                observer.arm(101, 7, seconds=seconds)

    def test_memory_bound_clock_gaps_and_missing_first_render(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        samples = np.full(1600, 0.1)
        for i in range(100):
            observer.microphone(7, START + i * 100_000_000, samples)
        self.assertEqual(len(observer.mic), MIC_LIMIT)
        observer.microphone(7, START + 11_000_000_000, samples)
        self.assertEqual(probe.result['status'], 'capture_clock_gap')
        probe = observer.arm(101, 7)
        observer.render(101, 320, START, samples)
        self.assertEqual(probe.result['status'], 'render_gap')
        probe = observer.arm(101, 7)
        observer.render(101, 0, START, samples)
        for i in range(1, 5):
            observer.render(101, i * 1600, START + i * 200_000_000, samples)
        self.assertFalse(probe.done.is_set())
        observer.cancel('expired')
        self.assertEqual(probe.result['status'], 'expired')
        self.assertEqual(probe.result['render_clock_resets'], 4)
        self.assertEqual(probe.result['max_render_clock_step_ms'], 100)

    def test_clock_refinement_or_real_gap_uses_only_a_new_contiguous_window(self):
        reference = public_signal()
        for actual_gap in (False, True):
            with self.subTest(actual_gap=actual_gap):
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7)
                first_stamp = START if actual_gap else START + 300_000_000
                observer.render(101, 0, first_stamp, reference[:1600])
                shift = 200_000_000 if actual_gap else 0
                for frame in range(1600, len(reference), 320):
                    observer.render(
                        101,
                        frame,
                        START + round(frame * 1e9 / RATE) + shift,
                        reference[frame : frame + 320],
                    )
                if actual_gap:
                    mic = np.zeros(40000)
                    start = 3200 + 1920
                    mic[start : start + 1600] = reference[:1600] * 0.3
                    tail = start + 1600 + 3200
                    mic[tail : tail + len(reference) - 1600] = reference[1600:] * 0.3
                else:
                    mic = scene(reference)
                for frame in range(0, len(mic), 320):
                    observer.microphone(
                        7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
                    )
                self.assertEqual(probe.result['status'], 'observed')
                self.assertEqual(
                    probe.result['leading_acoustic_evidence'],
                    {'status': 'render_clock_discontinuous'},
                )
                self.assertEqual(probe.result['render_clock_resets'], 1)
                self.assertAlmostEqual(probe.result['matched_window_delay_ms'], 120, delta=1)
                self.assertFalse(probe.result['first_audible_onset_measured'])

    def test_cannot_stitch_two_short_templates_across_a_clock_jump(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        ref = public_signal()[:3200]
        observer.render(101, 0, START, ref[:1600])
        observer.render(101, 1600, START + 300_000_000, ref[1600:])
        mic = np.zeros(32000)
        mic[5120:8320] = ref
        for frame in range(0, len(mic), 320):
            observer.microphone(
                7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
            )
        self.assertFalse(probe.done.is_set())
        self.assertEqual(len(observer.reference), 1600)
        observer.cancel('expired')
        self.assertEqual(probe.result['status'], 'expired')
        self.assertEqual(probe.result['render_clock_resets'], 1)

    def test_response_prefix_requires_continuous_capture_across_render_clock_jump(self):
        reference = public_signal()
        for condition in ('projection', 'physical_gap', 'late_gap', 'missing_first_block'):
            with self.subTest(condition=condition):
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
                split = 320 if condition == 'late_gap' else 1600
                observer.render(101, 0, START + 300_000_000, reference[:split])
                actual_gap = condition == 'physical_gap'
                shift = 200_000_000 if actual_gap else 0
                for frame in range(split, len(reference), 320):
                    observer.render(
                        101,
                        frame,
                        START + shift + round(frame * 1e9 / RATE),
                        reference[frame : frame + 320],
                    )
                start = 3200 + 1920
                mic = scene(reference)
                if actual_gap:
                    mic.fill(0)
                    mic[start : start + 1600] = reference[:1600] * 0.3
                    tail = start + 1600 + 3200
                    mic[tail : tail + len(reference) - 1600] = reference[1600:] * 0.3
                if condition == 'late_gap':
                    mic.fill(0)
                    cut = 2240
                    mic[start : start + cut] = reference[:cut] * 0.3
                    mic[start + cut + 320 : start + len(reference) + 320] = reference[cut:] * 0.3
                if condition == 'missing_first_block':
                    mic[start : start + 320] = 0
                for frame in range(0, len(mic), 320):
                    observer.microphone(
                        7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
                    )
                self.assertEqual(probe.result['status'], 'observed')
                leading = probe.result['leading_acoustic_evidence']
                metric = 'last_voiced_to_confirmed_output_block_ms'
                if condition == 'projection':
                    self.assertEqual(leading['status'], 'observed')
                    self.assertEqual(leading['alignment'], 'matched_render_frames')
                    self.assertAlmostEqual(leading[metric], 720, delta=1)
                else:
                    self.assertEqual(
                        leading['status'],
                        {
                            'physical_gap': 'leading_match_unconfirmed',
                            'late_gap': 'response_prefix_discontinuous',
                            'missing_first_block': 'leading_match_unconfirmed',
                        }[condition],
                    )
                    self.assertNotIn(metric, leading)
                self.assertFalse(observer.response_prefix.size)
                self.assertFalse(probe.result['first_audible_onset_measured'])

    def test_room_fit_recovers_reflected_prefix_but_keeps_missing_audio_unconfirmed(self):
        reference = public_signal()
        reference[640:960] = 0.12 * np.sin(2 * np.pi * 250 * np.arange(320) / RATE)
        room = np.zeros(25)
        room[0], room[-1] = 0.55, 0.5
        for condition in ('continuous', 'first_missing', 'late_gap'):
            with self.subTest(condition=condition):
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
                for frame in range(0, len(reference), 320):
                    clock_step = 120_000_000 if frame < 640 else 0
                    stamp = START + round(frame * 1e9 / RATE) + clock_step
                    observer.render(101, frame, stamp, reference[frame : frame + 320])
                audible = reference.copy()
                if condition == 'first_missing':
                    audible[:320] = 0
                if condition == 'late_gap':
                    audible = np.concatenate((audible[:2240], np.zeros(320), audible[2240:]))
                echoed = np.convolve(audible, room)
                mic = np.zeros(32000)
                mic[5120 : 5120 + len(echoed)] = echoed * 0.35
                for frame in range(0, len(mic), 320):
                    observer.microphone(
                        7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
                    )
                evidence = probe.result['leading_acoustic_evidence']
                metric = 'last_voiced_to_confirmed_output_block_ms'
                if condition == 'continuous':
                    self.assertEqual(evidence['status'], 'observed')
                    self.assertEqual(evidence['alignment'], 'matched_render_frames_room_fit')
                    self.assertAlmostEqual(evidence[metric], 720, delta=1)
                else:
                    self.assertNotEqual(evidence['status'], 'observed')
                    self.assertNotIn(metric, evidence)
                self.assertFalse(observer.response_prefix.size or observer.mic.size)

    def test_response_room_fit_waits_for_later_reference_packets(self):
        reference = public_signal()
        reference[640:960] = 0.12 * np.sin(2 * np.pi * 250 * np.arange(320) / RATE)
        room = np.zeros(25)
        room[0], room[-1] = 0.55, 0.5
        for condition in (
            'continuous',
            'missing_block',
            'expired',
            'render_gap',
            'capture_clock_gap',
            'new_clock_step',
            'cancelled',
        ):
            with self.subTest(condition=condition):
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
                self.addCleanup(observer.cancel)

                def render(start, stop):
                    for frame in range(start, stop, 320):
                        stamp = START + round(frame * 1e9 / RATE)
                        if frame < 640:
                            stamp += 120_000_000
                        observer.render(101, frame, stamp, reference[frame : frame + 320])

                render(0, 3840)
                mic = np.zeros(32000)
                echoed = np.convolve(reference, room)
                mic[5120 : 5120 + len(echoed)] = echoed * 0.35
                if condition == 'missing_block':
                    mic[5120 + 640 : 5120 + 960] = 0
                for frame in range(0, len(mic), 320):
                    stamp = START - 200_000_000 + round(frame * 1e9 / RATE)
                    observer.microphone(7, stamp, mic[frame : frame + 320])
                self.assertFalse(probe.done.is_set())
                if condition == 'expired':
                    observer.cancel('expired', probe)
                elif condition == 'render_gap':
                    render(4160, 4480)
                elif condition == 'capture_clock_gap':
                    observer.microphone(7, START + 3_000_000_000, mic[:320])
                elif condition == 'new_clock_step':
                    observer.render(101, 3840, START + 360_000_000, reference[3840:4160])
                    observer.cancel('expired', probe)
                elif condition == 'cancelled':
                    observer.cancel(probe=probe)
                else:
                    render(3840, 5760)
                self.assertTrue(probe.done.is_set())
                leading = probe.result['leading_acoustic_evidence']
                metric = 'last_voiced_to_confirmed_output_block_ms'
                if condition == 'continuous':
                    self.assertEqual(leading['alignment'], 'matched_render_frames_room_fit')
                    self.assertAlmostEqual(leading[metric], 720, delta=1)
                else:
                    self.assertNotIn(metric, leading)
                    if condition in ('expired', 'missing_block'):
                        self.assertEqual(leading['status'], 'response_prefix_discontinuous')
                        self.assertEqual(
                            leading['room_fit_status'],
                            'reference_coverage_missing'
                            if condition == 'expired'
                            else 'prefix_capture_below_floor',
                        )
                    else:
                        expected = 'expired' if condition == 'new_clock_step' else condition
                        self.assertEqual(probe.result['status'], expected)
                self.assertFalse(observer.response_prefix.size or observer.mic.size)

    def test_room_model_realigns_multipath_but_rejects_missing_or_shifted_audio(self):
        # Changing pitch and envelope make the two unfiltered windows favor
        # different peaks of the same stationary two-path acoustic response.
        rng = np.random.default_rng(2)
        seconds = np.arange(RATE) / RATE
        knots = np.arange(0, RATE + 1, 640) / RATE
        frequency = np.interp(seconds, knots, rng.uniform(110, 300, size=len(knots)))
        pitch = 2 * np.pi * np.cumsum(frequency) / RATE
        envelope = np.interp(seconds, knots, rng.uniform(0.02, 1, size=len(knots)))
        reference = envelope * sum(
            amplitude * np.sin(harmonic * pitch + rng.uniform(0, np.pi))
            for harmonic, amplitude in enumerate(rng.uniform(0.01, 0.1, size=9), 1)
        )
        reference += 0.005 * rng.normal(size=RATE)
        room = np.zeros(97)
        room[0], room[-1] = 0.52, 0.55
        for condition in ('continuous', 'first_missing', 'short_gap'):
            with self.subTest(condition=condition):
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
                for frame in range(0, len(reference), 320):
                    step = 120_000_000 if frame == 0 else 0
                    stamp = START + round(frame * 1e9 / RATE) + step
                    observer.render(101, frame, stamp, reference[frame : frame + 320])
                audible = reference.copy()
                if condition == 'first_missing':
                    audible[:320] = 0
                if condition == 'short_gap':
                    audible = np.concatenate((audible[:640], np.zeros(70), audible[640:]))
                echoed = np.convolve(audible, room)
                mic = np.zeros(48000)
                mic[5120 : 5120 + len(echoed)] = echoed * 0.35
                for frame in range(0, len(mic), 320):
                    observer.microphone(
                        7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
                    )
                evidence = probe.result['leading_acoustic_evidence']
                metric = 'last_voiced_to_confirmed_output_block_ms'
                if condition == 'continuous':
                    self.assertEqual(evidence['status'], 'observed')
                    self.assertEqual(evidence['alignment'], 'matched_render_frames_room_alignment')
                    self.assertAlmostEqual(evidence[metric], 726, delta=1)
                else:
                    self.assertNotEqual(evidence['status'], 'observed')
                    self.assertNotIn(metric, evidence)
                self.assertFalse(observer.response_prefix.size or observer.mic.size)

    def test_room_alignment_cannot_hide_a_local_pause_in_a_voiced_prefix(self):
        rng = np.random.default_rng(1168)
        seconds = np.arange(RATE) / RATE
        knots = np.arange(0, RATE + 1, 1600) / RATE
        frequency = np.interp(seconds, knots, rng.uniform(150, 320, size=len(knots)))
        pitch = 2 * np.pi * np.cumsum(frequency) / RATE
        envelope = np.interp(seconds, knots, rng.uniform(0.02, 1, size=len(knots)))
        reference = envelope * sum(
            amplitude * np.sin(harmonic * pitch + rng.uniform(0, np.pi))
            for harmonic, amplitude in enumerate(rng.uniform(0.01, 0.1, size=9), 1)
        )
        reference += 0.0002 * rng.normal(size=RATE)
        room = np.zeros(65)
        room[0], room[-1] = 0.55, 0.52
        for gap in (False, True):
            with self.subTest(gap=gap):
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
                for frame in range(0, len(reference), 320):
                    step = 120_000_000 if frame < 640 else 0
                    observer.render(
                        101,
                        frame,
                        START + round(frame * 1e9 / RATE) + step,
                        reference[frame : frame + 320],
                    )
                audible = (
                    np.concatenate((reference[:640], np.zeros(70), reference[640:]))
                    if gap
                    else reference
                )
                echoed = np.convolve(audible, room)
                mic = np.zeros(48000)
                mic[5120 : 5120 + len(echoed)] = echoed * 0.35
                for frame in range(0, len(mic), 320):
                    observer.microphone(
                        7,
                        START - 200_000_000 + round(frame * 1e9 / RATE),
                        mic[frame : frame + 320],
                    )
                evidence = probe.result['leading_acoustic_evidence']
                metric = 'last_voiced_to_confirmed_output_block_ms'
                if gap:
                    self.assertEqual(evidence['status'], 'response_prefix_alignment_mismatch')
                    self.assertNotIn(metric, evidence)
                else:
                    self.assertEqual(evidence['status'], 'observed')
                    self.assertIn(metric, evidence)
                self.assertFalse(observer.response_prefix.size or observer.mic.size)

    def test_response_probe_waits_for_late_leading_blocks_with_a_stable_clock(self):
        for has_leading in (True, False):
            with self.subTest(has_leading=has_leading):
                reference = public_signal()
                # The prelude matches as a 200 ms shape, but no individual
                # 20 ms block is energetic enough to timestamp the output.
                levels = [24, -19, 18, 23, -24, 27, -29, -22, 21, -18]
                if not has_leading:
                    levels.extend([20, -25, 22])
                prelude = len(levels) * 320
                reference[:prelude] = np.repeat(levels, 320) / 1000 + reference[:prelude] * 0.02
                mic = scene(reference, noise=0.0002)
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
                self.addCleanup(observer.cancel)

                def render(start, end):
                    for frame in range(start, end, 320):
                        stamp = START + round(frame * 1e9 / RATE)
                        observer.render(101, frame, stamp, reference[frame : frame + 320])

                render(0, 3200)
                for frame in range(0, len(mic), 320):
                    stamp = START - 200_000_000 + round(frame * 1e9 / RATE)
                    observer.microphone(7, stamp, mic[frame : frame + 320])
                self.assertFalse(probe.done.is_set())
                render(3200, 3520)
                self.assertFalse(probe.done.is_set())
                render(3520, 3840)
                if not has_leading:
                    self.assertFalse(probe.done.is_set())
                    render(3840, 4160)
                self.assertTrue(probe.done.is_set())
                self.assertEqual(probe.result['status'], 'observed')
                self.assertEqual(probe.result['render_clock_resets'], 0)
                leading = probe.result['leading_acoustic_evidence']
                if has_leading:
                    self.assertEqual(leading['status'], 'observed')
                    self.assertEqual(leading['reference_block_start_ms'], 200)
                    self.assertAlmostEqual(leading['last_voiced_to_confirmed_output_block_ms'], 920)
                else:
                    self.assertEqual(
                        leading,
                        {
                            'status': 'reference_not_found',
                            'reference_prefix': 'nonzero_below_floor',
                        },
                    )

    def test_response_probe_waits_for_late_render_reference_after_clock_jump(self):
        for outcome in ('complete', 'expired', 'render_gap', 'no_leading_block'):
            with self.subTest(outcome=outcome):
                reference = public_signal()
                silent_frames = 4800 if outcome == 'no_leading_block' else 1280
                reference[:silent_frames] = 0
                mic = scene(reference)
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)

                def render(start, end):
                    for frame in range(start, end, 320):
                        step = 120_000_000 if frame < 640 else 0
                        stamp = START + round(frame * 1e9 / RATE) + step
                        observer.render(101, frame, stamp, reference[frame : frame + 320])

                # With 80 ms of silence the stable template already exists,
                # but the leading 200 ms window needs two more RENDER packets.
                render(0, 3840)
                for frame in range(0, len(mic), 320):
                    observer.microphone(
                        7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
                    )
                self.assertFalse(probe.done.is_set())
                if outcome == 'no_leading_block':
                    render(3840, len(reference))
                    self.assertTrue(probe.done.is_set())
                    self.assertEqual(
                        probe.result['leading_acoustic_evidence'],
                        {'status': 'response_prefix_reference_missing'},
                    )
                elif outcome == 'complete':
                    render(3840, 4480)
                    self.assertTrue(probe.done.is_set())
                    self.assertEqual(probe.result['status'], 'observed')
                    leading = probe.result['leading_acoustic_evidence']
                    self.assertEqual(leading['alignment'], 'matched_render_frames')
                    self.assertEqual(leading['reference_block_start_ms'], 80)
                    self.assertAlmostEqual(leading['last_voiced_to_confirmed_output_block_ms'], 800)
                else:
                    if outcome == 'expired':
                        observer.cancel('expired', probe)
                    else:
                        render(4160, 4480)
                    self.assertTrue(probe.done.is_set())
                    self.assertEqual(probe.result['status'], outcome)
                    render(3840, 4480)
                    self.assertEqual(probe.result['status'], outcome)
                self.assertFalse(observer.response_prefix.size or observer.mic.size)

    def test_repeated_clock_refinements_can_still_produce_a_stable_later_window(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        ref = public_signal()
        for i in range(5):
            observer.render(101, i * 160, START + i * 100_000_000, ref[i * 160 : (i + 1) * 160])
        observer.render(101, 800, START + 500_000_000, ref[800:])
        mic = np.zeros(32000)
        mic[13120:28320] = ref[800:] * 0.3
        for frame in range(0, len(mic), 320):
            observer.microphone(
                7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
            )
        self.assertEqual(probe.result['status'], 'observed')
        self.assertEqual(probe.result['render_clock_resets'], 5)
        self.assertEqual(probe.result['max_render_clock_step_ms'], 90)
        self.assertAlmostEqual(probe.result['matched_window_delay_ms'], 120, delta=1)

    def test_invalid_samples_and_missing_search_coverage_are_explicit(self):
        observer = PlaybackEvidence()
        for method, expected in (
            (observer.microphone, 'invalid_capture'),
            (observer.render, 'invalid_render'),
        ):
            probe = observer.arm(101, 7)
            if expected == 'invalid_capture':
                method(7, START, np.array([float('nan')]))
            else:
                method(101, 0, START, np.array([float('inf')]))
            self.assertEqual(probe.result['status'], expected)
        probe = observer.arm(101, 7)
        observer.render(101, 0, START, public_signal())
        observer.microphone(7, START + 200_000_000, np.ones(32000))
        self.assertEqual(probe.result['status'], 'insufficient_capture_coverage')

    def test_cancel_and_feed_race_cannot_retain_samples_or_complete_twice(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        barrier = threading.Barrier(2)

        def producer():
            barrier.wait()
            feed(observer, public_signal(), scene(public_signal()))

        thread = threading.Thread(target=producer)
        thread.start()
        barrier.wait()
        observer.cancel()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(probe.done.is_set())
        self.assertIn(probe.result['status'], ('observed', 'cancelled'))
        self.assertEqual(len(observer.mic) + len(observer.reference), 0)


class ProbeIntegrationTests(unittest.TestCase):
    def bridge(self):
        bridge = Bridge(None, None)
        bridge.session.connection = Sink()
        bridge.save_status = lambda: None
        return bridge

    def test_owner_probe_cannot_open_mic_and_rejects_busy_capture(self):
        bridge = self.bridge()
        with self.assertRaises(RuntimeError):
            bridge.playback_probe()
        self.assertFalse(bridge.session.active)
        bridge.session.active = True
        for state in ('playing', 'calibrating'):
            owner = bridge.playback if state == 'playing' else bridge.calibration
            attr = 'playing' if state == 'playing' else 'active'
            setattr(owner, attr, True)
            with self.assertRaises(RuntimeError):
                bridge.playback_probe()
            setattr(owner, attr, False)
        self.assertIsNone(bridge.capture.evidence.current)

    def test_queue_failure_ends_trace_and_wipes_probe(self):
        bridge = self.bridge()
        bridge.session.active = True
        with patch.object(bridge.response, 'offer', side_effect=queue.Full):
            with self.assertRaisesRegex(RuntimeError, 'queue busy'):
                bridge.playback_probe()
        self.assertIsNone(bridge.capture.evidence.current)
        self.assertIsNone(bridge.announcements.generation)
        self.assertFalse(bridge.capture.measuring)
        self.assertEqual(bridge.diagnostics.report()['requests'][-1]['outcome'], 'failed')
        self.assertTrue(bridge.session.active)

    def test_passive_response_probe_binds_the_fixed_input_on_owner_socket(self):
        from device_smoke import rpc

        bridge = self.bridge()
        bridge.session.wake = True
        bridge.session.awake = False
        bridge.session.epoch = 7
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'probe.sock'
            server = AnnouncementServer(bridge, path)
            server.start()
            try:
                arm = {'operation': 'response_probe_arm', 'case': 1}
                self.assertNotEqual(rpc(path, arm)['status'], 'armed')
                self.assertFalse(bridge.session.active)
                bridge.session.active = True
                result = rpc(path, arm)
                self.assertEqual(result['status'], 'armed')
                fetch = {'operation': 'response_probe_result', 'token': result['token']}
                self.assertEqual(rpc(path, fetch)['status'], 'waiting')
                self.assertIsNone(bridge.capture.evidence.current)
                bridge.recognition.hooks.bind_response_probe(
                    '大黄蜂，一加一等于几？', 101, START - 600_000_000
                )
                self.assertFalse(bridge.capture.measuring)
                self.assertEqual(rpc(path, fetch)['status'], 'observing')
                reference = public_signal()
                feed(bridge.capture.evidence, reference, scene(reference))
                receipt = rpc(path, fetch)
                self.assertEqual(
                    set(receipt),
                    {'status', 'generation', 'last_voiced_to_confirmed_output_block_ms'},
                )
                self.assertEqual(receipt['generation'], 101)
                self.assertEqual(receipt['status'], 'observed')
                self.assertAlmostEqual(
                    receipt['last_voiced_to_confirmed_output_block_ms'], 720, delta=1
                )
                self.assertEqual(rpc(path, fetch)['status'], 'unavailable')
                self.assertFalse(bridge.capture.evidence.mic.size)
            finally:
                server.close()
                bridge.capture.clear_response_probe()

    def test_passive_response_probe_rejects_other_input_and_discards_on_epoch_reset(self):
        bridge = self.bridge()
        capture = bridge.capture
        bridge.session.active = True
        bridge.session.epoch = 7
        token = capture.arm_response_probe(1)
        capture.bind_response_probe('二加一等于几？', 101, START)
        self.assertEqual(capture.read_response_probe(token), {'status': 'unmatched_input'})
        self.assertIsNone(capture.evidence.current)
        token = capture.arm_response_probe(1)
        capture.bind_response_probe('一加一等于几？', 101, None)
        self.assertEqual(capture.read_response_probe(token), {'status': 'input_clock_unavailable'})
        self.assertIsNone(capture.evidence.current)
        capture.reset_transport('wifi')
        self.assertEqual(capture.read_response_probe(token), {'status': 'unavailable'})
        token = capture.arm_response_probe(1)
        capture.bind_response_probe('一加一等于几？', 102, START)
        capture.evidence.microphone(7, START, np.ones(320))
        self.assertTrue(capture.evidence.mic.size)
        capture.reset_session()
        self.assertEqual(capture.read_response_probe(token), {'status': 'unavailable'})
        self.assertFalse(capture.evidence.mic.size)

    def test_passive_probe_keeps_failure_at_the_window_or_leading_block(self):
        bridge = self.bridge()
        bridge.session.active = True
        bridge.session.epoch = 7
        capture = bridge.capture
        reference = public_signal()
        missing_prefix = scene(reference)
        missing_prefix[5120:5440] = 0
        for audio, reason in (
            (np.zeros(32000), 'not_observed'),
            (missing_prefix, 'leading_match_unconfirmed'),
        ):
            with self.subTest(reason=reason):
                token = capture.arm_response_probe(1)
                capture.bind_response_probe('一加一等于几？', 101, START - 600_000_000)
                feed(capture.evidence, reference, audio)
                receipt = capture.read_response_probe(token)
                if reason == 'not_observed':
                    self.assertIn('window_diagnostic', receipt)
                    self.assertEqual(
                        receipt.pop('window_diagnostic'),
                        {'correlation': 0.0, 'competing_correlation': 0.0},
                    )
                else:
                    self.assertIn('leading_block_correlations', receipt)
                    first, second = receipt.pop('leading_block_correlations')
                    self.assertEqual(first, 0.0)
                    self.assertGreater(second, 0.55)
                self.assertEqual(
                    receipt, {'status': 'unconfirmed', 'generation': 101, 'reason': reason}
                )
                self.assertFalse(capture.evidence.mic.size)
                self.assertEqual(capture.read_response_probe(token), {'status': 'unavailable'})

    def test_response_can_confirm_first_output_after_exact_digital_silence(self):
        metric = 'last_voiced_to_confirmed_output_block_ms'
        for condition, expected in (
            (
                'exact_silence',
                {'status': 'observed', metric: 1040, 'alignment': 'matched_after_digital_silence'},
            ),
            (
                'weak_prelude',
                {
                    'status': 'unconfirmed',
                    'reason': 'reference_not_found',
                    'reference_prefix': 'nonzero_below_floor',
                },
            ),
            (
                'past_limit',
                {
                    'status': 'unconfirmed',
                    'reason': 'reference_not_found',
                    'reference_prefix': 'digital_silence',
                },
            ),
            ('weak_first_block', {'status': 'unconfirmed', 'reason': 'reference_not_sustained'}),
            (
                'weak_full_rate_block',
                {'status': 'unconfirmed', 'reason': 'reference_not_sustained'},
            ),
            (
                'weak_first_capture_block',
                {'status': 'unconfirmed', 'reason': 'leading_match_unconfirmed'},
            ),
        ):
            for render_first in (False, True):
                with self.subTest(condition=condition, render_first=render_first):
                    bridge = self.bridge()
                    bridge.session.active = True
                    bridge.session.epoch = 7
                    capture = bridge.capture
                    token = capture.arm_response_probe(1)
                    capture.bind_response_probe('一加一等于几？', 101, START - 600_000_000)
                    reference = public_signal()
                    start = 7360 if condition == 'past_limit' else 5120
                    reference[:start] = 0
                    if condition == 'weak_prelude':
                        reference[640] = 1 / 32768
                    if condition == 'weak_first_block':
                        reference[start - 320] = 1 / 32768
                    if condition == 'weak_full_rate_block':
                        block = reference[start : start + 320]
                        block[1::2] = 0
                        block[::2] -= block[::2].mean()
                        block[::2] *= 0.012 / np.std(block[::2])
                    microphone = scene(reference, noise=0.0002)
                    if condition == 'weak_first_capture_block':
                        block = microphone[5120 + start : 5120 + start + 320]
                        block[:] = 0.04 + (block - 0.04) * 0.035
                    feed(capture.evidence, reference, microphone, render_first=render_first)
                    receipt = capture.read_response_probe(token)
                    if condition == 'weak_first_capture_block':
                        self.assertIn('leading_block_correlations', receipt)
                        first, second = receipt.pop('leading_block_correlations')
                        self.assertLess(first, 0.55)
                        self.assertGreater(second, 0.55)
                    self.assertEqual(receipt, {'generation': 101, **expected})
                    self.assertEqual(capture.read_response_probe(token), {'status': 'unavailable'})
                    self.assertFalse(
                        capture.evidence.response_prefix.size or capture.evidence.mic.size
                    )

    def test_response_receipt_keeps_room_fit_failure_separate_from_prefix_match(self):
        reference = public_signal()
        reference[640:960] = 0.12 * np.sin(2 * np.pi * 250 * np.arange(320) / RATE)
        room = np.zeros(25)
        room[0], room[-1] = 0.55, 0.5
        for condition, expected in (
            ('reference', 'reference_coverage_missing'),
            ('fit', 'model_unconfirmed'),
            ('silence', 'prefix_capture_below_floor'),
            ('gain', 'prefix_gain_out_of_range'),
            ('shape', 'prefix_shape_mismatch'),
        ):
            with self.subTest(condition=condition):
                bridge = self.bridge()
                bridge.session.active = True
                bridge.session.epoch = 7
                capture = bridge.capture
                token = capture.arm_response_probe(1)
                capture.bind_response_probe('一加一等于几？', 101, START - 600_000_000)
                observer = capture.evidence
                probe = observer.current
                render_end = 3840 if condition == 'reference' else len(reference)
                for frame in range(0, render_end, 320):
                    stamp = START + round(frame * 1e9 / RATE)
                    observer.render(
                        101,
                        frame,
                        stamp + (120_000_000 if frame < 640 else 0),
                        reference[frame : frame + 320],
                    )
                mic = np.zeros(32000)
                echoed = np.convolve(reference, room)
                mic[5120 : 5120 + len(echoed)] = echoed * 0.35
                prefix_block = mic[5120 + 640 : 5120 + 960]
                if condition == 'silence':
                    prefix_block.fill(0)
                if condition == 'gain':
                    prefix_block *= 0.25
                if condition == 'shape':
                    prefix_block *= -1
                if condition == 'fit':
                    # The 200 ms prefix is followed by context, training and holdout.
                    mic[5120 + 3200 + 1696 : 5120 + 3200 + 2336] = 0
                for frame in range(0, len(mic), 320):
                    observer.microphone(
                        7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
                    )
                if condition == 'reference':
                    self.assertFalse(probe.done.is_set())
                    observer.cancel('expired', probe)
                self.assertEqual(
                    capture.read_response_probe(token),
                    {
                        'status': 'unconfirmed',
                        'generation': 101,
                        'reason': 'response_prefix_discontinuous',
                        'room_fit_status': expected,
                    },
                )
                self.assertEqual(capture.read_response_probe(token), {'status': 'unavailable'})
                self.assertFalse(observer.response_prefix.size or observer.mic.size)

    def test_response_receipt_keeps_competing_window_scores_without_timing(self):
        bridge = self.bridge()
        bridge.session.active = True
        bridge.session.epoch = 7
        capture = bridge.capture
        token = capture.arm_response_probe(1)
        capture.bind_response_probe('一加一等于几？', 101, START - 600_000_000)
        observer = capture.evidence
        probe = observer.current
        tone = 0.15 * np.sin(np.arange(16000) * 2 * np.pi * 400 / RATE)
        feed(observer, tone, scene(tone), render_first=True)
        self.assertEqual(probe.result['status'], 'ambiguous')
        self.assertEqual(capture.read_response_probe('0' * 32), {'status': 'unavailable'})
        self.assertEqual(
            capture.read_response_probe(token),
            {
                'status': 'unconfirmed',
                'generation': 101,
                'reason': 'ambiguous',
                'window_diagnostic': {
                    'correlation': probe.result['correlation'],
                    'competing_correlation': probe.result['competing_correlation'],
                },
            },
        )
        self.assertEqual(capture.read_response_probe(token), {'status': 'unavailable'})
        self.assertFalse(observer.response_prefix.size or observer.mic.size)

    def test_passive_probe_reports_signed_anchor_difference_without_accepting_a_gap(self):
        bridge = self.bridge()
        bridge.session.active = True
        bridge.session.epoch = 7
        capture = bridge.capture
        token = capture.arm_response_probe(1)
        capture.bind_response_probe('一加一等于几？', 101, START - 600_000_000)
        reference = public_signal()
        split, gap = 3840, 960
        observer = capture.evidence
        for frame in range(0, len(reference), 320):
            stamp = START + round((frame + (gap if frame >= split else 0)) * 1e9 / RATE)
            observer.render(101, frame, stamp, reference[frame : frame + 320])
        mic = np.zeros(32000)
        start = 5120
        mic[start : start + split] = reference[:split] * 0.3
        mic[start + split + gap : start + len(reference) + gap] = reference[split:] * 0.3
        for frame in range(0, len(mic), 320):
            observer.microphone(
                7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
            )
        receipt = capture.read_response_probe(token)
        self.assertEqual(receipt['status'], 'unconfirmed')
        self.assertEqual(receipt['reason'], 'response_prefix_alignment_mismatch')
        self.assertEqual(receipt['generation'], 101)
        self.assertNotIn('last_voiced_to_confirmed_output_block_ms', receipt)
        self.assertEqual(receipt['anchor_diagnostic']['prefix_minus_projected_ms'], -60)
        self.assertEqual(receipt['anchor_diagnostic']['reference_anchor_spacing_ms'], 240)
        self.assertEqual(receipt['anchor_diagnostic']['prefix_block_status'], 'observed')
        self.assertGreater(receipt['anchor_diagnostic']['prefix_correlation'], 0.99)
        self.assertFalse(observer.response_prefix.size or observer.mic.size)

    def test_server_deadline_and_invalid_shape_never_open_capture(self):
        from announce import Announcement

        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        job = Announcement(101, PROBE_TEXT)
        cancelled = []
        finished = []
        stub = SimpleNamespace(
            playback_probe=lambda: (job, probe),
            cancel_playback_probe=lambda job, probe: (
                cancelled.append(job.generation),
                observer.cancel(),
            ),
            finish_playback_probe=finished.append,
        )
        server = AnnouncementServer(stub)
        reader, writer = socket.socketpair()
        try:
            with patch('announce.time.monotonic', side_effect=(0, 23)):
                self.assertEqual(
                    server.playback_probe(reader, {'operation': 'playback_probe'})['status'],
                    'timeout',
                )
            self.assertEqual(cancelled, [101])
            self.assertEqual(finished, [probe])
            with self.assertRaises(ValueError):
                server.playback_probe(reader, {'operation': 'playback_probe', 'text': 'arbitrary'})
        finally:
            reader.close()
            writer.close()
            observer.cancel()

    def test_late_client_cleanup_cannot_cancel_or_unmute_a_successor_probe(self):
        bridge = self.bridge()
        bridge.session.active = True
        with patch.object(bridge.response, 'offer'):
            old_job, old_probe = bridge.playback_probe()
            bridge.cancel('new_request')
            new_job, new_probe = bridge.playback_probe()
            bridge.cancel_playback_probe(old_job, old_probe)
            bridge.finish_playback_probe(old_probe)
            self.assertTrue(bridge.valid(new_job.generation))
            self.assertIs(bridge.capture.probe, new_probe)
            self.assertFalse(new_probe.done.is_set())
            bridge.cancel_playback_probe(new_job, new_probe)
            self.assertTrue(new_probe.done.is_set())
            self.assertIsNone(bridge.capture.probe)

    def test_framed_packets_use_raw_microphone_and_ignore_old_generation(self):
        bridge = self.bridge()
        bridge.session.active = True
        bridge.session.epoch = 7
        probe = bridge.capture.evidence.arm(bridge.generation, 7)
        bridge.capture.echo.process = lambda stamp, raw: CleanFrame(
            stamp, raw, raw, raw * 0, raw * 0
        )
        bridge.capture.endpoint.consume = lambda frame: None
        reference = public_signal()
        mic = scene(reference)
        for frame in range(0, 32000, 320):
            bridge.on_packet(Packet(MIC, 6, frame // 320, frame, START, b'\0' * 640))
            bridge.on_packet(
                Packet(
                    MIC,
                    7,
                    frame // 320,
                    frame,
                    START - 200_000_000 + round(frame * 1e9 / RATE),
                    (mic[frame : frame + 320] * 32768).astype('<i2').tobytes(),
                )
            )
            if 3200 <= frame < 19200:
                offset = frame - 3200
                pcm = (reference[offset : offset + 320] * 32768).astype('<i2').tobytes()
                bridge.on_packet(Packet(RENDER, bridge.generation - 1, 0, offset, START, pcm))
                bridge.on_packet(
                    Packet(
                        RENDER,
                        bridge.generation,
                        0,
                        offset,
                        START + round(offset * 1e9 / RATE),
                        pcm,
                    )
                )
        self.assertEqual(probe.result['status'], 'observed')
        self.assertAlmostEqual(probe.result['matched_window_delay_ms'], 120, delta=1)
        for action in (
            lambda: bridge.cancel('test'),
            bridge.capture.close,
            lambda: bridge.capture.reset_transport('wifi'),
            bridge.capture.reset_session,
        ):
            probe = bridge.capture.evidence.arm(bridge.generation, 7)
            action()
            self.assertTrue(probe.done.is_set())
            self.assertNotEqual(probe.result['status'], 'observed')

    def test_real_owner_socket_waits_for_evidence_and_disconnect_cancels_only_owned_job(self):
        from announce import Announcement

        job = Announcement(101, PROBE_TEXT)
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        entered = threading.Event()
        cancelled = []

        def start():
            entered.set()
            return job, probe

        def cancel(owned, ticket):
            self.assertIs(owned, job)
            self.assertIs(ticket, probe)
            cancelled.append(True)
            observer.cancel()

        stub = SimpleNamespace(playback_probe=start, cancel_playback_probe=cancel)
        stub.finish_playback_probe = lambda probe: None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'probe.sock'
            server = AnnouncementServer(stub, path)
            server.start()
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(2)
                client.connect(str(path))
                client.sendall(b'{"operation":"playback_probe"}\n')
                self.assertTrue(entered.wait(1))
                feed(observer, public_signal(), scene(public_signal()))
                job.result = 'completed'
                job.done.set()
                result = json.loads(client.recv(4096))
                self.assertEqual(result['evidence']['status'], 'observed')
                client.close()
                self.assertFalse(cancelled)
                entered.clear()
                job.done.clear()
                probe = observer.arm(101, 7)
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(path))
                client.sendall(b'{"operation":"playback_probe"}\n')
                self.assertTrue(entered.wait(1))
                client.close()
                self.assertTrue(probe.done.wait(1))
                self.assertTrue(cancelled)
            finally:
                server.close()
                observer.cancel()

    def test_full_bridge_probe_rpc_with_actual_framing_dispatch_and_completion(self):
        reference = public_signal()

        class Speech:
            def stream(self, text, current):
                self_text.append(text)
                yield (reference * 32768).astype('<i2')

        self_text = []
        bridge = Bridge(Speech(), None)
        bridge.session.active = True
        bridge.session.epoch = 7
        bridge.save_status = lambda: None
        endpoint_frames = []
        bridge.capture.endpoint.consume = endpoint_frames.append
        bridge.capture.echo.process = lambda stamp, raw: CleanFrame(stamp, raw, raw, raw, raw)

        class AcousticLoopback:
            def sendall(self, data):
                packet = receive(Fragmented(data))
                if packet.kind == SPEAKER:
                    bridge.on_packet(
                        Packet(
                            RENDER,
                            packet.generation,
                            packet.sequence,
                            packet.frame,
                            START + round(packet.frame * 1e9 / RATE),
                            packet.data,
                        )
                    )
                if packet.kind == EVENT and json.loads(packet.data)['event'] == 'play_end':
                    microphone = scene(reference)
                    for frame in range(0, len(microphone), 320):
                        bridge.on_packet(
                            Packet(
                                MIC,
                                7,
                                frame // 320,
                                frame,
                                START - 200_000_000 + round(frame * 1e9 / RATE),
                                (microphone[frame : frame + 320] * 32768).astype('<i2').tobytes(),
                            )
                        )
                    bridge.on_event({'event': 'play_done', 'generation': packet.generation})

        bridge.session.connection = AcousticLoopback()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'probe.sock'
            server = AnnouncementServer(bridge, path)
            server.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(3)
                    client.connect(str(path))
                    client.sendall(b'{"operation":"playback_probe"}\n')
                    result = json.loads(client.recv(4096))
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['evidence']['status'], 'observed')
                leading = result['evidence']['leading_acoustic_evidence']
                self.assertEqual(leading['status'], 'observed')
                self.assertAlmostEqual(leading['capture_minus_render_origin_ms'], 120, delta=1)
                self.assertFalse(result['evidence']['raw_audio_saved'])
                self.assertEqual(self_text, [PROBE_TEXT])
                self.assertEqual(endpoint_frames, [])
                self.assertTrue(bridge.session.active)
                self.assertIsNone(bridge.capture.probe)
                self.assertIsNone(bridge.capture.evidence.current)
                self.assertFalse(bridge.capture.measuring)
            finally:
                server.close()
                bridge.cancel_playback_observation()

    def test_lookahead_measurement_tail_is_not_delivered_as_a_new_turn(self):
        bridge = self.bridge()
        bridge.session.active = True
        bridge.session.epoch = 7
        seen = []
        bridge.capture.endpoint.consume = seen.append
        bridge.capture.echo.process = lambda stamp, raw: CleanFrame(stamp, raw, raw, raw, raw)
        probe = bridge.capture.begin_probe(bridge.generation, 7)
        for sequence in range(3):
            stamp = START + sequence * 20_000_000
            bridge.on_packet(Packet(MIC, 7, sequence, sequence * 320, stamp, b'\0' * 640))
        bridge.capture.end_probe(probe)
        for sequence in range(3, 7):
            stamp = START + sequence * 20_000_000
            bridge.on_packet(Packet(MIC, 7, sequence, sequence * 320, stamp, b'\0' * 640))
        self.assertEqual(seen, [])
        for sequence in range(7, 10):
            stamp = START + sequence * 20_000_000
            bridge.on_packet(Packet(MIC, 7, sequence, sequence * 320, stamp, b'\0' * 640))
        self.assertTrue(seen)
        self.assertTrue(all(frame.nanos >= START + 60_000_000 for frame in seen))
        bridge.capture.reset_transport('usb')
        self.assertEqual(bridge.capture.discard_endpoint_before_ns, 0)


if __name__ == '__main__':
    unittest.main()

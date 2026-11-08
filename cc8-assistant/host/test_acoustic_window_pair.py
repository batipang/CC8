"""A periodic prefix needs independent later waveform evidence, not a lower threshold."""

import unittest
from unittest.mock import patch
import numpy as np
from playback_evidence import PlaybackEvidence
from test_playback_evidence import public_signal, scene, feed, START, RATE
from test_response_output_window import Clock


class WindowPairTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.enterContext(patch('playback_evidence.threading.Timer', self.clock.timer))

    def signal(self, *, periodic_onset=False):
        reference = public_signal()
        reference[:3200] = 0.12 * np.sin(np.arange(3200) * 2 * np.pi * 400 / RATE)
        if not periodic_onset:
            reference[:640] = public_signal()[:640] * 0.18
        return reference

    def test_two_distinct_contiguous_windows_confirm_a_periodic_prefix(self):
        ref = self.signal()
        for order in (False, True):
            with self.subTest(render_first=order):
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
                feed(observer, ref, scene(ref), render_first=order)
                self.assertTrue(probe.done.is_set())
                self.assertEqual(probe.result['status'], 'observed')
                leading = probe.result['leading_acoustic_evidence']
                self.assertEqual(leading['status'], 'observed')
                self.assertEqual(leading['alignment'], 'matched_contiguous_windows')
                self.assertAlmostEqual(
                    leading['last_voiced_to_confirmed_output_block_ms'], 720, delta=1
                )
                self.assertEqual(probe.result['matching_window_ms'], 400)

    def test_periodic_onset_with_distinct_tail_still_cannot_prove_prefix_position(self):
        ref = self.signal(periodic_onset=True)
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
        feed(observer, ref, scene(ref))
        self.assertEqual(probe.result['status'], 'ambiguous')

    def test_short_speaker_pause_inside_periodic_prefix_is_not_a_new_origin(self):
        ref = self.signal(periodic_onset=True)
        heard = np.r_[ref[:1600], np.zeros(160), ref[1600:-160]]
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
        feed(observer, ref, scene(heard))
        self.assertNotEqual(probe.result['leading_acoustic_evidence']['status'], 'observed')

    def test_periodic_both_windows_remain_ambiguous(self):
        ref = 0.12 * np.sin(np.arange(16000) * 2 * np.pi * 400 / RATE)
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
        feed(observer, ref, scene(ref))
        self.assertEqual(probe.result['status'], 'ambiguous')
        self.assertNotIn(
            'last_voiced_to_confirmed_output_block_ms', probe.result['leading_acoustic_evidence']
        )

    def test_unrelated_second_window_cannot_confirm_the_first_periodic_match(self):
        ref = self.signal()
        heard = ref.copy()
        heard[3200:6400] = np.random.default_rng(64).normal(0, 0.08, 3200)
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
        feed(observer, ref, scene(heard))
        self.assertEqual(probe.result['status'], 'ambiguous')
        self.assertNotIn(
            'last_voiced_to_confirmed_output_block_ms', probe.result['leading_acoustic_evidence']
        )


class WindowPairBoundaryTests(unittest.TestCase):
    setUp = WindowPairTests.setUp
    signal = WindowPairTests.signal

    def pending(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
        ref = self.signal()[:3200]
        feed(observer, ref, scene(ref))
        self.assertFalse(probe.done.is_set())
        self.assertIsNotNone(observer.pending_window_result)
        return observer, probe

    def test_missing_second_window_expires_as_original_ambiguity_and_wipes(self):
        observer, probe = self.pending()
        self.assertEqual(len(self.clock.timers), 1)
        self.clock.advance(20)
        self.assertEqual(probe.result['status'], 'ambiguous')
        self.assertFalse(
            observer.mic.size or observer.reference.size or observer.response_prefix.size
        )

    def test_cancel_and_old_timer_cannot_publish_or_clear_replacement(self):
        observer, old = self.pending()
        observer.cancel()
        replacement = observer.arm(102, 8, input_end_ns=START)
        self.clock.timers[0].fire()
        self.assertIs(observer.current, replacement)
        self.assertEqual(old.result['status'], 'cancelled')
        observer.cancel()

    def test_capture_gap_discards_pending_pair(self):
        observer, probe = self.pending()
        observer.microphone(7, observer.mic_end + 3_000_000, np.zeros(320))
        self.assertEqual(probe.result['status'], 'capture_clock_gap')
        self.assertIsNone(observer.pending_window_result)
        self.assertFalse(observer.mic.size)

    def test_render_clock_jump_cannot_stitch_two_windows(self):
        observer, probe = self.pending()
        observer.render(101, 3200, START + 500_000_000, self.signal()[3200:])
        self.assertEqual(observer.clock_resets, 1)
        self.assertIsNone(observer.pending_window_result)
        observer.cancel('expired', probe)
        self.assertNotEqual(
            probe.result['leading_acoustic_evidence'].get('alignment'), 'matched_contiguous_windows'
        )

    def test_ordinary_probe_keeps_legacy_ambiguous_result(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        ref = self.signal()
        feed(observer, ref, scene(ref))
        self.assertEqual(probe.result['status'], 'ambiguous')

    def test_duplicate_early_fingerprint_cannot_claim_unique_prefix(self):
        ref = self.signal()
        mic = scene(ref)
        mic[4320:4960] = 0.04 + 0.3 * ref[:640]
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
        feed(observer, ref, mic)
        self.assertEqual(probe.result['status'], 'ambiguous')

    def test_true_onset_and_later_anchor_disagreement_is_rejected(self):
        ref = self.signal()
        heard = np.r_[ref[:1600], np.zeros(160), ref[1600:-160]]
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7, input_end_ns=START - 600_000_000)
        feed(observer, ref, scene(heard))
        self.assertNotEqual(probe.result['leading_acoustic_evidence']['status'], 'observed')


class PairReceiptTests(unittest.TestCase):
    def proof(self):
        return dict(
            version=1,
            window_ms=200,
            window_correlations=[0.8, 0.9],
            joint_correlation=0.9,
            competing_correlation=0.4,
            prefix_window_ms=40,
            prefix_correlation=0.9,
            prefix_competing_correlation=0.4,
            prefix_offset_ms=0.125,
        )

    def receipt(self):
        return dict(
            status='observed',
            generation=101,
            last_voiced_to_confirmed_output_block_ms=720,
            alignment='matched_contiguous_windows',
            window_pair=self.proof(),
        )

    def test_valid_receipt_is_whitelisted_and_copied(self):
        from device_smoke import summarize_response_acoustic, observed_response_interval

        raw = self.receipt()
        result = summarize_response_acoustic(raw, 101)
        self.assertEqual(observed_response_interval(result), 720)
        raw['window_pair']['window_correlations'][0] = 0
        self.assertEqual(result['window_pair']['window_correlations'][0], 0.8)
        self.assertEqual(
            summarize_response_acoustic(self.receipt(), 102)['reason'], 'generation_mismatch'
        )

    def test_malformed_pair_or_weak_independent_evidence_cannot_pass(self):
        from device_smoke import summarize_response_acoustic, observed_response_interval

        for changes in (
            dict(version=True),
            dict(window_ms=400),
            dict(window_correlations=[0.8]),
            dict(window_correlations=[0.8, 0.54]),
            dict(joint_correlation=float('nan')),
            dict(competing_correlation=0.8),
            dict(prefix_window_ms=20),
            dict(prefix_correlation=0.54),
            dict(prefix_competing_correlation=0.8),
            dict(prefix_offset_ms=2.01),
            dict(prefix_offset_ms=True),
            dict(prefix_offset_ms=float('inf')),
            dict(private_text='discard'),
        ):
            with self.subTest(changes=changes):
                raw = self.receipt()
                raw['window_pair'].update(changes)
                self.assertIsNone(observed_response_interval(raw))
                self.assertEqual(summarize_response_acoustic(raw, 101)['status'], 'unconfirmed')
        raw = self.receipt()
        raw.pop('window_pair')
        self.assertIsNone(observed_response_interval(raw))
        raw = self.receipt()
        raw.pop('alignment')
        self.assertIsNone(observed_response_interval(raw))

    def test_long_pair_receipt_also_requires_independent_continuous_capture_clock(self):
        from device_smoke import observed_response_interval

        raw = self.receipt()
        raw['last_voiced_to_confirmed_output_block_ms'] = 30000
        self.assertIsNone(observed_response_interval(raw))
        raw['interval_clock'] = dict(version=1, coverage_ms=40000, discontinuities=0)
        self.assertEqual(observed_response_interval(raw), 30000)
        raw['window_pair']['prefix_offset_ms'] = 3
        self.assertIsNone(observed_response_interval(raw))

    def test_suite_audit_requires_original_valid_proof(self):
        from test_audit_device_suite import saved_suite
        from audit_device_suite import audit

        data = saved_suite(qa=True, acoustic_coverage=True)
        for phase in data['phases']:
            receipt = self.receipt()
            receipt.pop('generation')
            phase['report']['acoustic_evidence']['response_acoustic_timing'] = receipt
        good = audit(data)
        self.assertTrue(good['verified'])
        self.assertEqual(
            good['latencies_ms']['last_voiced_to_confirmed_output_block_ms']['samples'], 30
        )
        data['phases'][7]['report']['acoustic_evidence']['response_acoustic_timing'][
            'window_pair'
        ].pop('prefix_offset_ms')
        bad = audit(data)
        self.assertFalse(bad['verified'])
        self.assertEqual(bad['completed_phases'], 7)

    def test_actual_owner_socket_carries_only_correlated_current_pair(self):
        from bridge import Bridge
        from announce import AnnouncementServer
        from pathlib import Path
        import tempfile
        from device_smoke import rpc, summarize_response_acoustic
        from test_barge_in import Sink

        clock = Clock()
        with (
            patch('playback_evidence.threading.Timer', clock.timer),
            tempfile.TemporaryDirectory() as folder,
        ):
            bridge = Bridge(None, None)
            bridge.session.active = True
            bridge.session.wake = True
            bridge.session.awake = False
            bridge.session.epoch = 7
            bridge.session.connection = Sink()
            bridge.session.generation = 101
            bridge.save_status = lambda: None
            path = Path(folder) / 'probe.sock'
            server = AnnouncementServer(bridge, path)
            server.start()
            try:
                arm = rpc(path, dict(operation='response_probe_arm', case=1))
                self.assertEqual(arm['status'], 'armed')
                bridge.capture.bind_response_probe('一加一等于几？', 101, START - 600_000_000)
                bridge.capture.begin_response_output(101)
                ref = public_signal()
                ref[:3200] = 0.12 * np.sin(np.arange(3200) * 2 * np.pi * 400 / RATE)
                ref[:640] = public_signal()[:640] * 0.18
                feed(bridge.capture.evidence, ref, scene(ref))
                request = dict(operation='response_probe_result', token=arm['token'])
                result = summarize_response_acoustic(rpc(path, request), 101)
                self.assertEqual(result['status'], 'observed')
                self.assertEqual(result['alignment'], 'matched_contiguous_windows')
                self.assertEqual(len(result['window_pair']['window_correlations']), 2)
                self.assertEqual(rpc(path, request)['status'], 'unavailable')
                self.assertFalse(bridge.capture.evidence.mic.size)
            finally:
                server.close()
                bridge.capture.clear_response_probe()


if __name__ == '__main__':
    unittest.main()

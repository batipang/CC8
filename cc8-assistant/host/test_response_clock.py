"""A long interval needs a bounded, continuous same-capture witness."""

import unittest

from input_quality import InputQuality
from response_clock import interval_clock
from device_smoke import (
    ACOUSTIC_RESPONSE_METRIC,
    observed_response_interval,
    summarize_response_acoustic,
    acoustic_coverage_reason,
)


class ClockReceiptTests(unittest.TestCase):
    def receipt(self):
        return {
            'status': 'observed',
            'generation': 8,
            ACOUSTIC_RESPONSE_METRIC: 30000,
            'interval_clock': {'version': 1, 'coverage_ms': 40000, 'discontinuities': 0},
        }

    def test_long_receipt_requires_matching_generation_and_preserves_bounded_proof(self):
        raw = self.receipt()
        result = summarize_response_acoustic(raw, 8)
        self.assertEqual(observed_response_interval(result), 30000)
        self.assertEqual(result, {k: v for k, v in raw.items() if k != 'generation'})
        raw['interval_clock']['coverage_ms'] = 1
        self.assertEqual(result['interval_clock']['coverage_ms'], 40000)
        self.assertEqual(
            summarize_response_acoustic(self.receipt(), 9),
            {'status': 'unconfirmed', 'reason': 'generation_mismatch'},
        )

    def test_missing_or_malformed_proof_never_enlarges_the_legacy_range(self):
        for bad in (
            None,
            {},
            {'version': True, 'coverage_ms': 40000, 'discontinuities': 0},
            {'version': 1, 'coverage_ms': 29997, 'discontinuities': 0},
            {'version': 1, 'coverage_ms': float('nan'), 'discontinuities': 0},
            {'version': 1, 'coverage_ms': float('inf'), 'discontinuities': 0},
            {'version': 1, 'coverage_ms': 120003, 'discontinuities': 0},
            {'version': 1, 'coverage_ms': 40000, 'discontinuities': False},
            {'version': 1, 'coverage_ms': 40000, 'discontinuities': 1},
            {'version': 1, 'coverage_ms': 40000, 'discontinuities': 0, 'private_text': 'discard'},
        ):
            with self.subTest(bad=bad):
                value = self.receipt()
                value['interval_clock'] = bad
                self.assertIsNone(observed_response_interval(value))
                self.assertEqual(
                    summarize_response_acoustic(value, 8),
                    {'status': 'unconfirmed', 'reason': 'invalid_interval'},
                )
        raw = self.receipt()
        raw.pop('interval_clock')
        self.assertIsNone(observed_response_interval(raw))

    def test_endpoints_and_nonfinite_intervals_stay_bounded(self):
        for elapsed, coverage, valid in (
            (20000, None, True),
            (20000.001, 22000, True),
            (120000, 120000, True),
            (120000.001, 120000, False),
            (-1, 40000, False),
            (True, 40000, False),
            (float('nan'), 40000, False),
            (float('inf'), 40000, False),
            (30000, 29998, True),
        ):
            with self.subTest(elapsed=elapsed, coverage=coverage):
                value = self.receipt()
                value[ACOUSTIC_RESPONSE_METRIC] = elapsed
                if coverage is None:
                    value.pop('interval_clock')
                else:
                    value['interval_clock']['coverage_ms'] = coverage
                self.assertEqual(observed_response_interval(value) is not None, valid)
        self.assertIsNone(interval_clock(self.receipt()['interval_clock'], 20000))

    def test_unsupported_or_corrupt_intervals_do_not_become_coverage_only_misses(self):
        for value in (
            {'status': 'unconfirmed', 'reason': 'interval_out_of_bounds'},
            {'status': 'unconfirmed', 'reason': 'capture_clock_gap'},
            {'status': 'unconfirmed', 'reason': 'input_clock_unavailable'},
        ):
            self.assertIsNone(acoustic_coverage_reason(value))


class InputClockProofTests(unittest.TestCase):
    def setUp(self):
        self.reset_clock()

    def reset_clock(self):
        self.quality = InputQuality()
        self.quality.reset(enabled=True)
        self.nanos = 1_000_000_000
        self.packet()
        self.mark = self.quality.clock_mark()
        self.start = self.nanos

    def packet(self, shift=0, discontinuous=False):
        self.nanos += shift
        self.quality.observe(
            bytes(640),
            self.quality.token(),
            paired=False,
            nanos=self.nanos,
            discontinuous=discontinuous,
        )
        self.nanos += 20_000_000

    def test_proof_covers_only_continuous_received_samples_and_no_waveform(self):
        for _ in range(20):
            self.packet()
        proof = self.quality.prove_interval(
            self.mark, self.start + 100_000_000, self.start + 300_000_000
        )
        self.assertEqual(proof, {'version': 1, 'coverage_ms': 400.0, 'discontinuities': 0})
        self.assertIsNone(self.quality.prove_interval(self.mark, self.start - 1, self.start))
        self.assertIsNone(self.quality.prove_interval(self.mark, self.start, self.nanos + 1))
        self.assertIsNone(self.quality.prove_interval(self.mark, True, self.nanos))

    def test_packet_gap_clock_gap_and_legacy_read_gap_cannot_prove_an_interval(self):
        for kind in ('sequence', 'clock', 'read'):
            with self.subTest(kind=kind):
                self.reset_clock()
                if kind == 'read':
                    self.quality.break_run(self.quality.token())
                self.packet(
                    shift=3_000_000 if kind == 'clock' else 0, discontinuous=kind == 'sequence'
                )
                self.assertIsNone(self.quality.prove_interval(self.mark, self.start, self.nanos))

    def test_small_per_packet_drift_cannot_accumulate_into_a_long_clock_proof(self):
        for _ in range(20):
            self.packet(shift=1_000_000)
        self.assertEqual(self.quality.snapshot()['discontinuities'], 0)
        self.assertIsNone(self.quality.prove_interval(self.mark, self.start, self.nanos))

    def test_clock_proof_cannot_extend_the_original_absolute_lease(self):
        for _ in range(6001):
            self.packet()
        self.assertIsNone(self.quality.prove_interval(self.mark, self.start, self.nanos))

    def test_expanded_native_range_requires_an_input_clock_and_keeps_active_twenty_second_limit(
        self,
    ):
        from playback_evidence import PlaybackEvidence

        for limit, stamp in ((True, 1), (20001, 1), (120000, None), (120000, True), (120000, -1)):
            with self.subTest(limit=limit, stamp=stamp):
                observer = PlaybackEvidence()
                with self.assertRaises(ValueError):
                    observer.arm(8, 2, input_end_ns=stamp, interval_limit_ms=limit)
                self.assertIsNone(observer.current)
        observer = PlaybackEvidence()
        with self.assertRaises(ValueError):
            observer.arm(8, 2, seconds=21, input_end_ns=1, interval_limit_ms=120000)
        self.assertIsNone(observer.current)

    def test_reset_and_close_revoke_marks_without_reusing_success(self):
        self.packet()
        old = self.mark
        self.quality.reset(enabled=True)
        self.packet()
        self.assertIsNone(self.quality.prove_interval(old, self.start, self.nanos))
        mark = self.quality.clock_mark()
        self.packet()
        self.quality.close()
        self.assertIsNone(self.quality.prove_interval(mark, self.start, self.nanos))
        self.assertIsNone(self.quality.clock_mark())
        self.assertIsNone(self.quality.prove_interval(None, self.start, self.nanos))


class ResponseSuiteClockTests(unittest.TestCase):
    def data(self, *, missing=()):
        from test_audit_device_suite import saved_suite

        data = saved_suite(qa=True, acoustic_coverage=True, missing_acoustic=missing)
        data['backend_version'] = '0.15.75'
        for index, phase in enumerate(data['phases'], start=1):
            if index not in missing:
                phase['report']['acoustic_evidence']['response_acoustic_timing'] = {
                    'status': 'observed',
                    ACOUSTIC_RESPONSE_METRIC: 30000,
                    'interval_clock': {'version': 1, 'coverage_ms': 40000, 'discontinuities': 0},
                }
        return data

    def test_audit_recomputes_thirty_long_samples_from_original_proof_not_saved_totals(self):
        from audit_device_suite import audit

        data = self.data()
        data['latencies_ms'][ACOUSTIC_RESPONSE_METRIC] = {'samples': 0, 'p50': None, 'p95': None}
        result = audit(data, expect_backend='0.15.75')
        self.assertTrue(result['verified'])
        self.assertEqual(result['completed_phases'], 30)
        self.assertEqual(
            result['latencies_ms'][ACOUSTIC_RESPONSE_METRIC],
            {'samples': 30, 'p50': 30000.0, 'p95': 30000.0},
        )
        self.assertFalse(result['full_p0_acceptance'])

    def test_audit_rejects_missing_changed_or_inadequate_proof_in_a_later_phase(self):
        from audit_device_suite import audit

        for bad in (
            None,
            {'version': 1, 'coverage_ms': 20000, 'discontinuities': 0},
            {'version': 1, 'coverage_ms': 40000, 'discontinuities': 1},
            {'version': True, 'coverage_ms': 40000, 'discontinuities': 0},
        ):
            with self.subTest(bad=bad):
                data = self.data()
                data['phases'][7]['report']['acoustic_evidence']['response_acoustic_timing'][
                    'interval_clock'
                ] = bad
                result = audit(data)
                self.assertFalse(result['verified'])
                self.assertEqual(result['completed_phases'], 7)

    def test_long_samples_do_not_turn_existing_acoustic_misses_into_a_pass(self):
        from audit_device_suite import audit

        result = audit(self.data(missing=(8,)))
        self.assertFalse(result['verified'])
        self.assertEqual(result['completed_phases'], 30)
        self.assertEqual(result['reason'], 'suite_qa_acoustic_coverage_incomplete')
        self.assertEqual(result['latencies_ms'][ACOUSTIC_RESPONSE_METRIC]['samples'], 29)
        self.assertEqual(
            result['response_acoustic_unconfirmed'], {'response_prefix_alignment_mismatch': 1}
        )


if __name__ == '__main__':
    unittest.main()

"""Run capture validates receipts, never stores conversation/provider bodies."""

from copy import deepcopy
import json
import unittest
from diagnostic_run import DiagnosticRun, RunCapacityError, receipt
from diagnostics import TraceBook


class DiagnosticRunTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.book = TraceBook(limit=2, clock=lambda: self.now)
        self.run = DiagnosticRun()
        self.run.ingest(self.book.report())

    def request(self, generation=100):
        ident = self.book.begin('voice', generation)
        self.book.mark(ident, 'asr_start')
        self.now += 0.2
        self.book.mark(ident, 'asr_end')
        return ident

    def test_repeated_progress_updates_are_one_sample_with_real_latency(self):
        ident = self.request()
        self.run.ingest(self.book.report())
        self.run.ingest(self.book.report())
        self.book.finish(ident, 'completed')
        self.run.ingest(self.book.report())
        report = self.run.report()
        self.assertEqual(report['summary']['samples'], 1)
        self.assertEqual(report['summary']['success_rate'], 1)
        self.assertEqual(
            report['summary']['latencies']['asr_ms'], {'samples': 1, 'p50': 200, 'p95': 200}
        )
        self.assertFalse(report['render_ack_is_audible_onset'])

    def test_asr_input_count_is_optional_bounded_and_immutable(self):
        ident = self.book.begin('voice')
        self.run.ingest(self.book.report())
        self.book.asr_start(ident, 51200)
        self.run.ingest(self.book.report())
        row = self.run.report()['requests'][0]
        self.assertEqual(row['asr_input_samples'], 51200)
        for changed in (
            dict(row, origin='text'),
            dict(row, stages={}),
            *(dict(row, asr_input_samples=count) for count in (True, -1, 208001, 'private')),
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'ASR input'):
                receipt(changed)
        for replacement in (None, 51201):
            snapshot = self.book.report()
            if replacement is None:
                snapshot['requests'][0].pop('asr_input_samples')
            else:
                snapshot['requests'][0]['asr_input_samples'] = replacement
            with self.assertRaisesRegex(ValueError, 'Conflicting diagnostic result'):
                self.run.ingest(snapshot)
        self.assertEqual(self.run.report()['requests'][0], row)

    def test_initial_inflight_and_completed_requests_remain_excluded(self):
        ident = self.request()
        run = DiagnosticRun()
        run.ingest(self.book.report())
        self.book.finish(ident, 'completed')
        run.ingest(self.book.report())
        self.assertEqual(run.report()['summary']['samples'], 0)
        self.assertEqual(run.report()['coverage']['baseline_excluded'], 1)

    def test_many_windows_keep_all_observed_requests_and_do_not_alias_exports(self):
        for generation in range(200):
            ident = self.request(generation)
            self.book.finish(ident, 'completed')
            self.run.ingest(self.book.report())
        report = self.run.report()
        self.assertEqual(report['summary']['samples'], 200)
        self.assertEqual(report['coverage']['window_discontinuities'], 0)
        report['requests'][0]['stages'].clear()
        report['requests'].clear()
        self.assertEqual(self.run.report()['summary']['samples'], 200)
        self.assertTrue(self.run.report()['requests'][0]['stages'])

    def test_disjoint_window_is_visible_and_never_claims_full_coverage(self):
        self.request()
        self.run.ingest(self.book.report())
        self.request(101)
        self.request(102)
        self.run.ingest(self.book.report())
        coverage = self.run.report()['coverage']
        self.assertEqual(coverage['window_discontinuities'], 1)
        self.assertFalse(coverage['complete_capture_asserted'])

    def test_task_completion_is_separate_from_cancelled_narration(self):
        ident = self.request()
        self.book.task_result(ident, 'completed')
        self.book.finish(ident, 'cancelled')
        self.run.ingest(self.book.report())
        report = self.run.report()
        self.assertEqual(report['task_outcomes'], {'completed': 1})
        self.assertEqual(report['summary']['outcomes'], {'cancelled': 1})

    def test_untrusted_content_and_invalid_enums_fail_before_state_changes(self):
        self.request()
        good = self.book.report()
        for field, value in [
            ('text', 'SECRET'),
            ('origin', {}),
            ('outcome', []),
            ('active_stage', {}),
            ('error', {'stage': [], 'kind': 'timeout'}),
        ]:
            with self.subTest(field=field):
                invalid = deepcopy(good)
                invalid['requests'][0][field] = value
                before = self.run.report()
                with self.assertRaises(ValueError):
                    self.run.ingest(invalid)
                self.assertEqual(self.run.report(), before)
        good['provider_response'] = 'SECRET'
        self.run.ingest(good)
        self.assertNotIn('SECRET', json.dumps(self.run.report()))

    def test_playout_pair_requires_matching_receipt_count_and_order(self):
        self.request()
        self.now = 1
        ack = self.book.mark(100, 'playout_ack')
        self.now = 2
        self.book.playback_done(100, confirmed_ack_ms=ack)
        good = self.book.report()
        for field, value in (
            ('confirmed_playout_ack', None),
            ('confirmed_playback_done', None),
            ('confirmed_playback_clauses', None),
            ('confirmed_playout_ack', 999),
            ('confirmed_playout_ack', 2001),
            ('confirmed_playback_done', 1999),
            ('confirmed_playback_done', True),
        ):
            with self.subTest(field=field, value=value):
                malformed = deepcopy(good)
                row = malformed['requests'][0]
                target = row if field == 'confirmed_playback_clauses' else row['stages']
                if value is None:
                    del target[field]
                else:
                    target[field] = value
                with self.assertRaises(ValueError):
                    self.run.ingest(malformed)
        self.run.ingest(good)
        self.assertEqual(self.run.report()['requests'], good['requests'])

    def test_invalid_elapsed_values_never_enter_statistics(self):
        self.request()
        for value in (True, -1, float('inf'), float('nan'), 10**1000, 'SECRET'):
            with self.subTest(value_type=type(value).__name__):
                invalid = self.book.report()
                invalid['requests'][0]['stages']['asr_end'] = value
                with self.assertRaises(ValueError):
                    self.run.ingest(invalid)
        self.assertEqual(self.run.report()['summary']['samples'], 0)

    def test_result_rewrite_or_stage_rewrite_is_atomic_failure(self):
        ident = self.request()
        self.book.finish(ident, 'completed')
        self.run.ingest(self.book.report())
        before = self.run.report()
        for field, value in [('outcome', 'failed'), ('stages', {'asr_start': 500})]:
            invalid = self.book.report()
            invalid['requests'][0][field] = value
            with self.assertRaises(ValueError):
                self.run.ingest(invalid)
            self.assertEqual(self.run.report(), before)

    def test_capacity_stops_without_evicting_or_partially_committing(self):
        run = DiagnosticRun(limit=1)
        run.ingest(self.book.report())
        self.request()
        run.ingest(self.book.report())
        before = run.report()
        self.request(101)
        with self.assertRaises(RunCapacityError):
            run.ingest(self.book.report())
        self.assertEqual(run.report(), before)

    def test_wrong_clock_flags_duplicate_ids_or_missing_fields_are_rejected(self):
        self.request()
        for key, value in [
            ('clock', 'device_clock'),
            ('contains_content', 0),
            ('render_ack_is_audible_onset', True),
            ('schema', True),
        ]:
            invalid = self.book.report()
            invalid[key] = value
            with self.assertRaises(ValueError):
                self.run.ingest(invalid)
        invalid = self.book.report()
        invalid['requests'] *= 2
        with self.assertRaises(ValueError):
            self.run.ingest(invalid)
        invalid = self.book.report()
        del invalid['requests'][0]['generation']
        with self.assertRaises(ValueError):
            self.run.ingest(invalid)


if __name__ == '__main__':
    unittest.main()

"""Timing receipt privacy, local clocks and compatibility of cumulative runs."""

from copy import deepcopy
import json
import unittest
from diagnostics import TraceBook
from diagnostic_run import DiagnosticRun
from dot_client import DotBrain, DotError
import test_failure_feedback as fixture
from test_dot_client import FakeRpc
from reply_types import Reply


class DotDiagnosticContractTests(unittest.TestCase):
    def test_invalid_clock_stage_or_context_is_rejected_atomically(self):
        now = [10.0]
        book = TraceBook(clock=lambda: now[0])
        book.begin('text', 1)
        now[0] = 11.0
        for stage, at, reused in (
            ('SECRET', 10.5, None),
            ('dot_rpc_start', 10.5, True),
            ('dot_rpc_ready', 10.5, None),
            ('dot_rpc_ready', 10.5, 'SECRET'),
            ('dot_rpc_ready', 9.9, False),
            ('dot_rpc_ready', 11.1, False),
            ('dot_rpc_ready', True, False),
            ('dot_rpc_ready', float('nan'), False),
            ('dot_rpc_ready', float('inf'), False),
            ('dot_rpc_ready', 10**1000, False),
        ):
            with self.subTest(stage=stage, at_type=type(at).__name__):
                before = book.report()
                with self.assertRaises(ValueError):
                    book.dot_stage(1, stage, at, reused)
                self.assertEqual(book.report(), before)
        book.dot_stage(1, 'dot_rpc_ready', 10.5, False)
        before = book.report()
        with self.assertRaises(ValueError):
            book.dot_stage(1, 'dot_rpc_ready', 10.7, True)
        self.assertEqual(book.report(), before)
        self.assertNotIn('SECRET', json.dumps(book.report()))

    def test_context_groups_remain_separate_and_legacy_is_unreported(self):
        now = [0.0]
        book = TraceBook(clock=lambda: now[0])
        run = DiagnosticRun()
        run.ingest(book.report())
        for generation, reused in ((1, False), (2, True), (3, None)):
            book.begin('text', generation)
            if reused is not None:
                book.dot_stage(generation, 'dot_rpc_start', now[0])
                now[0] += 0.2 if not reused else 0.01
                book.dot_stage(generation, 'dot_rpc_ready', now[0], reused)
                now[0] += 0.1 if not reused else 0.01
                book.dot_stage(generation, 'dot_context_ready', now[0], reused)
            book.finish(generation, 'completed')
            run.ingest(book.report())
        for report in (book.report(), run.report()):
            groups = report['dot_contexts']
            for group in groups.values():
                self.assertEqual(
                    [group[k]['samples'] for k in ('new', 'reused', 'unreported')], [1, 1, 1]
                )
            self.assertEqual(groups['rpc']['new']['latencies']['dot_rpc_prepare_ms']['p50'], 200)
            self.assertEqual(groups['rpc']['reused']['latencies']['dot_rpc_prepare_ms']['p50'], 10)
            self.assertEqual(
                groups['rpc']['unreported']['latencies']['dot_rpc_prepare_ms']['samples'], 0
            )

    def test_run_rejects_invalid_missing_or_rewritten_context_fields(self):
        book = TraceBook(clock=lambda: 0.0)
        run = DiagnosticRun()
        run.ingest(book.report())
        book.begin('text', 1)
        book.dot_stage(1, 'dot_rpc_ready', 0.0, False)
        book.dot_stage(1, 'dot_context_ready', 0.0, False)
        good = book.report()
        run.ingest(good)
        before = run.report()
        for edit in (
            lambda r: r.update(dot_context_reused='SECRET'),
            lambda r: r.update(dot_rpc_reused=0),
            lambda r: r.update(dot_rpc_reused=True),
            lambda r: r.pop('dot_context_reused'),
            lambda r: r['stages'].pop('dot_rpc_ready'),
        ):
            invalid = deepcopy(good)
            edit(invalid['requests'][0])
            with self.assertRaises(ValueError):
                run.ingest(invalid)
            self.assertEqual(run.report(), before)

    def test_completion_only_protocol_never_fabricates_first_delta(self):
        case = fixture.FailureFeedbackTests()
        bridge = case.session(brain=DotBrain(FakeRpc()))
        bridge.speak = lambda *args: True
        case.run_one(bridge.response, Reply(bridge.generation, '固定公开测试'))
        report = bridge.diagnostics.report()
        self.assertNotIn('dot_first_delta', report['requests'][0]['stages'])
        self.assertEqual(report['groups']['text']['latencies']['dot_first_delta_ms']['samples'], 0)
        self.assertEqual(
            report['groups']['text']['latencies']['dot_turn_complete_ms']['samples'], 1
        )

    def test_prepare_failure_has_no_fake_ready_or_context(self):
        class FailedRpc(FakeRpc):
            def start(self):
                raise DotError('SECRET', kind='timeout')

        case = fixture.FailureFeedbackTests()
        bridge = case.session(brain=DotBrain(FailedRpc()))
        case.run_one(bridge.response, Reply(bridge.generation, '固定公开测试'))
        row = bridge.diagnostics.report()['requests'][0]
        self.assertIn('dot_rpc_start', row['stages'])
        self.assertNotIn('dot_rpc_ready', row['stages'])
        self.assertNotIn('dot_context_reused', row)
        self.assertNotIn('SECRET', json.dumps(bridge.diagnostics.report()))

    def test_arbitrary_timing_payload_cannot_enter_report(self):
        class Brain(fixture.Brain):
            def clauses(self, text, history, current, on_web):
                on_web('brain_stage', {'stage': 'dot_rpc_start', 'at': 0, 'text': 'SECRET'})
                yield 'unreachable'

        case = fixture.FailureFeedbackTests()
        bridge = case.session(brain=Brain())
        case.run_one(bridge.response, Reply(bridge.generation, '固定公开测试'))
        row = bridge.diagnostics.report()['requests'][0]
        self.assertNotIn('dot_rpc_start', row['stages'])
        self.assertEqual(row['outcome'], 'failed')
        self.assertNotIn('SECRET', json.dumps(bridge.diagnostics.report()))


if __name__ == '__main__':
    unittest.main()

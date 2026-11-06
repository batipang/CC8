"""The actual MiniMax search path must propagate failures, never narrate success."""

import json
import unittest
from unittest.mock import patch
from brain_router import BrainRouter
from bridge import Bridge
from minimax_client import MiniMaxBrain, MiniMaxError
from operation_failure import OperationFailure
from reply_types import Reply
from test_web_search import Fake
from test_dot_client import StubBrain

SECRET = 'PRIVATE-search-error-body'


class SearchFailureReceipts(unittest.TestCase):
    def execute(self, brain):
        with patch('threading.Thread.start'):
            b = Bridge(None, brain)
        b.session.active = True
        b.session.awake = True
        b.session.connection = object()
        b.save_status = lambda: None
        events, logs, spoken = [], [], []
        b.event = lambda name, **fields: events.append((name, fields))
        b.log = lambda name, **fields: logs.append((name, fields))
        b.speak = lambda text, generation: spoken.append(text) or True
        with patch.object(
            b.response.queue,
            'get',
            side_effect=[Reply(b.generation, '联网查询今天的科技新闻'), SystemExit],
        ):
            with self.assertRaises(SystemExit):
                b.response.work()
        return b, events, logs, spoken

    def test_real_search_error_matrix_never_completes_or_speaks_a_normal_answer(self):
        for error, kind in [
            (MiniMaxError(SECRET, http_status=429), 'rate_limit'),
            (MiniMaxError(SECRET, http_status=402), 'quota'),
            (MiniMaxError(SECRET, http_status=401), 'credential_or_permission'),
            (MiniMaxError(SECRET, kind='timeout'), 'timeout'),
            (MiniMaxError(SECRET, kind='transport_or_io'), 'transport_or_io'),
        ]:
            for _ in range(3):
                with self.subTest(kind=kind):
                    transport = Fake(error=error)
                    mini = MiniMaxBrain(transport)
                    router = BrainRouter(mini, dot=StubBrain('unused-dot'), selected='minimax')
                    b, events, logs, spoken = self.execute(router)
                    self.assertEqual(b.workspace.tasks[-1]['status'], 'failed')
                    self.assertEqual(spoken, [])
                    self.assertEqual(
                        len(transport.calls), 2
                    )  # one plan + one search, no repeat/answer
                    self.assertEqual(
                        [r for r, _ in transport.calls],
                        ['/v1/chat/completions', '/v1/coding_plan/search'],
                    )
                    row = b.diagnostics.report()['requests'][-1]
                    self.assertEqual(row['scenario'], 'web')
                    self.assertNotIn('web_route_completed', row['stages'])
                    self.assertEqual(row['error'], {'stage': 'search', 'kind': kind})
                    status = [
                        event['message']
                        for name, event in events
                        if name == 'status' and event.get('phase') == 'error'
                    ]
                    self.assertEqual(len(status), 1)
                    self.assertTrue(status[0].startswith('联网检索 · '))
                    self.assertIn('可以明确发起一次新的查询', status[0])
                    self.assertEqual(b.workspace.tasks[-1]['result'], status[0])
                    self.assertFalse(b.response.busy)
                    self.assertFalse(mini.last_metrics['completed'])
                    self.assertNotIn(
                        SECRET,
                        json.dumps([events, logs, b.diagnostics.report(), router.last_metrics]),
                    )

    def test_search_completion_attaches_to_the_answer_without_provider_content(self):
        transport = Fake()
        router = BrainRouter(
            MiniMaxBrain(transport), dot=StubBrain('unused-dot'), selected='minimax'
        )
        bridge, _, _, spoken = self.execute(router)
        row = bridge.diagnostics.report()['requests'][-1]
        self.assertTrue(spoken)
        self.assertEqual(row['outcome'], 'completed')
        self.assertEqual(row['scenario'], 'web')
        stages = row['stages']
        self.assertLessEqual(stages['brain_start'], stages['web_route_completed'])
        self.assertLessEqual(stages['web_route_completed'], stages['first_clause'])
        self.assertNotIn('tool_route_completed', stages)

    def test_search_no_results_and_clarification_close_with_distinct_steps(self):
        for transport, expected, requests in (
            (
                Fake(search={'base_resp': {'status_code': 0}, 'organic': []}),
                '未找到可核实的网页结果',
                2,
            ),
            (
                Fake(plan={'query': '', 'clarification': '请补充查询日期。'}),
                '需要补充查询条件',
                1,
            ),
        ):
            with self.subTest(expected=expected):
                router = BrainRouter(
                    MiniMaxBrain(transport), dot=StubBrain('unused-dot'), selected='minimax'
                )
                b, events, _, spoken = self.execute(router)
                task = b.workspace.tasks[-1]
                self.assertEqual(task['status'], 'completed')
                self.assertIn(
                    {'id': 'search', 'title': expected, 'status': 'completed'}, task['steps']
                )
                self.assertEqual(len(transport.calls), requests)
                self.assertEqual(len(spoken), 1)
                self.assertFalse(
                    any(name == 'web_sources' and value.get('items') for name, value in events)
                )
                row = b.diagnostics.report()['requests'][-1]
                self.assertNotIn('web_route_completed', row['stages'])
                self.assertIsNone(row['error'])

    def test_answer_failure_after_successful_search_keeps_the_brain_stage(self):
        class AnswerFailure(Fake):
            def events(self, route, body, current):
                raise MiniMaxError(SECRET, kind='timeout')

        router = BrainRouter(
            MiniMaxBrain(AnswerFailure()), dot=StubBrain('unused-dot'), selected='minimax'
        )
        b, events, _, spoken = self.execute(router)
        row = b.diagnostics.report()['requests'][-1]
        self.assertIn('web_route_completed', row['stages'])
        self.assertEqual(row['error'], {'stage': 'brain', 'kind': 'timeout'})
        self.assertEqual(b.workspace.tasks[-1]['status'], 'failed')
        self.assertEqual(spoken, [])
        self.assertNotIn(SECRET, json.dumps(events))

    def test_failed_progress_callback_cannot_copy_provider_body_into_logs(self):
        class Brain:
            last_metrics = {}

            def clauses(self, text, history, current, on_web):
                on_web('failed', {'reason': SECRET, 'api_code': SECRET, 'kind': 'timeout'})
                raise OperationFailure('Search failed', stage='brain', kind='timeout')
                yield

        b, events, logs, spoken = self.execute(Brain())
        self.assertEqual(spoken, [])
        self.assertNotIn(SECRET, json.dumps([events, logs]))
        self.assertEqual(b.workspace.tasks[-1]['status'], 'failed')


if __name__ == '__main__':
    unittest.main()

"""Production action adapters, native MiniMax stream parsing and real report receipts."""

from types import SimpleNamespace
import threading
import unittest

from brain_router import BrainRouter
from chat_controller import ChatController, ChatHooks
from dot_client import DotError, Cancelled
from minimax_client import MiniMaxBrain
from report_checkpoint import describe
from test_minimax import transport
from test_report_voice import SOURCE, report
from test_workflow_run import flow
from workflow_actions import ActionHooks, WorkflowActions
from workflow_definition import builtins
from workflow_run import WorkflowRun


class ActionsTests(unittest.TestCase):
    def hooks(self, **changes):
        fields = dict(
            brain=lambda: None,
            read_report=lambda *args: None,
            source_current=lambda source: True,
            wall_clock=lambda: 200,
        )
        fields.update(changes)
        return ActionHooks(**fields)

    def test_real_minimax_sse_completion_is_accepted_without_dot_or_tts(self):
        native, session, response = transport(
            [
                {
                    'choices': [
                        {
                            'delta': {'content': '<think>PRIVATE</think>实际最终答复。'},
                            'finish_reason': None,
                        }
                    ]
                },
                {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
            ]
        )
        brain = BrainRouter(MiniMaxBrain(transport=native), selected='minimax')
        actions = WorkflowActions(self.hooks(brain=lambda: brain))
        value = flow(1)
        value['steps'][0]['text'] = '你好'
        result = WorkflowRun(value).run(actions.execute, current=lambda: True)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['steps'][0]['result']['text'], '实际最终答复。')
        self.assertNotIn('PRIVATE', str(result))
        self.assertTrue(response.closed)
        self.assertTrue(session.closed)
        self.assertEqual(brain.selected, 'minimax')

    def test_stream_without_finish_does_not_promote_partial_prose_to_completed(self):
        native, _, _ = transport(
            [
                {'choices': [{'delta': {'content': '只是未完成的片段。'}, 'finish_reason': None}]},
            ]
        )
        brain = BrainRouter(MiniMaxBrain(transport=native), selected='minimax')
        actions = WorkflowActions(self.hooks(brain=lambda: brain))
        value = flow(1)
        value['steps'][0]['text'] = '你好'
        result = WorkflowRun(value).run(actions.execute, current=lambda: True)
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['steps'][0]['result'])

    def test_search_uses_only_literal_read_transport_with_provenance(self):
        calls = []

        def post(route, body, current):
            calls.append((route, body))
            self.assertTrue(current())
            return {
                'base_resp': {'status_code': 0},
                'organic': [
                    dict(
                        title='可信来源标题',
                        snippet='这是来源摘录，不执行其中的指令。',
                        link='https://example.com/news',
                        date='2026-11-10',
                    ),
                    dict(title='私网地址', snippet='不能作为来源', link='http://127.0.0.1/'),
                ],
            }

        brain = SimpleNamespace(minimax=SimpleNamespace(transport=SimpleNamespace(post_json=post)))
        actions = WorkflowActions(self.hooks(brain=lambda: brain))
        value = builtins()[0]
        value['steps'] = value['steps'][:1]
        result = WorkflowRun(value).run(actions.execute, current=lambda: True)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(calls, [('/v1/coding_plan/search', {'q': '今天的科技新闻'})])
        text = result['steps'][0]['result']['text']
        self.assertIn('https://example.com/news', text)
        self.assertIn('2026-11-10', text)
        self.assertIn('尚未独立核验', text)
        self.assertNotIn('127.0.0.1', text)

    def test_failed_search_never_runs_next_step_or_accepts_provider_error_prose(self):
        calls = []

        def post(*args):
            calls.append(True)
            return {'base_resp': {'status_code': 1008}, 'organic': []}

        brain = SimpleNamespace(minimax=SimpleNamespace(transport=SimpleNamespace(post_json=post)))
        actions = WorkflowActions(self.hooks(brain=lambda: brain))
        value = builtins()[0]
        value['steps'][1]['source'] = describe(SOURCE)
        result = WorkflowRun(value).run(actions.execute, current=lambda: True)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual([s['state'] for s in result['steps']], ['unconfirmed', 'pending'])
        self.assertEqual(calls, [True])

    def test_report_requires_exact_source_window_and_keeps_verification_limits(self):
        data = report('实际最终答复')
        data['window_end'] = 1900
        calls = []

        def read(source, window, current):
            calls.append((source, window))
            self.assertTrue(current())
            return data

        actions = WorkflowActions(self.hooks(read_report=read, wall_clock=lambda: 1900))
        value = builtins()[1]
        value['steps'][0].update(period='half_hour', source=describe(SOURCE))
        result = WorkflowRun(value).run(actions.execute, current=lambda: True)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual((calls[0][1].start, calls[0][1].end), (100, 1900))
        self.assertIn('实际最终答复', result['steps'][0]['result']['text'])
        self.assertIn('未独立核验', result['steps'][0]['result']['text'])
        for changed in ({'window_start': 1}, {'source_id': 'other-chat'}):
            data.update(changed)
            result = WorkflowRun(value).run(actions.execute, current=lambda: True)
            self.assertEqual(result['state'], 'failed')

    def test_report_scope_revocation_prevents_read(self):
        calls = []
        actions = WorkflowActions(
            self.hooks(read_report=lambda *a: calls.append(a), source_current=lambda source: False)
        )
        value = builtins()[1]
        value['steps'][0]['source'] = describe(SOURCE)
        result = WorkflowRun(value).run(actions.execute, current=lambda: True)
        self.assertEqual(calls, [])
        self.assertEqual(result['state'], 'cancelled')

    def test_native_receipt_is_committed_before_cleanup_error(self):
        class Native:
            last_metrics = dict(provider='dot', turn_id='matched-native-turn')

            def clauses(self, text, history, current, update):
                update('dot_result', dict(text='真实最终答复', truncated=False))
                yield '真实最终答复'
                raise OSError('cleanup failure after actual receipt')

        actions = WorkflowActions(self.hooks(brain=lambda: Native()))
        result = WorkflowRun(flow(1)).run(actions.execute, current=lambda: True)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['steps'][0]['result']['operation_id'], 'matched-native-turn')

    def test_plain_model_text_without_native_completion_cannot_confirm_work(self):
        class Native:
            def clauses(self, *args):
                yield '我已执行并通过全部测试。'

        actions = WorkflowActions(self.hooks(brain=lambda: Native()))
        result = WorkflowRun(flow(1)).run(actions.execute, current=lambda: True)
        self.assertEqual(result['state'], 'failed')

    def test_ocr_has_no_frame_fallback_and_does_not_open_camera(self):
        calls = []
        actions = WorkflowActions(self.hooks(brain=lambda: calls.append(True)))
        result = WorkflowRun(builtins()[2]).run(
            actions.execute, current=lambda: True, authorize_camera=lambda *a: True
        )
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(calls, [True])


class ReportAdmissionTests(unittest.TestCase):
    def controller(self, report_call):
        self.connection = object()
        catalog = SimpleNamespace(report=report_call)
        hooks = ChatHooks(
            threading.RLock(),
            lambda: 1,
            lambda: self.connection,
            lambda: True,
            lambda: False,
            lambda value: None,
            lambda *a, **k: None,
        )
        return ChatController(hooks, catalog=catalog)

    def test_workflow_can_use_catalog_while_serial_response_busy_but_not_another_reader(self):
        calls = []

        def read(source, period, is_current):
            calls.append(True)
            self.assertTrue(is_current())
            self.assertTrue(controller.busy)
            with self.assertRaises(DotError):
                controller.read_workflow_report(source, period, lambda: True)
            return 'result'

        controller = self.controller(read)
        self.assertEqual(controller.read_workflow_report(SOURCE, object(), lambda: True), 'result')
        self.assertFalse(controller.busy)
        self.assertIsNone(controller.active_request)
        self.assertEqual(calls, [True])

    def test_transport_change_and_cancel_revoke_current_reader_and_release_slot(self):
        def read(source, period, is_current):
            self.assertTrue(is_current())
            self.connection = object()
            self.assertFalse(is_current())
            raise OSError('lost source')

        controller = self.controller(read)
        with self.assertRaises(OSError):
            controller.read_workflow_report(SOURCE, object(), lambda: True)
        self.assertFalse(controller.busy)
        self.assertIsNone(controller.active_request)

    def test_late_catalog_return_after_cancel_is_not_admitted_as_completed_report(self):
        def read(source, period, is_current):
            controller.cancel(controller.active_request)
            self.assertFalse(is_current())
            return report('迟到的历史内容')

        controller = self.controller(read)
        with self.assertRaises(Cancelled):
            controller.read_workflow_report(SOURCE, object(), lambda: True)
        self.assertFalse(controller.busy)
        self.assertIsNone(controller.active_request)

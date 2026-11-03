"""Real Dot parser/worker receipts must not wait behind speech consumption."""

import io
import json
import threading
from types import SimpleNamespace
import unittest
import numpy as np
from dot_client import DotBrain, CodexRpc, DotError, DotApprovalRequired
from minimax_client import MiniMaxError
from reply_types import Reply
from test_dot_stream import StreamRpc
import test_failure_feedback as fixture
import test_vision_failure_feedback as vision_fixture


class ReceivingRpc(StreamRpc, CodexRpc):
    def __init__(self):
        super().__init__()
        # Real notification admission and queue, with fixed offline replies.
        CodexRpc.__init__(self)

    def request(self, method, params, timeout=15):
        if method == 'turn/start':
            self.calls.append((method, params))
        return super().request(method, params, timeout)

    def event(self, method, **fields):
        getattr(self, '_receive', self.events.put_nowait)(
            {
                'method': method,
                'params': {'threadId': 'voice-thread', 'turnId': 'stream-turn', **fields},
            }
        )


class ReceiptDeliveryTests(unittest.TestCase):
    def suspended(self, rpc, updates, current=lambda: True):
        brain = DotBrain(rpc)
        stream = brain.clauses('固定测试', [], current, lambda k, v: updates.append((k, v)))
        self.assertEqual(next(stream), '这是第一句。')
        return brain, stream

    def test_authoritative_receipt_arrives_while_first_clause_is_suspended(self):
        rpc = ReceivingRpc()
        updates = []
        brain = DotBrain(rpc)
        stream = brain.clauses('固定测试', [], on_web=lambda k, v: updates.append((k, v)))
        try:
            self.assertEqual(next(stream), '这是第一句。')
            rpc.complete()
            results = [v for k, v in updates if k == 'dot_result']
            self.assertEqual([r['text'] for r in results], ['这是第一句。这是第二句。'])
            self.assertEqual(''.join(stream), '这是第二句。')
            self.assertEqual(sum(k == 'dot_result' for k, _ in updates), 1)
        finally:
            stream.close()
        self.assertFalse(any(k == 'turn/interrupt' for k, _ in rpc.calls))

    def test_tts_failure_after_queued_completion_keeps_task_receipt(self):
        for error in (
            TimeoutError(),
            ConnectionError(),
            MiniMaxError('fixed', http_status=429),
            MiniMaxError('fixed', http_status=402),
            MiniMaxError('fixed', http_status=403),
        ):
            with self.subTest(error=type(error).__name__):
                rpc = ReceivingRpc()
                calls = []

                class Speech:
                    def synthesize(self, text):
                        calls.append(text)
                        rpc.complete()
                        raise error

                case = fixture.FailureFeedbackTests()
                b = case.session(Speech(), DotBrain(rpc))
                case.run_one(b.response, Reply(b.generation, '固定测试'))
                self.assertEqual(len(calls), 1)
                self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
                self.assertEqual(b.workspace.tasks[-1]['result'], '这是第一句。这是第二句。')
                row = b.diagnostics.report()['requests'][-1]
                self.assertEqual(row['task_outcome'], 'completed')
                self.assertEqual(row['outcome'], 'failed')
                self.assertEqual(row['error']['stage'], 'tts')
                self.assertFalse(any(k == 'turn/interrupt' for k, _ in rpc.calls))

    def test_streaming_tts_failure_and_session_invalidation_keep_completed_work(self):
        rpc = ReceivingRpc()

        class Speech:
            def stream(self, text, current):
                yield np.ones(3200, dtype=np.int16)
                rpc.complete()
                raise TimeoutError()

        case = fixture.FailureFeedbackTests()
        b = case.session(Speech(), DotBrain(rpc))
        b.send = lambda packet: None
        generation = b.generation
        case.run_one(b.response, Reply(generation, '固定测试'))
        self.assertGreater(b.generation, generation)
        self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
        row = b.diagnostics.report()['requests'][-1]
        self.assertEqual(row['task_outcome'], 'completed')
        self.assertEqual(row['error']['stage'], 'tts')

    def test_vision_router_preserves_real_result_when_first_speech_fails(self):
        rpc = ReceivingRpc()

        class Speech:
            def synthesize(self, text):
                rpc.complete()
                raise TimeoutError()

        case = vision_fixture.VisionFailureTests()
        b = case.bridge(DotBrain(rpc))
        b.speech = Speech()
        case.capture_events(b)
        case.run_visual(b)
        self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
        self.assertEqual(b.workspace.tasks[-1]['result'], '这是第一句。这是第二句。')
        self.assertEqual(b.diagnostics.report()['requests'][-1]['task_outcome'], 'completed')
        self.assertTrue(
            any(
                i['type'] == 'image'
                for method, p in rpc.calls
                if method == 'turn/start'
                for i in p['input']
            )
        )

    def test_stop_after_receipt_cannot_downgrade_task_or_emit_more_speech(self):
        rpc = ReceivingRpc()
        case = fixture.FailureFeedbackTests()
        b = case.session(brain=DotBrain(rpc))
        calls = []

        def speak(text, generation):
            calls.append(text)
            rpc.complete()
            b.cancel('speech')
            return False

        b.speak = speak
        case.run_one(b.response, Reply(b.generation, '固定测试'))
        self.assertEqual(calls, ['这是第一句。'])
        self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')

    def test_raw_rpc_reader_admits_completion_before_speech_failure(self):
        rpc = ReceivingRpc()

        class Speech:
            def synthesize(self, text):
                messages = [
                    {
                        'method': 'item/completed',
                        'params': {
                            'threadId': 'voice-thread',
                            'turnId': 'stream-turn',
                            'item': {
                                'id': 'final',
                                'type': 'agentMessage',
                                'phase': 'final_answer',
                                'text': '这是第一句。这是第二句。',
                            },
                        },
                    },
                    {
                        'method': 'turn/completed',
                        'params': {
                            'threadId': 'voice-thread',
                            'turn': {'id': 'stream-turn', 'status': 'completed'},
                        },
                    },
                ]
                rpc.process = SimpleNamespace(
                    stdout=io.BytesIO(('\n'.join(json.dumps(m) for m in messages) + '\n').encode())
                )
                CodexRpc._read(rpc)
                raise TimeoutError()

        case = fixture.FailureFeedbackTests()
        b = case.session(Speech(), DotBrain(rpc))
        case.run_one(b.response, Reply(b.generation, '固定测试'))
        self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
        self.assertEqual(b.diagnostics.report()['requests'][-1]['task_outcome'], 'completed')

    def test_failed_changed_commentary_or_oversized_final_never_commits(self):
        for text, status, phase in (
            ('这是第一句。', 'failed', 'final_answer'),
            ('改过的内容。', 'completed', 'final_answer'),
            ('这是第一句。', 'completed', 'commentary'),
            ('x' * 64001, 'completed', 'final_answer'),
        ):
            with self.subTest(status=status, phase=phase, size=len(text)):
                rpc, updates = ReceivingRpc(), []
                _, stream = self.suspended(rpc, updates)
                try:
                    rpc.event(
                        'item/completed',
                        item={'id': 'final', 'type': 'agentMessage', 'phase': phase, 'text': text},
                    )
                    rpc.event('turn/completed', turn={'id': 'stream-turn', 'status': status})
                    self.assertFalse(any(k == 'dot_result' for k, _ in updates))
                    with self.assertRaises(DotError):
                        list(stream)
                finally:
                    stream.close()

    def test_scope_duplicates_and_completion_before_turn_ack(self):
        class EarlyRpc(ReceivingRpc):
            def request(self, method, params, timeout=15):
                result = super().request(method, params, timeout)
                if method == 'turn/start':
                    self.event('item/agentMessage/delta', itemId='final', delta='这是第二句。')
                    self.complete()
                return result

        rpc, updates = EarlyRpc(), []
        brain = DotBrain(rpc)
        self.assertEqual(
            ''.join(brain.clauses('固定测试', [], on_web=lambda k, v: updates.append((k, v)))),
            '这是第一句。这是第二句。',
        )
        self.assertEqual(
            [v['text'] for k, v in updates if k == 'dot_result'], ['这是第一句。这是第二句。']
        )
        self.assertEqual(len(getattr(rpc, 'listeners', ())), 0)

        rpc, updates = ReceivingRpc(), []
        _, stream = self.suspended(rpc, updates)
        try:
            for fields in ({'threadId': 'other'}, {'turn': {'id': 'other', 'status': 'completed'}}):
                rpc.event(
                    'turn/completed',
                    **{'turn': {'id': 'stream-turn', 'status': 'completed'}, **fields},
                )
            self.assertFalse(any(k == 'dot_result' for k, _ in updates))
            rpc.complete()
            rpc.complete()
            self.assertEqual(sum(k == 'dot_result' for k, _ in updates), 1)
        finally:
            stream.close()
        self.assertEqual(len(getattr(rpc, 'listeners', ())), 0)

    def test_cancel_before_completion_rejects_late_receipt(self):
        rpc, updates, live = ReceivingRpc(), [], [True]
        brain, stream = self.suspended(rpc, updates, lambda: live[0])
        live[0] = False
        brain.cancel()
        rpc.complete()
        stream.close()
        self.assertFalse(any(k == 'dot_result' for k, _ in updates))
        self.assertEqual(len(getattr(rpc, 'listeners', ())), 0)

    def test_completed_turn_cannot_execute_a_late_tool_while_audio_is_pending(self):
        rpc, updates = ReceivingRpc(), []
        brain, stream = self.suspended(rpc, updates)
        turn = brain.current
        actions = []
        brain.device_control = lambda action, percent: actions.append(action) or {'ok': True}
        try:
            rpc.complete()
            result = brain.call_tool(
                turn,
                {
                    'threadId': 'voice-thread',
                    'turnId': 'stream-turn',
                    'tool': 'cc8_control',
                    'arguments': {'action': 'status'},
                },
            )
            self.assertEqual(result, {'ok': False, 'error': 'stale_turn'})
            self.assertEqual(actions, [])
        finally:
            stream.close()

    def test_final_item_without_terminal_receipt_and_conflicting_terminals_do_not_commit(self):
        for first_status in (None, 'failed', 'interrupted'):
            with self.subTest(status=first_status):
                rpc, updates = ReceivingRpc(), []
                _, stream = self.suspended(rpc, updates)
                try:
                    rpc.event(
                        'item/completed',
                        item={
                            'id': 'final',
                            'type': 'agentMessage',
                            'phase': 'final_answer',
                            'text': '这是第一句。',
                        },
                    )
                    self.assertFalse(any(k == 'dot_result' for k, _ in updates))
                    rpc.event('turn/completed', turn={'id': 'stream-turn', 'status': first_status})
                    rpc.complete()
                    self.assertFalse(any(k == 'dot_result' for k, _ in updates))
                    with self.assertRaises(DotError):
                        list(stream)
                finally:
                    stream.close()

    def test_approval_empty_final_and_listener_delivery_error_keep_classification(self):
        rpc, updates = ReceivingRpc(), []
        _, stream = self.suspended(rpc, updates)
        rpc.approval_seen.set()
        rpc.complete('')
        with self.assertRaises(DotApprovalRequired):
            list(stream)
        self.assertFalse(any(k == 'dot_result' for k, _ in updates))

        rpc = ReceivingRpc()
        case = fixture.FailureFeedbackTests()
        b = case.session(brain=DotBrain(rpc))
        original_event = b.event

        def event(name, **fields):
            if name == 'dot_result' and fields.get('text'):
                raise OSError('fixed offline delivery failure')
            original_event(name, **fields)

        b.event = event

        def speak(text, generation):
            rpc.complete()
            return True

        b.speak = speak
        case.run_one(b.response, Reply(b.generation, '固定测试'))
        self.assertFalse(rpc.failed)
        self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
        self.assertEqual(len(getattr(rpc, 'listeners', ())), 0)

    def test_generator_cleanup_waits_for_admitted_receipt_without_deadlock(self):
        rpc = ReceivingRpc()
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()

        def update(kind, payload):
            if kind == 'dot_result':
                entered.set()
                if not release.wait(2):
                    raise TimeoutError('fixture did not release admission')

        stream = DotBrain(rpc).clauses('固定测试', [], on_web=update)
        self.assertEqual(next(stream), '这是第一句。')
        receiver = threading.Thread(target=rpc.complete)
        closer = threading.Thread(target=lambda: (stream.close(), closed.set()))
        try:
            receiver.start()
            self.assertTrue(entered.wait(1))
            closer.start()
            self.assertFalse(closed.wait(0.03))
        finally:
            release.set()
            receiver.join(2)
            if closer.ident:
                closer.join(2)
            else:
                stream.close()
        self.assertFalse(receiver.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertTrue(closed.is_set())
        self.assertEqual(len(getattr(rpc, 'listeners', ())), 0)


if __name__ == '__main__':
    unittest.main()

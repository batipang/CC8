"""Actual Dot/worker timing, including notifications before turn acknowledgement."""

import unittest
import test_failure_feedback as fixture
from test_dot_receipt_delivery import ReceivingRpc
from diagnostics import TraceBook
from dot_client import DotBrain
from reply_types import Reply
from brain_router import BrainRouter


class TimedRpc(ReceivingRpc):
    def __init__(self, now):
        super().__init__()
        self.now = now
        self.turns = 0

    def start(self):
        if not self.initialized:
            self.now[0] += 0.3
            self.initialized = True

    def request(self, method, params, timeout=15):
        if method == 'thread/start':
            self.now[0] += 0.2
        if method == 'turn/start':
            self.turns += 1
        result = super().request(method, params, timeout)
        if method == 'turn/start':
            self.now[0] += 0.1
            return {'turn': {'id': 'stream-turn-' + str(self.turns)}}
        return result

    def event(self, method, **fields):
        if method == 'item/agentMessage/delta':
            self.now[0] += 0.1 if fields['itemId'] == 'comment' else 0.2
        turn_id = 'stream-turn-' + str(self.turns)
        if 'turn' in fields:
            fields['turn'] = {**fields['turn'], 'id': turn_id}
        getattr(self, '_receive', self.events.put_nowait)(
            {
                'method': method,
                'params': {'threadId': 'voice-thread', 'turnId': turn_id, **fields},
            }
        )


class DotTimingTests(unittest.TestCase):
    def test_router_links_turn_before_first_tts_failure_for_text_and_vision(self):
        from test_vision import jpeg

        for vision in (False, True):
            with self.subTest(vision=vision):
                now = [0.0]
                rpc = TimedRpc(now)
                brain = DotBrain(rpc)
                brain.clock = lambda: now[0]
                case = fixture.FailureFeedbackTests()
                bridge = case.session(brain=BrainRouter(fixture.Brain(), brain))
                bridge.diagnostics = TraceBook(clock=lambda: now[0])

                def speak(*args):
                    rpc.complete()
                    raise TimeoutError('fixed public speech failure')

                bridge.speak = speak
                with self.assertRaises(TimeoutError):
                    bridge.response.answer(
                        '固定公开测试', bridge.generation, jpeg() if vision else None
                    )
                row = bridge.diagnostics.report()['requests'][0]
                self.assertIn('turn_id', row)
                self.assertEqual(row['turn_id'], 'stream-turn-1')
                self.assertIn('dot_receipt', row['stages'])
                self.assertEqual(row['task_outcome'], 'completed')
                self.assertEqual(bridge.workspace.tasks[-1]['status'], 'completed')

    def test_whitespace_or_commentary_cannot_be_first_answer_delta(self):
        now = [0.0]

        class WhitespaceRpc(TimedRpc):
            def event(self, method, **fields):
                if method == 'item/agentMessage/delta' and fields['itemId'] == 'final':
                    self._receive(
                        {
                            'method': method,
                            'params': {
                                'threadId': 'voice-thread',
                                'turnId': 'stream-turn-' + str(self.turns),
                                'itemId': 'final',
                                'delta': '  ',
                            },
                        }
                    )
                    now[0] += 0.05
                super().event(method, **fields)

        rpc = WhitespaceRpc(now)
        brain = DotBrain(rpc)
        brain.clock = lambda: now[0]
        signals = []
        stream = brain.clauses('固定公开测试', [], on_web=lambda k, v: signals.append((k, v)))
        try:
            next(stream)
            rpc.complete('  这是第一句。这是第二句。')
            list(stream)
        finally:
            stream.close()
        first = [v for k, v in signals if k == 'brain_stage' and v['stage'] == 'dot_first_delta']
        self.assertEqual(len(first), 1)
        self.assertAlmostEqual(first[0]['at'], 0.85)

    def test_pre_ack_delta_and_receipt_during_speech_keep_observed_clock(self):
        now = [0.0]
        rpc = TimedRpc(now)
        brain = DotBrain(rpc)
        brain.clock = lambda: now[0]
        case = fixture.FailureFeedbackTests()
        bridge = case.session(brain=brain)
        bridge.diagnostics = TraceBook(clock=lambda: now[0])
        spoken = []

        def speak(text, generation):
            spoken.append(text)
            if len(spoken) == 1:
                now[0] += 0.7
                rpc.complete()
                now[0] += 0.5
            return True

        bridge.speak = speak
        case.run_one(bridge.response, Reply(bridge.generation, '固定公开测试'))
        report = bridge.diagnostics.report()
        row = report['requests'][0]
        self.assertEqual(row['outcome'], 'completed')
        self.assertTrue(
            {
                'dot_rpc_start',
                'dot_rpc_ready',
                'dot_context_ready',
                'dot_turn_start',
                'dot_turn_ack',
                'dot_first_delta',
                'dot_receipt',
            }
            <= row['stages'].keys()
        )
        self.assertEqual(row['stages']['dot_first_delta'], 800)
        self.assertEqual(row['stages']['dot_turn_ack'], 900)
        self.assertEqual(row['stages']['dot_receipt'], 1600)
        self.assertEqual(row['stages']['task_result'], 1600)
        self.assertFalse(row['dot_rpc_reused'])
        self.assertFalse(row['dot_context_reused'])
        latencies = report['groups']['text']['latencies']
        for name, expected in (
            ('dot_rpc_prepare_ms', 300),
            ('dot_context_ms', 200),
            ('dot_turn_ack_ms', 400),
            ('dot_first_delta_ms', 300),
            ('dot_delta_to_clause_ms', 100),
            ('dot_turn_complete_ms', 1100),
        ):
            self.assertEqual(latencies[name], {'samples': 1, 'p50': expected, 'p95': expected})

    def test_existing_rpc_and_context_reuse_are_explicit_not_assumed(self):
        now = [0.0]
        rpc = TimedRpc(now)
        brain = DotBrain(rpc)
        brain.clock = lambda: now[0]
        runs = []
        for _ in range(2):
            signals = []
            stream = brain.clauses('固定公开测试', [], on_web=lambda k, v: signals.append((k, v)))
            self.assertEqual(next(stream), '这是第一句。')
            rpc.complete()
            list(stream)
            runs.append({v['stage']: v for k, v in signals if k == 'brain_stage'})
        self.assertTrue({'dot_rpc_ready', 'dot_context_ready'} <= runs[0].keys())
        self.assertFalse(runs[0]['dot_rpc_ready']['reused'])
        self.assertFalse(runs[0]['dot_context_ready']['reused'])
        self.assertTrue(runs[1]['dot_rpc_ready']['reused'])
        self.assertTrue(runs[1]['dot_context_ready']['reused'])
        self.assertEqual(sum(k == 'thread/start' for k, _ in rpc.calls), 1)

    def test_completion_before_ack_keeps_receive_time_separate_from_task_admission(self):
        now = [0.0]

        class EarlyRpc(TimedRpc):
            def request(self, method, params, timeout=15):
                result = super().request(method, params, timeout)
                if method == 'turn/start':
                    self.complete()
                    now[0] += 0.2
                return result

        rpc = EarlyRpc(now)
        brain = DotBrain(rpc)
        brain.clock = lambda: now[0]
        case = fixture.FailureFeedbackTests()
        bridge = case.session(brain=brain)
        bridge.diagnostics = TraceBook(clock=lambda: now[0])
        bridge.speak = lambda *args: True
        case.run_one(bridge.response, Reply(bridge.generation, '固定公开测试'))
        row = bridge.diagnostics.report()['requests'][0]
        self.assertEqual(row['stages']['dot_receipt'], 900)
        self.assertEqual(row['stages']['dot_turn_ack'], 1100)
        self.assertEqual(row['stages']['task_result'], 1100)

    def test_cancelled_turn_cannot_add_late_timing_to_next_generation(self):
        now = [0.0]
        rpc = TimedRpc(now)
        brain = DotBrain(rpc)
        brain.clock = lambda: now[0]
        case = fixture.FailureFeedbackTests()
        bridge = case.session(brain=brain)
        bridge.diagnostics = TraceBook(clock=lambda: now[0])

        def speak(*args):
            bridge.cancel('speech')
            bridge.diagnostics.begin('text', bridge.generation)
            rpc.complete()
            return False

        bridge.speak = speak
        case.run_one(bridge.response, Reply(bridge.generation, '固定公开测试'))
        old, new = bridge.diagnostics.report()['requests']
        self.assertNotIn('dot_receipt', old['stages'])
        self.assertEqual(new['stages'], {})
        self.assertNotIn('dot_context_reused', new)


if __name__ == '__main__':
    unittest.main()

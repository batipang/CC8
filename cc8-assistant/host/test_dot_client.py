import queue
import threading
import time
import unittest
from unittest.mock import patch

from dot_client import DotBrain, CodexRpc, DotError, codex_command, answer_sources
from brain_router import BrainRouter
from minimax_client import Cancelled
from bridge import Bridge


class FakeRpc:
    def __init__(self, text='我是大黄蜂。', status='completed'):
        self.events = queue.Queue()
        self.approval_seen = threading.Event()
        self.failed = False
        self.calls = []
        self.counter = 0
        self.text = text
        self.status = status
        self.before_ack = None

    def start(self):
        pass

    def close(self):
        self.closed = True

    def request(self, method, params, timeout=15):
        self.calls.append((method, params))
        if method == 'thread/start':
            return {'thread': {'id': 'voice-thread'}}
        if method == 'turn/start':
            self.counter += 1
            ident = 'turn-' + str(self.counter)
            if self.before_ack:
                self.before_ack()
            for thread, turn, text, phase in [
                ('other', 'other', 'wrong', 'final_answer'),
                ('voice-thread', ident, '不该播报的过程说明', 'commentary'),
                ('voice-thread', ident, self.text, 'final_answer'),
            ]:
                self.events.put(
                    {
                        'method': 'item/completed',
                        'params': {
                            'threadId': thread,
                            'turnId': turn,
                            'item': {'type': 'agentMessage', 'text': text, 'phase': phase},
                        },
                    }
                )
            self.events.put(
                {
                    'method': 'turn/completed',
                    'params': {
                        'threadId': 'voice-thread',
                        'turn': {'id': ident, 'status': self.status},
                    },
                }
            )
            return {'turn': {'id': ident}}
        return {}


class DotTests(unittest.TestCase):
    def test_source_links_stop_at_markdown_and_chinese_punctuation(self):
        sources = answer_sources(
            '接口是 `https://api.minimax.cn/v1/speech_to_text`，使用表单。来源：https://platform.minimax.cn/docs/api-reference/speech-to-text。'
        )
        self.assertEqual(sources[0]['url'], 'https://api.minimax.cn/v1/speech_to_text')
        self.assertEqual(
            sources[1]['url'], 'https://platform.minimax.cn/docs/api-reference/speech-to-text'
        )

    def test_completed_final_only_and_scoped_tool_session(self):
        rpc = FakeRpc()
        b = DotBrain(rpc)
        events = []
        self.assertEqual(
            ''.join(b.clauses('你好', [], on_web=lambda k, v: events.append((k, v)))),
            '我是大黄蜂。',
        )
        thread = rpc.calls[0][1]
        turn = rpc.calls[1][1]
        self.assertTrue(thread['ephemeral'])
        self.assertEqual(
            {t['name'] for t in thread['dynamicTools']}, {'cc8_control', 'mac_open', 'cc8_reminder'}
        )
        self.assertNotIn('sandbox', thread)
        self.assertNotIn('approvalPolicy', thread)
        self.assertNotIn('sandboxPolicy', turn)
        self.assertNotIn('approvalPolicy', turn)
        self.assertNotIn('effort', turn)
        self.assertTrue(any(k == 'dot_result' for k, _ in events))
        self.assertTrue(b.last_metrics['completed'])

    def test_same_owned_session_is_reused(self):
        rpc = FakeRpc()
        b = DotBrain(rpc)
        list(b.clauses('第一句', []))
        list(b.clauses('第二句', []))
        self.assertEqual(sum(m == 'thread/start' for m, _ in rpc.calls), 1)

    def test_real_web_search_activity_can_publish_answer_source_links(self):
        rpc = FakeRpc(text='这是结果。来源：[官方](https://example.com/news)。')
        rpc.before_ack = lambda: rpc.events.put(
            {
                'method': 'item/completed',
                'params': {
                    'threadId': 'voice-thread',
                    'turnId': 'turn-1',
                    'item': {'type': 'webSearch'},
                },
            }
        )
        events = []
        b = DotBrain(rpc)
        list(b.clauses('查一下', [], on_web=lambda kind, data: events.append((kind, data))))
        sources = next(data for kind, data in events if kind == 'sources')
        self.assertEqual(sources['items'][0]['url'], 'https://example.com/news')
        self.assertEqual(b.last_metrics['web_search_calls'], 1)

    def test_cancel_before_turn_ack_interrupts_late_turn(self):
        rpc = FakeRpc()
        b = DotBrain(rpc)
        rpc.before_ack = b.cancel
        with self.assertRaises(Cancelled):
            list(b.clauses('测试', []))
        self.assertIn(
            ('turn/interrupt', {'threadId': 'voice-thread', 'turnId': 'turn-1'}), rpc.calls
        )

    def test_cancel_never_waits_for_interrupt_rpc(self):
        rpc = FakeRpc()
        b = DotBrain(rpc)
        from dot_client import Turn

        b.thread_id = 'voice-thread'
        b.current = Turn(ident='current')
        release = threading.Event()
        started = threading.Event()

        def blocking(*args, **kwargs):
            started.set()
            release.wait(2)
            return {}

        rpc.request = blocking
        start = time.monotonic()
        b.cancel()
        self.assertLess(time.monotonic() - start, 0.15)
        self.assertTrue(started.wait(1))
        release.set()

    def test_stale_before_start_does_not_send_request(self):
        rpc = FakeRpc()
        with self.assertRaises(Cancelled):
            list(DotBrain(rpc).clauses('测试', [], lambda: False))
        self.assertEqual(rpc.calls, [])

    def test_failure_does_not_speak_partial_success(self):
        rpc = FakeRpc(text='不完整结果', status='failed')
        with self.assertRaises(DotError):
            list(DotBrain(rpc).clauses('测试', []))

    def test_large_answer_is_bounded_for_device_and_voice(self):
        rpc = FakeRpc(text='这是一句测试。' * 2000)
        b = DotBrain(rpc)
        events = []
        spoken = ''.join(b.clauses('测试', [], on_web=lambda k, v: events.append((k, v))))
        self.assertLess(len(spoken), 500)
        detail = next(v for k, v in events if k == 'dot_result')
        self.assertLessEqual(len(detail['text']), 10000)
        self.assertTrue(detail['truncated'])

    def test_server_approvals_are_not_automatically_granted(self):
        rpc = CodexRpc(command=[])
        sent = []
        rpc._send = sent.append
        rpc._server_request({'id': 1, 'method': 'item/commandExecution/requestApproval'})
        rpc._server_request({'id': 2, 'method': 'item/permissions/requestApproval'})
        self.assertEqual(sent[0]['result'], {'decision': 'decline'})
        self.assertEqual(sent[1]['result']['permissions'], {})

    def test_child_inherits_desktop_capabilities_without_changing_configuration(self):
        with patch('dot_client.shutil.which', return_value='/bin/codex'):
            command = codex_command()
        self.assertEqual(command[0], '/bin/codex')
        self.assertIn('web_search="live"', command)
        self.assertFalse(
            any(part.startswith(('sandbox_mode=', 'approval_policy=')) for part in command)
        )
        self.assertFalse(any('enabled=false' in part for part in command))


class StubBrain:
    def __init__(self, label):
        self.label = label
        self.requests = []
        self.last_metrics = {}
        self.failure = None

    def cancel(self):
        pass

    def close(self):
        pass

    def clauses(self, text, history, current, on_web):
        self.requests.append(text)
        if self.failure:
            raise self.failure
        yield self.label


class RouterTests(unittest.TestCase):
    def test_default_and_explicit_voice_delegation(self):
        mini = StubBrain('mini')
        dot = StubBrain('dot')
        router = BrainRouter(mini, dot)
        self.assertEqual(''.join(router.clauses('你好', [])), 'dot')
        router.select('minimax')
        self.assertEqual(''.join(router.clauses('让Dot帮我分析这个项目', [])), 'dot')
        self.assertEqual(dot.requests, ['你好', '分析这个项目'])
        self.assertEqual(''.join(router.clauses('Dot是什么', [])), 'mini')

    def test_provider_switch_and_homophone_delegation(self):
        mini = StubBrain('mini')
        dot = StubBrain('dot')
        router = BrainRouter(mini, dot)
        router.select('dot')
        self.assertEqual(''.join(router.clauses('你好', [])), 'dot')
        router.select('minimax')
        self.assertEqual(''.join(router.clauses('让多特帮我看一下', [])), 'dot')

    def test_dot_failure_is_explicit_without_silent_minimax_fallback(self):
        mini = StubBrain('mini')
        dot = StubBrain('dot')
        dot.failure = DotError('failed')
        router = BrainRouter(mini, dot)
        router.select('dot')
        with self.assertRaises(DotError):
            list(router.clauses('你好', []))
        self.assertEqual(mini.requests, [])

    def test_unknown_provider_rejected(self):
        router = BrainRouter(StubBrain('mini'), StubBrain('dot'))
        with self.assertRaises(ValueError):
            router.select('untrusted')


class ProviderSettingsTests(unittest.TestCase):
    def bridge(self):
        b = Bridge(None, BrainRouter(StubBrain('mini'), StubBrain('dot')))
        b.save_status = lambda: None
        b.event = lambda *args, **kwargs: None
        b.session.wake = True
        b.session.awake = False
        return b

    def test_switch_cancels_old_turn_without_waking_or_starting_microphone(self):
        b = self.bridge()
        old = b.session.generation
        b.on_event({'event': 'settings', 'brain_provider': 'dot'})
        self.assertEqual(b.brain.selected, 'dot')
        self.assertGreater(b.session.generation, old)
        self.assertTrue(b.session.wake)
        self.assertFalse(b.session.awake)
        self.assertFalse(b.session.active)

    def test_provider_change_cannot_interrupt_calibration(self):
        b = self.bridge()
        b.calibration.active = True
        old = b.session.generation
        b.on_event({'event': 'settings', 'brain_provider': 'dot'})
        self.assertEqual(b.brain.selected, 'dot')
        self.assertEqual(b.session.generation, old)


if __name__ == '__main__':
    unittest.main()

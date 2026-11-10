"""Guest transport sees only its own bounded dialogue; no Dot/private-context callbacks."""

import json
import threading
import unittest

from guest_brain import GuestBrain
from minimax_client import Cancelled, MiniMaxError


class Transport:
    def __init__(self):
        self.calls = []
        self.channels = []
        self.cancelled = 0
        self.before = lambda: None
        self.events_value = [
            {'choices': [{'delta': {'content': '你好，访客。'}, 'finish_reason': 'stop'}]}
        ]

    def create(self):
        fixture = self

        class Channel:
            def __init__(self):
                self.cancelled = False
                self.cancel_hook = lambda: None

            def events(self, route, body, current):
                fixture.calls.append((route, body))
                fixture.before()
                yield from fixture.events_value

            def cancel(self):
                self.cancel_hook()
                self.cancelled = True
                fixture.cancelled += 1

        result = Channel()
        self.channels.append(result)
        return result


class GuestBrainTests(unittest.TestCase):
    def make(self):
        transport = Transport()
        brain = GuestBrain(transport.create, 'MiniMax-M3')
        self.assertEqual(transport.calls, [])
        return brain, transport

    def test_owner_history_is_not_inspected_and_no_tool_or_search_surface_is_sent(self):
        class OwnerHistory:
            def __iter__(self):
                raise AssertionError('Owner history accessed')

        brain, transport = self.make()
        brain.activate()
        self.assertEqual(''.join(brain.clauses('介绍一下', OwnerHistory())), '你好，访客。')
        route, body = transport.calls[0]
        self.assertEqual(route, '/v1/chat/completions')
        self.assertEqual([r['role'] for r in body['messages']], ['system', 'user'])
        self.assertNotIn('tools', body)
        self.assertNotIn('tool_choice', body)
        self.assertEqual(brain.last_metrics['provider'], 'guest_minimax')

    def test_only_completed_guest_turns_enter_guest_history_and_mode_exit_clears_it(self):
        brain, transport = self.make()
        brain.activate()
        list(brain.clauses('访客第一问', [{'role': 'user', 'content': 'OWNER_SECRET'}]))
        list(brain.clauses('访客第二问', []))
        self.assertIn('访客第一问', json.dumps(transport.calls[-1][1], ensure_ascii=False))
        self.assertNotIn('OWNER_SECRET', json.dumps(transport.calls))
        brain.deactivate()
        self.assertEqual(list(brain.history), [])
        with self.assertRaises(Cancelled):
            list(brain.clauses('不应发送', []))
        brain.activate()
        list(brain.clauses('全新访客', []))
        self.assertEqual(len(transport.calls[-1][1]['messages']), 2)

    def test_provider_tool_calls_or_partial_completions_never_enter_history(self):
        for delta, finish in (
            ({'tool_calls': [{'name': 'read_private'}]}, 'stop'),
            ({'content': 'partial'}, None),
            ({'content': 'partial'}, 'length'),
        ):
            brain, transport = self.make()
            brain.activate()
            transport.events_value = [{'choices': [{'delta': delta, 'finish_reason': finish}]}]
            with self.assertRaises(MiniMaxError):
                list(brain.clauses('看看主人资料', []))
            self.assertEqual(list(brain.history), [])

    def test_late_guest_stream_after_exit_cannot_commit_or_publish(self):
        brain, transport = self.make()
        brain.activate()
        transport.before = brain.deactivate
        events = []
        with self.assertRaises(Cancelled):
            list(brain.clauses('访客问题', [], on_web=lambda kind, value: events.append(kind)))
        self.assertEqual(list(brain.history), [])
        self.assertNotIn('dot_result', events)

    def test_delayed_old_cancellation_cannot_close_new_request_transport(self):
        brain, transport = self.make()
        brain.activate()
        transport.events_value = [
            {'choices': [{'delta': {'content': '第一句。第二句。'}, 'finish_reason': 'stop'}]}
        ]
        old = brain.clauses('旧问题', [])
        next(old)
        entered, release = threading.Event(), threading.Event()

        def block():
            entered.set()
            if not release.wait(2):
                raise AssertionError('cancel barrier timeout')

        transport.channels[0].cancel_hook = block
        worker = threading.Thread(target=brain.cancel, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertTrue(''.join(brain.clauses('新问题', [])))
            self.assertFalse(transport.channels[1].cancelled)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(transport.channels[0].cancelled)
        self.assertFalse(transport.channels[1].cancelled)
        with self.assertRaises(Cancelled):
            list(old)
        self.assertEqual([r['content'] for r in brain.history if r['role'] == 'user'], ['新问题'])

    def test_validation_and_inactive_state_make_no_request(self):
        brain, transport = self.make()
        with self.assertRaises(Cancelled):
            list(brain.clauses('hello', []))
        brain.activate()
        for text in ('', 'a' * 4001, '\ud800'):
            with self.assertRaises(ValueError):
                list(brain.clauses(text, []))
        self.assertEqual(transport.calls, [])

    def test_invalid_provider_unicode_is_rejected_before_clause_can_reach_tts(self):
        brain, transport = self.make()
        brain.activate()
        transport.events_value = [
            {'choices': [{'delta': {'content': '\ud800。'}, 'finish_reason': 'stop'}]}
        ]
        stream = brain.clauses('正常问题', [])
        with self.assertRaises(ValueError):
            next(stream)
        self.assertEqual(list(brain.history), [])

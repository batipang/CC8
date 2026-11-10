"""Exercise the actual owned Dot tool path and final source-event production."""

import threading
import unittest
from unittest.mock import patch

from dot_client import DotBrain, Turn
from minimax_client import Cancelled
from test_dot_client import FakeRpc
from voice_tools import VoiceTools


URL = 'https://example.com/article'


class ReadingRpc(FakeRpc):
    def __init__(self, count=1):
        super().__init__('原文中说的是公开测试内容。来源：' + URL)
        self.count = count
        self.outputs = []

    def request(self, method, params, timeout=15):
        if method != 'turn/start':
            return super().request(method, params, timeout)
        self.calls.append((method, params))
        self.counter += 1
        turn = 'turn-' + str(self.counter)

        def finish():
            for i in range(self.count):
                self.outputs.append(
                    self.tool_handler(
                        dict(
                            threadId='voice-thread',
                            turnId=turn,
                            tool='cc8_read_source',
                            arguments={'url': URL},
                        )
                    )
                )
            self.events.put(
                dict(
                    method='item/completed',
                    params=dict(
                        threadId='voice-thread',
                        turnId=turn,
                        item=dict(type='agentMessage', text=self.text, phase='final_answer'),
                    ),
                )
            )
            self.events.put(
                dict(
                    method='turn/completed',
                    params=dict(threadId='voice-thread', turn=dict(id=turn, status='completed')),
                )
            )

        threading.Thread(target=finish, daemon=True).start()
        return {'turn': {'id': turn}}


class SourceToolTests(unittest.TestCase):
    def receipt(self, *args):
        return dict(
            ok=True,
            url=URL,
            final_url=URL,
            text='Actual text from source',
            publication_labels=['2026-11-10'],
            received_bytes=100,
            http_status=200,
            facts_verified=False,
            untrusted_page=True,
        )

    def test_read_tool_exposes_only_one_url_and_respects_cancellation(self):
        calls = []
        tools = VoiceTools(source_reader=lambda url, current: calls.append(url) or self.receipt())
        self.assertTrue(tools.call('cc8_read_source', {'url': URL})['ok'])
        self.assertFalse(tools.call('cc8_read_source', {'url': URL}, lambda: False)['ok'])
        for args in ({}, {'url': URL, 'headers': {'Authorization': 'secret'}}, {'url': True}):
            with self.assertRaises(ValueError):
                tools.call('cc8_read_source', args)
        self.assertEqual(calls, [URL])

    def test_owned_tool_result_reaches_model_and_final_source_receipt_without_search_items(self):
        rpc = ReadingRpc()
        brain = DotBrain(rpc)
        events = []
        with patch('dot_client.read_page', side_effect=self.receipt) as reader:
            list(brain.clauses('核对这个公开来源', [], on_web=lambda k, v: events.append((k, v))))
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(rpc.outputs[0]['text'], 'Actual text from source')
        row = next(payload for kind, payload in events if kind == 'sources')['items'][0]
        self.assertEqual(row['access_evidence'], 'page_body_received')
        self.assertEqual(row['page_excerpt'], rpc.outputs[0]['text'])
        self.assertIs(row['body_verified'], False)
        self.assertNotIn('原文中', row['page_excerpt'])

    def test_per_turn_budget_is_shared_across_tool_calls_and_resets_for_new_turn(self):
        rpc = ReadingRpc(5)
        brain = DotBrain(rpc)
        with patch('dot_client.read_page', side_effect=self.receipt) as reader:
            list(brain.clauses('核对', []))
            self.assertEqual(reader.call_count, 4)
            self.assertEqual(rpc.outputs[-1]['error'], 'source_read_limit')
            list(brain.clauses('再次核对', []))
            self.assertEqual(reader.call_count, 8)

    def test_stale_tool_ids_cannot_fetch_a_page(self):
        brain = DotBrain(ReadingRpc())
        turn = Turn(ident='one')
        turn.ident_ready.set()
        brain.thread_id = 'voice-thread'
        brain.current = turn
        with patch('dot_client.read_page') as reader:
            result = brain.call_tool(
                turn,
                dict(
                    threadId='other', turnId='one', tool='cc8_read_source', arguments={'url': URL}
                ),
                source_reader=reader,
            )
        self.assertEqual(result['error'], 'stale_turn')
        reader.assert_not_called()

    def test_cancellation_during_page_read_never_publishes_late_text_or_sources(self):
        rpc = ReadingRpc()
        brain = DotBrain(rpc)
        active = [True]
        events = []

        def late(*args):
            active[0] = False
            return self.receipt()

        with patch('dot_client.read_page', side_effect=late):
            with self.assertRaises(Cancelled):
                list(
                    brain.clauses(
                        '核对', [], lambda: active[0], on_web=lambda k, v: events.append((k, v))
                    )
                )
        self.assertFalse(any(kind == 'sources' for kind, _ in events))


if __name__ == '__main__':
    unittest.main()

"""Selected metadata/history boundaries, context isolation and real async cancellation."""

import json
import threading
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from brain_router import BrainRouter
from bridge import Bridge
from dot_client import DotBrain, DotError, Turn, Cancelled
from dot_handoff import ChatCatalog, Handoff
from protocol import validate_device_event
from test_dot_client import FakeRpc


def turn(text='背景', status='completed'):
    return {
        'status': status,
        'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': text}]},
            {'type': 'reasoning', 'text': 'HIDDEN_REASONING'},
            {'type': 'agentMessage', 'phase': 'commentary', 'text': 'HIDDEN_COMMENTARY'},
            {'type': 'commandExecution', 'aggregatedOutput': 'HIDDEN_TOOL'},
            {'type': 'agentMessage', 'phase': 'final_answer', 'text': '完成的回答'},
        ],
    }


class CatalogFixture:
    def __init__(self):
        self.calls, self.clients = [], []
        self.rows = [
            {'id': 'chat-' + str(i), 'name': '聊天' + str(i), 'preview': 'HIDDEN_PREVIEW'}
            for i in range(5)
        ]
        self.turns = [turn('第三轮'), turn('第二轮'), turn('第一轮')]
        self.next_cursor = None
        self.failed_start = False
        self.cwd = str(Path(__file__).resolve().parents[2])
        self.environments = []

    def factory(self):
        fixture = self

        class Rpc:
            closed = False

            def start(self):
                if fixture.failed_start:
                    raise DotError('PRIVATE_PROVIDER_MESSAGE')

            def request(self, name, params, **kwargs):
                fixture.calls.append((name, params))
                if name == 'thread/list':
                    return {'data': fixture.rows, 'nextCursor': fixture.next_cursor}
                if name == 'thread/read':
                    return {
                        'thread': {
                            'id': params['threadId'],
                            'turns': ['HIDDEN_UNRELATED'],
                            'cwd': fixture.cwd,
                            'environments': fixture.environments,
                        }
                    }
                if name == 'thread/turns/list':
                    return {'data': fixture.turns, 'nextCursor': 'older'}
                raise AssertionError('Catalog must never start, resume or interrupt a thread')

            def close(self):
                self.closed = True

        result = Rpc()
        self.clients.append(result)
        return result


class CatalogTests(unittest.TestCase):
    def catalog(self):
        fixture = CatalogFixture()
        return fixture, ChatCatalog(fixture.factory)

    def test_listing_five_metadata_rows_never_reads_history_or_exposes_previews(self):
        f, catalog = self.catalog()
        result = catalog.list()
        self.assertEqual(len(result['items']), 5)
        self.assertEqual({name for name, _ in f.calls}, {'thread/list'})
        self.assertTrue(f.calls[0][1]['useStateDbOnly'])
        self.assertNotIn('HIDDEN', json.dumps(result))
        self.assertTrue(all(client.closed for client in f.clients))

    def test_selection_only_reads_the_offered_id_and_completed_plain_text(self):
        f, catalog = self.catalog()
        catalog.list()
        handoff = catalog.select('chat-3')
        self.assertEqual(handoff.source_title, '聊天3')
        self.assertEqual(
            [text for role, text in handoff.messages if role == 'user'],
            ['第一轮', '第二轮', '第三轮'],
        )
        for method, params in f.calls[1:]:
            self.assertEqual(params['threadId'], 'chat-3')
            if method == 'thread/read':
                self.assertIs(params['includeTurns'], False)
            else:
                self.assertEqual(method, 'thread/turns/list')
                self.assertEqual(params['limit'], 3)
        self.assertNotIn('HIDDEN', handoff.input())
        self.assertTrue(handoff.truncated)
        self.assertEqual(len(handoff.messages), 6)

    def test_unlisted_invalid_and_expired_identifiers_never_read_history(self):
        f = CatalogFixture()
        now = [10.0]
        catalog = ChatCatalog(f.factory, clock=lambda: now[0])
        catalog.list()
        for value in ('other-chat', '../chat-1', '', None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                catalog.select(value)
        now[0] = 311
        with self.assertRaises(ValueError):
            catalog.select('chat-1')
        self.assertEqual(len(f.calls), 1)

    def test_pagination_requires_an_issued_live_cursor_and_refresh_invalidates_old_choices(self):
        f, catalog = self.catalog()
        f.next_cursor = 'page-2'
        catalog.list()
        with self.assertRaises(ValueError):
            catalog.list('unissued')
        f.rows = [{'id': 'next-chat', 'name': '下一页'}]
        catalog.list('page-2')
        self.assertEqual(f.calls[-1][1]['cursor'], 'page-2')
        catalog.list()
        with self.assertRaises(ValueError):
            catalog.select('chat-0')

    def test_active_unanswered_and_phase_less_turns_cannot_be_handed_over(self):
        f, catalog = self.catalog()
        catalog.list()
        f.turns = [turn('正在执行的请求', 'inProgress'), turn('旧请求')]
        f.turns[1]['items'][-1]['phase'] = None
        with self.assertRaises(DotError):
            catalog.select('chat-1')
        f.turns.append(turn('已完成请求'))
        result = catalog.select('chat-1')
        self.assertEqual(len(result.messages), 2)
        self.assertNotIn('正在执行', result.input())
        self.assertNotIn('旧请求', result.input())

    def test_context_is_bounded_and_catalog_clients_close_on_bad_data_or_start_failure(self):
        f, catalog = self.catalog()
        catalog.list()
        f.turns = [turn('x' * 2000)]
        result = catalog.select('chat-1')
        self.assertEqual(len(result.messages[0][1]), 1000)
        self.assertTrue(result.truncated)
        f.turns = [turn()] * 4
        with self.assertRaises(DotError):
            catalog.select('chat-1')
        f.failed_start = True
        with self.assertRaises(DotError):
            catalog.list()
        self.assertTrue(all(client.closed for client in f.clients))

    def test_cancel_after_metadata_read_prevents_the_history_request(self):
        f, catalog = self.catalog()
        catalog.list()

        def current():
            return not any(name == 'thread/read' for name, _ in f.calls)

        with self.assertRaises(Cancelled):
            catalog.select('chat-1', is_current=current)
        self.assertNotIn('thread/turns/list', [name for name, _ in f.calls])
        self.assertTrue(all(client.closed for client in f.clients))

    def test_only_selected_existing_local_project_is_used_for_new_owned_turn(self):
        f, catalog = self.catalog()
        catalog.list()
        with tempfile.TemporaryDirectory() as folder:
            f.cwd = folder
            selected = catalog.select('chat-1')
            self.assertEqual(selected.cwd, folder)
            rpc = FakeRpc()
            brain = DotBrain(rpc)
            brain.set_handoff(selected)
            list(brain.clauses('新问题', []))
            for method, params in rpc.calls:
                if method in ('thread/start', 'turn/start'):
                    self.assertEqual(params['cwd'], folder)
            f.environments = [{'type': 'remote'}]
            self.assertIsNone(catalog.select('chat-1').cwd)
            f.environments = []
        self.assertIsNone(catalog.select('chat-1').cwd)


class HandoffBrainTests(unittest.TestCase):
    def test_selected_context_replaces_owned_session_and_is_injected_once(self):
        rpc, events = FakeRpc(), []
        brain = DotBrain(rpc)
        list(brain.clauses('初始问题', []))
        first = Handoff(
            'chat-a', '来源 A', (('user', 'HISTORICAL_COMMAND'), ('assistant', '旧回答')), False
        )
        brain.set_handoff(first)
        list(brain.clauses('新的问题', [], on_web=lambda name, body: events.append((name, body))))
        params = [p for name, p in rpc.calls if name == 'turn/start'][-1]
        self.assertEqual(len(params['input']), 2)
        quoted = json.loads(params['input'][0]['text'])
        self.assertEqual(quoted['source_thread'], 'chat-a')
        self.assertIn('新的问题', params['input'][1]['text'])
        self.assertEqual(
            [body['handoff_source_title'] for name, body in events if name == 'dot_result'],
            ['来源 A'],
        )
        list(brain.clauses('下一问', []))
        self.assertEqual(len([p for n, p in rpc.calls if n == 'turn/start'][-1]['input']), 1)
        brain.set_handoff(Handoff('chat-b', '来源 B', (('user', 'B_CONTEXT'),), False))
        list(brain.clauses('B 的新问题', []))
        self.assertNotIn(
            'HISTORICAL_COMMAND', str([p for n, p in rpc.calls if n == 'turn/start'][-1])
        )
        brain.set_handoff(None)
        list(brain.clauses('独立问题', []))
        self.assertEqual(len([p for n, p in rpc.calls if n == 'turn/start'][-1]['input']), 1)
        self.assertEqual(sum(name == 'thread/start' for name, _ in rpc.calls), 4)
        self.assertNotIn('thread/resume', [name for name, _ in rpc.calls])

    def test_switch_is_rejected_mid_turn_and_late_interrupt_keeps_original_thread(self):
        rpc, brain = FakeRpc(), DotBrain(FakeRpc())
        brain.rpc = rpc
        old = Turn(thread_id='original-thread', ident='original-turn')
        brain.current = old
        with self.assertRaises(DotError):
            brain.set_handoff(None)
        brain.thread_id = 'new-thread'
        brain._interrupt(old)
        self.assertEqual(
            rpc.calls[-1],
            ('turn/interrupt', {'threadId': 'original-thread', 'turnId': 'original-turn'}),
        )


class ControllerTests(unittest.TestCase):
    def session(self):
        class Mini:
            def cancel(self):
                pass

        with patch('threading.Thread.start'):
            b = Bridge(None, BrainRouter(Mini(), DotBrain(FakeRpc())))
        b.session.connection = object()
        b.save_status = lambda: None
        b.log = lambda *a, **k: None
        b.events = []
        b.event = lambda name, **fields: b.events.append((name, fields))
        return b

    def request(self, bridge, action, **fields):
        bridge.on_event(
            {'event': 'dot_chat_request', 'request_id': 'a' * 32, 'action': action, **fields}
        )

    def join(self, bridge):
        bridge.chats.worker.join(2)
        self.assertFalse(bridge.chats.worker.is_alive())
        self.assertFalse(bridge.chats.busy)

    def test_select_clear_and_state_use_real_dispatch_without_starting_capture(self):
        b, fixture = self.session(), CatalogFixture()
        b.chats.catalog = ChatCatalog(fixture.factory)
        self.request(b, 'list')
        self.join(b)
        self.request(b, 'select', source_id='chat-2')
        self.join(b)
        self.assertEqual(b.brain.dot.handoff.source_id, 'chat-2')
        self.assertEqual(b.chats.state()['mode'], 'summary_handoff')
        self.assertFalse(b.session.active)
        self.assertNotIn('messages', str(b.chats.state()))
        self.assertFalse(b.chats.state()['shared_chat'])
        self.request(b, 'clear')
        self.join(b)
        self.assertIsNone(b.brain.dot.handoff)
        self.assertEqual(b.chats.state()['mode'], 'independent')
        self.assertEqual(
            {m for m, _ in fixture.calls}, {'thread/list', 'thread/read', 'thread/turns/list'}
        )

    def test_busy_request_is_rejected_without_loading_history(self):
        b, fixture = self.session(), CatalogFixture()
        b.chats.catalog = ChatCatalog(fixture.factory)
        b.response.busy = True
        self.request(b, 'list')
        self.assertEqual(fixture.calls, [])
        self.assertFalse(b.events[-1][1]['ok'])
        self.assertIsNone(b.chats.worker)

    def test_failed_reselection_preserves_context_without_exposing_rpc_error(self):
        b, fixture = self.session(), CatalogFixture()
        b.chats.catalog = ChatCatalog(fixture.factory)
        self.request(b, 'list')
        self.join(b)
        self.request(b, 'select', source_id='chat-2')
        self.join(b)
        original = b.brain.dot.handoff
        fixture.failed_start = True
        self.request(b, 'select', source_id='chat-3')
        self.join(b)
        self.assertIs(b.brain.dot.handoff, original)
        self.assertIs(b.chats.selection, original)
        self.assertFalse(b.events[-1][1]['ok'])
        self.assertNotIn('PRIVATE_PROVIDER_MESSAGE', str(b.events))

    def test_selection_metadata_and_history_never_enter_health_or_status_file(self):
        from dataclasses import replace
        from pathlib import Path
        import tempfile

        b = self.session()
        selected = Handoff(
            'PRIVATE_SOURCE_ID', 'PRIVATE_SOURCE_TITLE', (('user', 'PRIVATE_HISTORY'),), False
        )
        b.brain.dot.set_handoff(selected)
        b.chats.selection = selected
        with tempfile.TemporaryDirectory() as folder:
            b.reporting.hooks = replace(b.reporting.hooks, private_directory=lambda: Path(folder))
            b.reporting.save_status()
            text = (Path(folder) / 'status.json').read_text() + json.dumps(b.health())
        self.assertNotIn('PRIVATE_', text)

    def test_cancel_disconnect_and_new_generation_discard_pending_selection(self):
        for change in ('cancel', 'disconnect', 'generation', 'busy'):
            with self.subTest(change=change):
                b = self.session()
                entered, release = threading.Event(), threading.Event()

                class Catalog:
                    def select(self, source, is_current):
                        entered.set()
                        if not release.wait(2):
                            raise RuntimeError('Catalog barrier timed out')
                        return Handoff(source, '选定来源', (('user', 'PRIVATE_HISTORY'),), False)

                b.chats.catalog = Catalog()
                self.request(b, 'select', source_id='chat-1')
                try:
                    self.assertTrue(entered.wait(1))
                    if change == 'cancel':
                        b.on_event({'event': 'dot_chat_cancel', 'request_id': 'a' * 32})
                    elif change == 'disconnect':
                        b.session.connection = object()
                    elif change == 'generation':
                        b.cancel('new_owner_request')
                    else:
                        b.response.busy = True
                finally:
                    release.set()
                    self.join(b)
                self.assertIsNone(b.chats.selection)
                self.assertIsNone(b.brain.dot.handoff)
                self.assertNotIn('PRIVATE_HISTORY', str(b.events))

    def test_protocol_rejects_invalid_ids_and_actions(self):
        for request in (
            {'event': 'dot_chat_request', 'request_id': True, 'action': 'list'},
            {'event': 'dot_chat_request', 'request_id': 'a' * 32, 'action': 'resume'},
            {'event': 'dot_chat_cancel', 'request_id': '../other'},
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate_device_event(request)


if __name__ == '__main__':
    unittest.main()

"""Complete synthetic document coverage and owned final receipts, without a model."""

import json
import queue
import threading
import unittest

from dot_document import DocumentAnswer, DotDocument, MAX_INPUT_BYTES
from dot_receipt import DotError
from meeting_analysis import MeetingAnalyzer, source, parts
from meeting_asr import Transcript
from meeting_session import CaptureOwner, MeetingSession
from minimax_client import Cancelled


def model(texts):
    value = MeetingSession('a' * 32)
    for index, text in enumerate(texts):
        value.start(CaptureOwner(1, index + 1, format(index + 1, '032x')))
        value.receive(value.owner, 0, 0, b'\x00\x20' * 1600)
        chunk = value.pause()[0]
        value.complete(chunk, Transcript(text, 100))
    value.end()
    return value


class Client:
    def __init__(self, change=lambda raw, number: raw):
        self.calls = []
        self.closed = False
        self.change = change

    def run(self, prompt, schema, current, accepted):
        self.calls.append(prompt)
        data = json.loads(prompt[prompt.index('{') :])
        if 'segments' in data:
            raw = dict(
                segments=[
                    dict(id=row['id'], text='translated ' + row['text']) for row in data['segments']
                ]
            )
        else:
            raw = dict(
                summary='总结完整结果' * 2000,
                decisions=['实际决定'],
                action_items=['仅提取，未执行'],
                uncertainties=['识别需核对'],
                covered_rows=[row['id'] for row in data['rows']],
            )
        raw = self.change(raw, len(self.calls))
        if not accepted(
            DocumentAnswer(
                json.dumps(raw, ensure_ascii=False),
                'thread-' + str(len(self.calls)),
                'turn-' + str(len(self.calls)),
            )
        ):
            raise Cancelled('Synthetic cancellation')

    def close(self):
        self.closed = True


class MeetingAnalysisTests(unittest.TestCase):
    def analyze(self, frozen, kind='summarize', language='zh', client=None, current=lambda: True):
        client = client or Client()
        results = []
        clients = []
        engine = MeetingAnalyzer(lambda: client)
        engine.run(
            frozen,
            kind,
            language,
            current,
            lambda result: results.append(result) or True,
            clients.append,
        )
        self.assertEqual(clients, [client])
        self.assertTrue(client.closed)
        return client, results

    def test_full_summary_input_and_final_result_have_no_voice_clipping(self):
        value = model(['甲' * 11000, '乙' * 11000])
        frozen = source(value, value.ident, value.revision)
        client, results = self.analyze(frozen)
        self.assertIn('乙' * 11000, client.calls[0])
        self.assertGreater(len(results[0].text), 10000)
        self.assertEqual(results[0].source, frozen)
        self.assertEqual(results[0].turns, (('thread-1', 'turn-1'),))
        self.assertIn('未执行', results[0].text)

    def test_translation_covers_all_unicode_parts_and_original_order(self):
        value = model(['😀 English。' * 1000, '最后一段 ending'])
        frozen = source(value, value.ident, value.revision)
        segments = parts(frozen)
        for row in frozen.rows:
            self.assertEqual(
                ''.join(
                    part['text'] for part in segments if part['id'].startswith(row.ident + ':')
                ),
                row.text,
            )
        client, results = self.analyze(frozen, 'translate', 'en')
        self.assertGreater(len(client.calls), 1)
        ids = [
            row['id']
            for prompt in client.calls
            for row in json.loads(prompt[prompt.index('{') :])['segments']
        ]
        self.assertEqual(ids, [row['id'] for row in segments])
        self.assertIn('最后一段 ending', results[0].text)
        self.assertEqual(len(results[0].turns), len(client.calls))

    def test_missing_duplicate_or_reordered_translation_never_commits_partial_success(self):
        value = model(['source' * 2000])
        frozen = source(value, value.ident, value.revision)
        for change in (
            lambda raw, n: dict(segments=[]),
            lambda raw, n: dict(segments=list(reversed(raw['segments']))),
            lambda raw, n: dict(segments=[dict(id='foreign', text='invented')]),
        ):
            client = Client(change)
            results = []
            with self.assertRaises(ValueError):
                MeetingAnalyzer(lambda: client).run(
                    frozen, 'translate', 'en', lambda: True, results.append, lambda c: None
                )
            self.assertEqual(results, [])
            self.assertTrue(client.closed)

    def test_missing_summary_range_and_failed_later_batch_do_not_return_partial_results(self):
        value = model(['甲' * 11000, '乙' * 11000])
        frozen = source(value, value.ident, value.revision)

        def fail_summary(raw, n):
            raw['covered_rows'] = raw['covered_rows'][:-1]
            return raw

        for kind, change in (
            ('summarize', fail_summary),
            ('translate', lambda raw, n: dict(segments=[]) if n == 2 else raw),
        ):
            client = Client(change)
            results = []
            with self.assertRaises(ValueError):
                MeetingAnalyzer(lambda: client).run(
                    frozen, kind, 'en', lambda: True, results.append, lambda c: None
                )
            self.assertEqual(results, [])
            self.assertTrue(client.closed)

    def test_digest_tracks_content_not_derived_revision_and_never_reuses_original_after_edit(self):
        value = model(['original'])
        before = source(value, value.ident, value.revision)
        value.revision += 1
        self.assertEqual(source(value, value.ident, value.revision).sha256, before.sha256)
        value.edit(value.rows[0]['id'], value.revision, 'corrected')
        after = source(value, value.ident, value.revision)
        self.assertNotEqual(after.sha256, before.sha256)
        self.assertNotIn('original', after.document)
        self.assertTrue(before.rows[0].text == 'original')

    def test_known_recording_scope_and_interruption_are_not_lost_in_generated_output(self):
        value = model(['confirmed words'])
        value.reason = 'duration_limit'
        frozen = source(value, value.ident, value.revision)
        _, results = self.analyze(frozen)
        self.assertIn('duration_limit', results[0].text)
        self.assertIn('16000 Hz', results[0].text)
        self.assertIn('未确认', results[0].text)
        self.assertIn('"processing_complete":false', frozen.document)


class Rpc:
    def __init__(self, *, observable=False, policy='readOnly', status='completed'):
        self.events = queue.Queue()
        self.failed = False
        self.calls = []
        self.listeners = []
        self.policy = policy
        self.status = status
        self.number = 0
        self.closed = threading.Event()
        self.observable = observable
        self.extra = []
        self.servers = {}
        self.inventory = None
        self.cursor = None
        self.bad_config = False
        if not observable:
            self.listen = None

    def start(self):
        pass

    def close(self):
        self.closed.set()

    def listen(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def emit(self, event):
        for listener in list(self.listeners):
            listener(event)
        self.events.put(event)

    def request(self, method, params, timeout=20):
        self.calls.append((method, params))
        if method == 'config/read':
            return dict(config=dict(mcp_servers=[] if self.bad_config else self.servers))
        if method == 'mcpServerStatus/list':
            rows = self.inventory
            if rows is None:
                rows = [
                    dict(
                        name=name,
                        runtimeStatus='disabled',
                        tools={},
                        resources=[],
                        resourceTemplates=[],
                    )
                    for name in self.servers
                ]
            return dict(data=rows, nextCursor=self.cursor)
        if method == 'thread/start':
            self.number += 1
            return dict(thread=dict(id='doc-' + str(self.number)), sandbox=dict(type=self.policy))
        if method == 'turn/start':
            thread = params['threadId']
            turn = 'turn-' + str(self.number)
            for item in self.extra:
                self.emit(
                    dict(
                        method='item/started', params=dict(threadId=thread, turnId=turn, item=item)
                    )
                )
            for owner, phase, text in [
                ('wrong', 'final_answer', 'wrong'),
                (thread, 'commentary', 'private reasoning'),
                (thread, 'final_answer', '完整输出' * 4000),
            ]:
                self.emit(
                    dict(
                        method='item/completed',
                        params=dict(
                            threadId=owner,
                            turnId=turn,
                            item=dict(type='agentMessage', phase=phase, text=text),
                        ),
                    )
                )
            self.emit(
                dict(
                    method='turn/completed',
                    params=dict(threadId=thread, turn=dict(id=turn, status=self.status)),
                )
            )
            return dict(turn=dict(id=turn))
        return {}


class DocumentClientTests(unittest.TestCase):
    def test_inherited_mcp_is_explicitly_disabled_and_confirmed_before_any_document_turn(self):
        rpc = Rpc(observable=True)
        rpc.servers = {'configured': {'command': 'not-executed'}}
        client = DotDocument(rpc)
        client.run('complete document', {}, lambda: True, lambda result: True)
        params = next(p for m, p in rpc.calls if m == 'thread/start')
        self.assertEqual(params['config']['mcp_servers'], {'configured': {'enabled': False}})
        methods = [method for method, _ in rpc.calls]
        self.assertLess(methods.index('mcpServerStatus/list'), methods.index('turn/start'))
        inventory = next(p for m, p in rpc.calls if m == 'mcpServerStatus/list')
        self.assertEqual(inventory['threadId'], 'doc-1')
        client.close()

    def test_document_deadline_includes_configuration_and_is_not_restarted_for_model_turn(self):
        now = [0]
        rpc = Rpc()
        original = rpc.request

        def request(method, params, timeout=20):
            if method == 'config/read':
                now[0] += 10
            return original(method, params, timeout)

        rpc.request = request
        client = DotDocument(rpc, clock=lambda: now[0], timeout=5)
        with self.assertRaises(DotError) as failed:
            client.run('document', {}, lambda: True, lambda result: True)
        self.assertEqual(failed.exception.kind, 'timeout')
        self.assertFalse(any(method == 'turn/start' for method, _ in rpc.calls))
        client.close()

    def test_unconfirmed_mcp_scope_never_submits_document_or_turn(self):
        for mode in ('tools', 'resources', 'status', 'extra', 'cursor', 'config'):
            with self.subTest(mode=mode):
                rpc = Rpc(observable=True)
                rpc.servers = {'configured': {'command': 'not-executed'}}
                row = dict(
                    name='configured',
                    runtimeStatus='disabled',
                    tools={},
                    resources=[],
                    resourceTemplates=[],
                )
                if mode == 'tools':
                    row['tools'] = {'write': {}}
                if mode == 'resources':
                    row['resources'] = [{'uri': 'private'}]
                if mode == 'status':
                    row['runtimeStatus'] = 'connected'
                if mode == 'extra':
                    row['name'] = 'unknown-plugin-server'
                rpc.inventory = [row]
                rpc.cursor = 'more' if mode == 'cursor' else None
                rpc.bad_config = mode == 'config'
                client = DotDocument(rpc)
                with self.assertRaises(DotError):
                    client.run('PRIVATE DOCUMENT', {}, lambda: True, lambda result: True)
                self.assertFalse(any(method == 'turn/start' for method, _ in rpc.calls))
                self.assertNotIn('PRIVATE DOCUMENT', repr(rpc.calls))
                client.close()

    def test_unexpected_tool_notification_prevents_even_early_terminal_admission(self):
        for observable in (True, False):
            rpc = Rpc(observable=observable)
            rpc.extra = [dict(type='commandExecution')]
            client = DotDocument(rpc)
            results = []
            with self.assertRaises(DotError):
                client.run('source', {}, lambda: True, lambda r: results.append(r) or True)
            self.assertEqual(results, [])
            self.assertEqual(rpc.listeners, [])
            client.close()

    def test_exact_final_receipt_and_complete_prompt_for_observable_and_injected_rpc(self):
        for observable in (True, False):
            with self.subTest(observable=observable):
                rpc = Rpc(observable=observable)
                client = DotDocument(rpc)
                results = []
                answer = client.run(
                    'input' * 6000, {}, lambda: True, lambda result: results.append(result) or True
                )
                self.assertEqual(answer.text, '完整输出' * 4000)
                self.assertEqual(results, [answer])
                params = next(p for m, p in rpc.calls if m == 'thread/start')
                self.assertTrue(params['ephemeral'])
                self.assertEqual(params['sandbox'], 'read-only')
                self.assertEqual(params['dynamicTools'], [])
                self.assertEqual(params['config']['mcp_servers'], {})
                self.assertNotIn('model', params)
                self.assertNotIn('effort', params)
                self.assertNotIn('approvalPolicy', params)
                turn = next(p for m, p in rpc.calls if m == 'turn/start')
                self.assertEqual(turn['input'][0]['text'], 'input' * 6000)
                self.assertNotIn('private reasoning', answer.text)
                self.assertEqual(rpc.listeners, [])
                client.close()

    def test_invalid_read_only_receipt_stops_before_submitting_any_document(self):
        rpc = Rpc(policy='dangerFullAccess')
        client = DotDocument(rpc)
        with self.assertRaises(DotError):
            client.run('source', {}, lambda: True, lambda r: True)
        self.assertFalse(any(method == 'turn/start' for method, _ in rpc.calls))
        client.close()

    def test_completed_admission_survives_cancellation_before_consumer_cleans_up(self):
        rpc = Rpc(observable=True)
        client = DotDocument(rpc)
        results = []

        def accepted(value):
            results.append(value)
            client.stopped.set()
            return True

        answer = client.run('source', {}, lambda: True, accepted)
        self.assertEqual(results, [answer])
        client.close()

    def test_failed_turn_cancelled_owner_and_oversize_input_never_publish(self):
        for mode in ('failed', 'cancelled', 'oversize'):
            rpc = Rpc(status='failed' if mode == 'failed' else 'completed')
            client = DotDocument(rpc)
            results = []
            with self.assertRaises((DotError, Cancelled, ValueError)):
                client.run(
                    'x' * (MAX_INPUT_BYTES + 1) if mode == 'oversize' else 'source',
                    {},
                    lambda: mode != 'cancelled',
                    results.append,
                )
            self.assertEqual(results, [])
            if mode != 'failed':
                self.assertEqual(rpc.calls, [])
            client.close()


if __name__ == '__main__':
    unittest.main()

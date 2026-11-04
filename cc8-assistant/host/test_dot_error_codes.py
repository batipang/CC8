"""Fixed wire errors traverse real RPC admission, DotBrain and worker feedback."""

from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dot_client import CodexRpc, DotBrain, DotError
from dot_receipt import ReceiptWatch
from failure_feedback import feedback
from reply_types import Reply
from test_dot_client import FakeRpc
import test_failure_feedback as fixture

SECRET = 'SECRET-provider-body-auth-url'
THREAD, TURN = 'voice-thread', 'structured-turn'


def error(info):
    return {'message': SECRET, 'additionalDetails': SECRET, 'codexErrorInfo': info}


def notice(info, *, thread=THREAD, turn=TURN, retry=False):
    return {
        'method': 'error',
        'params': {
            'threadId': thread,
            'turnId': turn,
            'willRetry': retry,
            'error': error(info),
            'extra': SECRET,
        },
    }


def terminal(info=None, *, status='failed'):
    return {
        'method': 'turn/completed',
        'params': {
            'threadId': THREAD,
            'turn': {'id': TURN, 'status': status, 'error': error(info)},
        },
    }


def answer():
    return {
        'method': 'item/completed',
        'params': {
            'threadId': THREAD,
            'turnId': TURN,
            'item': {
                'id': 'final',
                'type': 'agentMessage',
                'phase': 'final_answer',
                'text': '固定成功结果。',
            },
        },
    }


class WireRpc(FakeRpc, CodexRpc):
    def __init__(self, events, *, approval=False):
        FakeRpc.__init__(self)
        CodexRpc.__init__(self)
        self.wire_events = events
        self.approval = approval

    def request(self, method, params, timeout=15):
        if method != 'turn/start':
            return FakeRpc.request(self, method, params, timeout)
        self.calls.append((method, params))
        if self.approval:
            self._record_approval({'params': {'threadId': THREAD, 'turnId': TURN}})
        wire = b''.join((json.dumps(row) + '\n').encode() for row in self.wire_events)
        self.process = SimpleNamespace(stdout=io.BytesIO(wire))
        CodexRpc._read(self)
        with self.events.mutex:
            self.admitted = deepcopy(list(self.events.queue))
        return {'turn': {'id': TURN}}


class DotErrorCodeTests(unittest.TestCase):
    def test_unsupported_or_input_request_is_not_an_approval_refusal(self):
        for method in ('unsupportedFixedMethod', 'item/tool/requestUserInput'):
            with self.subTest(method=method):
                rpc = CodexRpc(command=[])
                sent = []
                rpc._send = sent.append
                rpc._server_request({'id': 1, 'method': method})
                self.assertFalse(rpc.approval_seen.is_set())
                self.assertEqual(len(sent), 1)
        for method in ('item/permissions/requestApproval', 'item/commandExecution/requestApproval'):
            with self.subTest(method=method):
                rpc = CodexRpc(command=[])
                sent = []
                rpc._send = sent.append
                rpc._server_request({'id': 1, 'method': method})
                self.assertTrue(rpc.approval_seen.is_set())
                self.assertNotIn('accept', json.dumps(sent))

    def failed(self, events, kind, *, approval=False):
        rpc = WireRpc(events, approval=approval)
        updates = []
        brain = DotBrain(rpc)
        with self.assertRaises(DotError) as caught:
            list(brain.clauses('固定公开测试', [], on_web=lambda k, v: updates.append((k, v))))
        self.assertEqual(caught.exception.kind, kind)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertFalse(any(k == 'dot_result' for k, _ in updates))
        self.assertEqual(sum(m == 'turn/start' for m, _ in rpc.calls), 1)
        self.assertIsNone(brain.current)
        return rpc, brain

    def test_wire_terminal_codes_keep_specific_failure_categories(self):
        cases = [
            ('rateLimitExceeded', 'rate_limit'),
            ('usageLimitExceeded', 'quota'),
            ('unauthorized', 'credential_or_permission'),
        ]
        for variant in (
            'httpConnectionFailed',
            'responseStreamConnectionFailed',
            'responseStreamDisconnected',
            'responseTooManyFailedAttempts',
        ):
            for status, kind in (
                (401, 'credential_or_permission'),
                (403, 'credential_or_permission'),
                (402, 'quota'),
                (408, 'timeout'),
                (504, 'timeout'),
                (429, 'provider_or_runtime'),
                (503, 'provider_or_runtime'),
            ):
                cases.append(({variant: {'httpStatusCode': status}}, kind))
        for info, kind in cases:
            for repeat in range(3):
                with self.subTest(info=info, repeat=repeat):
                    self.failed([terminal(info)], kind)

    def test_connection_variants_without_http_status_are_transport_failures(self):
        for variant in (
            'httpConnectionFailed',
            'responseStreamConnectionFailed',
            'responseStreamDisconnected',
        ):
            for body in ({}, {'httpStatusCode': None}):
                with self.subTest(variant=variant, body=body):
                    self.failed([terminal({variant: body})], 'transport_or_io')

    def test_unknown_or_malformed_info_is_generic_without_text_matching(self):
        invalid = (
            [
                None,
                SECRET,
                'contextWindowExceeded',
                'sandboxError',
                429,
                True,
                [],
                {'responseTooManyFailedAttempts': {}},
            ]
            + [
                {'httpConnectionFailed': {'httpStatusCode': value}}
                for value in (True, 401.0, '401', 0, 99, 600)
            ]
            + [
                {'httpConnectionFailed': None},
                {'httpConnectionFailed': {'httpStatusCode': 401}, 'extra': SECRET},
            ]
        )
        for info in invalid:
            with self.subTest(info=info):
                self.failed([terminal(info)], 'provider_or_runtime')

    def test_non_retry_notice_is_correlated_and_kept_before_turn_ack(self):
        self.failed([notice('usageLimitExceeded'), terminal()], 'quota')
        self.failed([notice({'responseStreamDisconnected': {}}), terminal()], 'transport_or_io')

    def test_stale_invalid_and_retried_notices_do_not_poison_later_failure(self):
        for event in (
            notice('usageLimitExceeded', thread='other'),
            notice('usageLimitExceeded', turn='old'),
            notice('usageLimitExceeded', retry=True),
            notice('usageLimitExceeded', retry='false'),
            notice('usageLimitExceeded', retry=0),
        ):
            with self.subTest(event=event):
                self.failed([event, terminal()], 'provider_or_runtime')
        self.failed([notice('usageLimitExceeded'), notice(None), terminal()], 'provider_or_runtime')
        self.failed(
            [notice('usageLimitExceeded'), notice(None, retry=True), terminal()],
            'provider_or_runtime',
        )

    def test_terminal_cause_overrides_prior_hint_and_approval_fallback(self):
        self.failed([notice('rateLimitExceeded'), terminal('usageLimitExceeded')], 'quota')
        for unknown in (
            'sandboxError',
            SECRET,
            False,
            {'httpConnectionFailed': {'httpStatusCode': 503}},
            {'httpConnectionFailed': {'httpStatusCode': 429}},
            {'httpConnectionFailed': {'httpStatusCode': '401'}},
        ):
            with self.subTest(unknown=unknown):
                self.failed(
                    [notice('usageLimitExceeded'), terminal(unknown)], 'provider_or_runtime'
                )
        self.failed([terminal('usageLimitExceeded')], 'quota', approval=True)
        rpc, _ = self.failed([terminal()], 'approval_declined', approval=True)
        self.assertTrue(rpc.approval_seen.is_set())

    def test_success_stays_completed_despite_prior_error_notices(self):
        rpc = WireRpc(
            [
                notice('usageLimitExceeded'),
                notice('rateLimitExceeded', retry=True),
                answer(),
                terminal('unauthorized', status='completed'),
            ]
        )
        updates = []
        self.assertEqual(
            ''.join(
                DotBrain(rpc).clauses('固定测试', [], on_web=lambda k, v: updates.append((k, v)))
            ),
            '固定成功结果。',
        )
        self.assertEqual(sum(k == 'dot_result' for k, _ in updates), 1)

    def test_receive_and_pre_ack_buffer_keep_only_fixed_error_fields(self):
        rpc = CodexRpc(command=[])
        watch = ReceiptWatch(
            rpc, THREAD, lambda: True, lambda *args: self.fail('Failed turn published')
        )
        self.addCleanup(watch.close)
        event = notice({'httpConnectionFailed': {'httpStatusCode': 402, 'body': SECRET}})
        rpc._receive(event)
        self.assertEqual(len(watch.early), 1)
        event['params']['error']['codexErrorInfo']['httpConnectionFailed']['httpStatusCode'] = 401
        self.assertNotIn(SECRET, json.dumps(watch.early))
        with rpc.events.mutex:
            admitted = list(rpc.events.queue)
        self.assertNotIn(SECRET, json.dumps(admitted))
        rpc._receive(terminal())
        watch.bind(TURN)
        self.assertEqual(getattr(watch.state, 'failure_kind', None), 'quota')
        rpc._receive(notice('unauthorized'))
        self.assertEqual(getattr(watch.state, 'failure_kind', None), 'quota')

    def test_wire_error_body_does_not_remain_in_event_queue(self):
        rpc, _ = self.failed([terminal('usageLimitExceeded')], 'quota')
        self.assertNotIn(SECRET, json.dumps(rpc.admitted))

    def test_real_worker_feedback_matches_wire_error_and_never_retries(self):
        case = fixture.FailureFeedbackTests()
        for info, kind in (
            ('usageLimitExceeded', 'quota'),
            ('rateLimitExceeded', 'rate_limit'),
            ('unauthorized', 'credential_or_permission'),
            ({'httpConnectionFailed': {'httpStatusCode': 504}}, 'timeout'),
            ({'responseStreamDisconnected': {}}, 'transport_or_io'),
        ):
            for repeat in range(3):
                with self.subTest(kind=kind, repeat=repeat):
                    rpc = WireRpc([terminal(info)])
                    b = case.session(brain=DotBrain(rpc))
                    b.speak = lambda *args: self.fail('Failure became speech')
                    case.run_one(b.response, Reply(b.generation, '固定公开测试'))
                    self.assertEqual(b.workspace.tasks[-1]['status'], 'failed')
                    row = b.diagnostics.report()['requests'][-1]
                    self.assertEqual(row['error'], {'stage': 'brain', 'kind': kind})
                    self.assertEqual(case.guidance(b), [feedback('brain', kind).message])
                    self.assertFalse(b.response.busy)
                    self.assertEqual(sum(m == 'turn/start' for m, _ in rpc.calls), 1)
                    self.assertNotIn(SECRET, json.dumps([b.events, b.logs, b.diagnostics.report()]))

    def test_real_stdio_child_delivers_pre_ack_cause_without_model_or_tools(self):
        script = """import json,sys
turns=0
def send(value):
 wire=(json.dumps(value)+'\\n').encode()
 # Deliberately split JSON across writes. The real reader must frame it.
 sys.stdout.buffer.write(wire[:17]);sys.stdout.buffer.flush()
 sys.stdout.buffer.write(wire[17:]);sys.stdout.buffer.flush()
for line in sys.stdin.buffer:
 request=json.loads(line)
 if 'id' not in request:continue
 method=request.get('method')
 if method=='thread/start':result={'thread':{'id':'voice-thread'}}
 elif method=='turn/start':
  turns+=1
  send({'method':'error','params':{'threadId':'voice-thread','turnId':'structured-turn',
    'willRetry':False,'error':{'message':'SECRET-provider-body-auth-url',
    'codexErrorInfo':sys.argv[1]}}})
  send({'id':request['id'],'result':{'turn':{'id':'structured-turn'}}})
  send({'method':'turn/completed','params':{'threadId':'voice-thread',
    'turn':{'id':'structured-turn','status':'failed','error':None,'fixtureTurnCount':turns}}})
  continue
 else:result={}
 send({'id':request['id'],'result':result})
"""
        original_thread = threading.Thread
        with tempfile.TemporaryDirectory(prefix='cc8-fixed-rpc-peer-') as temporary:
            path = Path(temporary) / 'peer.py'
            path.write_text(script)
            for info, kind in (
                ('rateLimitExceeded', 'rate_limit'),
                ('usageLimitExceeded', 'quota'),
                ('unauthorized', 'credential_or_permission'),
            ):
                with self.subTest(info=info):
                    threads, seen = [], []

                    def make_thread(*args, **kwargs):
                        thread = original_thread(*args, **kwargs)
                        threads.append(thread)
                        return thread

                    rpc = CodexRpc(command=[sys.executable, '-u', str(path), info])
                    remove = rpc.listen(seen.append)
                    try:
                        with patch('dot_client.threading.Thread', side_effect=make_thread):
                            with self.assertRaises(DotError) as caught:
                                list(DotBrain(rpc).clauses('固定公开测试', []))
                        self.assertEqual(caught.exception.kind, kind)
                        terminals = [e for e in seen if e.get('method') == 'turn/completed']
                        self.assertEqual(len(terminals), 1)
                        self.assertEqual(terminals[0]['params']['turn']['fixtureTurnCount'], 1)
                        self.assertNotIn(SECRET, json.dumps(seen))
                    finally:
                        remove()
                        rpc.close()
                        for thread in threads:
                            thread.join(2)
                        if rpc.process is not None:
                            for stream in (
                                rpc.process.stdin,
                                rpc.process.stdout,
                                rpc.process.stderr,
                            ):
                                stream.close()
                    self.assertFalse(any(t.is_alive() for t in threads))
                    self.assertIsNotNone(rpc.process.poll())


if __name__ == '__main__':
    unittest.main()

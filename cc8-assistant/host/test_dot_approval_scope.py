"""Approval refusals belong to one thread/turn, including before start ACK."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from dot_client import CodexRpc, DotBrain, DotError
from test_dot_error_codes import THREAD, TURN, WireRpc, answer, terminal
from test_dot_receipt_delivery import ReceivingRpc


def approval(*, thread=THREAD, turn=TURN, method='item/commandExecution/requestApproval'):
    return {
        'id': 71,
        'method': method,
        'params': {'threadId': thread, 'turnId': turn, 'command': 'PRIVATE-command'},
    }


class ApprovalRpc(WireRpc):
    def __init__(self, events):
        super().__init__(events)
        self.sent = []

    def _send(self, value, **kwargs):
        self.sent.append(deepcopy(value))


class ApprovalScopeTests(unittest.TestCase):
    def failure(self, request, expected, *, completion=None):
        rpc = ApprovalRpc([request, completion or terminal()])
        updates = []
        brain = DotBrain(rpc)
        with self.assertRaises(DotError) as caught:
            list(brain.clauses('固定公开测试', [], on_web=lambda k, v: updates.append((k, v))))
        self.assertEqual(caught.exception.kind, expected)
        self.assertEqual(sum(m == 'turn/start' for m, _ in rpc.calls), 1)
        self.assertFalse(any(k == 'dot_result' for k, _ in updates))
        self.assertIsNone(brain.current)
        return rpc

    def test_old_or_other_thread_approval_cannot_relabel_current_failure(self):
        for fields in (
            {'turn': 'old-turn'},
            {'thread': 'other-thread'},
            {'thread': 'other-thread', 'turn': 'old-turn'},
        ):
            with self.subTest(fields=fields):
                rpc = self.failure(approval(**fields), 'provider_or_runtime')
                self.assertEqual(rpc.sent, [{'id': 71, 'result': {'decision': 'decline'}}])

    def test_malformed_scope_is_declined_but_cannot_relabel_failure(self):
        for key in ('threadId', 'turnId'):
            for value in (None, '', False, 71, [], {}, 'x' * 4097):
                with self.subTest(key=key, type=type(value).__name__):
                    request = approval()
                    request['params'][key] = value
                    self.failure(request, 'provider_or_runtime')
        for params in (None, [], 'invalid'):
            request = approval()
            request['params'] = params
            self.failure(request, 'provider_or_runtime')

    def test_pre_ack_matching_approval_is_retained_and_never_auto_accepted(self):
        for method in (
            'item/commandExecution/requestApproval',
            'item/fileChange/requestApproval',
            'item/permissions/requestApproval',
        ):
            with self.subTest(method=method):
                rpc = self.failure(approval(method=method), 'approval_declined')
                expected = (
                    {'permissions': {}, 'scope': 'turn'}
                    if method == 'item/permissions/requestApproval'
                    else {'decision': 'decline'}
                )
                self.assertEqual(rpc.sent, [{'id': 71, 'result': expected}])

    def test_current_approval_does_not_replace_specific_terminal_error(self):
        self.failure(approval(), 'quota', completion=terminal('usageLimitExceeded'))

    def test_approval_after_terminal_cannot_rewrite_failure_cause(self):
        for status in ('failed', 'completed', 'interrupted'):
            with self.subTest(status=status):
                rpc = ApprovalRpc([terminal(status=status), approval()])
                with self.assertRaises(DotError) as caught:
                    list(DotBrain(rpc).clauses('固定公开测试', []))
                self.assertEqual(caught.exception.kind, 'provider_or_runtime')
        rpc = ReceivingRpc()
        rpc._send = lambda value: None
        stream = DotBrain(rpc).clauses('固定公开测试', [])
        try:
            self.assertEqual(next(stream), '这是第一句。')
            rpc.event('turn/completed', turn={'id': 'stream-turn', 'status': 'failed'})
            rpc._server_request(approval(turn='stream-turn'))
            with self.assertRaises(DotError) as caught:
                list(stream)
            self.assertEqual(caught.exception.kind, 'provider_or_runtime')
        finally:
            stream.close()

    def test_success_after_approval_stays_completed(self):
        rpc = ApprovalRpc([approval(), answer(), terminal(status='completed')])
        updates = []
        result = ''.join(
            DotBrain(rpc).clauses('固定公开测试', [], on_web=lambda k, v: updates.append((k, v)))
        )
        self.assertEqual(result, '固定成功结果。')
        self.assertEqual(sum(k == 'dot_result' for k, _ in updates), 1)

    def test_receipt_watch_empty_answer_uses_exact_approval_scope(self):
        for thread, turn, expected in (
            (THREAD, 'old', 'provider_or_runtime'),
            ('other', 'stream-turn', 'provider_or_runtime'),
            (THREAD, 'stream-turn', 'approval_declined'),
        ):
            with self.subTest(thread=thread, turn=turn):
                rpc = ReceivingRpc()
                rpc._send = lambda value: None
                brain = DotBrain(rpc)
                stream = brain.clauses('固定公开测试', [])
                try:
                    self.assertEqual(next(stream), '这是第一句。')
                    rpc._server_request(approval(thread=thread, turn=turn))
                    rpc.complete('')
                    with self.assertRaises(DotError) as caught:
                        list(stream)
                    self.assertEqual(caught.exception.kind, expected)
                finally:
                    stream.close()

    def test_scope_storage_is_bounded_deduplicated_and_excludes_command(self):
        rpc = ApprovalRpc([])
        lookup = getattr(rpc, 'approval_required', None)
        self.assertTrue(callable(lookup))
        for index in range(400):
            rpc._server_request(approval(turn=f'fixed-{index}'))
        self.assertFalse(lookup(THREAD, 'fixed-0'))
        self.assertTrue(lookup(THREAD, 'fixed-399'))
        for _ in range(400):
            rpc._server_request(approval(turn='fixed-399'))
        self.assertTrue(lookup(THREAD, 'fixed-398'))
        self.assertFalse(lookup('other', 'fixed-399'))
        self.assertFalse(lookup(THREAD, None))
        self.assertLessEqual(len(rpc.approval_scopes), 128)
        self.assertNotIn('PRIVATE-command', repr(rpc.approval_scopes))

    def test_real_stdio_late_approval_between_two_turns_cannot_relabel_failure(self):
        script = """import json,sys
turns=0
def send(value):
 wire=(json.dumps(value)+'\\n').encode()
 sys.stdout.buffer.write(wire[:11]);sys.stdout.buffer.flush()
 sys.stdout.buffer.write(wire[11:]);sys.stdout.buffer.flush()
for line in sys.stdin.buffer:
 request=json.loads(line)
 if 'id' not in request:continue
 method=request.get('method')
 if method=='thread/start':result={'thread':{'id':'voice-thread'}}
 elif method=='turn/start':
  turns+=1
  ident='first-turn' if turns==1 else 'second-turn'
  send({'id':71,'method':sys.argv[1],
    'params':{'threadId':'voice-thread','turnId':'first-turn','command':'PRIVATE-command'}})
  refused=json.loads(sys.stdin.buffer.readline())
  expected={'permissions':{},'scope':'turn'} if sys.argv[1]=='item/permissions/requestApproval' else {'decision':'decline'}
  assert refused=={'id':71,'result':expected}
  send({'id':request['id'],'result':{'turn':{'id':ident}}})
  send({'method':'turn/completed','params':{'threadId':'voice-thread',
    'turn':{'id':ident,'status':'failed','error':None,'fixtureTurnCount':turns}}})
  continue
 else:result={}
 send({'id':request['id'],'result':result})
"""
        original_thread = threading.Thread
        with tempfile.TemporaryDirectory(prefix='cc8-fixed-approval-peer-') as temporary:
            path = Path(temporary) / 'peer.py'
            path.write_text(script)
            for method in (
                'item/commandExecution/requestApproval',
                'item/fileChange/requestApproval',
                'item/permissions/requestApproval',
            ):
                with self.subTest(method=method):
                    threads, seen = [], []

                    def make_thread(*args, **kwargs):
                        thread = original_thread(*args, **kwargs)
                        threads.append(thread)
                        return thread

                    rpc = CodexRpc(command=[sys.executable, '-u', str(path), method])
                    remove = rpc.listen(seen.append)
                    brain = DotBrain(rpc)
                    try:
                        with patch('dot_client.threading.Thread', side_effect=make_thread):
                            for kind in ('approval_declined', 'provider_or_runtime'):
                                with self.assertRaises(DotError) as caught:
                                    list(brain.clauses('固定公开测试', []))
                                self.assertEqual(caught.exception.kind, kind)
                        terminals = [e for e in seen if e.get('method') == 'turn/completed']
                        self.assertEqual(
                            [e['params']['turn']['fixtureTurnCount'] for e in terminals], [1, 2]
                        )
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

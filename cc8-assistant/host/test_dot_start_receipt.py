"""Unconfirmed Dot starts traverse real stdio and the production task worker."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from brain_router import BrainRouter
from dot_client import CodexRpc, DotBrain, DotError
from reply_types import Reply
from test_dot_client import FakeRpc
import test_failure_feedback as support


PEER = r"""
import json, pathlib, sys
mode, audit = sys.argv[1:]
for line in sys.stdin:
    req = json.loads(line)
    method = req.get('method')
    if method == 'initialized':
        continue
    ident = req['id']
    result = {}
    if method == 'thread/start':
        result = {'thread': {'id': 'fixed-thread'}}
    if method == 'turn/start':
        with pathlib.Path(audit).open('a') as stream:
            stream.write(mode + '\n')
        if mode == 'silent':
            continue
        if mode == 'malformed':
            result = {'turn': {'id': False}}
        if mode == 'reject':
            print(json.dumps({'id': ident, 'error': {'message': 'PRIVATE_PEER_ERROR'}}), flush=True)
            continue
        if mode == 'success':
            result = {'turn': {'id': 'fixed-turn'}}
            print(json.dumps({'id': ident, 'result': result}), flush=True)
            for event in (
                {'method': 'item/completed', 'params': {
                    'threadId': 'fixed-thread', 'turnId': 'fixed-turn',
                    'item': {'id': 'final', 'type': 'agentMessage',
                             'phase': 'final_answer', 'text': '固定恢复结果。'},
                }},
                {'method': 'turn/completed', 'params': {
                    'threadId': 'fixed-thread', 'turn': {'id': 'fixed-turn', 'status': 'completed'},
                }},
            ):
                print(json.dumps(event, ensure_ascii=False), flush=True)
            continue
    print(json.dumps({'id': ident, 'result': result}), flush=True)
"""


class ShortStartRpc(CodexRpc):
    def request(self, method, params, timeout=15):
        return super().request(method, params, timeout=0.2 if method == 'turn/start' else timeout)


class StartReceiptTests(unittest.TestCase):
    def peer(self, folder, mode, audit):
        path = folder / 'peer.py'
        path.write_text(PEER)
        return ShortStartRpc(
            command=[sys.executable, '-I', '-S', str(path), mode, str(audit)],
            popen=lambda args, **kwargs: subprocess.Popen(
                args, **{**kwargs, 'env': {'PATH': '/usr/bin:/bin'}, 'cwd': folder}
            ),
        )

    def close(self, rpc):
        rpc.close()
        if rpc.process is not None:
            for stream in (rpc.process.stdin, rpc.process.stdout, rpc.process.stderr):
                stream.close()

    def test_unknown_start_closes_old_peer_without_retry_and_next_request_uses_new_peer(self):
        for mode, category in (('silent', 'timeout'), ('malformed', 'provider_or_runtime')):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                folder = Path(folder)
                audit = folder / 'audit'
                old = self.peer(folder, mode, audit)
                dot = DotBrain(old)
                new = self.peer(folder, 'success', audit)
                dot.rpc_factory = Mock(return_value=new)
                worker = support.FailureFeedbackTests()
                b = worker.session(brain=BrainRouter(support.Brain(), dot))
                b.speak = lambda *args: True
                try:
                    worker.run_one(b.response, Reply(b.generation, '固定测试'))
                    task = b.workspace.tasks[-1]
                    self.assertEqual(task['status'], 'failed')
                    self.assertEqual(task['stage'], '执行结果待核对')
                    self.assertIn('电脑端核对', task['result'])
                    self.assertIn('结果待核对', worker.guidance(b)[-1])
                    row = b.diagnostics.report()['requests'][-1]
                    self.assertEqual(row['error'], {'stage': 'brain', 'kind': category})
                    self.assertTrue(old.failed)
                    self.assertIsNotNone(old.process.poll())
                    self.assertIsNone(dot.current)
                    self.assertEqual(audit.read_text().splitlines(), [mode])
                    dot.rpc_factory.assert_not_called()
                    b.cancel('new_explicit_test')
                    worker.run_one(b.response, Reply(b.generation, '固定新请求'))
                    dot.rpc_factory.assert_called_once()
                    self.assertEqual(audit.read_text().splitlines(), [mode, 'success'])
                    self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
                    self.assertEqual(b.workspace.tasks[-2]['stage'], '执行结果待核对')
                finally:
                    self.close(old)
                    self.close(new)

    def test_explicit_start_error_does_not_mark_execution_unconfirmed_or_close_peer(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            audit = folder / 'audit'
            rpc = self.peer(folder, 'reject', audit)
            worker = support.FailureFeedbackTests()
            b = worker.session(brain=BrainRouter(support.Brain(), DotBrain(rpc)))
            try:
                worker.run_one(b.response, Reply(b.generation, '固定测试'))
                self.assertEqual(b.workspace.tasks[-1]['stage'], '请求未完成')
                self.assertNotIn('结果待核对', worker.guidance(b)[-1])
                self.assertNotIn('PRIVATE_PEER_ERROR', str(b.events))
                self.assertFalse(rpc.failed)
                self.assertEqual(audit.read_text().splitlines(), ['reject'])
            finally:
                self.close(rpc)

    def test_cleanup_uncertainty_blocks_replacement(self):
        rpc = FakeRpc()
        rpc.failed = True
        rpc.close = Mock(return_value=False)
        dot = DotBrain(rpc)
        dot.rpc_factory = Mock()
        with self.assertRaisesRegex(DotError, 'still closing'):
            list(dot.clauses('固定测试', []))
        dot.rpc_factory.assert_not_called()
        self.assertEqual(rpc.calls, [])

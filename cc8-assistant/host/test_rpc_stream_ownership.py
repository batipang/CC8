"""Parent pipe ownership on local RPC exit, without providers or tools."""

import sys
import threading
import time
import unittest
from unittest.mock import patch
from dot_client import CodexRpc, DotError

PEER = (
    'import json,sys\n'
    'for line in sys.stdin:\n'
    ' request=json.loads(line)\n'
    ' if "id" in request:\n'
    '  print(json.dumps({"id":request["id"],"result":{}}),flush=True)\n'
)


class RpcStreamOwnershipTests(unittest.TestCase):
    def wait_closed(self, process):
        until = time.monotonic() + 2
        while (
            not all(s.closed for s in (process.stdin, process.stdout, process.stderr))
            and time.monotonic() < until
        ):
            time.sleep(0.005)
        self.assertTrue(
            all(s.closed for s in (process.stdin, process.stdout, process.stderr)),
            'Confirmed RPC close retains parent pipe descriptors',
        )

    def test_confirmed_close_releases_owned_stdio_without_garbage_collection(self):
        rpc = CodexRpc(command=[sys.executable, '-I', '-S', '-u', '-c', PEER])
        try:
            with patch('dot_client.runtime_environment', return_value={'PATH': '/usr/bin:/bin'}):
                rpc.start()
            process = rpc.process
            self.assertTrue(rpc.close())
            self.wait_closed(process)
            self.assertIsNotNone(process.poll())
        finally:
            rpc.close()
            if rpc.process:
                for stream in (rpc.process.stdin, rpc.process.stdout, rpc.process.stderr):
                    stream.close()

    def test_reader_keeps_its_pipe_until_an_admitted_receipt_finishes(self):
        peer = (
            'import json,sys\nfor line in sys.stdin:\n'
            ' request=json.loads(line)\n'
            ' if request.get("method")=="fixture/emit":\n'
            '  print(json.dumps({"method":"item/completed","params":{"item":{"type":"agentMessage","text":"fixed"}}}),flush=True)\n'
            ' elif "id" in request: print(json.dumps({"id":request["id"],"result":{}}),flush=True)\n'
        )
        rpc = CodexRpc(command=[sys.executable, '-I', '-S', '-u', '-c', peer])
        entered, release = threading.Event(), threading.Event()
        observed = []

        def listener(event):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Test did not release admitted receipt')
            observed.append(event)

        try:
            with patch('dot_client.runtime_environment', return_value={'PATH': '/usr/bin:/bin'}):
                rpc.start()
            rpc.listen(listener)
            rpc.notify('fixture/emit', {})
            self.assertTrue(entered.wait(1))
            self.assertTrue(rpc.close())
            self.assertFalse(rpc.process.stdout.closed)
            release.set()
            self.wait_closed(rpc.process)
            self.assertEqual(len(observed), 1)
            self.assertEqual(rpc.events.get(timeout=1), observed[0])
        finally:
            release.set()
            rpc.close()
            if rpc.process:
                for stream in (rpc.process.stdin, rpc.process.stdout, rpc.process.stderr):
                    stream.close()

    def test_partial_reader_startup_failure_stops_guardian_and_releases_pipes(self):
        for failure_at in (1, 2, 3):
            with self.subTest(failure_at=failure_at):
                self.partial_reader_failure(failure_at)

    def partial_reader_failure(self, failure_at):
        native_start = threading.Thread.start
        rpc = CodexRpc(command=[sys.executable, '-I', '-S', '-u', '-c', PEER])
        primary = RuntimeError('test thread start failure')
        calls = []

        def start(thread):
            calls.append(thread)
            if len(calls) == failure_at:
                raise primary
            return native_start(thread)

        try:
            with (
                patch('dot_client.runtime_environment', return_value={'PATH': '/usr/bin:/bin'}),
                patch('dot_client.threading.Thread.start', new=start),
                self.assertRaises(RuntimeError) as caught,
            ):
                rpc.start()
            self.assertIs(caught.exception, primary)
            self.assertIsNotNone(rpc.process.poll())
            self.assertTrue(rpc.failed)
            self.wait_closed(rpc.process)
            with self.assertRaises(DotError):
                rpc.start()
        finally:
            rpc.close()
            if rpc.process:
                for stream in (rpc.process.stdin, rpc.process.stdout, rpc.process.stderr):
                    stream.close()

    def test_close_cancels_a_backpressured_writer_before_closing_its_pipe(self):
        peer = (
            'import json,sys,time\nfor line in sys.stdin:\n'
            ' request=json.loads(line)\n'
            ' if "id" in request: print(json.dumps({"id":request["id"],"result":{}}),flush=True)\n'
            ' if request.get("method")=="fixture/freeze":time.sleep(20)\n'
        )
        rpc = CodexRpc(command=[sys.executable, '-I', '-S', '-u', '-c', peer])
        done, errors = threading.Event(), []
        writer = None

        def send():
            try:
                rpc.request('fixture/large', {'text': 'x' * 262144}, timeout=10)
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        try:
            with patch('dot_client.runtime_environment', return_value={'PATH': '/usr/bin:/bin'}):
                rpc.start()
            rpc.request('fixture/freeze', {})
            writer = threading.Thread(target=send, daemon=True)
            writer.start()
            until = time.monotonic() + 1
            while not rpc.writer.lock.locked() and not done.is_set() and time.monotonic() < until:
                time.sleep(0.002)
            self.assertTrue(rpc.writer.lock.locked())
            self.assertFalse(done.is_set())
            self.assertTrue(rpc.close())
            self.assertTrue(done.wait(2))
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], DotError)
            self.assertEqual(rpc.pending, {})
            self.wait_closed(rpc.process)
        finally:
            rpc.close()
            if writer is not None:
                writer.join(2)
            if rpc.process:
                for stream in (rpc.process.stdin, rpc.process.stdout, rpc.process.stderr):
                    stream.close()


if __name__ == '__main__':
    unittest.main()

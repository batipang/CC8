"""Real local pipe backpressure; no device, model, network or tool execution."""

import json
import os
import threading
import time
from types import SimpleNamespace
import unittest

from dot_client import CodexRpc, DotError
from dot_receipt import UnconfirmedTurnStart


class PipeFixture:
    def __init__(self):
        read_fd, write_fd = os.pipe()
        self.read = os.fdopen(read_fd, 'rb', buffering=0)
        self.write = os.fdopen(write_fd, 'wb', buffering=0)
        self.rpc = CodexRpc(command=[])
        self.rpc.process = SimpleNamespace(stdin=self.write)
        self.threads = []

    def spawn(self, callback):
        result = {'done': threading.Event()}

        def run():
            try:
                result['value'] = callback()
            except Exception as error:
                result['error'] = error
            finally:
                result['done'].set()

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return result

    def receive(self, count, *, before_read=0, before_reply=0):
        time.sleep(before_read)
        data = bytearray()
        rows = []
        while len(rows) < count:
            chunk = self.read.read(65536)
            if not chunk:
                raise RuntimeError('Fixture pipe closed before complete packet')
            data.extend(chunk)
            while b'\n' in data:
                line, _, rest = data.partition(b'\n')
                data = bytearray(rest)
                rows.append(json.loads(line))
        time.sleep(before_reply)
        for row in rows:
            with self.rpc.lock:
                waiter = self.rpc.pending.get(row['id'])
            if waiter is not None:
                waiter.put_nowait({'id': row['id'], 'result': {'received': row['params']}})
        return rows

    def close(self):
        # Release even the old blocking implementation before assertions leave
        # the fixture. No test thread or open pipe may leak into another case.
        self.read.close()
        for thread in self.threads:
            thread.join(2)
        self.write.close()
        if any(thread.is_alive() for thread in self.threads):
            raise AssertionError('Local pipe fixture thread did not exit')


class DotRpcIoTests(unittest.TestCase):
    def setUp(self):
        self.pipe = PipeFixture()
        self.addCleanup(self.pipe.close)

    def request_async(self, *, timeout=0.08, text=None):
        return self.pipe.spawn(
            lambda: self.pipe.rpc.request(
                'fixture', {'text': text if text is not None else '公开合成输入' * 32768}, timeout
            )
        )

    def assert_failure(self, result, kind):
        self.assertTrue(result['done'].wait(1.2), 'Pipe write outlived request budget')
        self.assertIsInstance(result.get('error'), DotError)
        self.assertEqual(result['error'].kind, kind)

    def test_turn_start_timeout_after_complete_write_has_unknown_outcome(self):
        reader = self.pipe.spawn(lambda: self.pipe.receive(1, before_reply=0.15))
        result = self.pipe.spawn(lambda: self.pipe.rpc.request('turn/start', {}, timeout=0.06))
        self.assert_failure(result, 'timeout')
        self.assertIsInstance(result['error'], UnconfirmedTurnStart)
        self.assertTrue(reader['done'].wait(0.5))
        self.assertEqual(reader['value'][0]['method'], 'turn/start')
        self.assertEqual(self.pipe.rpc.pending, {})

    def test_turn_start_timeout_waiting_to_write_is_not_an_unknown_dispatch(self):
        with self.pipe.rpc.writer.lock:
            result = self.pipe.spawn(lambda: self.pipe.rpc.request('turn/start', {}, timeout=0.04))
            self.assert_failure(result, 'timeout')
        self.assertNotIsInstance(result['error'], UnconfirmedTurnStart)
        self.assertFalse(self.pipe.rpc.failed)
        self.assertEqual(self.pipe.rpc.pending, {})

    def test_full_pipe_times_out_and_partial_packet_cannot_be_reused(self):
        result = self.request_async()
        self.assert_failure(result, 'timeout')
        self.assertEqual(self.pipe.rpc.pending, {})
        self.assertTrue(self.pipe.rpc.failed)
        before = os.read(self.pipe.read.fileno(), 65536)
        self.assertTrue(before)
        self.assertNotIn(b'\n', before)
        with self.assertRaises(DotError) as caught:
            self.pipe.rpc.request('mustNotAppendToPartialPacket', {}, timeout=0.05)
        self.assertEqual(caught.exception.kind, 'transport_or_io')

    def test_waiting_writer_has_own_budget_without_blocking_pending_receipts(self):
        first = self.request_async(timeout=2)
        time.sleep(0.04)
        second = self.request_async(timeout=0.05, text='second')
        self.assert_failure(second, 'timeout')
        self.assertFalse(first['done'].is_set())
        self.assertFalse(self.pipe.rpc.failed, 'Unsent lock timeout must not poison first writer')
        self.assertEqual(len(self.pipe.rpc.pending), 1)

    def test_disconnect_interrupts_backpressured_write(self):
        result = self.request_async(timeout=10)
        time.sleep(0.04)
        self.pipe.rpc.failed = True
        self.assert_failure(result, 'transport_or_io')
        self.assertEqual(self.pipe.rpc.pending, {})

    def test_broken_pipe_is_transport_failure_and_marks_stream_unusable(self):
        self.pipe.read.close()
        result = self.request_async(text='short')
        self.assert_failure(result, 'transport_or_io')
        self.assertTrue(self.pipe.rpc.failed)
        self.assertEqual(self.pipe.rpc.pending, {})

    def test_backpressured_writer_does_not_block_an_earlier_response(self):
        received, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def peer():
            data = bytearray()
            while b'\n' not in data:
                chunk = self.pipe.read.read(4096)
                if not chunk:
                    raise RuntimeError('Fixture closed before first packet')
                data.extend(chunk)
            row = json.loads(data.partition(b'\n')[0])
            received.set()
            if not release.wait(2):
                raise RuntimeError('Fixture response gate expired')
            with self.pipe.rpc.lock:
                waiter = self.pipe.rpc.pending.get(row['id'])
            if waiter is not None:
                waiter.put_nowait({'id': row['id'], 'result': {'received': row['params']}})

        reader = self.pipe.spawn(peer)
        first = self.request_async(timeout=2, text='first')
        self.assertTrue(received.wait(1))
        second = self.request_async(timeout=2)
        time.sleep(0.04)
        release.set()
        self.assertTrue(first['done'].wait(0.5), 'Response mutex held by pipe writer')
        self.assertNotIn('error', first)
        self.assertEqual(first['value'], {'received': {'text': 'first'}})
        self.assertTrue(reader['done'].wait(0.5))
        self.assertNotIn('error', reader)
        self.assertFalse(second['done'].is_set())

    def test_write_and_response_share_one_budget(self):
        # Actual writes unblock after 250ms. A response at ~700ms is later
        # than the 600ms total budget, but inside the old fresh reply budget.
        peer = self.pipe.spawn(lambda: self.pipe.receive(1, before_read=0.25, before_reply=0.45))
        result = self.request_async(timeout=0.6)
        self.assert_failure(result, 'timeout')
        self.assertTrue(peer['done'].wait(2))
        self.assertNotIn('error', peer)
        self.assertEqual(self.pipe.rpc.pending, {})

    def test_concurrent_unicode_packets_remain_complete_and_received_once(self):
        peer = self.pipe.spawn(lambda: self.pipe.receive(2))
        first = self.request_async(timeout=2, text='第一份固定数据' * 8192)
        second = self.request_async(timeout=2, text='second fixture' * 8192)
        for result in (first, second, peer):
            self.assertTrue(result['done'].wait(3))
            self.assertNotIn('error', result)
        rows = peer['value']
        self.assertEqual(len({row['id'] for row in rows}), 2)
        self.assertEqual(
            {row['params']['text'] for row in rows},
            {'第一份固定数据' * 8192, 'second fixture' * 8192},
        )
        self.assertEqual(self.pipe.rpc.pending, {})
        self.assertFalse(self.pipe.rpc.failed)


if __name__ == '__main__':
    unittest.main()

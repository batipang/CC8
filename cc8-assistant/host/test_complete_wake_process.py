"""Actual anonymous child IPC, bounded full inputs and late-close isolation."""

import select
import tempfile
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import numpy as np

from complete_wake_process import CompleteWakeVerifierProcess, MAX_COMPLETE_SAMPLES


WORKER = """
import os,socket,struct,sys,time
assert 'CC8_MINIMAX_API_KEY' not in os.environ
with socket.socket(fileno=int(sys.argv[1])) as s:
 s.sendall(b'R')
 while True:
  header=s.recv(4,socket.MSG_WAITALL)
  if not header:break
  size,=struct.unpack('>I',header)
  data=s.recv(size,socket.MSG_WAITALL)
  assert len(data)==size
  REPLY
"""


class CompleteProcessTests(unittest.TestCase):
    def worker(self, reply="s.sendall(b'\\x01')"):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        p = Path(folder.name) / 'worker.py'
        p.write_text(WORKER.replace('REPLY', reply))
        w = CompleteWakeVerifierProcess()
        w._worker_script = p
        self.addCleanup(w.close)
        with patch.dict('os.environ', {'CC8_MINIMAX_API_KEY': 'test-only-inherited-sentinel'}):
            w.start()
        self.assertTrue(select.select((w._channel,), (), (), 2)[0])
        self.assertTrue(w.ready)
        return w

    def test_full_bound_exceeds_legacy_window_and_returns_fixed_verdict(self):
        w = self.worker()
        source = np.ones(MAX_COMPLETE_SAMPLES, dtype=np.float32)
        self.assertEqual(w.classify_complete(source), (True, False))
        self.assertTrue(np.all(source == 1))
        self.assertTrue(w.ready)

    def test_invalid_input_never_sends_or_closes_a_healthy_channel(self):
        w = self.worker()
        channel = w._channel
        for source in (
            [],
            np.zeros(0),
            np.ones(161),
            np.zeros((1, 160)),
            np.ones(MAX_COMPLETE_SAMPLES + 160),
            np.full(160, np.nan),
            np.ones(160, dtype=np.int16),
        ):
            with self.subTest(shape=getattr(source, 'shape', None)):
                self.assertIsNone(w.classify_complete(source))
                self.assertIs(w._channel, channel)
        self.assertEqual(w.classify_complete(np.zeros(160)), (True, False))

    def test_unknown_is_a_valid_reply_but_invalid_or_timeout_is_unavailable(self):
        w = self.worker("s.sendall(b'\\x00')")
        self.assertEqual(w.classify_complete(np.zeros(160)), (False, False))
        bad = self.worker("s.sendall(b'x')")
        self.assertIsNone(bad.classify_complete(np.zeros(160)))
        self.assertFalse(bad.ready)
        slow = self.worker("time.sleep(5);s.sendall(b'\\x01')")
        process = slow._process
        start = time.monotonic()
        self.assertIsNone(slow.classify_complete(np.zeros(160)))
        self.assertLess(time.monotonic() - start, 1.8)
        self.assertIsNotNone(process.wait(timeout=2))
        self.assertFalse(slow.ready)

    def test_old_decode_error_cannot_close_replacement(self):
        w = self.worker("time.sleep(5);s.sendall(b'\\x01')")
        old = w._process
        entered = threading.Event()
        release = threading.Event()
        result = []
        original = w._invalidate

        def late(channel, process):
            entered.set()
            self.assertTrue(release.wait(2))
            original(channel, process)

        w._invalidate = late
        t = threading.Thread(target=lambda: result.append(w.classify_complete(np.zeros(160))))
        t.start()
        try:
            self.assertTrue(entered.wait(2))
            w.close()
            self.assertIsNotNone(old.wait(timeout=2))
            w.start()
            self.assertTrue(select.select((w._channel,), (), (), 2)[0])
            self.assertTrue(w.ready)
            new = w._process
            channel = w._channel
            self.assertIsNone(w.classify_complete(np.zeros(160)))
        finally:
            release.set()
            t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(result, [None])
        self.assertIs(w._channel, channel)
        self.assertIs(w._process, new)
        self.assertTrue(w.ready)

    def test_no_model_load_or_process_at_construction(self):
        w = CompleteWakeVerifierProcess()
        self.assertIsNone(w._channel)
        self.assertIsNone(w._process)
        self.assertIsNone(w.classify_complete(np.zeros(160)))


if __name__ == '__main__':
    unittest.main()

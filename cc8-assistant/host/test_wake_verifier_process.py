import os
from pathlib import Path
import select
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from wake_verifier_process import WakeVerifierProcess


FAKE_WORKER = """
import os
import socket
import struct
import sys
import time

assert 'CC8_WAKE_PRIVATE_SENTINEL' not in os.environ
with socket.socket(fileno=int(sys.argv[1])) as stream:
    time.sleep(0.05)
    stream.sendall(b'R')
    while header := stream.recv(4, socket.MSG_WAITALL):
        size, = struct.unpack('>I', header)
        data = stream.recv(size, socket.MSG_WAITALL)
        assert len(data) == size
        {reply}
"""


class WakeVerifierProcessTests(unittest.TestCase):
    def launch(self, reply):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        script = Path(folder.name) / 'fixture.py'
        script.write_text(FAKE_WORKER.format(reply=reply))
        worker = WakeVerifierProcess()
        self.addCleanup(worker.close)
        with (
            patch('wake_verifier_process.SCRIPT', script),
            patch.dict(os.environ, {'CC8_WAKE_PRIVATE_SENTINEL': 'private'}),
        ):
            worker.start()
            worker.start()
        return worker

    def wait_for_ack(self, worker):
        self.assertTrue(select.select((worker._channel,), (), (), 3)[0])

    def test_ready_reply_can_be_observed_after_calibration_and_close_reaps(self):
        worker = self.launch("stream.sendall(b'\\x01')")
        self.wait_for_ack(worker)
        worker._startup_deadline = time.monotonic() - 1
        self.assertTrue(worker.ready)
        self.assertEqual(worker.classify(np.zeros(160)), (True, False))
        self.assertEqual(worker.classify(np.ones(28_800)), (True, False))
        process = worker._process
        worker.close()
        self.assertIsNotNone(process.wait(timeout=2))
        self.assertFalse(worker.ready)

    def test_timeout_invalidates_pipe_and_prevents_reusing_stale_reply(self):
        worker = self.launch("time.sleep(5); stream.sendall(b'\\x01')")
        self.wait_for_ack(worker)
        process = worker._process
        started = time.monotonic()
        self.assertEqual(worker.classify(np.ones(160)), (False, False))
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(worker.ready)
        self.assertEqual(worker.classify(np.ones(160)), (False, False))
        self.assertIsNotNone(process.wait(timeout=2))

    def test_invalid_reply_or_oversized_input_closes_the_channel(self):
        for size in (160, 28_801):
            with self.subTest(size=size):
                worker = self.launch("stream.sendall(b'x')")
                self.wait_for_ack(worker)
                self.assertEqual(worker.classify(np.ones(size)), (False, False))
                self.assertFalse(worker.ready)

    def test_process_launch_failure_leaves_standard_wake_available(self):
        worker = WakeVerifierProcess()
        with patch('wake_verifier_process.subprocess.Popen', side_effect=OSError('unavailable')):
            worker.start()
        self.assertFalse(worker.ready)
        self.assertIsNone(worker._channel)
        self.assertIsNone(worker._process)
        self.assertEqual(worker.classify(np.zeros(160)), (False, False))

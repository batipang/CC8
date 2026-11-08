"""Real child replacement and model-lease/session commit ordering."""

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
import numpy as np

from complete_wake_process import CompleteWakeVerifierProcess
from dsp import FRAME
from local_wake import WakeDetector
from wake_process_recovery import CaptureWakeRecovery
import test_wake_controller as fixtures

SCRIPT = """
import socket,struct,sys
with socket.socket(fileno=int(sys.argv[1])) as s:
 s.sendall(b'R')
 while True:
  header=s.recv(4,socket.MSG_WAITALL)
  if not header:break
  size,=struct.unpack('>I',header)
  assert len(s.recv(size,socket.MSG_WAITALL))==size
  s.sendall(b'\\x01')
"""


class RecoveryIntegrationTests(unittest.TestCase):
    def wait_ready(self, actor):
        deadline = time.monotonic() + 2
        while not actor.available and time.monotonic() < deadline:
            actor.poll_capture()
            time.sleep(0.002)
        self.assertTrue(actor.available)

    def test_real_dead_child_is_reaped_replaced_and_never_restarted_after_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker.py'
            path.write_text(SCRIPT)
            worker = CompleteWakeVerifierProcess()
            worker._worker_script = path
            now = [0.0]
            actor = CaptureWakeRecovery(worker, clock=lambda: now[0])
            try:
                actor.start_capture()
                self.wait_ready(actor)
                old, generation = worker._process, actor.generation
                old.kill()
                old.wait(timeout=2)
                actor.poll_capture()
                self.assertEqual(actor.snapshot['state'], 'retry_wait')
                now[0] = 2
                actor.poll_capture()
                self.wait_ready(actor)
                self.assertGreater(actor.generation, generation)
                self.assertIsNot(worker._process, old)
                self.assertIsNotNone(old.returncode)
                self.assertEqual(worker.classify_complete(np.zeros(FRAME)), (True, False))
                latest = worker._process
                actor.stop_capture()
                self.assertIsNotNone(latest.wait(timeout=2))
                now[0] = 100
                actor.poll_capture()
                self.assertFalse(worker.running)
                self.assertEqual(actor.snapshot['start_attempts'], 2)
            finally:
                actor.stop_capture()

    def owner(self):
        f = fixtures.WakeControllerTests()
        f.setUp()
        worker = Mock(ready=True, running=True, last_ready=True)
        worker.classify_complete.return_value = (True, False)
        detector = WakeDetector(
            Mock(), Mock(), chinese_complete=worker, complete_chinese_enabled=True
        )
        detector.chinese.feed.return_value = '大黄蜂'
        detector.english.feed.return_value = ''
        detector.start_capture()
        detector.poll_capture()
        f.controller.detector = detector
        ticket = f.controller.waiting_utterance(100)
        f.controller.feed(np.zeros(FRAME), 100, capture_nanos=150)
        ticket = f.controller.complete_chinese_ticket(ticket, 200)
        return f, detector, ticket

    def test_real_decode_timeout_discards_result_and_next_owner_can_use_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker.py'
            # Stall only the isolated child after receiving one bounded request.
            path.write_text(SCRIPT.replace("  s.sendall(b'\\x01')", '  import time;time.sleep(10)'))
            worker = CompleteWakeVerifierProcess()
            worker._worker_script = path
            now = [0.0]
            actor = CaptureWakeRecovery(worker, clock=lambda: now[0])
            try:
                actor.start_capture()
                self.wait_ready(actor)
                old = worker._process
                self.assertIsNone(worker.classify_complete(np.zeros(FRAME)))
                self.assertIsNotNone(old.wait(timeout=2))
                actor.poll_capture()
                path.write_text(SCRIPT)
                now[0] = 2
                actor.poll_capture()
                self.wait_ready(actor)
                self.assertIsNot(worker._process, old)
                self.assertEqual(worker.classify_complete(np.zeros(FRAME)), (True, False))
                self.assertEqual(actor.snapshot['start_attempts'], 2)
            finally:
                actor.stop_capture()

    def test_model_transition_cannot_split_final_check_and_wake_commit(self):
        f, detector, ticket = self.owner()
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        results = []

        def awake(value):
            entered.set()
            self.assertTrue(release.wait(2))
            f.set_awake(value)

        f.controller.hooks = replace(f.controller.hooks, set_awake=awake)
        commit = threading.Thread(
            target=lambda: results.append(
                f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100)
            )
        )
        stop = threading.Thread(target=lambda: (detector.stop_capture(), stopped.set()))
        commit.start()
        try:
            self.assertTrue(entered.wait(1))
            stop.start()
            self.assertFalse(stopped.wait(0.05))
        finally:
            release.set()
            commit.join(2)
            if stop.ident is not None:
                stop.join(2)
        self.assertFalse(commit.is_alive())
        self.assertFalse(stop.is_alive())
        self.assertEqual(results, [True])
        self.assertTrue(stopped.is_set())

    def test_commit_guard_is_released_before_route_activity_callbacks(self):
        f, detector, ticket = self.owner()
        finished = threading.Event()
        stop = threading.Thread(target=lambda: (detector.stop_capture(), finished.set()))

        def touch():
            stop.start()
            self.assertTrue(finished.wait(1))
            f.touch()

        f.controller.hooks = replace(f.controller.hooks, touch_activity=touch)
        try:
            self.assertTrue(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
        finally:
            if stop.ident is not None:
                stop.join(2)
        self.assertFalse(stop.is_alive())
        self.assertTrue(f.state.awake)

    def test_route_activity_cancellation_does_not_publish_stale_wake_status(self):
        f, detector, ticket = self.owner()
        f.controller.hooks = replace(
            f.controller.hooks, touch_activity=lambda: f.cancel('test_route_loss')
        )
        self.assertIsNone(f.controller.recheck_utterance(ticket, np.zeros(FRAME), 100))
        self.assertNotIn(('listening', '我在听，请继续说'), f.statuses)


if __name__ == '__main__':
    unittest.main()

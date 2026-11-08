"""Capture ownership, bounded restarts and nonblocking health observations."""

import threading
import time
import unittest
from unittest.mock import Mock

from wake_process_recovery import CaptureWakeRecovery


class RecoveryTests(unittest.TestCase):
    def fixture(self):
        worker = Mock(ready=False, running=False)
        now = [0.0]
        actor = CaptureWakeRecovery(worker, clock=lambda: now[0])
        return actor, worker, now

    def test_no_start_from_construction_health_or_unowned_frames(self):
        actor, worker, now = self.fixture()
        for stamp in (0, 100, 10000):
            now[0] = stamp
            actor.poll_capture()
            self.assertEqual(actor.snapshot['state'], 'stopped')
        worker.start.assert_not_called()

    def test_failed_burst_is_bounded_and_manual_capture_restart_resets_it(self):
        actor, worker, now = self.fixture()
        actor.start_capture()
        actor.start_capture()
        self.assertEqual(worker.start.call_count, 1)
        actor.poll_capture()
        self.assertEqual(actor.snapshot['retry_in_ms'], 2000)
        now[0] = 1.99
        actor.poll_capture()
        self.assertEqual(worker.start.call_count, 1)
        now[0] = 2
        actor.poll_capture()
        self.assertEqual(worker.start.call_count, 2)
        actor.poll_capture()
        now[0] = 7
        actor.poll_capture()
        self.assertEqual(worker.start.call_count, 3)
        actor.poll_capture()
        self.assertEqual(actor.snapshot['state'], 'failed')
        now[0] = 100000
        actor.poll_capture()
        self.assertEqual(worker.start.call_count, 3)
        actor.stop_capture()
        actor.start_capture()
        self.assertEqual(worker.start.call_count, 4)
        self.assertEqual(actor.snapshot['budget_used'], 1)

    def test_loading_and_unreaped_workers_are_never_duplicated(self):
        actor, worker, now = self.fixture()
        actor.start_capture()
        worker.running = True
        for stamp in (1, 2, 3):
            now[0] = stamp
            actor.poll_capture()
        self.assertEqual(worker.start.call_count, 1)
        worker.running = False
        actor.poll_capture()
        now[0] += 2
        actor.poll_capture()
        self.assertEqual(worker.start.call_count, 2)

    def test_unreapable_start_is_explicitly_failed_without_starting_a_duplicate(self):
        actor, worker, now = self.fixture()
        actor.start_capture()
        worker.running = True
        now[0] = 7
        actor.poll_capture()
        self.assertEqual(actor.snapshot['state'], 'failed')
        worker.close.assert_called_once_with()
        now[0] = 100
        actor.poll_capture()
        self.assertEqual(worker.start.call_count, 1)

    def test_spawn_exceptions_consume_the_same_bounded_budget(self):
        actor, worker, now = self.fixture()
        worker.start.side_effect = OSError('test-only spawn error')
        actor.start_capture()
        actor.poll_capture()
        now[0] = 2
        actor.poll_capture()
        actor.poll_capture()
        now[0] = 7
        actor.poll_capture()
        actor.poll_capture()
        self.assertEqual(actor.snapshot['state'], 'failed')
        self.assertEqual(worker.start.call_count, 3)
        self.assertEqual(worker.close.call_count, 3)

    def test_inspection_or_unreaped_close_failure_keeps_circuit_closed(self):
        for inspection_error in (False, True):
            with self.subTest(inspection_error=inspection_error):
                actor, worker, now = self.fixture()
                actor.start_capture()
                worker.running = True
                worker.close.side_effect = OSError('test-only close error')
                if inspection_error:
                    from unittest.mock import PropertyMock

                    type(worker).ready = PropertyMock(side_effect=OSError('test-only read error'))
                now[0] = 7
                actor.poll_capture()
                self.assertEqual(actor.snapshot['state'], 'failed')
                now[0] = 100
                actor.poll_capture()
                self.assertEqual(worker.start.call_count, 1)
                worker.close.assert_called_once_with()

    def test_stable_ready_replenishes_budget_but_does_not_spawn(self):
        actor, worker, now = self.fixture()
        actor.start_capture()
        worker.ready = worker.running = True
        actor.poll_capture()
        now[0] = 29.99
        actor.poll_capture()
        self.assertEqual(actor.snapshot['budget_used'], 1)
        now[0] = 30
        actor.poll_capture()
        self.assertEqual(actor.snapshot['budget_used'], 0)
        worker.ready = worker.running = False
        actor.poll_capture()
        now[0] = 32
        actor.poll_capture()
        self.assertEqual(worker.start.call_count, 2)

    def test_long_healthy_worker_has_fresh_cleanup_grace_after_failure(self):
        actor, worker, now = self.fixture()
        actor.start_capture()
        worker.ready = worker.running = True
        actor.poll_capture()
        now[0] = 30
        actor.poll_capture()
        now[0] = 100
        worker.ready = False
        actor.poll_capture()
        self.assertEqual(actor.snapshot['state'], 'starting')
        now[0] = 100.5
        worker.running = False
        actor.poll_capture()
        now[0] = 102.5
        actor.poll_capture()
        self.assertEqual(worker.start.call_count, 2)

    def test_stop_invalidates_generation_and_all_pending_retries(self):
        actor, worker, now = self.fixture()
        actor.start_capture()
        before = actor.generation
        actor.poll_capture()
        actor.stop_capture()
        self.assertGreater(actor.generation, before)
        for stamp in (2, 7, 100):
            now[0] = stamp
            actor.poll_capture()
        self.assertFalse(actor.available)
        self.assertEqual(actor.snapshot['state'], 'stopped')
        self.assertEqual(worker.start.call_count, 1)
        worker.close.assert_called_once_with()

    def test_health_never_waits_on_a_slow_start_and_stop_cannot_be_overtaken(self):
        actor, worker, now = self.fixture()
        entered, release = threading.Event(), threading.Event()
        worker.start.side_effect = lambda: (entered.set(), release.wait(2))
        starter = threading.Thread(target=actor.start_capture)
        stopper = threading.Thread(target=actor.stop_capture)
        starter.start()
        try:
            self.assertTrue(entered.wait(1))
            began = time.monotonic()
            self.assertEqual(actor.snapshot['state'], 'starting')
            self.assertLess(time.monotonic() - began, 0.05)
            stopper.start()
        finally:
            release.set()
            starter.join(2)
            if stopper.ident is not None:
                stopper.join(2)
        self.assertFalse(starter.is_alive())
        self.assertFalse(stopper.is_alive())
        now[0] = 100
        actor.poll_capture()
        self.assertEqual(actor.snapshot['state'], 'stopped')
        self.assertEqual(worker.start.call_count, 1)
        worker.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

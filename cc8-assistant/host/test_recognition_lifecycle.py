"""Concurrent startup cannot create two consumers for one recognition queue."""

import threading
import unittest
from unittest.mock import patch

from recognition_worker import RecognitionWorker


class RecognitionLifecycleTests(unittest.TestCase):
    def test_concurrent_start_is_single_owner_and_never_invokes_provider(self):
        worker = RecognitionWorker(None)
        real_thread = threading.Thread
        barrier = threading.Barrier(3)
        rejected = []

        def start():
            barrier.wait(timeout=3)
            try:
                worker.start()
            except RuntimeError:
                rejected.append(True)

        with patch('recognition_worker.threading.Thread') as child:
            attempts = [real_thread(target=start) for _ in range(2)]
            for attempt in attempts:
                attempt.start()
            barrier.wait(timeout=3)
            for attempt in attempts:
                attempt.join(3)
                self.assertFalse(attempt.is_alive())
            self.assertEqual(child.call_count, 1)
            self.assertEqual(child.return_value.start.call_count, 1)
            self.assertEqual(rejected, [True])
        self.assertTrue(worker.queue.empty())


if __name__ == '__main__':
    unittest.main()

"""Concurrent startup cannot create two consumers for one recognition/response queue."""

import threading
import unittest
from unittest.mock import patch

from recognition_worker import RecognitionWorker
from response_worker import ResponseWorker


class RecognitionLifecycleTests(unittest.TestCase):
    def test_concurrent_start_is_single_owner_and_never_invokes_provider(self):
        self.check_start(RecognitionWorker(None), 'recognition_worker')

    def test_response_concurrent_start_is_single_owner_without_provider(self):
        self.check_start(ResponseWorker(None), 'response_worker')

    def check_start(self, worker, module):
        real_thread = threading.Thread
        barrier = threading.Barrier(3)
        rejected = []

        def start():
            barrier.wait(timeout=3)
            try:
                worker.start()
            except RuntimeError:
                rejected.append(True)

        with patch(module + '.threading.Thread') as child:
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

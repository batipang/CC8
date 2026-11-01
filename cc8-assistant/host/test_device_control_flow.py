"""Request routing and cleanup with real callers, without a device or capture."""

import queue
import threading
import unittest
from unittest.mock import Mock, patch

from bridge import Bridge


class DeviceControlFlowTests(unittest.TestCase):
    def session(self):
        with patch('threading.Thread.start'):
            session = Bridge(None, None)
        session.session.connection = object()
        session.event = Mock()
        return session

    def test_concurrent_results_are_routed_by_id_and_filtered(self):
        session = self.session()
        requests, results = queue.Queue(), queue.Queue()
        session.event = lambda name, **fields: requests.put(fields)

        def call(percent):
            try:
                results.put((percent, session.control_device('volume', percent)))
            except Exception as error:
                results.put(error)

        workers = [
            threading.Thread(target=call, args=(percent,), daemon=True) for percent in (21, 73)
        ]
        for worker in workers:
            worker.start()
        pending = [requests.get(timeout=2) for _ in workers]
        self.assertEqual(len({item['request_id'] for item in pending}), 2)
        for item in reversed(pending):
            session.on_event(
                {
                    'event': 'device_control_result',
                    'request_id': item['request_id'],
                    'ok': True,
                    'volume_percent': item['percent'],
                    'unrelated': 'not returned to caller',
                }
            )
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        received = [results.get(timeout=1) for _ in workers]
        for result in received:
            self.assertIsInstance(result, tuple)
            percent, receipt = result
            self.assertEqual(receipt, {'ok': True, 'volume_percent': percent})
        self.assertEqual(session.device.waiters, {})
        session.on_event({'event': 'device_control_result', 'request_id': 'unknown', 'ok': True})
        self.assertFalse(session.session.active)

    def test_timeout_and_send_failure_release_waiters(self):
        for send_failure in (False, True):
            with self.subTest(send_failure=send_failure):
                session = self.session()
                waiter = Mock()
                waiter.get.side_effect = queue.Empty
                session.event.side_effect = (
                    EOFError('synthetic disconnect') if send_failure else None
                )
                with patch('device_controller.queue.Queue', return_value=waiter):
                    with self.assertRaises(EOFError if send_failure else RuntimeError):
                        session.control_device('status')
                self.assertEqual(session.device.waiters, {})
                if not send_failure:
                    waiter.get.assert_called_once_with(timeout=5)
                self.assertFalse(session.session.active)

    def test_rejected_actions_cannot_send_or_start_capture(self):
        session = self.session()
        for action in ('start_microphone', 'camera', 'unknown'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                session.control_device(action)
        session.session.connection = None
        with self.assertRaises(RuntimeError):
            session.control_device('status')
        session.event.assert_not_called()
        self.assertEqual(session.device.waiters, {})
        self.assertEqual(session.device.serial, 0)
        self.assertFalse(session.session.active)


if __name__ == '__main__':
    unittest.main()

"""Disconnect and disk-error recovery without real devices or cloud requests."""

import threading
import time
import unittest
from unittest.mock import patch

from announce import Announcement
from bridge import Bridge, Reply, TextReply
from vision import DesktopSnapshot


class WorkerRecoveryTests(unittest.TestCase):
    def bridge(self, speech=None):
        b = Bridge(speech, None)
        b.connection = object()
        b.active = True
        b.event = lambda *a, **k: None
        b.save_status = lambda: None
        b.log = lambda *a, **k: None
        return b

    def test_text_done_disconnect_does_not_kill_response_worker(self):
        b = self.bridge()
        disconnected = threading.Event()
        answered = threading.Event()

        def event(name, **fields):
            if name == 'text_done':
                disconnected.set()
                raise EOFError('closed transport')

        b.event = event
        b.text_generation = b.generation
        b.answer = lambda text, generation: answered.set() if text == 'second' else None
        with patch('threading.excepthook'):
            b.enqueue_response(TextReply(b.generation, 'first'))
            self.assertTrue(disconnected.wait(1))
            b.enqueue_response(Reply(b.generation, 'second'))
            self.assertTrue(answered.wait(1), 'response worker died during text cleanup')
        self.assertTrue(b.responder.is_alive())
        self.assertIsNone(b.text_generation)

    def test_resume_disconnect_does_not_kill_recognition_worker(self):
        class Speech:
            values = iter(['', '你好'])

            def recognize(self, audio):
                return next(self.values)

        b = self.bridge(Speech())
        disconnected = threading.Event()
        answered = threading.Event()
        b.playback.playing = True
        b.playback.pause_state = {
            'token': 1,
            'generation': b.generation,
            'started': time.monotonic(),
        }

        def event(name, **fields):
            if name == 'playback_resume':
                disconnected.set()
                raise EOFError('closed transport')

        b.event = event
        b.enqueue_response = lambda job: answered.set()
        evidence = {'echo_risk': False, 'pause_token': 1, 'captured_at': time.monotonic()}
        with patch('threading.excepthook'):
            b.queue.put((b.generation, [], False, [], evidence))
            self.assertTrue(disconnected.wait(1))
            b.queue.put((b.generation, [], False, [], evidence))
            self.assertTrue(answered.wait(1), 'recognition worker died while resuming playback')
        self.assertTrue(b.worker.is_alive())

    def test_announcement_always_finishes_waiter_even_if_status_write_fails(self):
        b = self.bridge()
        b.announcement_generation = b.generation
        b.speak = lambda *a: True
        job = Announcement(b.generation, '测试')
        with (
            patch.object(b, 'save_status', side_effect=OSError('disk full')),
            self.assertRaises(OSError),
        ):
            b.read_announcement(job)
        self.assertTrue(job.done.is_set())
        self.assertEqual(job.result, 'completed')

    def test_heartbeat_retries_after_transport_and_storage_errors(self):
        b = self.bridge()
        calls = []
        for error in (EOFError('closed'), OSError('disk full'), RuntimeError('busy')):
            with (
                self.subTest(error=error),
                patch.object(b, 'idle_tick', side_effect=[error, None]) as tick,
            ):
                b.event = lambda event, **fields: calls.append(event)
                b.heartbeat_tick()
                b.heartbeat_tick()
                self.assertEqual(tick.call_count, 2)
        self.assertEqual(calls, ['ping'] * 3)
        self.assertTrue(b.active)

    def test_failed_first_workspace_send_does_not_leave_task_running(self):
        b = self.bridge()
        with (
            patch.object(b, 'publish_workspace', side_effect=EOFError('closed')),
            self.assertRaises(EOFError),
        ):
            b.answer('测试', b.generation)
        self.assertEqual(b.workspace.snapshot()['tasks'][0]['status'], 'failed')

    def test_completed_dot_work_survives_failed_result_notification(self):
        class Brain:
            last_metrics = {'completed': True}

            def clauses(self, text, history, current, on_web):
                on_web('dot_result', {'text': '已完成的结果', 'truncated': False})
                yield '已完成的结果'

        b = self.bridge()
        b.brain = Brain()

        def event(name, **fields):
            if name == 'dot_result':
                raise EOFError('closed')

        b.event = event
        with self.assertRaises(EOFError):
            b.answer('测试', b.generation)
        task = b.workspace.snapshot()['tasks'][0]
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['result'], '已完成的结果')

    def test_snapshot_waiter_finishes_when_final_device_notification_disconnects(self):
        b = self.bridge()
        capture = b.visual.inbox.begin(b.generation)
        job = DesktopSnapshot(b.generation, capture.ident)
        b.visual.snapshot_job = job
        b.visual.generation = b.generation

        def event(name, **fields):
            if name == 'vision_done':
                raise EOFError('closed')

        b.event = event
        # Synthetic bytes suffice here: image validation is covered by CameraInbox.
        with (
            patch.object(b.visual.inbox, 'wait', return_value=b'synthetic-frame'),
            self.assertRaises(EOFError),
        ):
            b.capture_snapshot(job, capture)
        self.assertTrue(job.done.is_set())
        self.assertIsNone(b.visual.snapshot_job)


if __name__ == '__main__':
    unittest.main()

"""A delayed old cancellation must leave the next request's owners untouched."""

from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock, patch

from bridge import Bridge
from dot_client import DotBrain, Turn
from minimax_client import RequestSlot, Transport
from test_dot_client import FakeRpc


class CancelOwnershipTests(unittest.TestCase):
    def bridge(self):
        self.transport = Transport(key='sk-cp-public-test-only')
        speech = Mock()
        speech.cancel.side_effect = self.transport.cancel
        brain = DotBrain(rpc=FakeRpc())
        with patch('threading.Thread.start'):
            b = Bridge(speech, brain)
        b.session.connection = object()
        b.event, b.status, b.log, b.save_status = Mock(), Mock(), Mock(), Mock()
        return b

    def overlap(self, b, action, install_fence):
        entered, release, new_done = (threading.Event() for _ in range(3))
        errors, next_request = [], {}

        def fence():
            if threading.current_thread().name == 'old-request-cancel':
                entered.set()
                if not release.wait(3):
                    raise AssertionError('Cancellation barrier expired')

        install_fence(fence)

        def old():
            try:
                action()
            except BaseException as error:
                errors.append(error)

        def new():
            try:
                next_request.update(b.submit_text('New explicit request'))
                generation = next_request['generation']
                next_request['task'] = b.workspace.task_start(generation, 'New work')
                # Real cancellation implementations, without a network or model.
                next_request['turn'] = Turn()
                b.brain.current = next_request['turn']
                next_request['slot'] = RequestSlot()
                with self.transport.lock:
                    self.transport.slots.add(next_request['slot'])
            except BaseException as error:
                errors.append(error)
            finally:
                new_done.set()

        first = threading.Thread(target=old, name='old-request-cancel', daemon=True)
        second = threading.Thread(target=new, name='new-request', daemon=True)
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            second.start()
            # A serialized implementation may defer the new request. Either
            # order is valid; only cancelling its newer owners is forbidden.
            new_done.wait(0.2)
        finally:
            release.set()
            first.join(3)
            if second.ident is not None:
                second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(new_done.is_set())
        self.assertEqual(b.generation, next_request['generation'])
        task = next(t for t in b.workspace.snapshot()['tasks'] if t['id'] == next_request['task'])
        self.assertEqual(task['status'], 'running')
        self.assertFalse(next_request['turn'].stopped.is_set())
        self.assertFalse(next_request['slot'].stopped.is_set())

    def test_checked_old_task_cannot_cancel_new_text_request(self):
        b = self.bridge()
        ident = b.workspace.task_start(b.generation, 'Old work')
        original = b.device_events.cancel

        def install(fence):
            def cancel(reason):
                fence()
                return original(reason)

            b.device_events = replace(b.device_events, cancel=cancel)

        self.overlap(b, lambda: b.on_event({'event': 'task_cancel', 'id': ident}), install)

    def test_checked_old_vision_cannot_cancel_new_text_request(self):
        b = self.bridge()
        b.visual.generation = b.generation
        b.visual.nonce = 'a' * 32
        original = b.device_events.cancel

        def install(fence):
            def cancel(reason):
                fence()
                return original(reason)

            b.device_events = replace(b.device_events, cancel=cancel)

        self.overlap(b, lambda: b.on_event({'event': 'vision_cancel', 'nonce': 'a' * 32}), install)

    def test_superseded_cleanup_does_not_cancel_new_provider_requests(self):
        b = self.bridge()
        original = b.playback.notify

        def install(fence):
            def notify():
                fence()
                return original()

            b.playback.notify = notify

        self.overlap(b, lambda: b.cancel('old_request'), install)

    def test_new_request_cannot_enter_between_brain_and_speech_cancellation(self):
        b = self.bridge()
        original = b.brain.cancel

        def install(fence):
            def cancel():
                original()
                fence()

            b.brain.cancel = cancel

        self.overlap(b, lambda: b.cancel('old_request'), install)

    def test_current_cancellation_stops_providers_and_preserves_completed_results(self):
        b = self.bridge()
        completed = b.workspace.task_start(b.generation, 'Finished work')
        b.workspace.task_finish(completed, 'completed', 'Authoritative result')
        running = b.workspace.task_start(b.generation, 'Current work')
        turn, slot = Turn(), RequestSlot()
        b.brain.current = turn
        self.transport.slots.add(slot)
        generation = b.generation
        b.on_event({'event': 'task_cancel', 'id': running})
        self.assertEqual(b.generation, generation + 1)
        tasks = {task['id']: task for task in b.workspace.snapshot()['tasks']}
        self.assertEqual(tasks[running]['status'], 'cancelled')
        self.assertEqual(tasks[completed]['status'], 'completed')
        self.assertEqual(tasks[completed]['result'], 'Authoritative result')
        self.assertTrue(turn.stopped.is_set())
        self.assertTrue(slot.stopped.is_set())
        self.assertFalse(b.session.active)

    def test_terminal_task_and_unmatched_vision_buttons_leave_current_request_untouched(self):
        b = self.bridge()
        completed = b.workspace.task_start(b.generation, 'Finished work')
        b.workspace.task_finish(completed, 'completed')
        generation = b.submit_text('Next work')['generation']
        running = b.workspace.task_start(generation, 'Current work')
        b.visual.generation, b.visual.nonce = generation, 'a' * 32
        turn, slot = Turn(), RequestSlot()
        b.brain.current = turn
        self.transport.slots.add(slot)
        b.on_event({'event': 'task_cancel', 'id': completed})
        b.on_event({'event': 'vision_cancel', 'nonce': 'b' * 32})
        self.assertEqual(b.generation, generation)
        tasks = {task['id']: task for task in b.workspace.snapshot()['tasks']}
        self.assertEqual(tasks[running]['status'], 'running')
        self.assertFalse(turn.stopped.is_set())
        self.assertFalse(slot.stopped.is_set())


if __name__ == '__main__':
    unittest.main()

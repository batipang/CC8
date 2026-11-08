"""Explicit wake presentation states, without device capture or providers."""

import threading
import unittest
from unittest.mock import Mock

from session_controller import SessionController


class ListeningStateTests(unittest.TestCase):
    def owner(self):
        hooks = Mock()
        owner = SessionController(hooks)
        owner.connection = object()
        owner.capture_id = 'a' * 32
        owner.epoch = 7
        return owner, hooks

    def test_listening_state_comes_from_capture_not_status_prose(self):
        owner, hooks = self.owner()
        for active, awake, expected in (
            (False, False, 'unknown'),
            (False, True, 'unknown'),
            (True, False, 'waiting'),
            (True, True, 'awake'),
        ):
            with self.subTest(active=active, awake=awake):
                owner.active, owner.awake = active, awake
                owner.status('listening', 'arbitrary translated message')
                hooks.event.assert_called_with(
                    'status',
                    phase='listening',
                    message='arbitrary translated message',
                    listening_state=expected,
                    capture_id=owner.capture_id if active else None,
                    epoch=7,
                )

    def test_other_phases_do_not_invent_listening_state_or_send_when_disconnected(self):
        owner, hooks = self.owner()
        for phase in ('speaking', 'thinking', 'calibrating', 'error', 'idle'):
            owner.status(phase, 'message')
            hooks.event.assert_called_with('status', phase=phase, message='message')
        hooks.event.reset_mock()
        owner.connection = None
        owner.status('listening', 'message')
        hooks.event.assert_not_called()

    def test_state_transition_cannot_overtake_its_published_label(self):
        owner, hooks = self.owner()
        owner.active, owner.awake = True, False
        entered, release, changed = threading.Event(), threading.Event(), threading.Event()
        labels, errors = [], []

        def publish(name, **fields):
            labels.append(fields['listening_state'])
            if len(labels) == 1:
                entered.set()
                if not release.wait(2):
                    errors.append('publication_not_released')

        def transition():
            with owner.guard:
                owner.awake = True
                changed.set()
                owner.status('listening', 'new')

        hooks.event.side_effect = publish
        old = threading.Thread(target=lambda: owner.status('listening', 'old'))
        new = threading.Thread(target=transition)
        old.start()
        try:
            self.assertTrue(entered.wait(1))
            new.start()
            self.assertFalse(changed.wait(0.05))
        finally:
            release.set()
            old.join(2)
            if new.ident is not None:
                new.join(2)
        self.assertFalse(old.is_alive())
        self.assertFalse(new.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(labels, ['waiting', 'awake'])

    def test_unrelated_error_notification_keeps_its_existing_lock_contract(self):
        owner, hooks = self.owner()
        delivered = threading.Event()
        hooks.event.side_effect = lambda *args, **fields: delivered.set()
        sender = threading.Thread(target=lambda: owner.status('error', 'existing failure'))
        with owner.guard:
            sender.start()
            delivered_while_held = delivered.wait(1)
        sender.join(2)
        self.assertFalse(sender.is_alive())
        self.assertTrue(delivered_while_held)


if __name__ == '__main__':
    unittest.main()

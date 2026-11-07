"""Deferred self-check signals use the real capture/playback owner guards."""

import io
from pathlib import Path
import queue
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import bridge


class EchoRetryTests(unittest.TestCase):
    def exercise(self, session, actions=(), *, verifier='off'):
        handlers, hub = {}, Mock()
        steps = iter(actions)

        def event(**kwargs):
            action = next(steps, None)
            if action is None:
                handlers[signal.SIGTERM]()
            else:
                action(handlers)
            raise queue.Empty

        hub.events.get.side_effect = event
        hub.preferred.return_value = session.session.connection
        hub.active = session.session.connection
        hub.role_uncertain = False
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(bridge, 'PRIVATE', Path(folder)),
            patch.object(sys, 'argv', ['bridge.py', '--no-fast-stop']),
            patch(
                'bridge.load_config',
                return_value=SimpleNamespace(
                    brain='dot',
                    tls_port=18766,
                    wake_window_seconds=60,
                    chinese_wake_verifier=verifier,
                ),
            ),
            patch('bridge.DirectSpeech') as speech_factory,
            patch('bridge.WakeDetector') as wake_factory,
            patch('bridge.MiniMaxBrain'),
            patch('bridge.BrainRouter'),
            patch('bridge.Workspace'),
            patch('bridge.Bridge', return_value=session),
            patch('bridge.Pairing'),
            patch('bridge.TransportHub', return_value=hub),
            patch('bridge.AnnouncementServer'),
            patch('bridge.threading.Thread.start'),
            patch(
                'bridge.signal.signal',
                side_effect=lambda name, handler: handlers.update({name: handler}),
            ),
            patch('sys.stdout', new_callable=io.StringIO),
        ):
            bridge.run_backend()
        speech_factory.return_value.recognize.assert_not_called()
        if verifier == 'sensevoice':
            wake_factory.load.assert_called_once_with(None, verify_chinese=True)
        else:
            wake_factory.load.assert_called_once_with(None)

    def test_backend_passes_opt_in_before_registering_the_capture_free_handler(self):
        with patch('threading.Thread.start'):
            session = bridge.Bridge(None, None)
        session.save_status = Mock()
        self.exercise(session, verifier='sensevoice')
        self.assertFalse(session.session.active)

    def test_signal_retry_checks_real_playback_owner_and_never_starts_capture(self):
        with patch('threading.Thread.start'):
            session = bridge.Bridge(None, None)
        session.event, session.log, session.save_status = Mock(), Mock(), Mock()
        session.session.connection = Mock()
        blocked = (
            (False, False, False, False),
            (True, True, False, False),
            (True, False, True, False),
            (True, False, False, True),
        )
        with patch('bridge.threading.Thread') as worker:
            actions = []
            for active, calibrating, running, playing in blocked:

                def request(handlers, state=(active, calibrating, running, playing)):
                    worker.reset_mock()
                    (
                        session.session.active,
                        session.calibration.active,
                        session.calibration.running,
                        session.playback.playing,
                    ) = state
                    self.generation = session.session.generation
                    handlers[signal.SIGUSR1]()
                    self.assertEqual(session.session.generation, self.generation)

                def verify(handlers, active=active):
                    worker.assert_not_called()
                    self.assertEqual(session.session.generation, self.generation)
                    self.assertEqual(session.session.active, active)

                actions.extend((request, verify))

            def request_allowed(handlers):
                worker.reset_mock()
                session.session.active = True
                session.calibration.active = session.calibration.running = (
                    session.playback.playing
                ) = False
                self.generation = session.session.generation
                handlers[signal.SIGUSR1]()
                self.assertEqual(session.session.generation, self.generation)
                worker.assert_not_called()

            def verify_allowed(handlers):
                self.assertTrue(
                    session.session.active
                    and session.calibration.active
                    and session.calibration.pending
                )
                self.assertEqual(session.session.generation, self.generation + 1)
                worker.assert_called_once_with(
                    target=session.calibrate, args=(session.session.generation,), daemon=True
                )
                worker.return_value.start.assert_called_once_with()

            actions.extend((request_allowed, verify_allowed))
            self.exercise(session, actions)
        self.assertFalse(session.session.active)

    def test_a_delayed_selfcheck_cannot_cancel_a_new_generation_or_capture_epoch(self):
        for changed in ('generation', 'epoch'):
            with patch('threading.Thread.start'):
                session = bridge.Bridge(None, None)
            session.event, session.log, session.save_status = Mock(), Mock(), Mock()
            session.session.connection = Mock()
            with patch('bridge.threading.Thread') as worker:

                def request(handlers):
                    worker.reset_mock()
                    session.session.active = True
                    handlers[signal.SIGUSR1]()
                    if changed == 'generation':
                        session.cancel('new_owner')
                    else:
                        session.session.epoch += 1
                    self.generation = session.session.generation

                def verify(handlers):
                    worker.assert_not_called()
                    self.assertEqual(session.session.generation, self.generation)
                    self.assertFalse(session.calibration.active)
                    self.assertTrue(session.session.active)

                with self.subTest(changed=changed):
                    self.exercise(session, (request, verify))

    def test_scoped_cancel_rejects_stale_and_bad_owners_without_mutating_resources(self):
        with patch('threading.Thread.start'):
            session = bridge.Bridge(None, None)
        session.session.active = True
        session.session.connection = Mock()
        session.brain, session.speech = Mock(), Mock()
        session.event, session.log, session.save_status = Mock(), Mock(), Mock()
        session.playback.playing = True
        generation, epoch = session.generation, session.session.epoch
        task = session.workspace.task_start(generation, 'current work')
        metrics = dict(session.metrics)
        for old_generation, old_epoch in (
            (generation - 1, epoch),
            (generation, epoch + 1),
            (True, epoch),
            (generation, None),
        ):
            self.assertIsNone(
                session.cancel(
                    'echo_check_retry', expected_generation=old_generation, expected_epoch=old_epoch
                )
            )
            self.assertEqual(session.generation, generation)
            self.assertEqual(session.metrics, metrics)
            self.assertTrue(session.playback.playing)
            self.assertEqual(session.workspace.tasks[-1]['id'], task)
            self.assertEqual(session.workspace.tasks[-1]['status'], 'running')
            session.brain.cancel.assert_not_called()
            session.speech.cancel.assert_not_called()
            session.event.assert_not_called()
        self.assertEqual(
            session.cancel(
                'echo_check_retry', expected_generation=generation, expected_epoch=epoch
            ),
            generation + 1,
        )
        self.assertFalse(session.playback.playing)
        session.brain.cancel.assert_called_once_with()
        session.speech.cancel.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

"""Exercise the registered self-check signal without live providers or capture."""

import io
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import bridge


class EchoRetryTests(unittest.TestCase):
    def registered_handler(self, session, *, verifier='off'):
        handlers = {}
        hub = Mock()
        # End the fake event loop immediately, after actual handler registration.
        hub.events.get.side_effect = StopIteration('end synthetic event loop')
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
            self.assertRaises(StopIteration),
        ):
            bridge.run_backend()
        speech_factory.return_value.recognize.assert_not_called()
        if verifier == 'sensevoice':
            wake_factory.load.assert_called_once_with(None, verify_chinese=True)
        else:
            wake_factory.load.assert_called_once_with(None)
        return handlers[signal.SIGUSR1]

    def test_backend_passes_opt_in_before_registering_the_capture_free_handler(self):
        with patch('threading.Thread.start'):
            session = bridge.Bridge(None, None)
        session.save_status = Mock()
        self.registered_handler(session, verifier='sensevoice')
        self.assertFalse(session.session.active)

    def test_signal_retry_checks_real_playback_owner_and_never_starts_capture(self):
        with patch('threading.Thread.start'):
            session = bridge.Bridge(None, None)
        session.event = Mock()
        session.log = Mock()
        session.save_status = Mock()
        handler = self.registered_handler(session)
        for active, calibrating, running, playing in (
            (False, False, False, False),
            (True, True, False, False),
            (True, False, True, False),
            (True, False, False, True),
        ):
            with self.subTest(
                active=active, calibrating=calibrating, running=running, playing=playing
            ):
                session.session.active = active
                session.calibration.active = calibrating
                session.calibration.running = running
                session.playback.playing = playing
                generation = session.session.generation
                with patch('bridge.threading.Thread') as worker:
                    handler()
                worker.assert_not_called()
                self.assertEqual(session.session.generation, generation)
                self.assertEqual(session.session.active, active)
        session.session.active = True
        session.calibration.active = session.calibration.running = session.playback.playing = False
        session.session.connection = object()
        generation = session.session.generation
        with patch('bridge.threading.Thread') as worker:
            handler()
        self.assertTrue(session.session.active)
        self.assertEqual(session.session.generation, generation + 1)
        self.assertTrue(session.calibration.active)
        self.assertTrue(session.calibration.pending)
        worker.assert_called_once_with(
            target=session.calibrate, args=(session.session.generation,), daemon=True
        )
        worker.return_value.start.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

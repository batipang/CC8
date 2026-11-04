"""Failed notifications must not prevent local teardown or mislabel output as TTS."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import numpy as np
from bridge import Bridge
from protocol import EVENT, SPEAKER, decode_event


class TransportCleanupTests(unittest.TestCase):
    def session(self, brain=None):
        with patch('threading.Thread.start'):
            b = Bridge(None, brain)
        b.session.active = True
        b.session.awake = True
        b.session.connection = object()
        b.save_status = Mock()
        b.log = Mock()
        b.event = Mock()
        b.send = Mock()
        return b

    def test_manual_stop_releases_aec_even_when_cancel_notification_fails(self):
        b = self.session()
        aec = Mock()
        b.capture.echo.aec = aec
        b.playback.playing = True
        b.playback.notify = Mock()
        b.event.side_effect = EOFError('fixture disconnected')
        b.stop()
        self.assertFalse(b.session.active)
        self.assertFalse(b.playback.playing)
        aec.close.assert_called_once()
        self.assertIsNone(b.capture.echo.aec)
        b.playback.notify.assert_called_once()
        b.save_status.assert_called_once()

    def test_cancel_advances_once_and_notifies_waiters_despite_failed_event(self):
        b = self.session()
        b.playback.notify = Mock()
        generation = b.generation
        b.event.side_effect = BrokenPipeError('fixture disconnected')
        result = b.cancel('test_cancel')
        self.assertEqual(result, generation + 1)
        self.assertEqual(b.generation, generation + 1)
        self.assertTrue(b.session.active)
        b.playback.notify.assert_called_once()

    def test_unexpected_provider_cancel_error_still_releases_stop_resources(self):
        brain = SimpleNamespace(cancel=Mock(side_effect=ValueError('fixture provider bug')))
        b = self.session(brain)
        aec = Mock()
        b.capture.echo.aec = aec
        b.playback.notify = Mock()
        with self.assertRaises(ValueError):
            b.stop()
        self.assertFalse(b.session.active)
        aec.close.assert_called_once()
        b.playback.notify.assert_called_once()
        b.save_status.assert_called_once()

    def test_provider_cancellation_error_still_sends_device_stop(self):
        for failed in ('brain', 'speech'):
            with self.subTest(failed=failed):
                brain, speech = SimpleNamespace(cancel=Mock()), SimpleNamespace(cancel=Mock())
                provider = brain if failed == 'brain' else speech
                provider.cancel.side_effect = ValueError('fixture provider bug')
                b = self.session(brain)
                b.speech = speech
                b.event = Bridge.event.__get__(b)
                b.playback.playing = True
                b.diagnostics.begin('announcement', b.generation)
                generation = b.generation + 1
                with self.assertRaisesRegex(ValueError, 'fixture provider bug'):
                    b.cancel('streaming_stop_keyword')
                brain.cancel.assert_called_once_with()
                speech.cancel.assert_called_once_with()
                packet = b.send.call_args.args[0]
                self.assertEqual((packet.kind, packet.generation), (EVENT, generation))
                self.assertEqual(decode_event(packet.data), {'event': 'cancel'})
                self.assertEqual(b.generation, generation)
                self.assertFalse(b.playback.playing)
                self.assertEqual(b.diagnostics.report()['requests'][-1]['outcome'], 'cancelled')

    def test_transport_failure_at_begin_pcm_or_end_stops_output_and_keeps_stage(self):
        for phase in ('play_begin', 'speaker', 'play_end'):
            with self.subTest(phase=phase):
                b = self.session()
                generation = b.generation
                b.diagnostics.begin('announcement', generation)

                def event(name, **fields):
                    if name == phase:
                        raise EOFError('fixture event failure')

                def send(packet):
                    if phase == 'speaker' and packet.kind == SPEAKER:
                        raise BrokenPipeError('fixture PCM failure')

                b.event.side_effect = event
                b.send.side_effect = send
                with self.assertRaises(Exception):
                    b.play(np.ones(3200, dtype=np.int16), 'test', generation)
                self.assertFalse(b.playback.playing)
                self.assertGreater(b.generation, generation)
                row = b.diagnostics.report()['requests'][-1]
                self.assertEqual(row['error'], {'stage': 'transport', 'kind': 'transport_or_io'})
                self.assertTrue(b.session.active)

    def test_caption_transport_error_inside_provider_iterator_is_not_rewrapped_as_tts(self):
        b = self.session()
        generation = b.generation
        b.diagnostics.begin('announcement', generation)
        timing = SimpleNamespace(rows=lambda: [], publish=None)
        closed = []

        def chunks():
            try:
                yield np.ones(3200, dtype=np.int16)
                timing.publish([])
            finally:
                closed.append(True)

        def event(name, **fields):
            if name == 'caption_timing':
                raise EOFError('fixture timing failure')

        b.event.side_effect = event
        with self.assertRaises(Exception):
            b.play_stream(chunks(), 'test', generation, timing=timing)
        self.assertEqual(closed, [True])
        self.assertIsNone(timing.publish)
        self.assertFalse(b.playback.playing)
        self.assertEqual(
            b.diagnostics.report()['requests'][-1]['error'],
            {'stage': 'transport', 'kind': 'transport_or_io'},
        )

    def test_failed_startup_playback_exits_calibrating_but_handover_preserves_it(self):
        b = self.session()
        b.speech = SimpleNamespace(synthesize=lambda text: np.ones(16000, dtype=np.int16))
        b.calibration.begin()

        def event(name, **fields):
            if name == 'play_begin':
                raise EOFError('fixture startup send failure')

        b.event.side_effect = event
        b.calibrate(b.generation)
        self.assertFalse(b.calibration.active)
        self.assertFalse(b.playback.playing)
        b.calibration.begin()
        b.event.side_effect = None
        b.cancel('transport')
        self.assertTrue(b.calibration.active)


if __name__ == '__main__':
    unittest.main()

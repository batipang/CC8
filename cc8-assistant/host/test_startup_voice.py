"""Opening voice replaces the old reference, without removing echo calibration."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import numpy as np
from bridge import Bridge, STARTUP_GREETING
from dsp import RATE


class StartupVoiceTests(unittest.TestCase):
    def bridge(self):
        speech = SimpleNamespace(synthesize=Mock(return_value=np.ones(RATE * 2, dtype=np.int16)))
        value = Bridge(speech, None)
        value.active = True
        value.connection = object()
        value.calibration.active = True
        value.play = Mock(return_value=True)
        value.speak = Mock()
        value.align_reference = Mock(return_value=True)
        value.save_status = Mock()
        value.status = Mock()
        value.log = Mock()
        return value

    def test_only_current_voice_greeting_once_in_both_modes(self):
        for wake in (False, True):
            with self.subTest(wake=wake):
                value = self.bridge()
                value.wake = wake
                with (
                    patch(
                        'bridge.wav_samples',
                        side_effect=AssertionError('Old female reference used'),
                    ),
                    patch('bridge.time.sleep') as sleep,
                ):
                    value.calibrate(value.generation)
                value.speech.synthesize.assert_called_once_with('汽车人，变形！')
                value.play.assert_called_once()
                self.assertEqual(
                    value.play.call_args.args[1:],
                    (STARTUP_GREETING, value.generation, 'calibration'),
                )
                self.assertEqual(len(value.play.call_args.args[0]), RATE * 2)
                value.speak.assert_not_called()
                value.align_reference.assert_called_once_with(value.generation)
                self.assertFalse(value.calibration.active)
                self.assertGreater(sleep.call_args.args[0], 6)

    def test_tts_waiting_audio_is_excluded_from_alignment_window(self):
        value = self.bridge()
        value.capture.echo.calibration_frames = [('waiting',)]

        def play(*args):
            self.assertEqual(value.capture.echo.calibration_frames, [])
            value.capture.echo.calibration_frames.append(('actual-playback',))
            return True

        value.play.side_effect = play
        with patch('bridge.time.sleep'):
            value.calibrate(value.generation)
        self.assertEqual(value.capture.echo.calibration_frames, [('actual-playback',)])

    def test_cancelled_synthesis_cannot_clear_or_play_new_session(self):
        value = self.bridge()
        generation = value.generation

        def synthesize(text):
            value.generation += 1
            value.capture.echo.calibration_frames = [('new-session',)]
            return np.ones(RATE * 2, dtype=np.int16)

        value.speech.synthesize.side_effect = synthesize
        value.calibrate(generation)
        value.play.assert_not_called()
        value.align_reference.assert_not_called()
        self.assertTrue(value.calibration.active)
        self.assertEqual(value.capture.echo.calibration_frames, [('new-session',)])

    def test_synthesis_failure_never_falls_back_to_old_female_reference(self):
        value = self.bridge()
        value.failure = Mock()
        value.speech.synthesize.side_effect = RuntimeError()
        with patch('bridge.wav_samples') as old:
            value.calibrate(value.generation)
        old.assert_not_called()
        value.play.assert_not_called()
        value.failure.assert_called_once()

    def test_empty_short_long_silent_and_wrong_format_audio_are_rejected(self):
        for pcm in (
            np.ones(100, dtype=np.int16),
            np.ones(RATE * 7, dtype=np.int16),
            np.zeros(RATE, dtype=np.int16),
            np.ones(RATE, dtype=np.float32),
            np.ones((2, RATE), dtype=np.int16),
        ):
            with self.subTest(shape=pcm.shape, dtype=pcm.dtype):
                value = self.bridge()
                value.speech.synthesize.return_value = pcm
                with self.assertRaises(ValueError):
                    value.startup_reference()


if __name__ == '__main__':
    unittest.main()

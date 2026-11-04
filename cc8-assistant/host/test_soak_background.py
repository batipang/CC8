"""A pinned background fixture never opens the live audio output in offline tests."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from soak_background import Background, RATE


class BackgroundTests(unittest.TestCase):
    def track(self, path, *, frames=RATE, sample=b'\x01\x00', rate=RATE):
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, rate, frames, 'NONE', ''))
            wav.writeframes(sample * frames)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @patch('soak_background.PLAYBACK_SECONDS', 1)
    def test_verified_snapshot_survives_source_replacement_and_cleans_up_player(self):
        with tempfile.TemporaryDirectory() as folder:
            track = Path(folder) / 'fixture.wav'
            pin = self.track(track)
            data = track.read_bytes()
            background = Background.load(track, 'tv', pin)
            track.unlink()
            player = Mock()
            player.poll.return_value = None
            with patch('soak_background.subprocess.Popen', return_value=player) as spawn:
                with background.play() as active:
                    self.assertTrue(active())
                    command = spawn.call_args.args[0]
                    self.assertEqual(command[:3], ['/usr/bin/afplay', '-t', '1'])
                    staged = Path(command[-1])
                    self.assertEqual(staged.read_bytes(), data)
                self.assertFalse(staged.exists())
                player.terminate.assert_called_once_with()
                player.wait.assert_called_once_with(timeout=3)

    @patch('soak_background.PLAYBACK_SECONDS', 1)
    def test_rejects_wrong_pin_invalid_wav_short_silent_or_linked_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            track = Path(folder) / 'fixture.wav'
            pin = self.track(track)
            with self.assertRaisesRegex(ValueError, 'pinned digest'):
                Background.load(track, 'low-noise', '0' * 64)
            alias = track.with_name('alias.wav')
            alias.symlink_to(track)
            with self.assertRaises(OSError):
                Background.load(alias, 'low-noise', pin)
            for arguments, message in (
                ({'frames': RATE - 1}, 'long'),
                ({'sample': b'\x00\x00'}, 'silent'),
                ({'rate': 8000}, '16 kHz'),
            ):
                with self.subTest(arguments=arguments):
                    pin = self.track(track, **arguments)
                    with self.assertRaisesRegex(ValueError, message):
                        Background.load(track, 'low-noise', pin)
            track.write_bytes(b'bad wave')
            with self.assertRaisesRegex(ValueError, 'Invalid background WAV'):
                Background.load(track, 'low-noise', hashlib.sha256(track.read_bytes()).hexdigest())

"""Execute preparation, playback and cleanup with real WAV files, no audio device."""

from contextlib import contextmanager
import hashlib
import io
from pathlib import Path
import struct
import subprocess
import unittest
from unittest.mock import patch
import wave

import mac_voice
from device_smoke import Device, CheckFailure, interruption_input


def metadata():
    return dict(
        schema=1,
        sample_rate=16000,
        channels=1,
        sample_width_bytes=2,
        frames=1600,
        pcm_sha256='a' * 64,
        wav_sha256='b' * 64,
        acoustic_playback_confirmed=False,
    )


class PreparedSpeechTests(unittest.TestCase):
    @contextmanager
    def commands(self, *, fail=None, silent=False):
        paths, played = [], []
        pcm = (
            b'\0\0' * 1600
            if silent
            else b''.join(struct.pack('<h', (i % 7) * 1000) for i in range(1600))
        )

        def command(args, **kwargs):
            if args[0] == '/usr/bin/say':
                output = Path(args[args.index('-o') + 1])
                paths.append(output.parent)
                output.write_bytes(b'public synthetic source')
                self.assertNotIn('-r', args)
            elif args[0] == '/usr/bin/afconvert':
                with wave.open(args[-1], 'wb') as target:
                    target.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    target.writeframes(pcm)
            elif args[0] == '/usr/bin/afplay':
                self.assertEqual(kwargs['timeout'], 20)
                self.assertTrue(kwargs['check'])
                played.append(mac_voice.read_pcm(Path(args[1])))
            else:
                self.fail('unexpected external command')
            if fail == args[0]:
                raise subprocess.CalledProcessError(1, args, stderr='private-error-body')
            return subprocess.CompletedProcess(args, 0)

        with (
            patch('mac_voice.resolve_voice', return_value='Tingting (Chinese CN)'),
            patch('mac_voice.subprocess.run', side_effect=command) as call,
        ):
            yield pcm, paths, played, call

    def test_play_uses_the_same_validated_wav_and_owns_its_bounded_samples(self):
        with self.commands() as (pcm, paths, played, calls):
            speech = mac_voice.PreparedSpeech('Tingting', '换个问题，三加五等于几？')
            try:
                self.assertEqual(paths[0].stat().st_mode & 0o777, 0o700)
                self.assertEqual((paths[0] / 'source.wav').stat().st_mode & 0o777, 0o600)
                value = speech.metadata
                self.assertEqual(value['pcm_sha256'], hashlib.sha256(pcm).hexdigest())
                self.assertEqual(value['frames'], 1600)
                self.assertIs(value['acoustic_playback_confirmed'], False)
                self.assertEqual(speech.pcm, pcm)
                value['frames'] = 1
                self.assertEqual(speech.metadata['frames'], 1600)
                speech.play()
                self.assertEqual(played, [pcm])
                self.assertEqual(
                    [c.args[0][0] for c in calls.call_args_list],
                    ['/usr/bin/say', '/usr/bin/afconvert', '/usr/bin/afplay'],
                )
            finally:
                speech.close()
            self.assertFalse(paths[0].exists())
            speech.close()
            with self.assertRaises(ValueError):
                speech.play()
            with self.assertRaises(ValueError):
                _ = speech.pcm

    def test_synthesis_conversion_and_silent_output_fail_before_play_and_remove_partial_files(self):
        for failure, silent in (
            ('/usr/bin/say', False),
            ('/usr/bin/afconvert', False),
            (None, True),
        ):
            with (
                self.subTest(failure=failure, silent=silent),
                self.commands(fail=failure, silent=silent) as (_, paths, played, calls),
            ):
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    mac_voice.PreparedSpeech('Tingting', '停一下')
                self.assertTrue(paths)
                self.assertTrue(all(not path.exists() for path in paths))
                self.assertFalse(played)

    def test_changed_truncated_oversized_or_symlinked_wav_cannot_be_played(self):
        for change in ('changed', 'truncated', 'oversized', 'symlink'):
            with self.subTest(change=change), self.commands() as (_, paths, played, calls):
                speech = mac_voice.PreparedSpeech('Tingting', '停一下')
                try:
                    path = paths[0] / 'source.wav'
                    if change == 'changed':
                        path.write_bytes(path.read_bytes() + b'changed')
                    elif change == 'truncated':
                        path.write_bytes(b'')
                    elif change == 'oversized':
                        with path.open('r+b') as output:
                            output.truncate(speech.MAX_WAV_BYTES + 1)
                    else:
                        path.unlink()
                        path.symlink_to(paths[0] / 'source.aiff')
                    with self.assertRaises(ValueError):
                        speech.play()
                    self.assertFalse(played)
                finally:
                    speech.close()
                self.assertFalse(paths[0].exists())

    def test_playback_error_keeps_cleanup_possible_without_synthesizing_again(self):
        with self.commands(fail='/usr/bin/afplay') as (_, paths, played, calls):
            speech = mac_voice.PreparedSpeech('Tingting', '停一下')
            try:
                with self.assertRaises(subprocess.CalledProcessError):
                    speech.play()
                self.assertEqual(len(calls.call_args_list), 3)
            finally:
                speech.close()
            self.assertFalse(paths[0].exists())

    def test_pcm_and_file_digest_must_describe_the_same_byte_snapshot(self):
        other = io.BytesIO()
        with wave.open(other, 'wb') as target:
            target.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            target.writeframes(b'\x03\0' * 1600)
        with (
            self.commands() as (_, paths, played, calls),
            patch.object(mac_voice.PreparedSpeech, '_read_artifact', return_value=other.getvalue()),
        ):
            with self.assertRaisesRegex(ValueError, 'Invalid prepared speech samples'):
                mac_voice.PreparedSpeech('Tingting', '停一下')
            self.assertTrue(all(not path.exists() for path in paths))
            self.assertFalse(played)

    def test_each_fixed_stop_kind_keeps_its_original_phrase_and_closes_the_previous_file(self):
        from device_smoke import ASR_STOP_QUERY, STOP_QUERY

        device = Device.__new__(Device)
        with self.commands() as (_, paths, played, calls):
            for flags, phrase, kind in (
                ({'stop': True}, STOP_QUERY, 'local_stop'),
                ({'stop': True, 'asr_stop': True}, ASR_STOP_QUERY, 'asr_stop'),
            ):
                receipt = device.prepare_interruption_probe(**flags)
                self.assertEqual(receipt['kind'], kind)
                says = [c.args[0] for c in calls.call_args_list if c.args[0][0] == '/usr/bin/say']
                self.assertEqual(says[-1][-1], phrase)
                if len(paths) > 1:
                    self.assertFalse(paths[-2].exists())
                device.speak_probe(**flags, expected_input=receipt)
            device.close_speech_probe()
            self.assertEqual(len(played), 2)
            self.assertTrue(all(not path.exists() for path in paths))

    def test_default_voice_and_option_like_or_unbounded_text_are_refused_before_files(self):
        with patch('mac_voice.TemporaryDirectory') as temporary:
            for text in ('', '-o other-file', 'a' * 121, 'x\ny'):
                with self.subTest(text=text), self.assertRaises(ValueError):
                    mac_voice.PreparedSpeech('Tingting', text)
            with patch('mac_voice.resolve_voice', return_value=None), self.assertRaises(ValueError):
                mac_voice.PreparedSpeech(None, '停一下')
            temporary.assert_not_called()

    def test_receipts_reject_content_false_physical_claims_and_invalid_format(self):
        value = metadata()
        self.assertEqual(mac_voice.validate_prepared_speech(value), value)
        for field, bad in (
            ('schema', True),
            ('sample_rate', 48000),
            ('channels', 2),
            ('sample_width_bytes', 1),
            ('frames', 0),
            ('frames', True),
            ('frames', 160001),
            ('pcm_sha256', 'bad'),
            ('wav_sha256', 'bad'),
            ('acoustic_playback_confirmed', True),
            ('text', 'must never enter a report'),
        ):
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                mac_voice.validate_prepared_speech({**value, field: bad})

    def test_device_plays_only_the_prepared_fixed_phrase_and_matching_kind(self):
        device = Device.__new__(Device)
        with self.commands() as (_, paths, played, calls):
            receipt = device.prepare_interruption_probe()
            self.assertEqual(receipt['kind'], 'barge')
            self.assertEqual(receipt['voice_family'], 'Tingting')
            self.assertEqual(interruption_input(receipt), receipt)
            with self.assertRaisesRegex(CheckFailure, 'synthetic_interruption_not_prepared'):
                device.speak_probe(stop=True)
            device.speak_probe(interruption=True)
            self.assertEqual(len(played), 1)
            with self.assertRaisesRegex(CheckFailure, 'synthetic_interruption_receipt_changed'):
                device.speak_probe(interruption=True, expected_input={**receipt, 'frames': 1})
            self.assertEqual(len(played), 1)
            device.speak_probe(interruption=True, expected_input=receipt)
            self.assertEqual(len(played), 2)
            device.close_speech_probe()
            with self.assertRaisesRegex(CheckFailure, 'synthetic_interruption_not_prepared'):
                device.speak_probe(interruption=True)
            self.assertFalse(paths[0].exists())

    def test_partial_constructor_cleanup_remains_owned_for_the_device_finally(self):
        device = Device.__new__(Device)
        real_cleanup = mac_voice.TemporaryDirectory.cleanup
        attempts = []

        def cleanup(temporary):
            attempts.append(temporary.name)
            if len(attempts) == 1:
                raise OSError('temporary cleanup unavailable')
            return real_cleanup(temporary)

        with (
            self.commands(fail='/usr/bin/say') as (_, paths, played, calls),
            patch.object(mac_voice.TemporaryDirectory, 'cleanup', cleanup),
        ):
            with self.assertRaises(mac_voice.SpeechFixtureCleanupError):
                device.prepare_interruption_probe()
            self.assertTrue(paths[0].exists())
            device.close_speech_probe()
            self.assertFalse(paths[0].exists())
            self.assertEqual(len(attempts), 2)


if __name__ == '__main__':
    unittest.main()

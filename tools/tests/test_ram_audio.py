"""Cross-language wire and failure ownership contracts for RAM-only diagnostic samples."""

import base64
import hashlib
import json
import unittest

from tools.ram_audio import CHUNK_BYTES, MIN_BYTES, RamAudioError, read_into

TOKEN = 'a' * 32


def response(data, offset, *, source=7, sample_rate=48000, channels=4, mutate=None):
    chunk = data[offset : offset + CHUNK_BYTES]
    meta = {
        'offset_bytes': offset,
        'total_bytes': len(data),
        'chunk_bytes': len(chunk),
        'sample_rate': sample_rate,
        'channels': channels,
        'source': source,
        'sha256': hashlib.sha256(chunk).hexdigest(),
    }
    if mutate:
        mutate(meta)
    return (
        'CC8_ANALYSIS_CHUNK '
        + json.dumps(meta)
        + '\nCC8_ANALYSIS_PCM '
        + base64.b64encode(chunk).decode()
        + '\n'
    )


class RamAudioTests(unittest.TestCase):
    def setUp(self):
        self.data = bytes(range(256)) * (MIN_BYTES // 256) + b'12345678'

    def test_ordered_chunks_reconstruct_including_short_final_chunk(self):
        commands = []

        def shell(command):
            commands.append(command)
            return response(self.data, int(command.rsplit(' ', 1)[1]))

        destination = bytearray(len(self.data))
        read_into(shell, TOKEN, 7, destination)
        self.assertEqual(destination, self.data)
        self.assertEqual([int(x.rsplit(' ', 1)[1]) for x in commands], [0, 192000, 384000])
        self.assertTrue(all('read-reference-analysis' in x for x in commands))

    def test_mono_baseline_is_explicit_and_uses_its_own_extent_and_metadata(self):
        data = self.data[:32008]
        destination = bytearray(len(data))
        read_into(
            lambda command: response(data, 0, sample_rate=16000, channels=1),
            TOKEN,
            7,
            destination,
            sample_rate=16000,
            channels=1,
        )
        self.assertEqual(destination, data)

    def failed_second_chunk(self, second):
        commands = []

        def shell(command):
            commands.append(command)
            if 'clear-reference-analysis' in command:
                return 'CC8_ANALYSIS_CLEARED'
            return response(self.data, 0) if len(commands) == 1 else second

        destination = bytearray(len(self.data))
        with self.assertRaises(RamAudioError) as failure:
            read_into(shell, TOKEN, 7, destination)
        self.assertFalse(any(destination))
        self.assertEqual(len(commands), 3)
        self.assertIn('clear-reference-analysis ' + TOKEN, commands[-1])
        self.assertNotIn('CC8_ANALYSIS_PCM', str(failure.exception))

    def test_missing_chunk_wipes_partial_host_audio_and_revokes_remaining_data(self):
        self.failed_second_chunk('CC8_ANALYSIS_UNAVAILABLE\n')

    def test_replayed_chunk_is_not_accepted_at_a_new_offset(self):
        self.failed_second_chunk(response(self.data, 0))

    def test_valid_base64_with_wrong_digest_is_rejected(self):
        self.failed_second_chunk(
            response(self.data, CHUNK_BYTES, mutate=lambda m: m.update(sha256='0' * 64))
        )

    def test_source_rate_channel_count_and_boolean_extent_cannot_be_substituted(self):
        for field, value in (
            ('source', 6),
            ('sample_rate', 16000),
            ('channels', 1),
            ('offset_bytes', True),
        ):
            with self.subTest(field=field):
                self.failed_second_chunk(
                    response(self.data, CHUNK_BYTES, mutate=lambda m: m.update({field: value}))
                )

    def test_duplicate_fields_and_duplicate_payloads_are_rejected(self):
        valid = response(self.data, CHUNK_BYTES)
        self.failed_second_chunk(valid.replace('{', '{"source":7,', 1))
        self.failed_second_chunk(valid + 'CC8_ANALYSIS_PCM AA==\n')

    def test_deep_metadata_is_rejected_with_a_fixed_error_and_cleans_partial_audio(self):
        self.failed_second_chunk(
            'CC8_ANALYSIS_CHUNK ' + '[' * 1000 + '0' + ']' * 1000 + '\nCC8_ANALYSIS_PCM AA==\n'
        )

    def test_invalid_request_makes_no_device_calls_and_clears_stale_host_buffer(self):
        for token, source, size in (
            (TOKEN + ';whoami', 7, MIN_BYTES),
            (TOKEN, True, MIN_BYTES),
            (TOKEN, 9, MIN_BYTES),
            (TOKEN, 7, MIN_BYTES + 1),
        ):
            with self.subTest(source=source, size=size):
                destination = bytearray(b'x' * size)
                with self.assertRaises(RamAudioError):
                    read_into(
                        lambda cmd: self.fail('Device called before validation'),
                        token,
                        source,
                        destination,
                    )
                self.assertFalse(any(destination))

    def test_transport_failure_is_redacted_even_when_error_contains_private_response(self):
        commands = []

        def shell(command):
            commands.append(command)
            raise RuntimeError('CC8_ANALYSIS_PCM private-samples')

        destination = bytearray(b'x' * MIN_BYTES)
        with self.assertRaisesRegex(RamAudioError, '^analysis_transfer_unconfirmed$'):
            read_into(shell, TOKEN, 7, destination)
        self.assertFalse(any(destination))
        self.assertEqual(len(commands), 2)
        self.assertIn('clear-reference-analysis', commands[-1])

    def test_interruption_wipes_audio_and_attempts_cleanup_without_retrying_read(self):
        commands = []

        def shell(command):
            commands.append(command)
            if 'clear-reference-analysis' in command:
                return 'CC8_ANALYSIS_CLEARED'
            raise KeyboardInterrupt

        destination = bytearray(b'x' * MIN_BYTES)
        with self.assertRaises(KeyboardInterrupt):
            read_into(shell, TOKEN, 7, destination)
        self.assertFalse(any(destination))
        self.assertEqual(len(commands), 2)


if __name__ == '__main__':
    unittest.main()

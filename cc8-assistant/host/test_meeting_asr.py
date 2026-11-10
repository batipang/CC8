"""Meeting ASR is isolated from ordinary utterance limits and uses no live API."""

import io
import unittest
import wave

from meeting_asr import MeetingAsr, CHUNK_SAMPLES
from minimax_client import Transport, MiniMaxError, Cancelled
from test_cloud_asr import JsonResponse
from test_minimax import Session

PCM = (8192).to_bytes(2, 'little', signed=True) * 16000


class MeetingAsrTests(unittest.TestCase):
    def client(self, data=None):
        if data is None:
            data = dict(
                text='你好', duration=1, segments=[dict(start=0, end=1, speaker='S1', text='你好')]
            )
        response = JsonResponse(data)
        session = Session(response)
        client = MeetingAsr(key='fixture-key', session_factory=lambda: session)
        return client, session, response

    def test_actual_multipart_uses_mono_wav_separate_verbose_request_and_language_header(self):
        client, session, response = self.client()
        value = client.transcribe_chunk(PCM, language='zh')
        self.assertEqual(value.text, '你好')
        self.assertEqual((value.duration_ms, value.metadata), (1000, 'available'))
        self.assertEqual((value.spans[0].speaker, value.spans[0].end_ms), ('S1', 1000))
        _, kwargs = session.calls[0]
        self.assertEqual(kwargs['data']['response_format'], 'verbose_json')
        self.assertEqual(kwargs['data']['timestamp_level'], 'sentence')
        self.assertEqual(kwargs['headers']['language'], 'zh')
        self.assertFalse(kwargs['allow_redirects'])
        with wave.open(io.BytesIO(kwargs['files']['file'][1]), 'rb') as wav:
            self.assertEqual(
                (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (16000, 1, 2)
            )
            self.assertEqual(wav.readframes(16001), PCM)
        self.assertTrue(response.closed)
        self.assertEqual(client.slots, set())

    def test_longer_chunk_is_explicitly_separate_and_regular_fourteen_second_bound_remains(self):
        client, session, _ = self.client(dict(text='meeting', duration=20))
        self.assertEqual(client.transcribe_chunk(PCM * 20).duration_ms, 20000)
        with self.assertRaises(ValueError):
            Transport(key='fixture-key', session_factory=lambda: session).transcribe(PCM * 20)
        self.assertEqual(len(session.calls), 1)

    def test_malformed_or_large_input_never_posts(self):
        client, session, _ = self.client()
        for value in (
            b'',
            b'x',
            b'x' * 3198,
            b'x' * (CHUNK_SAMPLES * 2 + 2),
            'file.wav',
            bytearray(PCM),
        ):
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                client.transcribe_chunk(value)
        for language in ('en-US', True, None):
            with self.assertRaises(ValueError):
                client.transcribe_chunk(PCM, language=language)
        self.assertEqual(session.calls, [])

    def test_duration_must_confirm_this_whole_uploaded_chunk(self):
        for duration in (None, True, 0, -1, 0.5, 500, 10**400):
            client, session, response = self.client(dict(text='private-spoken', duration=duration))
            with (
                self.subTest(duration=str(duration)[:20]),
                self.assertRaises(MiniMaxError) as caught,
            ):
                client.transcribe_chunk(PCM)
            self.assertNotIn('private-spoken', str(caught.exception))
            self.assertEqual(len(session.calls), 1)
            self.assertTrue(response.closed)

    def test_optional_alignment_missing_or_bad_does_not_invent_timestamps_or_erase_actual_text(
        self,
    ):
        for rows in (
            None,
            [],
            [dict(start=True, end=1, speaker='S1', text='你好')],
            [dict(start=0, end=1, speaker='a name', text='你好')],
            [dict(start=0, end=1, speaker='S1', text='other')],
            [dict(start=0.0001, end=0.0002, speaker='S1', text='你好')],
            [dict(start=10**400, end=1, speaker='S1', text='你好')],
        ):
            client, _, _ = self.client(dict(text='你好', duration=1, segments=rows))
            value = client.transcribe_chunk(PCM)
            self.assertEqual(value.text, '你好')
            self.assertEqual(value.spans, ())
            self.assertIn(value.metadata, ('unavailable', 'invalid'))

    def test_empty_transcript_is_not_claimed_as_known_speaker_or_retained_audio(self):
        client, _, _ = self.client(dict(text='', duration=1, segments=[], n_speakers=0))
        value = client.transcribe_chunk(PCM)
        self.assertEqual((value.text, value.spans, value.metadata), ('', (), 'available'))
        self.assertNotIn('pcm', repr(value))

    def test_alignment_whitespace_cannot_bypass_retained_text_memory_budget(self):
        # All spans normalize to the tiny root text; the provider can still pad
        # each segment internally. Reject only alignment, preserving actual ASR.
        content = 'a' + ' ' * 10000 + 'a'
        rows = [dict(start=i / 10, end=(i + 1) / 10, speaker='S1', text=content) for i in range(7)]
        client, _, _ = self.client(dict(text='aa' * 7, duration=1, segments=rows))
        value = client.transcribe_chunk(PCM)
        self.assertEqual((value.text, value.metadata, value.spans), ('aa' * 7, 'invalid', ()))

    def test_duplicate_fields_invalid_utf8_and_excess_body_are_fixed_errors(self):
        for raw in (
            b'{"text":"first","text":"second","duration":1}',
            b'{"text":"\xed\xa0\x80","duration":1}',
            b'x' * (512 * 1024 + 1),
        ):
            client, _, response = self.client()
            response.raw = raw
            with self.assertRaises(MiniMaxError):
                client.transcribe_chunk(PCM)
            self.assertTrue(response.closed)

    def test_http_business_errors_are_not_retried(self):
        client, session, response = self.client()
        response.status_code = 302
        with self.assertRaises(MiniMaxError):
            client.transcribe_chunk(PCM)
        self.assertEqual(len(session.calls), 1)
        client, session, _ = self.client(dict(base_resp=dict(status_code=1002)))
        with self.assertRaises(MiniMaxError) as caught:
            client.transcribe_chunk(PCM)
        self.assertEqual(caught.exception.kind, 'rate_limit')
        self.assertEqual(len(session.calls), 1)

    def test_cancelled_or_late_result_closes_only_owned_slot(self):
        client, session, _ = self.client()
        with self.assertRaises(Cancelled):
            client.transcribe_chunk(PCM, lambda: False)
        self.assertEqual(session.calls, [])
        client, _, response = self.client()
        state = dict(current=True)
        raw = response.iter_content

        def late(**kwargs):
            yield from raw(**kwargs)
            state['current'] = False

        response.iter_content = late
        with self.assertRaises(Cancelled):
            client.transcribe_chunk(PCM, lambda: state['current'])
        self.assertTrue(response.closed)
        self.assertEqual(client.slots, set())


if __name__ == '__main__':
    unittest.main()

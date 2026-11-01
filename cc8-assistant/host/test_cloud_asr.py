import io
import json
import unittest
import wave
import numpy as np

from minimax_client import Transport, MiniMaxError, Cancelled
from recognition import MandarinRecognizer
from test_minimax import Session


class JsonResponse:
    status_code = 200

    def __init__(self, data):
        self.raw = json.dumps(data).encode()
        self.closed = False

    def iter_content(self, **kwargs):
        yield self.raw

    def close(self):
        self.closed = True


class CloudAsrTests(unittest.TestCase):
    def transport(self, data):
        response = JsonResponse(data)
        session = Session(response)
        return (
            Transport(key='sk-cp-test-not-a-secret', session_factory=lambda: session),
            session,
            response,
        )

    def test_multipart_wav_and_bounded_response_use_official_host(self):
        t, session, response = self.transport({'text': '大黄蜂你好', 'duration': 1})
        self.assertEqual(t.transcribe(b'RIFFtest'), '大黄蜂你好')
        url, params = session.calls[0]
        self.assertEqual(url, 'https://api.minimax.cn/v1/speech_to_text')
        self.assertFalse(params['allow_redirects'])
        self.assertEqual(params['data']['model'], 'asr-1.0')
        self.assertEqual(params['files']['file'][2], 'audio/wav')
        self.assertTrue(response.closed)

    def test_backend_uploads_a_container_not_bare_pcm_and_silence_is_not_uploaded(self):
        class Backend:
            requests = []

            def transcribe(self, data):
                self.requests.append(data)
                return '常州'

            def cancel(self):
                self.cancelled = True

        backend = Backend()
        asr = MandarinRecognizer(engine='minimax', recognizer=backend)
        self.assertEqual(asr.recognize(np.zeros(16000)), '')
        self.assertEqual(backend.requests, [])
        self.assertEqual(asr.recognize(np.sin(np.arange(16000) * 0.1) * 0.02), '常州')
        with wave.open(io.BytesIO(backend.requests[0])) as f:
            self.assertEqual(f.getframerate(), 16000)
            self.assertEqual(f.getnchannels(), 1)
            self.assertEqual(f.getsampwidth(), 2)
        asr.cancel()
        self.assertTrue(backend.cancelled)

    def test_stale_upload_does_not_start_or_return_text(self):
        t, session, response = self.transport({'text': '旧回答'})
        with self.assertRaises(Cancelled):
            t.transcribe(b'RIFFtest', lambda: False)
        self.assertEqual(session.calls, [])
        t, session, response = self.transport({'text': '旧回答'})
        checks = [True, False]
        with self.assertRaises(Cancelled):
            t.transcribe(b'RIFFtest', lambda: checks.pop(0) if checks else False)

    def test_redirect_api_error_and_malformed_json_are_failures(self):
        t, _, response = self.transport({})
        response.status_code = 302
        with self.assertRaises(MiniMaxError):
            t.transcribe(b'RIFFtest')
        for data in [{'base_resp': {'status_code': 1004}}, {'unexpected': True}]:
            t, _, _ = self.transport(data)
            with self.assertRaises(MiniMaxError):
                t.transcribe(b'RIFFtest')
        t, _, response = self.transport({})
        response.raw = b'invalid'
        with self.assertRaises(MiniMaxError):
            t.transcribe(b'RIFFtest')

    def test_large_upload_and_response_are_rejected(self):
        t, session, response = self.transport({'text': 'test'})
        with self.assertRaises(ValueError):
            t.transcribe(b'x' * (16000 * 2 * 14 + 1))
        self.assertEqual(session.calls, [])
        t, _, response = self.transport({'text': 'test'})
        response.raw = b'x' * (512 * 1024 + 1)
        with self.assertRaises(MiniMaxError):
            t.transcribe(b'RIFFtest')


if __name__ == '__main__':
    unittest.main()

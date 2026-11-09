"""Actual routing, bounded WAV and HTTP header checks; no device/network capture."""

from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch
import numpy as np
from minimax_client import Transport, Cancelled
from recognition import MandarinRecognizer
from speech_providers import DirectSpeech
import test_voice_preferences as voice_tests
from test_voice_preferences import Speech, request as voice_request
from protocol import validate_device_event


def request(language='zh', revision=0):
    return dict(event='recognition_language_set', language=language, revision=revision)


class LanguageSpeech(Speech):
    recognition_language_supported = True

    def __init__(self):
        super().__init__()
        self.hints = []

    def set_recognition_language(self, language):
        self.hints.append(language)


class LanguageTests(unittest.TestCase):
    def session(self):
        b = voice_tests.VoicePreferencesTests().session()
        b.speech = LanguageSpeech()
        return b

    def test_explicit_apply_preserves_other_settings_and_no_work(self):
        b = self.session()
        generation = b.generation
        b.on_event(voice_request(rate='slow', barge=False))
        b.on_event(request('zh', 1))
        state = b.session.voice_preferences_state()
        self.assertEqual(
            (
                state['recognition_language'],
                state['speech_rate'],
                state['natural_barge'],
                state['revision'],
            ),
            ('zh', 'slow', False, 2),
        )
        self.assertTrue(b.events[-1][1]['ok'])
        self.assertEqual(b.speech.hints, ['zh'])
        self.assertEqual(b.generation, generation)
        self.assertFalse(b.session.active)
        self.assertTrue(b.recognition.queue.empty() and b.response.queue.empty())
        self.assertEqual(b.workspace.tasks, [])
        b.on_event(voice_request(2))
        self.assertEqual(b.session.voice_preferences_state()['recognition_language'], 'zh')
        b.on_event(request('auto', 3))
        self.assertEqual(b.speech.hints, ['zh', 'auto'])

    def test_stale_cross_setting_duplicate_and_provider_failure(self):
        b = self.session()
        b.on_event(request())
        b.on_event(request('en'))
        self.assertFalse(b.events[-1][1]['ok'])
        b.on_event(voice_request())
        self.assertFalse(b.events[-1][1]['ok'])
        self.assertEqual(b.speech.rates, [])
        with patch.object(
            b.speech, 'set_recognition_language', side_effect=RuntimeError('fixture')
        ):
            with self.assertRaises(RuntimeError):
                b.on_event(request('en', 1))
        self.assertEqual(b.session.voice_preferences.recognition_language, 'zh')
        self.assertEqual(b.session.voice_preferences.revision, 1)

    def test_active_owner_and_all_pipeline_work_rejected(self):
        for owner, field, value in [
            ('session', 'active', True),
            ('session', 'text_generation', 9),
            ('playback', 'playing', True),
            ('calibration', 'active', True),
            ('calibration', 'running', True),
            ('recognition', 'busy', True),
            ('response', 'busy', True),
            ('visual', 'generation', 9),
            ('announcements', 'generation', 9),
        ]:
            with self.subTest(owner=owner, field=field):
                b = self.session()
                setattr(getattr(b, owner), field, value)
                b.on_event(request())
                self.assertFalse(b.events[-1][1]['ok'])
                self.assertEqual(b.speech.hints, [])
                self.assertEqual(b.session.voice_preferences.revision, 0)
        for worker in ('response', 'recognition'):
            b = self.session()
            getattr(b, worker).queue.put(object())
            b.on_event(request())
            self.assertEqual(b.speech.hints, [])

    def test_unsupported_provider_and_transport_preserve_selection(self):
        b = self.session()
        b.speech = Speech()
        b.on_event(request())
        self.assertFalse(b.events[-1][1]['ok'])
        b.speech = LanguageSpeech()
        b.on_event(request('en'))
        state = b.session.voice_preferences_state()
        for kind in ('wifi', 'usb'):
            b.session.change_transport(SimpleNamespace(kind=kind, route_token='a' * 32))
            self.assertEqual(b.session.voice_preferences_state(), state)
            self.assertFalse(b.session.active)
        self.assertEqual(
            self.session().session.voice_preferences_state()['recognition_language'], 'auto'
        )

    def test_protocol_rejects_ambiguous_and_reserved_values(self):
        for key, values in {
            'revision': [True, -1, 1.0, 2**31 - 1, '1', None],
            'language': ['ZH', 'zh-CN', 'auto\r\nAuthorization: nope', {}, [], True, None],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    data = request()
                    data[key] = value
                    with self.assertRaises(ValueError):
                        validate_device_event(data)
        with self.assertRaises(ValueError):
            validate_device_event({**request(), 'text': 'do not send'})

    def test_real_recognizer_header_and_returned_text_unchanged(self):
        class Response:
            status_code = 200

            def iter_content(self, **kw):
                yield json.dumps(
                    {'text': 'Bumblebee，常州属于江苏省。', 'base_resp': {'status_code': 0}},
                    ensure_ascii=False,
                ).encode()

            def close(self):
                pass

        class Session:
            def post(self, url, **kw):
                self.url = url
                self.kw = kw
                return Response()

            def close(self):
                pass

        session = Session()
        config = SimpleNamespace(
            minimax_base='https://api.minimaxi.com',
            asr_model='asr-1.0',
            connect_timeout=1,
            read_timeout=2,
        )
        transport = Transport(
            key='sk-cp-test-not-a-secret', session_factory=lambda: session, config=config
        )
        speech = DirectSpeech.__new__(DirectSpeech)
        speech.recognizer = MandarinRecognizer('minimax', transport)
        self.assertTrue(speech.recognition_language_supported)
        pcm = np.sin(np.arange(1600) * 0.1) * 0.04
        for language in ('auto', 'zh', 'en', 'auto'):
            speech.set_recognition_language(language)
            self.assertEqual(
                speech.recognize_current(pcm, lambda: True), 'Bumblebee，常州属于江苏省。'
            )
            self.assertEqual(
                session.kw['data'],
                {'model': 'asr-1.0', 'response_format': 'json', 'stream': 'false'},
            )
            self.assertEqual(
                session.kw['headers'].get('language'), None if language == 'auto' else language
            )
            self.assertEqual(session.url, 'https://api.minimaxi.com/v1/speech_to_text')
            self.assertFalse(session.kw['allow_redirects'])
            self.assertEqual(session.kw['files']['file'][0], 'utterance.wav')
            self.assertEqual(transport.slots, set())
        for language in ('fr', 'ZH', True, None):
            with self.assertRaises(ValueError):
                transport.transcribe(b'no upload', language=language)
        with self.assertRaises(Cancelled):
            transport.transcribe(b'no upload', lambda: False, language='zh')
        self.assertEqual(transport.slots, set())

    def test_local_recognizer_cannot_claim_cloud_hint(self):
        local = MandarinRecognizer('sensevoice', recognizer=object())
        with self.assertRaises(ValueError):
            local.set_language_hint('zh')
        speech = DirectSpeech.__new__(DirectSpeech)
        speech.recognizer = local
        self.assertFalse(speech.recognition_language_supported)


if __name__ == '__main__':
    unittest.main()

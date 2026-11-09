"""Actual setting admission, provider requests and audio gates, without capture."""

from dataclasses import replace
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import numpy as np

from minimax_client import MiniMaxVoice
from protocol import validate_device_event
from speech_providers import DirectSpeech
import test_capture_evidence as captures
import test_failure_feedback as fixtures


def request(revision=0, rate='normal', barge=True):
    return dict(
        event='voice_preferences_set', revision=revision, speech_rate=rate, natural_barge=barge
    )


class Speech:
    def __init__(self, text='换一个，介绍一下你自己'):
        self.text = text
        self.rates = []

    def set_voice_rate(self, rate):
        self.rates.append(rate)

    def recognize(self, pcm):
        return self.text


class VoicePreferencesTests(unittest.TestCase):
    def session(self):
        b = fixtures.FailureFeedbackTests().session(Speech())
        b.session.active = False
        return b

    def test_explicit_apply_and_restore_do_not_start_or_cancel_work(self):
        b = self.session()
        generation = b.generation
        b.on_event(request(rate='slow', barge=False))
        self.assertEqual(b.speech.rates, ['slow'])
        state = b.session.voice_preferences_state()
        self.assertEqual((state['speech_rate'], state['natural_barge']), ('slow', False))
        self.assertTrue(b.events[-1][1]['ok'])
        b.on_event(request(1))
        self.assertEqual(b.speech.rates, ['slow', 'normal'])
        self.assertTrue(b.session.voice_preferences.natural_barge)
        self.assertEqual(b.generation, generation)
        self.assertFalse(b.session.active)
        self.assertTrue(b.response.queue.empty())
        self.assertTrue(b.recognition.queue.empty())
        self.assertEqual(b.workspace.tasks, [])
        self.assertEqual(b.diagnostics.report()['requests'], [])

    def test_stale_duplicate_or_invalid_request_cannot_apply(self):
        b = self.session()
        b.on_event(request(rate='fast'))
        for data in (request(rate='slow'), request(barge=False)):
            b.on_event(data)
            self.assertFalse(b.events[-1][1]['ok'])
        for key, values in {
            'revision': [True, -1, 1.0, 2**31 - 1, '1', None],
            'speech_rate': ['turbo', {}, None, 1],
            'natural_barge': [1, 'false', None],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    data = request(1)
                    data[key] = value
                    with self.assertRaises(ValueError):
                        b.on_event(data)
        with self.assertRaises(ValueError):
            validate_device_event({**request(1), 'text': '不要发送给模型'})
        self.assertEqual(b.speech.rates, ['fast'])
        self.assertEqual(b.session.voice_preferences.revision, 1)

    def test_capture_or_any_live_worker_rejects_without_side_effects(self):
        for owner, field, value in (
            ('session', 'active', True),
            ('session', 'text_generation', 100),
            ('playback', 'playing', True),
            ('calibration', 'active', True),
            ('calibration', 'running', True),
            ('recognition', 'busy', True),
            ('response', 'busy', True),
            ('visual', 'generation', 100),
            ('announcements', 'generation', 100),
        ):
            with self.subTest(owner=owner, field=field):
                b = self.session()
                setattr(getattr(b, owner), field, value)
                b.on_event(request(rate='slow', barge=False))
                self.assertFalse(b.events[-1][1]['ok'])
                self.assertEqual(b.speech.rates, [])
                self.assertEqual(b.session.voice_preferences.revision, 0)
                self.assertEqual(getattr(getattr(b, owner), field), value)
        for worker in ('response', 'recognition'):
            b = self.session()
            getattr(b, worker).queue.put(object())
            b.on_event(request(rate='slow'))
            self.assertEqual(b.speech.rates, [])
            self.assertFalse(getattr(b, worker).queue.empty())
        b = self.session()
        b.session.active = True
        b.playback.playing = True
        b.playback.set_pause_capability(True)
        b.pause_for_voice()
        b.playback.playing = False
        b.session.active = False
        b.on_event(request(rate='slow'))
        self.assertEqual(b.speech.rates, [])
        self.assertIsNotNone(b.playback.pause_state)

    def test_unsupported_provider_and_apply_failure_do_not_claim_success(self):
        b = self.session()
        b.speech = object()
        b.on_event(request())
        self.assertFalse(b.events[-1][1]['ok'])
        self.assertFalse(b.session.voice_preferences_state()['supported'])
        b.speech = Speech()
        with patch.object(b.speech, 'set_voice_rate', side_effect=RuntimeError('fixture')):
            with self.assertRaises(RuntimeError):
                b.on_event(request(rate='fast'))
        self.assertEqual(b.session.voice_preferences.revision, 0)

    def test_route_change_does_not_reset_selection_or_enable_microphone(self):
        b = self.session()
        b.on_event(request(rate='slow', barge=False))
        state = b.session.voice_preferences_state()
        for kind in ('wifi', 'usb'):
            b.session.change_transport(SimpleNamespace(kind=kind, route_token='a' * 32))
            self.assertFalse(b.session.active)
            self.assertEqual(b.session.voice_preferences_state(), state)
        self.assertEqual(self.session().session.voice_preferences_state()['speech_rate'], 'normal')

    def test_provider_uses_selected_speed_in_real_minimax_request_only(self):
        class Transport:
            def events(self, endpoint, body, current):
                self.body = body
                yield {'data': {'audio': np.ones(160, dtype='<i2').tobytes().hex(), 'status': 2}}

        transport = Transport()
        speech = DirectSpeech.__new__(DirectSpeech)
        speech.voice = MiniMaxVoice(transport=transport, speed=1.03)
        speech.default_voice_speed = 1.03
        voice = speech.voice.voice
        for rate, speed in (('fast', 1.1845), ('slow', 0.8755), ('normal', 1.03)):
            speech.set_voice_rate(rate)
            list(speech.stream('固定公开句子'))
            self.assertAlmostEqual(transport.body['voice_setting']['speed'], speed)
            self.assertEqual(transport.body['voice_setting']['voice_id'], voice)
            self.assertEqual(transport.body['voice_setting']['pitch'], speech.voice.pitch)
        for base in (0.5, 2.0):
            speech.default_voice_speed = base
            for rate in ('slow', 'fast', 'normal'):
                speech.set_voice_rate(rate)
                self.assertTrue(0.5 <= speech.voice.speed <= 2)
            self.assertEqual(speech.voice.speed, base)

    def test_disabled_natural_barge_preserves_pause_generation_and_new_task_gates(self):
        for enabled in (False, True):
            b, frame, _ = captures.CaptureDiagnosticsTests().endpoint()
            b.playback.set_pause_capability(True)
            b.session.voice_preferences.natural_barge = enabled
            generation = b.generation
            b.capture.endpoint.consume(frame)
            self.assertEqual(b.playback.pause_state is not None, enabled)
            self.assertEqual(b.generation, generation)
            b.capture.endpoint.consume(replace(frame, nanos=frame.nanos + 10_000_000))
            self.assertFalse(b.recognition.queue.empty())

    def test_disabled_mode_keeps_explicit_stop_and_idle_question(self):
        for text, risky, accepted in (
            ('换一个，介绍一下你自己', True, False),
            ('看一看摄像头', True, False),
            ('换一个，介绍一下你自己', False, True),
            ('停一下', True, True),
        ):
            with self.subTest(text=text, risky=risky):
                b = self.session()
                b.speech.text = text
                b.session.active = True
                b.session.voice_preferences.natural_barge = False
                b.playback.playing = risky
                generation = b.generation
                trace = b.diagnostics.begin('voice')
                job = (
                    generation,
                    np.ones(16000),
                    risky,
                    [],
                    {'echo_risk': risky, 'captured_at': time.monotonic()},
                    None,
                    trace,
                )
                fixtures.FailureFeedbackTests().run_one(b.recognition, job)
                row = b.diagnostics.report()['requests'][-1]
                self.assertEqual(row['gate'], 'accepted' if accepted else 'barge_disabled')
                self.assertEqual(b.response.queue.empty(), not (accepted and text != '停一下'))
                self.assertIsNone(b.visual.generation)
                if not accepted:
                    self.assertEqual(b.generation, generation)
                    self.assertTrue(b.playback.playing)
                    self.assertFalse(any(name == 'transcript' for name, _ in b.events))

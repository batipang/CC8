"""Real endpoint/worker ownership with deterministic local and cloud test ports."""

from dataclasses import replace
import threading
import time
import unittest
from unittest.mock import Mock
import numpy as np
from bridge import Bridge
from dsp import FRAME
from protocol import MIC, Packet


class Channel:
    def __init__(self, kind='usb'):
        self.kind = kind

    def sendall(self, data):
        pass


class Echo:
    def process(self, mic, reference):
        return mic.copy()

    def close(self):
        pass


class Endpoint:
    """A short completed utterance with the same pre-roll metadata contract."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.talking = False
        self.audio = []

    def feed(self, pcm):
        self.audio.append(pcm.copy())
        count = len(self.audio)
        self.talking = 2 <= count < 5
        return count == 2, np.concatenate(self.audio) if count == 5 else None, -25


class Local:
    utterance_recheck_supported = True

    def __init__(self, hit, block=False):
        self.hit, self.block = hit, block
        self.checked, self.release = threading.Event(), threading.Event()
        self.reset = Mock()
        self.rechecked = []

    def feed(self, pcm, **fields):
        return ''

    def recheck_utterance(self, pcm, current):
        self.rechecked.append(pcm.copy())
        self.checked.set()
        if self.block and not self.release.wait(2):
            raise RuntimeError('test did not release local verification')
        return self.hit if current() else None


class WaitingUtterancePipelineTests(unittest.TestCase):
    def bridge(self, hit=True, block=False, text='Bumblebee, what is three plus five?'):
        asr, answered = threading.Event(), threading.Event()
        order, questions = [], []
        local = Local(hit, block)

        class Speech:
            def recognize(self, pcm):
                order.append('asr')
                asr.set()
                return text

        class Brain:
            last_metrics = {}

            def cancel(self):
                pass

            def clauses(self, question, history, current, on_web):
                order.append('brain')
                questions.append(question)
                answered.set()
                yield 'Eight.'

        b = Bridge(Speech(), Brain(), wake_detector=local)
        b.capture.echo.hooks = replace(b.capture.echo.hooks, factory=lambda **_: Echo())
        b.capture.echo.open()
        b.session.connection = Channel()
        b.session.active, b.session.wake, b.session.awake = True, True, False
        b.wake.reset('mode')
        b.capture.endpoint.vad = Endpoint()
        b.capture.endpoint.near_speech.feed = Mock(return_value=(True, True))
        b.event, b.log, b.save_status = lambda *a, **k: None, lambda *a, **k: None, lambda: None
        b.speak = lambda *a, **k: True
        original = local.recheck_utterance

        def recheck(*args):
            order.append('local')
            return original(*args)

        local.recheck_utterance = recheck
        return b, local, asr, answered, order, questions

    def frames(self, b, first=1, last=5):
        for n in range(first, last + 1):
            b.process_microphone(
                Packet(
                    MIC,
                    b.session.epoch,
                    n,
                    0,
                    10_000_000_000 + n * 10_000_000,
                    np.full(FRAME, 800, dtype='<i2').tobytes(),
                )
            )

    def terminal(self, b):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows = b.diagnostics.report()['requests']
            if rows and all(r.get('gate') is not None for r in rows) and not b.recognition.busy:
                return rows
            time.sleep(0.002)
        self.fail('test pipeline did not reach a terminal receipt')

    def test_closed_utterance_is_local_only_until_confirmed_then_answers_same_audio(self):
        b, local, asr, answered, order, questions = self.bridge(block=True)
        self.frames(b)
        try:
            self.assertTrue(local.checked.wait(1))
            self.assertFalse(asr.is_set())
            self.assertFalse(b.session.awake)
        finally:
            local.release.set()
        self.assertTrue(answered.wait(1))
        self.assertEqual(order, ['local', 'asr', 'brain'])
        self.assertEqual(questions, ['what is three plus five?'])
        self.assertEqual(len(local.rechecked[0]), 5 * FRAME)
        np.testing.assert_array_equal(local.rechecked[0], np.full(5 * FRAME, 800 / 32768))
        rows = self.terminal(b)
        self.assertEqual(rows[0]['gate'], 'accepted')
        self.assertTrue({'wake_detected', 'asr_start'} <= rows[0]['stages'].keys())
        self.assertEqual(b.metrics['local_wake_english_count'], 1)
        self.assertFalse(b.wake.claim_prefix())

    def test_live_wake_bypasses_finished_utterance_recheck(self):
        b, local, _, answered, order, questions = self.bridge()
        calls = []

        def feed(*args, **kwargs):
            calls.append(1)
            return 'Bumblebee' if len(calls) == 3 else ''

        local.feed = feed
        self.frames(b)
        self.assertTrue(answered.wait(1))
        self.assertEqual(order, ['asr', 'brain'])
        self.assertEqual(questions, ['what is three plus five?'])
        self.assertFalse(local.checked.is_set())
        self.assertEqual(self.terminal(b)[0]['gate'], 'accepted')

    def test_nonkeyword_never_starts_cloud_asr_or_dot(self):
        b, local, asr, answered, order, _ = self.bridge(hit=False)
        self.frames(b)
        rows = self.terminal(b)
        self.assertEqual(order, ['local'])
        self.assertFalse(asr.is_set() or answered.is_set() or b.session.awake)
        self.assertEqual(rows[0]['gate'], 'wake_closed')
        self.assertNotIn('asr_start', rows[0]['stages'])
        self.assertEqual(b.metrics['local_wake_utterances_withheld'], 1)

    def test_playback_tail_and_old_preroll_never_enter_local_recheck(self):
        for condition in ('playback_tail', 'old_preroll', 'playback_preroll'):
            b, local, asr, _, order, _ = self.bridge()
            if condition in ('playback_tail', 'playback_preroll'):
                b.playback.last_audio_end = time.monotonic()
            self.frames(b, last=1)
            if condition == 'playback_preroll':
                b.playback.last_audio_end = 0
            if condition == 'old_preroll':
                b.capture.discard_endpoint_before_ns = 10_020_000_000
            self.frames(b, first=2)
            with self.subTest(condition=condition):
                self.assertEqual(self.terminal(b)[0]['gate'], 'wake_closed')
                self.assertEqual(order, [])
                self.assertFalse(local.checked.is_set() or asr.is_set() or b.session.awake)

    def test_pending_check_cannot_survive_stop_gap_new_wake_or_transport_change(self):
        for condition in ('stop', 'gap', 'new_wake', 'transport', 'playback'):
            b, local, asr, _, _, _ = self.bridge(block=True)
            self.frames(b)
            try:
                self.assertTrue(local.checked.wait(1))
                if condition == 'stop':
                    b.session.stop()
                elif condition == 'gap':
                    b.capture.endpoint.discard_capture_gap()
                elif condition == 'new_wake':
                    b.session.set_awake(True)
                elif condition == 'transport':
                    b.session.change_transport(Channel('wifi'))
                else:
                    b.playback.playing = True
            finally:
                local.release.set()
            with self.subTest(condition=condition):
                rows = self.terminal(b)
                self.assertFalse(asr.is_set())
                self.assertNotIn('asr_start', rows[0]['stages'])
                self.assertEqual(b.session.awake, condition == 'new_wake')
                if condition == 'stop':
                    self.assertFalse(b.session.active)

    def test_new_live_wake_after_local_miss_does_not_authorize_old_closed_audio(self):
        b, _, asr, _, _, _ = self.bridge(hit=False)
        original = b.recognition.hooks.recheck_wake

        def recheck(*args):
            result = original(*args)
            b.session.set_awake(True)
            return result

        b.recognition.hooks = replace(b.recognition.hooks, recheck_wake=recheck)
        self.frames(b)
        rows = self.terminal(b)
        self.assertTrue(b.session.awake)
        self.assertFalse(asr.is_set())
        self.assertNotIn('asr_start', rows[0]['stages'])
        self.assertEqual(rows[0]['gate'], 'stale')

    def test_name_only_opens_window_without_a_brain_task(self):
        b, _, asr, answered, _, questions = self.bridge(text='Bumblebee!')
        self.frames(b)
        rows = self.terminal(b)
        self.assertTrue(b.session.awake and asr.is_set())
        self.assertFalse(answered.is_set())
        self.assertEqual(questions, [])
        self.assertEqual(rows[0]['gate'], 'wake_only')


if __name__ == '__main__':
    unittest.main()

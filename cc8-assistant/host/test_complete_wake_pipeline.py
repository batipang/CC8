"""Actual capture/endpoint/ASR/response ordering, with no device or cloud."""

from dataclasses import replace
import threading
import time
import unittest
from unittest.mock import Mock
from types import SimpleNamespace
import numpy as np
from dsp import FRAME, VoiceDetector
from recognition_worker import RecognitionWorker
from wake_utterance import WaitingUtterance
import test_waiting_utterance_pipeline as fixtures


class CompletePipelineTests(unittest.TestCase):
    def pipeline(self, hit=True, normal_voice=False):
        f = fixtures.WaitingUtterancePipelineTests()
        b, local, asr, answered, order, questions = f.bridge(text='大黄蜂，请问三加五是多少？')
        b.capture.endpoint.vad = VoiceDetector()
        local.complete_chinese_enabled = True
        local.recheck_chinese = local.recheck_utterance
        local.recheck_utterance = Mock(return_value=False)
        local.hit = hit
        local.owned = []
        original_enqueue = b.capture.endpoint.hooks.enqueue

        def enqueue(job):
            if len(job) > 7 and job[7].keyword == '大黄蜂':
                local.owned.append(job[1])
            original_enqueue(job)

        b.capture.endpoint.hooks = replace(b.capture.endpoint.hooks, enqueue=enqueue)
        count = [0]

        def live(pcm, **fields):
            count[0] += 1
            return '大黄蜂' if count[0] == 40 else ''

        local.feed = live

        def near(*args):
            b.capture.endpoint.near_speech.speech = count[0] <= 60
            return normal_voice and count[0] <= 60, False

        b.capture.endpoint.near_speech.feed = near
        return f, b, local, asr, answered, order, questions

    def settle(self, b):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows = b.diagnostics.report()['requests']
            if (
                rows
                and all(r.get('gate') is not None for r in rows)
                and b.recognition.queue.empty()
                and not b.recognition.busy
            ):
                return rows
            time.sleep(0.002)
        self.fail('recognition did not settle')

    def test_weak_local_name_preserves_full_question_and_confirms_before_cloud(self):
        f, b, local, asr, answered, order, questions = self.pipeline()
        local.block = True
        try:
            f.frames(b, last=140)
            self.assertTrue(local.checked.wait(1))
            self.assertFalse(b.session.awake)
            self.assertFalse(asr.is_set())
        finally:
            local.release.set()
        self.assertTrue(answered.wait(1))
        self.settle(b)
        self.assertEqual(order, ['local', 'asr', 'brain'])
        self.assertEqual(questions, ['请问三加五是多少？'])
        self.assertEqual(len(local.rechecked), 1)
        np.testing.assert_array_equal(local.rechecked[0], np.full(140 * FRAME, 800 / 32768))
        self.assertEqual(b.metrics['voice_asr_attempts'], 1)
        self.assertEqual(b.metrics['local_wake_chinese_recheck_hits'], 1)
        self.assertEqual(b.metrics.get('local_wake_english_count', 0), 0)
        self.assertFalse(b.wake.claim_prefix())
        self.assertTrue(local.owned)
        self.assertTrue(all(not np.any(pcm) for pcm in local.owned))

    def test_confusable_or_unavailable_remains_local_and_waiting(self):
        for result in (False, None):
            with self.subTest(result=result):
                f, b, local, asr, answered, order, _ = self.pipeline(result)
                f.frames(b, last=140)
                self.assertTrue(local.checked.wait(1))
                self.settle(b)
                self.assertFalse(asr.is_set())
                self.assertFalse(answered.is_set())
                self.assertFalse(b.session.awake)
                self.assertEqual(order, ['local'])
                self.assertEqual(b.metrics.get('voice_asr_attempts', 0), 0)
                self.assertTrue(all(not np.any(pcm) for pcm in local.owned))

    def test_model_change_while_collecting_cannot_enqueue_old_sentence(self):
        f, b, local, asr, answered, order, _ = self.pipeline()
        local.complete_recovery_generation = 1
        f.frames(b, last=45)
        self.assertTrue(b.capture.endpoint.complete_wake.vad.talking)
        local.complete_recovery_generation = 2
        f.frames(b, first=46, last=140)
        self.assertTrue(b.recognition.queue.empty())
        self.assertEqual(local.owned, [])
        self.assertFalse(local.checked.is_set())
        self.assertFalse(asr.is_set())
        self.assertFalse(answered.is_set())
        self.assertFalse(b.session.awake)
        self.assertEqual(order, [])

    def test_idle_waits_for_owned_weak_chinese_candidate_then_can_sleep(self):
        f, b, local, asr, answered, order, _ = self.pipeline(False)
        f.frames(b, last=45)
        self.assertFalse(b.capture.endpoint.vad.talking)
        self.assertTrue(b.capture.endpoint.complete_wake.vad.talking)
        b.session.last_interaction = time.monotonic() - 901
        b.session.idle_tick()
        self.assertFalse(b.session.screen_sleeping)
        f.frames(b, first=46, last=140)
        self.assertTrue(local.checked.wait(1))
        self.settle(b)
        b.session.idle_tick()
        self.assertTrue(b.session.screen_sleeping)
        self.assertFalse(asr.is_set())
        self.assertFalse(answered.is_set())

    def test_unowned_raw_speech_does_not_extend_screen_idle(self):
        f, b, local, *_ = self.pipeline(False)
        local.feed = lambda *a, **k: ''
        # The raw speech bit may be true for background sound; no local name.
        b.capture.endpoint.near_speech.feed = lambda *a: (
            setattr(b.capture.endpoint.near_speech, 'speech', True) or False,
            False,
        )
        f.frames(b, last=45)
        self.assertTrue(b.capture.endpoint.complete_wake.vad.talking)
        b.session.last_interaction = time.monotonic() - 901
        b.session.idle_tick()
        self.assertTrue(b.session.screen_sleeping)

    def test_cancelled_partial_candidate_cannot_hold_screen_awake(self):
        f, b, *_ = self.pipeline(False)
        f.frames(b, last=45)
        self.assertTrue(b.capture.endpoint.waiting_chinese_busy)
        b.wake.reset('cancel')
        self.assertFalse(b.capture.endpoint.waiting_chinese_busy)
        b.session.last_interaction = time.monotonic() - 901
        b.session.idle_tick()
        self.assertTrue(b.session.screen_sleeping)

    def test_idle_cannot_observe_vad_reset_before_completed_job_is_enqueued(self):
        f, b, local, *_ = self.pipeline(False)
        local.block = True
        f.frames(b, last=139)
        entered, release, idle_done = threading.Event(), threading.Event(), threading.Event()
        original = b.capture.endpoint.hooks.enqueue
        errors = []

        def enqueue(job):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test enqueue was not released')
            original(job)

        def capture():
            try:
                f.frames(b, first=140, last=140)
            except Exception as error:
                errors.append(error)

        def idle():
            b.session.idle_tick()
            idle_done.set()

        b.capture.endpoint.hooks = replace(b.capture.endpoint.hooks, enqueue=enqueue)
        b.session.last_interaction = time.monotonic() - 901
        recorder = threading.Thread(target=capture)
        ticker = threading.Thread(target=idle)
        recorder.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(b.capture.endpoint.complete_wake.vad.talking)
            self.assertTrue(b.recognition.queue.empty())
            ticker.start()
            self.assertFalse(idle_done.wait(0.05))
            release.set()
            recorder.join(1)
            self.assertTrue(local.checked.wait(1))
            self.assertTrue(idle_done.wait(1))
            self.assertFalse(b.session.screen_sleeping)
        finally:
            release.set()
            local.release.set()
            recorder.join(2)
            if ticker.ident is not None:
                ticker.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(recorder.is_alive())
        self.assertFalse(ticker.is_alive())
        self.settle(b)

    def test_eviction_and_discard_erase_only_owned_chinese_audio(self):
        diagnostics = Mock()
        worker = RecognitionWorker(SimpleNamespace(diagnostics=lambda: diagnostics))

        def job(chinese):
            ticket = (
                WaitingUtterance(1, 2, 0, '大黄蜂', 10, 'a' * 32)
                if chinese
                else WaitingUtterance(1, 2, 0)
            )
            return (2, np.ones(FRAME), False, [], {}, None, 'test-trace', ticket)

        first, second, legacy = job(True), job(True), job(False)
        worker.enqueue(first)
        worker.enqueue(second)
        worker.enqueue(legacy)
        self.assertFalse(np.any(first[1]))
        self.assertTrue(np.all(second[1] == 1))
        worker.discard()
        self.assertFalse(np.any(second[1]))
        self.assertTrue(np.all(legacy[1] == 1))

    def test_parallel_standard_endpoint_cannot_duplicate_new_chinese_authorization(self):
        f, b, local, asr, answered, order, questions = self.pipeline(normal_voice=True)
        release = threading.Event()
        original_speech = b.speech.recognize

        def recognize(pcm):
            value = original_speech(pcm)
            self.assertTrue(release.wait(2))
            return value

        b.speech.recognize = recognize
        original_feed = b.capture.endpoint.vad.feed

        def feed(pcm):
            # The completed candidate is now queued under the session guard.
            # Force ASR to start after that guard releases, before the parallel
            # ordinary endpoint finishes, just as the original race required.
            if local.owned:
                self.assertTrue(asr.wait(1))
            return original_feed(pcm)

        b.capture.endpoint.vad.feed = feed
        try:
            f.frames(b, last=140)
        finally:
            release.set()
        self.assertTrue(answered.wait(1))
        self.settle(b)
        self.assertEqual(questions, ['请问三加五是多少？'])
        self.assertEqual(b.metrics['voice_asr_attempts'], 1)
        self.assertEqual(order, ['local', 'asr', 'brain'])

    def test_reset_during_full_confirmation_cannot_submit_old_audio(self):
        for reason in ('transport', 'capture_gap', 'cancel', 'stop'):
            with self.subTest(reason=reason):
                f, b, local, asr, answered, order, _ = self.pipeline()
                local.block = True
                try:
                    f.frames(b, last=140)
                    self.assertTrue(local.checked.wait(1))
                    b.wake.reset(reason)
                finally:
                    local.release.set()
                self.settle(b)
                self.assertFalse(asr.is_set())
                self.assertFalse(answered.is_set())
                self.assertFalse(b.session.awake)


if __name__ == '__main__':
    unittest.main()

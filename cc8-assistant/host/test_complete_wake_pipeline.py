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
        original_enqueue = b.capture.endpoint.hooks.enqueue

        def enqueue(job):
            original_enqueue(job)
            if len(job) > 7 and job[7].keyword == '大黄蜂':
                self.assertTrue(asr.wait(1))

        b.capture.endpoint.hooks = replace(b.capture.endpoint.hooks, enqueue=enqueue)
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

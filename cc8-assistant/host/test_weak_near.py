"""Prevent low-evidence render-risk ASR from replacing a real playing task."""

from dataclasses import replace
import json
import threading
import time
import unittest
from unittest.mock import patch
import numpy as np

from bridge import Bridge
from diagnostics import ASR_STOP_STAGE
from test_barge_in import Sink


def captured(*, credible=2, risky=True, local_wake=False):
    # Synthetic observations model a brief confirmed burst, not a saved report.
    return dict(
        frames=120,
        voiced_frames=max(14, credible),
        credible_frames=credible,
        render_frames=20 if risky else 0,
        raw_correlated_frames=1 if risky else 0,
        residual_correlated_frames=0,
        pause_requested_frames=110 if risky else 0,
        voiced_after_pause_request_frames=12 if risky else 0,
        raw_peak_dbfs=-30.0,
        cleaned_peak_dbfs=-32.0,
        render_peak_dbfs=-25.0 if risky else -120.0,
        max_raw_correlation=0.83 if risky else 0,
        max_residual_correlation=0.52 if risky else 0,
        echo_risk=risky,
        during_playback=risky,
        local_wake=local_wake,
    )


class WeakNearTests(unittest.TestCase):
    def start(
        self,
        text='一九二九年。',
        *,
        capture=None,
        risky=True,
        local_wake=False,
        delayed=False,
        playing=None,
        references=None,
        render_audio=None,
    ):
        entered, release = threading.Event(), threading.Event()
        calls = []

        class Speech:
            def recognize(self, pcm):
                calls.append('recognize')
                entered.set()
                if delayed:
                    release.wait(2)
                return text

        b = Bridge(Speech(), None)
        b.session.connection = Sink()
        b.session.active = True
        b.session.awake = True
        b.session.wake = False
        b.playback.playing = risky if playing is None else playing
        b.playback.set_pause_capability(True)
        b.save_status = lambda: None
        replies = []
        b.recognition.hooks = replace(b.recognition.hooks, enqueue_response=replies.append)
        generation = b.session.generation
        token = b.pause_for_voice() if risky else None
        key = b.diagnostics.begin('voice')
        if capture is not None:
            b.diagnostics.capture(key, capture)
        b.recognition.queue.put(
            (
                generation,
                np.zeros(16000),
                risky,
                ['一加一等于二。'] if references is None else references,
                {
                    'echo_risk': risky,
                    'captured_at': time.monotonic(),
                    'pause_token': token,
                    'local_wake': local_wake,
                },
                render_audio,
                key,
            )
        )
        return b, key, generation, replies, calls, entered, release

    def wait(self, b, key):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            row = next(r for r in b.diagnostics.report()['requests'] if r['request_id'] == key)
            if 'gate' in row and not b.recognition.busy:
                return row
            time.sleep(0.005)
        self.fail('Recognition did not finish its gate decision')

    def test_brief_render_risk_hallucination_is_rejected_and_playback_resumes(self):
        b, key, generation, replies, calls, _, _ = self.start(capture=captured())
        row = self.wait(b, key)
        self.assertEqual(row['gate'], 'weak_near')
        self.assertEqual(row['outcome'], 'rejected')
        self.assertEqual(b.session.generation, generation)
        self.assertEqual(replies, [])
        self.assertEqual(calls, ['recognize'])
        self.assertIsNone(b.playback.pause_state)
        events = [json.loads(p.data) for p in b.session.connection.packets]
        self.assertFalse(any(e['event'] == 'transcript' for e in events))
        self.assertTrue(any(e['event'] == 'playback_resume' for e in events))

    def test_confirmed_near_speech_still_replaces_playback_and_queues_exact_question(self):
        b, key, generation, replies, _, _, _ = self.start(
            '换一个，讲个笑话。', capture=captured(credible=30)
        )
        row = self.wait(b, key)
        self.assertEqual(row['gate'], 'accepted')
        self.assertGreater(b.session.generation, generation)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].text, '换一个，讲个笑话。')

    def test_idle_voice_local_wake_and_stop_commands_are_not_blocked_by_weak_count(self):
        cases = [
            ('普通问题。', False, False, False),
            ('大黄蜂，换一个。', True, True, False),
            ('停一下。', True, False, True),
            ('大黄蜂，停一下。', True, True, True),
            ('大黄蜂，暂停。', True, False, True),
            ('Bumblebee，暂停。', True, False, True),
            ('大黄蜂，展厅怎么走？', True, False, False),
        ]
        for text, risky, local_wake, stop in cases:
            with self.subTest(text=text):
                b, key, _, replies, _, _, _ = self.start(
                    text,
                    capture=captured(
                        credible=30 if '展厅' in text else 2,
                        risky=risky,
                        local_wake=local_wake,
                    ),
                    risky=risky,
                    local_wake=local_wake,
                )
                row = self.wait(b, key)
                self.assertEqual(row['gate'], 'accepted')
                self.assertEqual(len(replies), int(not stop))
                if stop:
                    self.assertEqual(row['outcome'], 'completed')
                    self.assertGreaterEqual(row['stages'][ASR_STOP_STAGE], row['stages']['asr_end'])
                else:
                    self.assertNotIn(ASR_STOP_STAGE, row['stages'])

    def test_asr_stop_matching_playback_needs_confirmed_near_speech(self):
        for count, gate in ((2, 'echo_text'), (30, 'accepted')):
            with self.subTest(credible_frames=count):
                b, key, generation, replies, _, _, _ = self.start(
                    '暂停。',
                    capture=captured(credible=count),
                    references=['你可以说“暂停”来打断我。'],
                )
                row = self.wait(b, key)
                self.assertEqual(row['gate'], gate)
                self.assertEqual(replies, [])
                events = [json.loads(p.data) for p in b.session.connection.packets]
                if gate == 'echo_text':
                    self.assertEqual(row['outcome'], 'rejected')
                    self.assertEqual(b.session.generation, generation)
                    self.assertTrue(any(e['event'] == 'playback_resume' for e in events))
                    self.assertFalse(any(e['event'] == 'transcript' for e in events))
                else:
                    self.assertEqual(row['outcome'], 'completed')
                    self.assertGreater(b.session.generation, generation)

    def test_stale_inflight_asr_cannot_resume_or_reclassify_a_new_request(self):
        b, key, _, replies, _, entered, release = self.start(capture=captured(), delayed=True)
        self.assertTrue(entered.wait(1))
        b.cancel('new request')
        newer = b.diagnostics.begin('voice', b.session.generation)
        release.set()
        row = self.wait(b, key)
        self.assertEqual(row['gate'], 'stale')
        self.assertEqual(replies, [])
        current = next(r for r in b.diagnostics.report()['requests'] if r['request_id'] == newer)
        self.assertNotIn('gate', current)
        self.assertIsNone(current['outcome'])
        events = [json.loads(p.data) for p in b.session.connection.packets]
        self.assertFalse(any(e['event'] == 'playback_resume' for e in events))

    def test_stale_echo_comparison_cannot_update_status_or_start_another_asr(self):
        for comparison, result, asr_count in (
            (1, True, 1),
            (1, False, 1),
            (2, True, 2),
        ):
            with self.subTest(comparison=comparison, result=result):
                comparing, finish_comparison = threading.Event(), threading.Event()
                comparisons = 0

                def compare(*_):
                    nonlocal comparisons
                    comparisons += 1
                    if comparisons != comparison:
                        return False
                    comparing.set()
                    if not finish_comparison.wait(2):
                        raise TimeoutError('test comparison was not released')
                    return result

                with patch('recognition_worker.resembles_playback', side_effect=compare):
                    b, key, _, replies, calls, _, _ = self.start(
                        '旧播报内容。', render_audio=np.full(16000, 0.1)
                    )
                    try:
                        self.assertTrue(comparing.wait(1))
                        b.cancel('new request')
                        b.status('thinking', '新请求')
                        packet_count = len(b.session.connection.packets)
                    finally:
                        finish_comparison.set()
                    row = self.wait(b, key)
                self.assertEqual(calls, ['recognize'] * asr_count)
                self.assertEqual(row['gate'], 'stale')
                self.assertEqual(replies, [])
                self.assertEqual(b.session.connection.packets[packet_count:], [])
                self.assertEqual(b.metrics.get('echo_transcripts_rejected', 0), 0)
                self.assertEqual(b.metrics.get('render_asr_rejections', 0), 0)

    def test_weak_visual_words_do_not_open_camera_but_confirmed_request_still_routes(self):
        for count, expected in ((2, 0), (30, 1)):
            with self.subTest(credible_frames=count):
                b, key, _, _, _, entered, release = self.start(
                    '看一看。', capture=captured(credible=count), delayed=True
                )
                self.assertTrue(entered.wait(1))
                visual = []
                b.recognition.hooks = replace(
                    b.recognition.hooks, begin_visual=lambda *args: visual.append(args)
                )
                release.set()
                row = self.wait(b, key)
                self.assertEqual(len(visual), expected)
                self.assertEqual(row['gate'], 'accepted' if expected else 'weak_near')

    def test_rejected_recent_render_tail_restores_listening_instead_of_stuck_thinking(self):
        observations = captured()
        observations['during_playback'] = False
        b, key, _, replies, _, _, _ = self.start(capture=observations, playing=False)
        row = self.wait(b, key)
        self.assertEqual(row['gate'], 'weak_near')
        self.assertEqual(replies, [])
        events = [json.loads(p.data) for p in b.session.connection.packets]
        phases = [e['phase'] for e in events if e['event'] == 'status']
        self.assertIn('thinking', phases)
        self.assertEqual(phases[-1], 'listening')

    def test_empty_asr_only_prompts_to_repeat_for_confirmed_non_echo_speech(self):
        for text, count, risky, expected in (
            ('', 30, False, '刚才没听清，请再说一遍'),
            ('。', 2, False, '我在听'),
            ('谢谢观看', 30, False, '我在听'),
            ('', 30, True, '我在听'),
        ):
            with self.subTest(text=text, count=count, risky=risky):
                b, key, _, replies, _, _, _ = self.start(
                    text,
                    capture=captured(credible=count, risky=risky),
                    risky=risky,
                    playing=False,
                )
                row = self.wait(b, key)
                self.assertEqual(row['gate'], 'empty')
                self.assertEqual(row['outcome'], 'rejected')
                self.assertEqual(replies, [])
                events = [json.loads(p.data) for p in b.session.connection.packets]
                statuses = [e for e in events if e['event'] == 'status']
                self.assertEqual(statuses[-1]['message'], expected)
                self.assertFalse(any(e['event'] == 'transcript' for e in events))

    def test_empty_asr_resumes_playback_without_a_listening_status(self):
        b, key, generation, replies, _, entered, release = self.start(
            '', capture=captured(credible=30), delayed=True
        )
        self.assertTrue(entered.wait(1))
        resume = b.recognition.hooks.resume_rejected
        resumed = threading.Event()

        def record_resume(*args):
            try:
                return resume(*args)
            finally:
                resumed.set()

        b.recognition.hooks = replace(b.recognition.hooks, resume_rejected=record_resume)
        release.set()
        self.assertTrue(resumed.wait(1))
        row = self.wait(b, key)
        self.assertEqual(row['gate'], 'empty')
        self.assertEqual(b.session.generation, generation)
        self.assertEqual(replies, [])
        events = [json.loads(p.data) for p in b.session.connection.packets]
        self.assertTrue(any(e['event'] == 'playback_resume' for e in events))
        self.assertFalse(any(e['event'] == 'status' and e['phase'] == 'listening' for e in events))

    def test_cancel_during_evidence_read_does_not_overwrite_new_owner_status(self):
        for text, count, risky in (
            ('一九二九年。', 2, True),
            ('一九二九年。', 30, True),
            ('', 30, False),
        ):
            with self.subTest(text=text, credible_frames=count):
                b, key, _, replies, _, entered, release = self.start(
                    text,
                    capture=captured(credible=count, risky=risky),
                    risky=risky,
                    delayed=True,
                    playing=False,
                )
                self.assertTrue(entered.wait(1))
                reading, finish_read = threading.Event(), threading.Event()
                original = b.diagnostics.capture_record

                def read(current):
                    reading.set()
                    finish_read.wait(2)
                    return original(current)

                b.diagnostics.capture_record = read
                release.set()
                self.assertTrue(reading.wait(1))
                b.cancel('new owner')
                newer = b.diagnostics.begin('voice', b.session.generation)
                before = len(b.session.connection.packets)
                finish_read.set()
                old = self.wait(b, key)
                self.assertEqual(old['gate'], 'stale')
                events = [json.loads(p.data) for p in b.session.connection.packets[before:]]
                self.assertFalse(any(e['event'] in ('status', 'playback_resume') for e in events))
                current = next(
                    r for r in b.diagnostics.report()['requests'] if r['request_id'] == newer
                )
                self.assertNotIn('gate', current)
                self.assertIsNone(current['outcome'])
                self.assertEqual(b.metrics.get('weak_near_rejections', 0), 0)
                self.assertEqual(replies, [])

    def test_missing_legacy_observation_is_not_fabricated_as_zero_confidence(self):
        b, key, _, replies, _, _, _ = self.start('换一个。')
        row = self.wait(b, key)
        self.assertEqual(row['gate'], 'accepted')
        self.assertNotIn('capture', row)
        self.assertEqual(len(replies), 1)


if __name__ == '__main__':
    unittest.main()

"""Real Bridge/announcement/receipt composition, synthetic excerpts and no audio or model."""

from copy import deepcopy
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

import test_report_watch_controller as fixture
from test_report_voice import report


class WatchSummaryTests(unittest.TestCase):
    def start(self, folder, values):
        self.f = fixture.WatchControllerTests()
        b, store = self.f.session(folder)
        self.f.configure(b, mode='summary')
        self.jobs, self.spoken = [], []

        def speak(text, generation):
            self.spoken.append(text)
            return True

        b.announcements.hooks = replace(b.announcements.hooks, offer=self.jobs.append, speak=speak)
        self.value = report(*values)
        return b, store

    def poll(self, b, now=400):
        self.f.now = now

        def read(source, window, **kwargs):
            self.assertTrue(kwargs['is_current']())
            value = deepcopy(self.value)
            value.update(window_start=window.start, window_end=window.end)
            return value

        with patch.object(b.chats.catalog, 'report', side_effect=read):
            b.reminder_tick()
            b.chats.watch_ui.scheduler.worker.join(3)
            self.assertFalse(b.chats.watch_ui.scheduler.worker.is_alive())

    def test_complete_literal_items_are_acknowledged_individually_then_remaining_items_next_poll(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.start(folder, ['PRIVATE-EXCERPT-' + str(i) for i in range(5)])
            self.value['coverage_complete'] = False
            original_progress = b.chats.voice.progress
            self.poll(b)
            job = self.jobs[0]
            self.assertEqual(len(job.segments), 4)
            self.assertIn('覆盖不完整', job.segments[0])
            self.assertIn('之后的检查', job.segments[0])
            self.assertEqual(store.snapshot()['watch']['seen'], [])
            b.announcements.read_announcement(job)
            self.assertTrue(job.done.is_set())
            self.assertEqual(job.result, 'completed')
            self.assertEqual((job.text, job.segments, job.on_segment), ('', (), None))
            self.assertEqual(len(store.snapshot()['watch']['seen']), 3)
            self.assertIsNone(store.snapshot()['watch']['attempt'])
            self.assertIn('PRIVATE-EXCERPT-0', ''.join(self.spoken))
            self.assertIn('尚未独立核验', ''.join(self.spoken))
            self.assertNotIn('PRIVATE-EXCERPT-3', ''.join(self.spoken))
            self.poll(b, 700)
            self.assertEqual(len(self.jobs), 2)
            b.announcements.read_announcement(self.jobs[1])
            b.reminder_tick()
            self.assertEqual(len(store.snapshot()['watch']['seen']), 5)
            self.assertNotIn('PRIVATE-EXCERPT', store.path.read_text())
            self.assertIs(b.chats.voice.progress, original_progress)
            self.assertFalse(b.session.active)

    def test_interrupted_second_item_retains_first_ack_and_freezes_only_unconfirmed_items(self):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.start(folder, ['第一条' * 160, '第二条' * 160, '第三条'])
            second = [False]

            def speak(text, generation):
                self.spoken.append(text)
                if second[0]:
                    b.cancel('test_interruption')
                    return False
                if text.startswith('本次定时摘要第2项'):
                    second[0] = True
                return True

            b.announcements.hooks = replace(b.announcements.hooks, speak=speak)
            self.poll(b)
            b.announcements.read_announcement(self.jobs[0])
            b.reminder_tick()
            self.assertEqual(self.jobs[0].segments, ())
            watch = store.snapshot()['watch']
            self.assertEqual(len(watch['seen']), 1)
            self.assertEqual(len(watch['attempt']['items']), 2)
            self.assertEqual(watch['attempt']['status'], 'unconfirmed')
            self.assertFalse(watch['enabled'])
            self.assertIsNone(
                store.part(watch['id'], watch['attempt']['id'], watch['attempt']['items'][0])
            )
            for now in (700, 1000):
                self.f.now = now
                with patch.object(
                    b.chats.catalog, 'report', side_effect=AssertionError('No replay')
                ):
                    b.reminder_tick()
            self.assertEqual(len(self.jobs), 1)
            self.assertNotIn('第三条', ''.join(self.spoken))

    def test_whole_job_success_without_segment_acks_cannot_mark_summary_heard(self):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.start(folder, ['PRIVATE'])
            self.poll(b)
            self.jobs[0].result = 'completed'
            self.jobs[0].done.set()
            b.reminder_tick()
            watch = store.snapshot()['watch']
            self.assertEqual(watch['seen'], [])
            self.assertFalse(watch['enabled'])
            self.assertEqual(watch['attempt']['detail'], 'unconfirmed')

    def test_internal_bounds_reject_before_cancellation_and_public_limit_stays_500(self):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.start(folder, ['PRIVATE'])
            before = b.generation
            for values in ((), ['text'], ('a' * 1201,), ('x',) * 5, ('\ud800',)):
                with self.subTest(values=len(values)), self.assertRaises(ValueError):
                    b.announcements.announce_segments(
                        values, prepare=None, on_segment=lambda _: None
                    )
                self.assertEqual(b.generation, before)
            with self.assertRaises(ValueError):
                b.announce('x' * 501)
            self.assertEqual(b.generation, before)
            self.assertEqual(self.jobs, [])

    def test_failed_item_receipt_does_not_replay_audio_or_mark_remaining_items(self):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.start(folder, ['PRIVATE-1', 'PRIVATE-2'])
            self.poll(b)
            with patch.object(store, 'part', side_effect=OSError('PRIVATE disk failure')):
                b.announcements.read_announcement(self.jobs[0])
            self.assertEqual(self.jobs[0].result, 'failed')
            b.reminder_tick()
            self.assertFalse(store.snapshot()['watch']['enabled'])
            self.assertEqual(store.snapshot()['watch']['seen'], [])
            self.assertNotIn('PRIVATE-2', ''.join(self.spoken))
            self.assertEqual(len(self.jobs), 1)

    def test_deadline_invalidates_only_its_own_announcement_and_freezes_unconfirmed_delivery(self):
        for newer in (False, True):
            with self.subTest(newer=newer), tempfile.TemporaryDirectory() as folder:
                b, store = self.start(folder, ['PRIVATE'])
                self.poll(b)
                generation = b.generation
                if newer:
                    b.session.generation += 10
                expected = b.generation
                self.f.now = 1001
                b.reminder_tick()
                self.assertEqual(b.generation, expected if newer else generation + 1)
                self.assertFalse(store.snapshot()['watch']['enabled'])
                b.announcements.read_announcement(self.jobs[0])
                self.assertEqual(self.spoken, [])
                self.assertTrue(self.jobs[0].done.is_set())

    def test_superseded_submission_releases_excerpts_without_queueing(self):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.start(folder, ['PRIVATE'])
            job = b.announcements.announce_segments(
                ('PRIVATE',), prepare=lambda _: b.cancel('test'), on_segment=lambda _: None
            )
            self.assertTrue(job.done.is_set())
            self.assertEqual((job.text, job.segments, job.on_segment), ('', (), None))
            self.assertEqual(self.jobs, [])

    def test_queue_eviction_releases_summary_body_and_callback(self):
        from announce import Announcement

        with tempfile.TemporaryDirectory() as folder:
            b, store = self.start(folder, ['PRIVATE'])
            old = Announcement(-1, 'PRIVATE', segments=('PRIVATE',), on_segment=lambda _: None)
            b.response.queue.put_nowait(old)
            b.response.queue.put_nowait(Announcement(-1, 'ordinary'))
            b.response.enqueue(Announcement(-1, 'replacement'))
            self.assertTrue(old.done.is_set())
            self.assertEqual((old.text, old.segments, old.on_segment), ('', (), None))

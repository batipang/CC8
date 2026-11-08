"""Announcement admission and completion use the real session/queue, without audio."""

import json
import queue
from dataclasses import replace
import unittest
from unittest.mock import patch

from announce import Announcement
import test_text_admission as fixture


class AnnouncementAdmissionTests(unittest.TestCase):
    def bridge(self, *, active=False):
        return fixture.TextAdmissionTests(methodName='runTest').bridge(active=active)

    def latest_text(self, bridge):
        fixture.TextAdmissionTests(methodName='runTest').work_latest(bridge, '最新指令')

    def test_replacement_during_cancel_does_not_leave_stale_announcement_owner(self):
        bridge = self.bridge()
        original = bridge.event
        replaced = []

        def event(name, **fields):
            original(name, **fields)
            if name == 'cancel' and not replaced:
                replaced.append(True)
                bridge.submit_text('最新指令')

        bridge.event = event
        job = bridge.announce('旧播报')
        self.assertTrue(job.done.is_set())
        self.assertEqual(job.result, 'interrupted')
        self.assertIsNone(bridge.announcements.generation)
        self.latest_text(bridge)

    def test_prepare_replacement_does_not_enqueue_old_work(self):
        bridge = self.bridge()
        job = bridge.announcements.announce(
            '旧播报', prepare=lambda _: bridge.submit_text('最新指令')
        )
        self.assertTrue(job.done.is_set())
        self.assertEqual(job.result, 'interrupted')
        self.latest_text(bridge)

    def test_prepare_failure_after_replacement_preserves_new_owner(self):
        bridge = self.bridge()

        def prepare(_):
            bridge.submit_text('最新指令')
            raise OSError(fixture.SECRET)

        try:
            job = bridge.announcements.announce('旧播报', prepare=prepare)
        except OSError:
            self.fail('Old preparation error escaped after replacement')
        self.assertTrue(job.done.is_set())
        self.assertEqual(job.result, 'interrupted')
        self.latest_text(bridge)

    def test_diagnostic_start_failure_releases_owner_and_next_request_runs(self):
        bridge = self.bridge()
        with patch.object(bridge.diagnostics, 'begin', side_effect=OSError(fixture.SECRET)):
            with self.assertRaises(OSError):
                bridge.announce('旧播报')
        self.assertIsNone(bridge.announcements.generation)
        self.assertTrue(bridge.response.queue.empty())
        bridge.submit_text('最新指令')
        self.latest_text(bridge)

    def test_uncertain_enqueue_failure_invalidates_job_even_with_active_mic(self):
        for active in (False, True):
            with self.subTest(active=active):
                bridge = self.bridge(active=active)
                original = bridge.response.offer

                def offer(job):
                    original(job)
                    raise OSError(fixture.SECRET)

                with patch.object(bridge.response, 'offer', side_effect=offer):
                    with self.assertRaises(OSError):
                        bridge.announce('不得播放的固定测试句')
                job = bridge.response.queue.get_nowait()
                self.assertFalse(bridge.valid(job.generation))
                self.assertTrue(job.done.is_set())
                self.assertEqual(job.result, 'failed')
                self.assertEqual(bridge.session.active, active)
                with patch.object(bridge, 'speak') as speak:
                    bridge.response.respond_job(job)
                speak.assert_not_called()
                bridge.submit_text('最新指令')
                self.latest_text(bridge)

    def test_queue_busy_retains_fixed_error_and_records_terminal_failure(self):
        bridge = self.bridge(active=True)
        with patch.object(bridge.response, 'offer', side_effect=queue.Full(fixture.SECRET)):
            with self.assertRaisesRegex(RuntimeError, '^Announcement queue busy$'):
                bridge.announce('固定测试句')
        self.assertIsNone(bridge.announcements.generation)
        row = bridge.diagnostics_report()['requests'][-1]
        self.assertEqual(row['outcome'], 'failed')
        self.assertEqual((row.get('error') or {}).get('stage'), 'runtime')
        self.assertNotIn(fixture.SECRET, json.dumps((bridge.logs, row)))

    def test_secondary_cleanup_failure_does_not_mask_original_error(self):
        bridge = self.bridge()
        primary = OSError('primary-' + fixture.SECRET)
        with (
            patch.object(bridge.response, 'offer', side_effect=primary),
            patch.object(bridge.diagnostics, 'failure', side_effect=RuntimeError(fixture.SECRET)),
        ):
            with self.assertRaises(OSError) as caught:
                bridge.announce('固定测试句')
        self.assertIs(caught.exception, primary)
        self.assertIsNone(bridge.announcements.generation)
        self.assertNotIn(fixture.SECRET, json.dumps(bridge.logs))

    def test_failure_cleanup_callback_cannot_cancel_a_new_request(self):
        bridge = self.bridge()
        original = bridge.diagnostics.failure

        def failure(generation, stage, error):
            original(generation, stage, error)
            bridge.submit_text('最新指令')

        with (
            patch.object(bridge.response, 'offer', side_effect=OSError(fixture.SECRET)),
            patch.object(bridge.diagnostics, 'failure', side_effect=failure),
        ):
            with self.assertRaises(OSError):
                bridge.announce('旧播报')
        self.latest_text(bridge)

    def test_secondary_cancel_error_settles_uncertain_job_and_preserves_primary(self):
        bridge = self.bridge(active=True)
        original = bridge.response.offer
        primary = OSError('primary-' + fixture.SECRET)

        def offer(job):
            original(job)
            raise primary

        with (
            patch.object(bridge.response, 'offer', side_effect=offer),
            patch.object(bridge.brain, 'cancel', side_effect=[None, RuntimeError(fixture.SECRET)]),
        ):
            with self.assertRaises(OSError) as caught:
                bridge.announce('旧播报')
        self.assertIs(caught.exception, primary)
        job = bridge.response.queue.get_nowait()
        self.assertFalse(bridge.valid(job.generation))
        self.assertTrue(job.done.is_set())
        self.assertIsNone(bridge.announcements.generation)
        self.assertNotIn(fixture.SECRET, json.dumps(bridge.logs))

    def test_next_explicit_announcement_completes_once_after_startup_failure(self):
        bridge = self.bridge()

        def failed(_):
            raise OSError(fixture.SECRET)

        with self.assertRaises(OSError):
            bridge.announcements.announce('旧播报', prepare=failed)
        job = bridge.announce('新的固定播报')
        self.assertIs(bridge.response.queue.get_nowait(), job)
        with patch.object(bridge, 'speak', return_value=True) as speak:
            bridge.response.respond_job(job)
        speak.assert_called_once_with('新的固定播报', job.generation)
        self.assertEqual(job.result, 'completed')
        self.assertTrue(job.done.is_set())
        self.assertEqual(bridge.brain.calls, 0)
        self.assertFalse(bridge.session.active)

    def test_cancel_during_begin_prevents_stale_thinking_and_speech(self):
        bridge = self.bridge(active=True)
        job = bridge.announce('旧播报')
        bridge.response.queue.get_nowait()
        original = bridge.event

        def event(name, **fields):
            original(name, **fields)
            if name == 'announcement_begin':
                bridge.submit_text('最新指令')

        bridge.event = event
        with patch.object(bridge, 'speak') as speak:
            bridge.read_announcement(job)
        speak.assert_not_called()
        new = next(i for i, (name, _) in enumerate(bridge.events) if name == 'text_begin')
        self.assertFalse(any(name == 'status' for name, _ in bridge.events[new + 1 :]))
        self.assertTrue(job.done.is_set())
        self.latest_text(bridge)

    def test_completion_waiter_survives_each_auxiliary_failure(self):
        for phase in ('metric', 'diagnostics', 'event'):
            with self.subTest(phase=phase):
                bridge = self.bridge()
                job = bridge.announce('固定测试句')
                bridge.response.queue.get_nowait()
                if phase == 'metric':

                    def metric(*_):
                        raise OSError('aux')

                    context = patch.object(
                        bridge.announcements,
                        'hooks',
                        replace(bridge.announcements.hooks, metric=metric),
                    )
                elif phase == 'diagnostics':
                    context = patch.object(bridge.diagnostics, 'finish', side_effect=OSError('aux'))
                else:
                    original = bridge.event

                    def event(name, **fields):
                        if name == 'announcement_done':
                            raise ValueError('aux')
                        original(name, **fields)

                    context = patch.object(bridge, 'event', side_effect=event)
                try:
                    with context:
                        bridge.read_announcement(job)
                except (OSError, ValueError):
                    pass
                self.assertTrue(job.done.is_set())
                self.assertEqual(job.result, 'completed')
                self.assertIsNone(bridge.announcements.generation)

    def test_old_completion_cannot_overwrite_latest_announcement_metric(self):
        bridge = self.bridge()
        old = Announcement(bridge.generation - 1, '旧播报')
        bridge.record_metric('last_announcement', {'status': 'completed', 'characters': 99})
        bridge.read_announcement(old)
        self.assertEqual(
            bridge.metrics['last_announcement'], {'status': 'completed', 'characters': 99}
        )
        self.assertTrue(old.done.is_set())

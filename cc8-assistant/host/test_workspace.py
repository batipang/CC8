"""Real persistence, receipt ordering and reminder scheduling; no live side effects."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from workspace import Workspace, ZONE
from bridge import Bridge
from announce import Announcement
from voice_tools import VoiceTools


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.time = datetime(2026, 11, 1, 10, tzinfo=ZONE)
        self.board = Workspace(now=lambda: self.time)

    def create(self, board=None, **extra):
        return (board or self.board).reminder(
            {
                'action': 'create',
                'title': '测试事项',
                'due': (self.time + timedelta(hours=1)).isoformat(),
                **extra,
            }
        )['reminder']

    def test_private_atomic_store_survives_restart_without_task_transcripts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private/reminders.json'
            b = Workspace(path, now=lambda: self.time)
            b.task_start(101, '只保存在内存中的任务')
            item = self.create(b)
            reloaded = Workspace(path, now=lambda: self.time)
            self.assertEqual(reloaded.snapshot()['reminders'], [item])
            self.assertEqual(reloaded.snapshot()['tasks'], [])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn('任务', path.read_text())
            self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_bad_store_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'r.json'
            p.write_text('{broken')
            with self.assertRaises(ValueError):
                Workspace(p)
            self.assertEqual(p.read_text(), '{broken')

    def test_update_and_cancel_use_actual_id(self):
        item = self.create()
        updated = self.board.reminder({'action': 'update', 'id': item['id'], 'title': '新的事项'})
        self.assertEqual(updated['reminder']['title'], '新的事项')
        self.assertEqual(updated['reminder']['id'], item['id'])
        with self.assertRaises(ValueError):
            self.board.reminder({'action': 'cancel', 'id': 'not-real'})
        self.board.reminder({'action': 'cancel', 'id': item['id']})
        self.assertEqual(self.board.snapshot()['reminders'], [])

    def test_unknown_naive_past_oversized_and_bad_repeat_are_rejected(self):
        for args in [
            {'due': '2026-11-02T10:00:00'},
            {'due': self.time.isoformat()},
            {'title': 'x' * 121},
            {'repeat': 'hourly'},
            {'action': 'erase_all'},
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.create(**args)
        self.assertEqual(self.board.snapshot()['reminders'], [])

    def test_extreme_timezone_date_is_rejected_without_an_uncaught_overflow(self):
        with self.assertRaises(ValueError):
            self.create(due='9999-12-31T23:59:59-23:59')
        self.assertEqual(self.board.snapshot()['reminders'], [])

    def test_late_cancelled_turn_cannot_create_reminder(self):
        result = self.board.reminder(
            {'action': 'create', 'title': '不要创建', 'due': '2026-11-02T10:00:00+08:00'},
            lambda: False,
        )
        self.assertFalse(result['ok'])
        self.assertEqual(self.board.snapshot()['reminders'], [])

    def test_tool_requires_connected_reminder_service(self):
        with self.assertRaises(RuntimeError):
            VoiceTools().call('cc8_reminder', {'action': 'list'})
        result = VoiceTools(reminders=self.board.reminder).call('cc8_reminder', {'action': 'list'})
        self.assertTrue(result['ok'])

    def test_completed_once_and_failed_delivery_retry(self):
        item = self.create()
        self.time += timedelta(hours=2)
        self.assertEqual(self.board.due()['id'], item['id'])
        self.board.delivered(item['id'], False, revision=item['revision'])
        self.assertIsNone(self.board.due())
        self.time += timedelta(minutes=5)
        self.assertIsNotNone(self.board.due())
        self.board.delivered(item['id'], True, revision=self.board.due()['revision'])
        self.assertIsNone(self.board.due())
        self.assertEqual(self.board.snapshot()['reminders'], [])

    def test_repeats_skip_missed_intervals_without_notification_flood(self):
        item = self.create(repeat='daily')
        self.time += timedelta(days=40)
        self.board.delivered(item['id'], True, revision=item['revision'])
        self.assertIsNone(self.board.due())
        next_due = self.board._date(self.board.snapshot()['reminders'][0]['due'])
        self.assertGreater(next_due, self.time)
        self.assertEqual(next_due.hour, 11)

    def test_weekday_repeat_skips_weekend(self):
        self.time = datetime(2026, 11, 6, 10, tzinfo=ZONE)
        item = self.create(repeat='weekdays')
        self.time += timedelta(hours=2)
        self.board.delivered(item['id'], True, revision=item['revision'])
        self.assertEqual(self.board.snapshot()['reminders'][0]['due'], '2026-11-09T11:00:00+08:00')

    def test_first_weekday_reminder_never_fires_on_a_weekend(self):
        item = self.create(repeat='weekdays', due='2026-11-07T09:00:00+08:00')
        self.assertEqual(item['due'], '2026-11-09T09:00:00+08:00')

    def test_cancelled_reminder_is_not_resurrected_by_late_receipt(self):
        item = self.create()
        self.board.reminder({'action': 'cancel', 'id': item['id']})
        self.board.delivered(item['id'], False, revision=item['revision'])
        self.assertEqual(self.board.snapshot()['reminders'], [])

    def test_failed_disk_write_rolls_back_memory(self):
        item = self.create()
        with (
            patch.object(self.board, '_save', side_effect=OSError('disk full')),
            self.assertRaises(OSError),
        ):
            self.board.reminder({'action': 'update', 'id': item['id'], 'title': '未保存'})
        self.assertEqual(self.board.snapshot()['reminders'][0]['title'], '测试事项')

    def test_task_receipts_are_bounded_and_stale_updates_cannot_revive_cancel(self):
        ident = self.board.task_start(101, '查询并保存')
        self.board.task_stage(ident, 's', '联网检索', 'running')
        self.board.task_stage(ident, 's', '联网检索', 'completed')
        self.assertEqual(len(self.board.snapshot()['tasks'][0]['steps']), 1)
        self.board.cancel_generation(101)
        self.board.task_stage(ident, 'f', '修改文件', 'running')
        self.board.task_finish(ident, 'completed', '不应出现')
        task = self.board.snapshot()['tasks'][0]
        self.assertEqual(task['status'], 'cancelled')
        self.assertEqual(task['result'], '')
        self.assertEqual(len(task['steps']), 1)
        for n in range(30):
            self.board.task_start(n, '任务')
        self.assertEqual(len(self.board.snapshot()['tasks']), 12)

    def test_progress_label_tracks_current_stage_instead_of_staying_on_connection(self):
        ident = self.board.task_start(101, '测试阶段')
        self.board.task_stage(ident, 'analysis', '连接助理', 'running')
        self.board.task_stage(ident, 'analysis', '分析请求', 'running')
        task = self.board.snapshot()['tasks'][0]
        self.assertEqual(task['stage'], '分析请求')
        self.assertEqual(task['steps'][0]['title'], '分析请求')

    def test_snapshots_are_copies(self):
        self.create()
        snapshot = self.board.snapshot()
        snapshot['reminders'][0]['title'] = '外部改写'
        self.assertEqual(self.board.snapshot()['reminders'][0]['title'], '测试事项')

    def test_maximum_unicode_workspace_fits_the_real_wire_packet(self):
        from protocol import Packet

        for i in range(32):
            self.create(title='😀' * 120)
        for i in range(12):
            ident = self.board.task_start(i, '😀' * 80)
            for n in range(5):
                self.board.task_stage(ident, '😀' * 80 + str(n), '😀' * 40, 'completed')
            self.board.task_finish(ident, 'completed', '😀' * 1600)
        snapshot = self.board.snapshot()
        self.assertEqual(len(snapshot['reminders']), 32)
        self.assertEqual(snapshot['tasks'][0]['generation'], 11)
        self.assertLess(len(Packet.event('workspace', **snapshot).encode()), 65536)

    def test_scheduler_waits_for_quiet_connection_and_preserves_microphone_state(self):
        self.create()
        self.time += timedelta(hours=2)
        b = Bridge(None, None, workspace=self.board)
        jobs = []
        b.publish_workspace = lambda: None

        def announce(text):
            jobs.append(Announcement(b.generation, text))
            return jobs[-1]

        b.announce = announce
        for field in ['calibrating', 'playing', 'response_busy', 'recognition_busy']:
            b.connection = object()
            owner = (
                b.playback
                if field == 'playing'
                else b.calibration
                if field == 'calibrating'
                else b.response
                if field == 'response_busy'
                else b.recognition
                if field == 'recognition_busy'
                else b
            )
            setattr(
                owner,
                'busy'
                if field in ('recognition_busy', 'response_busy')
                else 'active'
                if field == 'calibrating'
                else field,
                True,
            )
            b.reminders.poll = 0
            b.reminder_tick()
            self.assertEqual(jobs, [])
            setattr(
                owner,
                'busy'
                if field in ('recognition_busy', 'response_busy')
                else 'active'
                if field == 'calibrating'
                else field,
                False,
            )
        b.connection = None
        b.reminders.poll = 0
        b.reminder_tick()
        self.assertEqual(jobs, [])
        b.connection = object()
        b.reminders.poll = 0
        b.reminder_tick()
        self.assertEqual(len(jobs), 1)
        self.assertFalse(b.active)
        b.reminder_tick()
        self.assertEqual(len(jobs), 1)
        jobs[0].result = 'completed'
        jobs[0].done.set()
        b.reminder_tick()
        self.assertEqual(self.board.snapshot()['reminders'], [])

    def test_late_playback_receipt_cannot_overwrite_an_edited_reminder(self):
        for success in (True, False):
            with self.subTest(success=success):
                board = Workspace(now=lambda: self.time)
                item = self.create(board)
                self.time += timedelta(hours=2)
                b = Bridge(None, None, workspace=board)
                b.connection = object()
                b.publish_workspace = lambda: None
                b.announce = lambda text: Announcement(b.generation, text)
                b.reminder_tick()
                job = b.reminders.job
                updated = board.reminder(
                    {
                        'action': 'update',
                        'id': item['id'],
                        'title': '改后的事项',
                        'due': (self.time + timedelta(hours=1)).isoformat(),
                    }
                )['reminder']
                job.result = 'completed' if success else 'interrupted'
                job.done.set()
                b.reminder_tick()
                self.assertEqual(board.snapshot()['reminders'], [updated])

    def test_duplicate_receipt_does_not_advance_a_recurring_reminder_twice(self):
        item = self.create(repeat='daily')
        self.time += timedelta(hours=2)
        self.assertTrue(self.board.delivered(item['id'], True, revision=item['revision']))
        next_occurrence = self.board.snapshot()['reminders'][0]
        self.time += timedelta(days=1)
        self.assertFalse(self.board.delivered(item['id'], True, revision=item['revision']))
        self.assertFalse(self.board.delivered(item['id'], False, revision=item['revision']))
        self.assertEqual(self.board.snapshot()['reminders'], [next_occurrence])

    def test_edit_then_restore_still_invalidates_old_receipt(self):
        item = self.create()
        for title in ('新事项', item['title']):
            self.board.reminder({'action': 'update', 'id': item['id'], 'title': title})
        self.assertFalse(self.board.delivered(item['id'], True, revision=item['revision']))
        self.assertEqual(self.board.snapshot()['reminders'][0]['status'], 'pending')

    def test_legacy_store_loads_without_revision_and_invalid_metadata_is_rejected(self):
        item = self.create()
        item.pop('revision')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'r.json'
            path.write_text(json.dumps({'version': 1, 'reminders': [item]}))
            b = Workspace(path, now=lambda: self.time)
            self.assertEqual(b.snapshot()['reminders'][0]['revision'], 0)
            self.time += timedelta(hours=2)
            self.assertTrue(b.delivered(item['id'], True, revision=0))
            self.assertEqual(Workspace(path, now=lambda: self.time).snapshot()['reminders'], [])
            for data in (
                [],
                {'version': 1, 'reminders': [item, item]},
                {'version': 1, 'reminders': [{**item, 'revision': True}]},
                {'version': 1, 'reminders': [{**item, 'retry_after': 'invalid'}]},
            ):
                with self.subTest(data=data):
                    path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        Workspace(path, now=lambda: self.time)
                    self.assertEqual(json.loads(path.read_text()), data)

    def test_text_request_works_without_microphone_and_interrupt_invalidates_queued_work(self):
        b = Bridge(None, None)
        b.connection = object()
        events = []
        b.event = lambda event, **fields: events.append((event, fields))
        b.enqueue_response = lambda job: setattr(b, 'queued_text', job)
        result = b.submit_text('请帮我查询')
        self.assertFalse(b.active)
        self.assertTrue(b.valid(result['generation']))
        self.assertEqual(b.queued_text.text, '请帮我查询')
        self.assertTrue(any(e == 'text_begin' for e, _ in events))
        b.on_event({'event': 'interrupt'})
        self.assertFalse(b.valid(result['generation']))
        self.assertIsNone(b.text_generation)
        self.assertFalse(b.active)

    def test_invalid_text_cannot_cancel_current_work_or_start_capture(self):
        b = Bridge(None, None)
        old = b.generation
        for value in ['', None, 'x' * 6001, 'bad\x00text']:
            with self.assertRaises(ValueError):
                b.submit_text(value)
        self.assertEqual(b.generation, old)
        self.assertFalse(b.active)

    def test_stopping_narration_does_not_cancel_already_completed_dot_work(self):
        class Brain:
            last_metrics = {'completed': True}

            def cancel(self):
                pass

            def clauses(self, text, history, current, on_web):
                on_web('dot_result', {'text': '实际工作已完成', 'truncated': False})
                yield '实际工作已完成'

        b = Bridge(None, Brain())
        b.connection = object()
        b.active = True
        b.event = lambda *a, **k: None

        def stop_speaking(*args):
            b.cancel('button')
            return False

        b.speak = stop_speaking
        b.answer('测试', b.generation)
        self.assertEqual(b.workspace.snapshot()['tasks'][0]['status'], 'completed')


if __name__ == '__main__':
    unittest.main()

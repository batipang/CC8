"""Real reminder persistence/admission/ACK paths with controlled time and no devices."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import stat
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from announce import Announcement
from bridge import Bridge
from reminder_policy import defaults, quiet, validate
from reminder_scheduler import ReminderHooks, ReminderScheduler
from reminder_store import ReminderCommitUncertain, ReminderStore
from voice_tools import VoiceTools
from workspace import Workspace, ZONE


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'private/reminders.json'
        self.now = datetime(2026, 11, 9, 12, tzinfo=ZONE)
        self.board = Workspace(self.path, now=lambda: self.now)
        self.jobs = []
        self.owner = 0
        self.can_deliver = True

        def announce(text):
            job = Announcement(10 + len(self.jobs), text)
            self.jobs.append(job)
            return job

        self.scheduler = ReminderScheduler(
            ReminderHooks(
                lock=self.board.lock,
                workspace=lambda: self.board,
                publish_workspace=lambda: None,
                can_deliver=lambda: self.can_deliver,
                touch=lambda: None,
                announce=announce,
                delivery_owner=lambda: self.owner,
            )
        )

    def create(self, **fields):
        return self.board.reminder(
            {
                'action': 'create',
                'title': '公开固定提醒',
                'due': (self.now + timedelta(minutes=1)).isoformat(),
                **fields,
            }
        )['reminder']

    def tick(self):
        self.scheduler.poll = 0
        self.scheduler.tick()

    def policy(self, **changes):
        p = self.board.reminder({'action': 'policy'})['policy']
        return self.board.reminder({'action': 'set_policy', 'policy': {**p, **changes}})['policy']

    def test_claim_is_on_disk_before_announcement_and_restart_cannot_replay_it(self):
        self.create()
        self.now += timedelta(minutes=2)
        seen = []
        announce = self.scheduler.hooks.announce

        def checked(text):
            saved = json.loads(self.path.read_text())
            self.assertEqual(saved['reminders'][0]['status'], 'delivering')
            seen.append(saved['reminders'][0]['delivery_attempt']['id'])
            return announce(text)

        self.scheduler.hooks = replace(self.scheduler.hooks, announce=checked)
        self.tick()
        self.assertEqual(len(seen), 1)
        restarted = Workspace(self.path, now=lambda: self.now)
        self.assertEqual(restarted.snapshot()['reminders'][0]['status'], 'unconfirmed')
        self.assertEqual(restarted.claim_reminders(), [])
        self.assertFalse(restarted.finish_reminders(self.scheduler.claims, True))
        self.assertEqual(len(self.jobs), 1)

    def test_real_isolated_process_exit_after_claim_leaves_no_restart_replay(self):
        self.create()
        script = """import os,sys
from datetime import datetime
from workspace import Workspace,ZONE
w=Workspace(sys.argv[1],now=lambda:datetime(2026,11,9,12,2,tzinfo=ZONE))
assert len(w.claim_reminders())==1
os._exit(23)
"""
        host = Path(__file__).resolve().parent
        code = 'import sys\nsys.path[:0]=' + repr([str(host), str(host.parents[1])]) + '\n' + script
        result = subprocess.run(
            [sys.executable, '-c', code, str(self.path)], capture_output=True, timeout=10
        )
        self.assertEqual(result.returncode, 23, result.stderr.decode())
        restarted = Workspace(self.path, now=lambda: self.now + timedelta(minutes=2))
        self.assertEqual(restarted.snapshot()['reminders'][0]['status'], 'unconfirmed')
        self.assertEqual(restarted.claim_reminders(), [])

    def test_only_completed_ack_marks_played_and_duplicate_ack_never_replays(self):
        self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        self.assertEqual(self.board.snapshot()['reminders'][0]['status'], 'delivering')
        claims = deepcopy(self.scheduler.claims)
        self.jobs[0].result = 'completed'
        self.jobs[0].done.set()
        self.tick()
        self.assertEqual(self.board.snapshot()['reminders'], [])
        receipt = self.board.reminder({'action': 'history'})['reminders'][0]
        self.assertEqual(receipt['delivery_attempt']['result'], 'played')
        self.assertFalse(self.board.finish_reminders(claims, True))
        self.now += timedelta(days=1)
        self.tick()
        self.assertEqual(len(self.jobs), 1)

    def test_interrupted_delivery_never_automatically_retries_even_after_restart(self):
        self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        self.jobs[0].done.set()
        self.tick()
        self.now += timedelta(days=20)
        self.tick()
        restarted = Workspace(self.path, now=lambda: self.now)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(restarted.snapshot()['reminders'][0]['status'], 'unconfirmed')
        self.assertEqual(restarted.claim_reminders(), [])

    def test_explicit_reschedule_is_new_occurrence_and_old_receipt_is_rejected(self):
        item = self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        old = deepcopy(self.scheduler.claims)
        self.board.reminder(
            {
                'action': 'update',
                'id': item['id'],
                'due': (self.now + timedelta(minutes=10)).isoformat(),
            }
        )
        self.jobs[0].result = 'completed'
        self.jobs[0].done.set()
        self.tick()
        self.assertEqual(self.board.snapshot()['reminders'][0]['status'], 'pending')
        self.assertFalse(self.board.finish_reminders(old, True))
        self.now += timedelta(minutes=11)
        self.tick()
        self.assertEqual(len(self.jobs), 2)

    def test_history_uses_last_state_change_instead_of_original_creation_order(self):
        first = self.create()
        self.now += timedelta(minutes=1)
        second = self.create()
        self.board.reminder({'action': 'cancel', 'id': second['id']})
        self.now += timedelta(minutes=1)
        self.board.reminder({'action': 'cancel', 'id': first['id']})
        records = self.board.reminder({'action': 'history'})['reminders']
        self.assertEqual([r['id'] for r in records], [first['id'], second['id']])

    def test_cancel_inflight_preserves_history_and_late_ack_cannot_revive_it(self):
        item = self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        self.board.reminder({'action': 'cancel', 'id': item['id']})
        self.jobs[0].result = 'completed'
        self.jobs[0].done.set()
        self.tick()
        self.assertEqual(self.board.snapshot()['reminders'], [])
        record = self.board.reminder({'action': 'history'})['reminders'][0]
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(record['delivery_attempt']['result'], 'unconfirmed')

    def test_coalesces_only_three_due_occurrences_into_one_ack_bound_batch(self):
        for n in range(4):
            self.create(title='事项' + str(n))
        self.now += timedelta(minutes=2)
        self.tick()
        self.assertEqual(len(self.scheduler.claims), 3)
        self.assertEqual(len(self.jobs), 1)
        self.assertTrue(all(('事项' + str(n)) in self.jobs[0].text for n in range(3)))
        self.jobs[0].result = 'completed'
        self.jobs[0].done.set()
        self.tick()
        self.assertEqual(len(self.jobs), 2)
        self.assertEqual(self.jobs[1].text, '提醒你：事项3')
        played = [r for r in self.board.reminders if r['status'] == 'delivered']
        self.assertEqual(len(played), 3)

    def test_repeat_advances_once_and_preserves_original_schedule_after_missed_days(self):
        self.create(repeat='weekdays')
        self.now += timedelta(days=5)
        self.tick()
        old = deepcopy(self.scheduler.claims)
        self.jobs[0].result = 'completed'
        self.jobs[0].done.set()
        self.tick()
        self.assertEqual(self.board.snapshot()['reminders'][0]['due'], '2026-11-16T12:01:00+08:00')
        self.assertFalse(self.board.finish_reminders(old, True))

    def test_announce_send_exception_freezes_claim_and_never_reissues_it(self):
        self.create()
        self.now += timedelta(minutes=2)
        calls = []

        def fail(text):
            calls.append(text)
            raise OSError('fixed public send fault')

        self.scheduler.hooks = replace(self.scheduler.hooks, announce=fail)
        with self.assertRaises(OSError):
            self.tick()
        self.now += timedelta(minutes=30)
        self.tick()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.board.snapshot()['reminders'][0]['status'], 'unconfirmed')

    def test_failed_claim_commit_never_announces_and_restores_memory(self):
        self.create()
        self.now += timedelta(minutes=2)
        before = self.board.snapshot()
        with patch.object(self.board, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.tick()
        self.assertEqual(self.board.snapshot(), before)
        self.assertEqual(self.jobs, [])

    def test_failed_ack_commit_retries_commit_only_not_audio(self):
        self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        self.jobs[0].result = 'completed'
        self.jobs[0].done.set()
        with patch.object(self.board, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.tick()
        self.assertIsNotNone(self.scheduler.job)
        self.tick()
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.board.snapshot()['reminders'], [])

    def test_receipt_publish_reentrancy_cannot_cancel_newer_work(self):
        self.create()
        self.now += timedelta(minutes=2)
        trace = []
        original = self.scheduler.hooks.announce

        def announce(text):
            trace.append('admitted')
            return original(text)

        def publish():
            trace.append('new user request')
            self.owner += 1

        self.scheduler.hooks = replace(
            self.scheduler.hooks, announce=announce, publish_workspace=publish
        )
        self.tick()
        self.assertEqual(trace, ['admitted', 'new user request'])

    def test_owner_change_before_admission_does_not_announce(self):
        self.create()
        self.now += timedelta(minutes=2)

        def touch():
            self.owner += 1

        self.scheduler.hooks = replace(self.scheduler.hooks, touch=touch)
        self.tick()
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.board.snapshot()['reminders'][0]['status'], 'unconfirmed')

    def test_crossing_quiet_boundary_during_claim_cannot_start_a_new_broadcast(self):
        self.policy(quiet_enabled=True)
        self.now = self.now.replace(hour=21, minute=58)
        self.create()
        self.now += timedelta(minutes=1)

        def touch():
            self.now += timedelta(minutes=1)

        self.scheduler.hooks = replace(self.scheduler.hooks, touch=touch)
        self.tick()
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.board.snapshot()['reminders'][0]['status'], 'unconfirmed')

    def test_quiet_cross_midnight_survives_restart_and_releases_at_exact_end(self):
        self.policy(quiet_enabled=True)
        self.now = self.now.replace(hour=21, minute=59)
        self.create()
        self.now += timedelta(minutes=1)
        self.tick()
        self.assertEqual(self.jobs, [])
        self.now += timedelta(hours=8, minutes=59)
        self.tick()
        self.assertEqual(self.jobs, [])
        self.assertTrue(Workspace(self.path, now=lambda: self.now).reminder_policy['quiet_enabled'])
        self.now += timedelta(minutes=1)
        self.tick()
        self.assertEqual(len(self.jobs), 1)

    def test_skip_late_works_offline_without_claim_or_false_played_receipt(self):
        self.policy(late_policy='skip', max_late_minutes=10)
        self.create()
        self.can_deliver = False
        self.now += timedelta(minutes=12)
        self.tick()
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.board.snapshot()['reminders'], [])
        history = self.board.reminder({'action': 'history'})['reminders'][0]
        self.assertEqual(history['status'], 'missed')
        self.assertEqual(history['delivery_attempt']['result'], 'missed')
        self.can_deliver = True
        self.tick()
        self.assertEqual(self.jobs, [])

    def test_late_exact_threshold_is_still_deliverable_and_repeat_skip_keeps_phase(self):
        self.policy(late_policy='skip', max_late_minutes=10)
        self.create(repeat='daily')
        self.now += timedelta(minutes=11)
        self.assertFalse(self.board.expire_reminders())
        self.now += timedelta(seconds=1)
        self.assertTrue(self.board.expire_reminders())
        record = self.board.snapshot()['reminders'][0]
        self.assertEqual(record['due'], '2026-11-10T12:01:00+08:00')
        self.assertEqual(record['delivery_attempt']['result'], 'missed')

    def test_stale_policy_duplicate_submit_cancelled_turn_and_failed_write(self):
        original = self.board.reminder({'action': 'policy'})['policy']
        args = {'action': 'set_policy', 'policy': {**original, 'quiet_enabled': True}}
        with patch.object(self.board, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.board.reminder(args)
        self.assertEqual(self.board.reminder_policy, original)
        self.assertFalse(
            VoiceTools(reminders=self.board.reminder).call('cc8_reminder', args, lambda: False)[
                'ok'
            ]
        )
        self.board.reminder(args)
        with self.assertRaises(ValueError):
            self.board.reminder(args)
        self.assertTrue(self.board.reminder_policy['quiet_enabled'])

    def test_actual_bridge_policy_and_busy_queue_do_not_open_capture(self):
        with patch('threading.Thread.start'):
            b = Bridge(None, None, workspace=self.board)
        b.session.connection = object()
        b.event = lambda *args, **kwargs: None
        b.publish_workspace = lambda: None
        b.on_event(
            {
                'event': 'reminder_action',
                'arguments': {
                    'action': 'set_policy',
                    'policy': {**defaults(), 'quiet_enabled': True},
                },
            }
        )
        self.assertFalse(b.session.active)
        self.assertEqual(self.jobs, [])
        self.assertTrue(self.board.reminder_policy['quiet_enabled'])
        self.policy(quiet_enabled=False)
        self.create()
        self.now += timedelta(minutes=2)
        b.announce = lambda text: self.fail('Pending ASR must not be interrupted')
        b.recognition.queue.put(object())
        b.reminder_tick()
        self.assertEqual(self.board.snapshot()['reminders'][0]['status'], 'pending')

    def test_atomic_rename_uncertain_commit_retains_frozen_state_in_memory_and_disk(self):
        self.create()
        self.now += timedelta(minutes=2)
        import os

        fsync = os.fsync

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('fixed directory sync fault')
            return fsync(fd)

        with patch('reminder_store.os.fsync', side_effect=fail_directory):
            with self.assertRaises(ReminderCommitUncertain):
                self.tick()
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.board.snapshot()['reminders'][0]['status'], 'unconfirmed')
        self.tick()
        self.assertEqual(self.jobs, [])
        self.assertEqual(
            Workspace(self.path, now=lambda: self.now).snapshot()['reminders'][0]['status'],
            'unconfirmed',
        )

    def test_expired_date_overflow_does_not_mutate_any_reminder(self):
        self.create(repeat='daily')
        self.board.reminders[0]['due'] = '9999-12-31T12:00:00+08:00'
        self.now = datetime(9999, 12, 31, 13, tzinfo=ZONE)
        self.policy(late_policy='skip', max_late_minutes=1)
        before = deepcopy(self.board.reminders)
        with self.assertRaises(ValueError):
            self.board.expire_reminders()
        self.assertEqual(self.board.reminders, before)


class PolicyAndStorageTests(unittest.TestCase):
    def test_quiet_intervals_and_strict_policy_types(self):
        for start, end, hour, expected in [
            ('22:00', '07:00', 23, True),
            ('22:00', '07:00', 7, False),
            ('09:00', '12:00', 10, True),
            ('09:00', '12:00', 12, False),
        ]:
            p = validate(
                {**defaults(), 'quiet_enabled': True, 'quiet_start': start, 'quiet_end': end}
            )
            self.assertEqual(quiet(p, datetime(2026, 11, 9, hour, tzinfo=ZONE)), expected)
        for changes in [
            {'quiet_enabled': 1},
            {'revision': True},
            {'quiet_start': '7:00'},
            {'quiet_start': '24:00'},
            {'quiet_start': '07:00'},
            {'max_late_minutes': False},
            {'max_late_minutes': 0},
            {'late_policy': 'always'},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate({**defaults(), **changes})

    def test_links_unsafe_files_corruption_duplicate_keys_and_external_changes_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'r.json'
            other = Path(folder) / 'other.json'
            other.write_text('{"version":1,"reminders":[]}')
            other.chmod(0o600)
            p.symlink_to(other)
            with self.assertRaises(OSError):
                Workspace(p)
            p.unlink()
            for raw in ['null', '{"version":1,"version":2,"reminders":[]}', '{broken']:
                p.write_text(raw)
                p.chmod(0o600)
                with self.assertRaises(ValueError):
                    Workspace(p)
                self.assertEqual(p.read_text(), raw)
            p.unlink()
            store = ReminderStore(p)
            store.save({'fixed': 1})
            p.write_text('{"owner changed":true}')
            with self.assertRaises(OSError):
                store.save({'fixed': 2})
            self.assertEqual(json.loads(p.read_text()), {'owner changed': True})

    def test_old_version_one_store_reads_without_writing_then_explicit_policy_upgrades(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'r.json'
            p.write_text('{"version":1,"reminders":[]}')
            p.chmod(0o600)
            original = p.read_bytes()
            b = Workspace(p)
            self.assertEqual(b.reminder_policy, defaults())
            self.assertEqual(p.read_bytes(), original)
            b.reminder({'action': 'set_policy', 'policy': {**defaults(), 'quiet_enabled': True}})
            self.assertEqual(json.loads(p.read_text())['version'], 2)
            self.assertTrue(Workspace(p).reminder_policy['quiet_enabled'])

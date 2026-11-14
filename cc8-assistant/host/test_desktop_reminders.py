"""Channel receipts, real atomic stores and local IPC; no system notification/capture."""

from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from announce import Announcement
from desktop_notifications import DesktopDelivery, DesktopNotifier
from reminder_scheduler import ReminderHooks, ReminderScheduler
from workspace import Workspace, ZONE


class DesktopReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'private/reminders.json'
        self.now = datetime(2026, 11, 9, 12, tzinfo=ZONE)
        self.board = Workspace(self.path, now=lambda: self.now)
        self.jobs, self.audio = [], []
        self.current = True

        def desktop(ident, body, valid):
            # The real scheduler, file commit and finish path run; OS is inert.
            self.assertEqual(
                json.loads(self.path.read_text())['reminders'][0]['status'], 'delivering'
            )
            job = Announcement(1, body)
            job.detail = 'unconfirmed'
            self.jobs.append((ident, body, valid, job))
            return job

        def announce(body):
            job = Announcement(1, body)
            self.audio.append(job)
            return job

        self.scheduler = ReminderScheduler(
            ReminderHooks(
                lock=self.board.lock,
                workspace=lambda: self.board,
                publish_workspace=lambda: None,
                can_deliver=lambda: self.current,
                touch=lambda: None,
                announce=announce,
                desktop_notify=desktop,
            )
        )

    def create(self, channel='desktop', repeat='none'):
        return self.board.reminder(
            {
                'action': 'create',
                'title': '公开固定提醒',
                'due': (self.now + timedelta(minutes=1)).isoformat(),
                'channel': channel,
                'repeat': repeat,
            }
        )['reminder']

    def tick(self):
        self.scheduler.poll = 0
        self.scheduler.tick()

    def test_explicit_channel_survives_restart_and_cannot_accept_a_playback_receipt(self):
        item = self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        self.assertEqual(self.audio, [])
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(self.board.finish_reminders(self.scheduler.claims, True))
        self.assertEqual(self.board.reminders[0]['status'], 'delivering')
        restarted = Workspace(self.path, now=lambda: self.now)
        self.assertEqual(restarted.reminders[0]['channel'], 'desktop')
        self.assertEqual(restarted.reminders[0]['status'], 'unconfirmed')
        self.assertEqual(restarted.claim_reminders(), [])
        self.assertFalse(
            restarted.delivered(item['id'], True, revision=restarted.reminders[0]['revision'])
        )

    def test_only_exact_notification_center_receipt_finishes_once_and_is_not_played(self):
        self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        job = self.jobs[0][3]
        job.result = 'notification_center'
        job.done.set()
        self.tick()
        item = self.board.reminders[0]
        self.assertEqual(item['status'], 'delivered')
        self.assertEqual(item['delivery_attempt']['result'], 'notified')
        self.assertEqual(item['delivery_attempt']['channel'], 'desktop')
        self.assertEqual(self.board.snapshot()['reminder_history'], [item])
        self.tick()
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.audio, [])

    def test_submit_or_permission_failure_is_frozen_not_retried_or_changed_to_speech(self):
        for detail in (
            'permission_denied',
            'not_installed',
            'unsupported',
            'submitted_unconfirmed',
            'rejected',
        ):
            with self.subTest(detail=detail):
                self.board.reminders = []
                self.jobs.clear()
                self.create()
                self.now += timedelta(minutes=2)
                self.tick()
                job = self.jobs[0][3]
                job.result = 'unconfirmed'
                job.detail = detail
                job.done.set()
                self.tick()
                self.assertEqual(self.board.reminders[0]['status'], 'unconfirmed')
                self.assertEqual(self.board.reminders[0]['delivery_attempt']['detail'], detail)
                self.now += timedelta(days=1)
                self.tick()
                self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.audio, [])

    def test_coalescing_never_mixes_desktop_and_speaker_channels(self):
        self.create()
        self.create('cc8')
        self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        self.assertEqual(len(self.scheduler.claims), 2)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.audio, [])
        notice_id = self.jobs[0][0]
        self.assertEqual({row['attempt_id'] for row in self.scheduler.claims}, {notice_id})
        job = self.jobs[0][3]
        job.result = 'notification_center'
        job.done.set()
        self.tick()
        self.assertEqual(len(self.audio), 1)
        self.assertEqual(self.scheduler.channel, 'cc8')
        self.audio[0].result = 'completed'
        self.audio[0].done.set()
        self.tick()
        self.assertEqual(
            [r['delivery_attempt']['result'] for r in self.board.reminders],
            ['notified', 'played', 'notified'],
        )
        reopened = Workspace(self.path, now=lambda: self.now)
        self.assertEqual(
            [r['delivery_attempt']['id'] == notice_id for r in reopened.reminders],
            [True, False, True],
        )

    def test_shared_notification_receipt_cannot_revive_a_cancelled_member(self):
        first, second = self.create(), self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        attempt_id, _, _, job = self.jobs[0]
        self.board.reminder({'action': 'cancel', 'id': first['id']})
        job.result = 'notification_center'
        job.done.set()
        self.tick()
        by_id = {r['id']: r for r in Workspace(self.path, now=lambda: self.now).reminders}
        self.assertEqual(by_id[first['id']]['status'], 'cancelled')
        self.assertEqual(by_id[first['id']]['delivery_attempt']['result'], 'unconfirmed')
        self.assertEqual(by_id[second['id']]['delivery_attempt']['result'], 'notified')
        self.assertEqual({r['delivery_attempt']['id'] for r in by_id.values()}, {attempt_id})
        self.assertEqual(len(self.jobs), 1)

    def test_new_user_or_policy_cancel_before_wire_invalidates_original_claim_callback(self):
        item = self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        valid = self.jobs[0][2]
        self.assertTrue(valid())
        self.current = False
        self.assertFalse(valid())
        self.current = True
        self.board.reminder({'action': 'cancel', 'id': item['id']})
        self.assertFalse(valid())
        self.jobs[0][3].result = 'notification_center'
        self.jobs[0][3].done.set()
        self.tick()
        self.assertEqual(self.board.reminders[0]['status'], 'cancelled')

    def test_repeat_retains_original_clock_but_uses_next_occurrence(self):
        self.create(repeat='daily')
        self.now += timedelta(days=4, minutes=2)
        self.tick()
        self.jobs[0][3].result = 'notification_center'
        self.jobs[0][3].done.set()
        self.tick()
        item = self.board.reminders[0]
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(item['due'], '2026-11-14T12:01:00+08:00')
        self.assertEqual(item['delivery_attempt']['result'], 'notified')

    def test_quiet_and_busy_postpone_both_channels_without_claim_or_helper(self):
        self.create()
        self.now += timedelta(minutes=2)
        self.current = False
        self.tick()
        self.assertEqual(self.jobs, [])
        self.current = True
        p = self.board.reminder_policy.copy()
        p.update(quiet_enabled=True, quiet_start='12:00', quiet_end='13:00')
        self.board.reminder({'action': 'set_policy', 'policy': p})
        self.tick()
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.board.reminders[0]['status'], 'pending')

    def test_legacy_schema_does_not_hide_new_channel_fields(self):
        self.create()
        data = json.loads(self.path.read_text())
        self.assertEqual(data['version'], 3)
        data['version'] = 2
        self.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'schema3'):
            Workspace(self.path)
        data['version'] = 3
        data['reminders'][0]['channel'] = 'both'
        self.path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            Workspace(self.path)

    def test_channel_update_keeps_id_and_old_channel_ack_cannot_finish_new_request(self):
        item = self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        old = self.scheduler.claims.copy()
        self.board.reminder(
            {
                'action': 'update',
                'id': item['id'],
                'channel': 'cc8',
                'due': (self.now + timedelta(minutes=1)).isoformat(),
            }
        )
        self.assertFalse(self.board.finish_reminders(old, True, channel='desktop'))
        self.assertEqual(self.board.reminders[0]['channel'], 'cc8')
        self.assertEqual(self.board.reminders[0]['status'], 'pending')

    def test_completed_audio_result_is_not_a_desktop_delivery_ack(self):
        self.create()
        self.now += timedelta(minutes=2)
        self.tick()
        job = self.jobs[0][3]
        job.result = 'completed'
        job.done.set()
        self.tick()
        self.assertEqual(self.board.reminders[0]['status'], 'unconfirmed')
        self.assertEqual(self.board.reminders[0]['delivery_attempt']['result'], 'unconfirmed')

    def test_missing_notifier_never_falls_back_to_speech(self):
        self.create()
        self.now += timedelta(minutes=2)
        self.scheduler.hooks = replace(self.scheduler.hooks, desktop_notify=None)
        with self.assertRaises(RuntimeError):
            self.tick()
        self.assertEqual(self.board.reminders[0]['status'], 'unconfirmed')
        self.assertEqual(self.audio, [])


class DesktopIPC(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'helper'
        self.directory.mkdir(mode=0o700)
        self.received = []
        self.threads = []
        self.socket_paths = []

    def request(self, request, reply, *, uid=None, current=lambda: True):
        class Process:
            def wait(self, timeout):
                return 0

        def launch(argv, **kwargs):
            self.assertEqual(argv[:4], ['/usr/bin/open', '-W', '-n', '-g'])
            self.assertNotIn(request.get('body', 'UNUSED'), argv)
            path = argv[-1]
            self.socket_paths.append(Path(path))

            def client():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                    stream.connect(path)
                    stream.settimeout(2)
                    data = b''
                    while not data.endswith(b'\n'):
                        block = stream.recv(1024)
                        if not block:
                            return
                        data += block
                    self.received.append(json.loads(data))
                    stream.sendall(reply)

            thread = threading.Thread(target=client)
            thread.start()
            self.threads.append(thread)
            return Process()

        client = DesktopNotifier(self.directory, self.root, run=launch)
        with (
            patch('desktop_notifications.platform.system', return_value='Darwin'),
            patch(
                'desktop_notifications.verify_bundle',
                return_value=self.directory / 'CC8Notifier.app',
            ),
            patch(
                'desktop_notifications.peer_uid', return_value=os.getuid() if uid is None else uid
            ),
        ):
            result = client.request(request, is_current=current)
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertTrue(
            all(not path.exists() and not path.parent.exists() for path in self.socket_paths)
        )
        return result

    def test_unicode_data_crosses_real_private_socket_not_shell_or_file(self):
        request = {
            'action': 'post',
            'id': 'a' * 32,
            'body': '公开提醒：引号"、反引号`、$()均是文字',
        }
        result = self.request(
            request,
            json.dumps({'protocol': 1, 'result': 'notification_center', 'id': 'a' * 32}).encode()
            + b'\n',
        )
        self.assertEqual(self.received, [request])
        self.assertEqual(result['result'], 'notification_center')

    def test_status_cannot_request_authorization_or_accept_a_post_result(self):
        result = self.request(
            {'action': 'status'},
            b'{"protocol":1,"result":"status","authorization":"not_determined"}\n',
        )
        self.assertEqual(self.received, [{'action': 'status'}])
        self.assertEqual(result['authorization'], 'not_determined')

    def test_wrong_id_or_malformed_receipt_never_confirms(self):
        for reply in [
            b'{"protocol":true,"result":"notification_center","id":"' + b'a' * 32 + b'"}\n',
            b'{"protocol":1,"result":"notification_center","id":"' + b'b' * 32 + b'"}\n',
            b'{"protocol":1,"result":"notification_center","id":"'
            + b'a' * 32
            + b'","body":"must not be accepted"}\n',
            b'not-json\n',
        ]:
            self.assertEqual(
                self.request({'action': 'post', 'id': 'a' * 32, 'body': '公开固定提醒'}, reply)[
                    'result'
                ],
                'unconfirmed',
            )

    def test_other_uid_receives_no_content(self):
        self.assertEqual(
            self.request({'action': 'status'}, b'{}\n', uid=os.getuid() + 1)['result'],
            'unconfirmed',
        )
        self.assertEqual(self.received, [])

    def test_cancel_before_launch_never_sends_data(self):
        self.assertEqual(
            self.request({'action': 'status'}, b'{}\n', current=lambda: False)['result'],
            'cancelled',
        )
        self.assertEqual(self.received, [])

    def test_status_cannot_accept_post_receipt_or_missing_authorization(self):
        for reply in (
            b'{"protocol":1,"result":"notification_center"}\n',
            b'{"protocol":1,"result":"status"}\n',
            b'{"protocol":1,"result":"status","result":"status","authorization":"denied"}\n',
        ):
            self.assertEqual(self.request({'action': 'status'}, reply)['result'], 'unconfirmed')

    def test_wrong_json_types_are_rejected_without_a_type_error_or_receipt(self):
        for reply in (
            b'{"protocol":1,"result":[]}\n',
            b'{"protocol":1,"result":"status","authorization":{}}\n',
        ):
            self.assertEqual(self.request({'action': 'status'}, reply)['result'], 'unconfirmed')

    def test_unsupported_host_never_launches(self):
        with (
            patch('desktop_notifications.platform.system', return_value='Linux'),
            patch('desktop_notifications.subprocess.Popen') as run,
        ):
            self.assertEqual(
                DesktopNotifier().request({'action': 'status'})['result'], 'unsupported'
            )
            run.assert_not_called()

    def test_invalid_content_is_rejected_before_platform_or_launch(self):
        for request in (
            {'action': 'authorize', 'body': 'bad'},
            {'action': 'post', 'id': 'bad', 'body': 'public'},
            {'action': 'post', 'id': 'a' * 32, 'body': 'x' * 4097},
            {'action': 'post', 'id': 'a' * 32, 'body': 'bad\x00'},
        ):
            with self.assertRaises(ValueError):
                DesktopNotifier().request(request)

    def test_async_delivery_never_waits_on_owner_thread_and_sanitizes_failure(self):
        gate = threading.Event()
        entered = threading.Event()

        class Notifier:
            def post(self, *args):
                entered.set()
                gate.wait(2)
                return {'result': 'permission_denied'}

        began = time.monotonic()
        job = DesktopDelivery(Notifier(), 'a' * 32, '公开固定提醒', lambda: True)
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertTrue(entered.wait(1))
        self.assertFalse(job.done.is_set())
        gate.set()
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.result, 'unconfirmed')
        self.assertEqual(job.detail, 'permission_denied')


if __name__ == '__main__':
    unittest.main()

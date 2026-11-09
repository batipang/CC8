"""Real scheduled reads and routed pages with synthetic history; no microphone or model."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_report_watch_controller as fixture
from test_report_voice import report
from report_voice import report_text
from protocol import validate_device_event


class WatchResultsTests(unittest.TestCase):
    def start(self, folder, value):
        f = fixture.WatchControllerTests()
        b, store = f.session(folder)
        f.configure(b)
        jobs = []
        b.announcements.hooks = replace(
            b.announcements.hooks, offer=jobs.append, speak=lambda *_: True
        )
        self.clock = 0
        b.chats.watch_ui.results.clock = lambda: self.clock
        value = deepcopy(value)
        value.update(window_start=100, window_end=400)
        f.now = 400
        with patch.object(b.chats.catalog, 'report', return_value=value) as read:
            b.reminder_tick()
            worker = b.chats.watch_ui.scheduler.worker
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(read.call_count, 1)
        for job in jobs:
            b.announcements.read_announcement(job)
        b.reminder_tick()
        return f, b, store, value

    def test_full_unicode_pages_are_read_only_and_never_query_or_restore_manual_context(self):
        with tempfile.TemporaryDirectory() as folder:
            f, b, store, value = self.start(folder, report(*(['😀' * 500] * 20)))
            voice = b.chats.voice
            original = (voice.snapshot, voice.progress, voice.revision, b.chats.selection)
            raw = store.path.read_bytes()
            with patch.object(b.chats.catalog, 'report', side_effect=AssertionError('No RPC')):
                state = f.request(b, 'watch_state')
                self.assertTrue(state['watch_has_results'])
                self.assertNotIn('report_text', state)
                first = f.request(b, 'watch_result')
                self.assertTrue(first['ok'])
                self.assertGreater(first['report_pages'], 1)
                self.assertEqual(first['watch_result_ttl_ms'], 600000)
                collected = []
                for page in range(first['report_pages']):
                    fields = f.request(
                        b, 'watch_result_page', snapshot_id=value['snapshot_id'], page=page
                    )
                    self.assertTrue(fields['ok'])
                    collected.append(fields['report_text'][len(fields['report_page_marker']) :])
                self.assertEqual(
                    ''.join(collected),
                    '最近定时检查 · 只读快照，非实时。可能含已提示结果；不改变手动汇报编号和补播进度。\n\n'
                    + report_text(value),
                )
                self.clock = 599
                tail = f.request(b, 'watch_result')
                self.assertEqual(tail['watch_result_ttl_ms'], 1000)
                for ident, page in ((value['snapshot_id'], 15), ('e' * 32, 0)):
                    self.assertFalse(
                        f.request(b, 'watch_result_page', snapshot_id=ident, page=page)['ok']
                    )
            self.assertEqual(store.path.read_bytes(), raw)
            self.assertEqual(
                (voice.snapshot, voice.progress, voice.revision, b.chats.selection), original
            )
            self.assertNotIn('😀', store.path.read_text())
            self.assertEqual([p.name for p in Path(folder).glob('*.json')], ['watch.json'])

    def test_empty_and_partial_reads_are_available_without_inventing_success(self):
        for partial in (False, True):
            with self.subTest(partial=partial), tempfile.TemporaryDirectory() as folder:
                value = report()
                value.update(coverage_complete=not partial, empty_confirmed=not partial)
                f, b, store, value = self.start(folder, value)
                fields = f.request(b, 'watch_result')
                self.assertTrue(fields['ok'])
                self.assertEqual('覆盖不完整' in fields['report_text'], partial)
                self.assertEqual(
                    store.snapshot()['watch']['last_check']['status'],
                    'partial' if partial else 'empty',
                )

    def test_expiry_route_maintenance_pause_reconfigure_and_source_removal_discard_results(self):
        for change in (
            'expiry',
            'route',
            'connection',
            'maintenance',
            'pause',
            'configure',
            'source',
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                f, b, store, value = self.start(folder, report())
                ui = b.chats.watch_ui
                self.assertTrue(ui.results.available())
                if change == 'expiry':
                    self.clock = 600
                elif change == 'route':
                    b.session.bound_route_token = 'changed-route'
                elif change == 'connection':
                    b.session.connection = object()
                elif change == 'maintenance':
                    ui.scheduler.hooks = replace(ui.scheduler.hooks, blocked=lambda: True)
                elif change == 'pause':
                    f.request(
                        b, 'pause_watch', revision=store.snapshot()['revision'], confirmed=True
                    )
                elif change == 'configure':
                    f.configure(b)
                else:
                    b.chats.scope_current = lambda source: False
                self.assertFalse(f.request(b, 'watch_result')['ok'])
                self.assertIsNone(ui.results.value)

    def test_failed_new_poll_removes_old_success_and_never_presents_it_as_latest(self):
        with tempfile.TemporaryDirectory() as folder:
            f, b, store, value = self.start(folder, report())
            self.assertTrue(f.request(b, 'watch_result')['ok'])
            f.now = 700
            with patch.object(b.chats.catalog, 'report', side_effect=RuntimeError('PRIVATE')):
                b.reminder_tick()
                b.chats.watch_ui.scheduler.worker.join(3)
            self.assertIsNone(b.chats.watch_ui.results.value)
            self.assertFalse(f.request(b, 'watch_result')['ok'])
            self.assertEqual(store.snapshot()['watch']['last_check']['status'], 'read_error')

    def test_heartbeat_drops_expired_ram_even_when_no_user_opens_results(self):
        with tempfile.TemporaryDirectory() as folder:
            f, b, store, value = self.start(folder, report())
            self.clock = 600
            with patch.object(
                b.chats.catalog, 'report', side_effect=AssertionError('No extra read')
            ):
                b.reminder_tick()
            self.assertIsNone(b.chats.watch_ui.results.value)

    def test_read_requests_reject_supplied_bodies_targets_consent_and_invalid_pages(self):
        base = dict(event='dot_chat_request', request_id='a' * 32, action='watch_result')
        validate_device_event(base)
        for extra in (
            dict(text='body'),
            dict(thread_id='other'),
            dict(confirmed=True),
            dict(page=0),
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_device_event(dict(base, **extra))
        page = dict(base, action='watch_result_page', snapshot_id='b' * 32, page=0)
        validate_device_event(page)
        for extra in (dict(page=True), dict(page=-1), dict(page=16), dict(snapshot_id='other')):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_device_event(dict(page, **extra))

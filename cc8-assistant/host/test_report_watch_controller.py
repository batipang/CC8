"""Real Bridge composition, wire admission, source consent and literal announcement receipts."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
import uuid
import threading
import fcntl
from unittest.mock import patch

import test_dot_handoff as sessions
from report_watch_store import ReportWatchStore
from test_report_voice import SOURCE, report
from protocol import validate_device_event, Packet


class WatchControllerTests(unittest.TestCase):
    def session(self, folder):
        self.now = 100
        store = ReportWatchStore(Path(folder) / 'watch.json', clock=lambda: self.now)
        b = sessions.ControllerTests().session(report_watch_store=store)
        b.chats.selection = SOURCE
        b.chats.watch_ui.scheduler.clock = lambda: self.now
        b.chats.watch_ui.scheduler.monotonic = lambda: self.now
        return b, store

    def request(self, b, action, **fields):
        data = dict(event='dot_chat_request', request_id=uuid.uuid4().hex, action=action, **fields)
        validate_device_event(data)
        b.on_event(data)
        if b.chats.worker is not None:
            b.chats.worker.join(3)
            self.assertFalse(b.chats.worker.is_alive())
        fields = b.events[-1][1]
        Packet.event('dot_chat_state', **fields).bind_route('b' * 32).encode()
        return fields

    def configure(self, b, mode='count'):
        value = self.request(b, 'watch_state')
        self.assertTrue(value['ok'])
        value = self.request(
            b,
            'configure_watch',
            revision=value['report_watch']['revision'],
            offer_id=value['watch_offer_id'],
            period_seconds=300,
            channel='cc8',
            confirmed=True,
            **({'mode': mode} if mode != 'count' else {}),
        )
        self.assertTrue(value['ok'])
        return value

    def test_view_is_passive_and_explicit_configuration_freezes_only_source_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.session(folder)
            with patch.object(b.chats.catalog, 'report', side_effect=AssertionError('No read')):
                state = self.request(b, 'watch_state')
                self.assertTrue(b.chats.state()['report_watch_supported'])
                self.assertFalse(store.path.exists())
                self.assertIn(SOURCE.source_title, state['watch_offer_text'])
                self.configure(b)
                raw = store.path.read_text()
                self.assertNotIn('excerpt', raw)
                self.assertEqual(store.snapshot()['watch']['source']['source_id'], SOURCE.source_id)
                self.assertFalse(b.session.active)
                self.assertIsNotNone(b.chats.watch_ui.scheduler.grant)
                self.request(b, 'pause_watch', revision=1, confirmed=True)
                self.assertFalse(store.snapshot()['watch']['enabled'])
                self.assertIsNone(b.chats.watch_ui.scheduler.grant)
                self.request(b, 'clear_watch', revision=2, confirmed=True)
                self.assertIsNone(store.snapshot()['watch'])

    def test_old_source_offer_generation_route_revision_expiry_and_capture_fail_closed(self):
        for change in ('source', 'generation', 'route', 'revision', 'expiry', 'capture'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                b, store = self.session(folder)
                value = self.request(b, 'watch_state')
                if change == 'source':
                    b.chats.selection = replace(SOURCE)
                elif change == 'generation':
                    b.session.generation += 1
                elif change == 'route':
                    b.session.connection = object()
                elif change == 'revision':
                    store.clear(0)
                elif change == 'expiry':
                    b.chats.watch_ui.clock = lambda: float('inf')
                else:
                    b.session.active = True
                fields = self.request(
                    b,
                    'configure_watch',
                    revision=0,
                    offer_id=value['watch_offer_id'],
                    period_seconds=300,
                    channel='cc8',
                    confirmed=True,
                )
                self.assertFalse(fields['ok'])
                self.assertIsNone(store.snapshot()['watch'])

    def test_actual_heartbeat_reads_frozen_source_and_sends_once_after_durable_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.session(folder)
            self.configure(b)
            original_progress = object()
            b.chats.voice.progress = original_progress
            b.chats.selection = replace(SOURCE, source_id='other-chat', source_title='其他来源')
            jobs, reads = [], []
            b.announcements.hooks = replace(b.announcements.hooks, offer=jobs.append)

            def read(source, window, **kwargs):
                self.assertEqual(source.source_id, SOURCE.source_id)
                self.assertTrue(kwargs['is_current']())
                reads.append((window.start, window.end))
                value = report('PRIVATE actual result')
                value.update(window_start=window.start, window_end=window.end)
                return value

            self.now = 400
            with patch.object(b.chats.catalog, 'report', side_effect=read):
                b.reminder_tick()
                b.chats.watch_ui.scheduler.worker.join(3)
                self.assertFalse(b.chats.watch_ui.scheduler.worker.is_alive())
            self.assertEqual(reads, [(100, 400)])
            self.assertEqual(len(jobs), 1)
            watch = store.snapshot()['watch']
            self.assertEqual(watch['attempt']['status'], 'claimed')
            self.assertEqual(watch['seen'], [])
            self.assertNotIn('PRIVATE', jobs[0].text)
            self.assertIs(b.chats.voice.progress, original_progress)
            jobs[0].result = 'completed'
            jobs[0].done.set()
            b.reminder_tick()
            self.assertEqual(len(store.snapshot()['watch']['seen']), 1)
            self.assertFalse(b.session.active)
            self.assertIs(b.chats.voice.progress, original_progress)

    def test_protocol_rejects_targets_bodies_wrong_channels_and_implicit_consent(self):
        base = dict(
            event='dot_chat_request',
            action='configure_watch',
            request_id='a' * 32,
            offer_id='b' * 32,
            revision=0,
            confirmed=True,
            period_seconds=300,
            channel='cc8',
        )
        for changes in (
            dict(thread_id='other'),
            dict(source_id='other'),
            dict(text='PRIVATE'),
            dict(path='/tmp/elsewhere'),
            dict(confirmed=1),
            dict(revision=True),
            dict(period_seconds=True),
            dict(period_seconds=1),
            dict(channel='email'),
            dict(mode='automatic'),
            dict(mode='summary', channel='desktop'),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_device_event(dict(base, **changes))

    def test_configuration_nonce_replay_cannot_pause_or_clear_a_new_state(self):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.session(folder)
            self.configure(b)
            data = dict(
                event='dot_chat_request',
                action='pause_watch',
                request_id=uuid.uuid4().hex,
                revision=1,
                confirmed=True,
            )
            b.on_event(data)
            b.chats.worker.join(3)
            raw = store.path.read_bytes()
            b.on_event(dict(data, action='clear_watch', revision=2))
            self.assertFalse(b.events[-1][1]['ok'])
            self.assertEqual(store.path.read_bytes(), raw)

    def test_unsupported_summary_cannot_be_enabled_by_bypassing_native_options(self):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.session(folder)
            scheduler = b.chats.watch_ui.scheduler
            scheduler.hooks = replace(scheduler.hooks, send_summary=None)
            offer = self.request(b, 'watch_state')
            self.assertFalse(offer['watch_summary_supported'])
            value = self.request(
                b,
                'configure_watch',
                revision=0,
                offer_id=offer['watch_offer_id'],
                period_seconds=300,
                channel='cc8',
                mode='summary',
                confirmed=True,
            )
            self.assertFalse(value['ok'])
            self.assertFalse(store.path.exists())

    def test_default_bridge_has_no_subscription_timer_or_private_reads(self):
        b = sessions.ControllerTests().session()
        self.assertIsNone(b.chats.watch_ui.scheduler)
        fields = self.request(b, 'watch_state')
        self.assertFalse(fields['ok'])

    def test_production_factory_explicitly_injects_passive_private_storage(self):
        import bridge as production

        with tempfile.TemporaryDirectory() as folder, patch('bridge.PRIVATE', Path(folder)):
            with patch('bridge.run_app', side_effect=lambda dependencies: dependencies):
                dependencies = production.run_backend()
            with patch('bridge.Bridge') as construct:
                dependencies.bridge(None, object())
                store = construct.call_args.kwargs['report_watch_store']
                self.assertIsInstance(store, ReportWatchStore)
                self.assertEqual(store.path, Path(folder) / 'work-report-watch.json')
                self.assertFalse(store.path.exists())
                dependencies.bridge(None, None)
                self.assertTrue(construct.call_args.kwargs['local_mode'])
                self.assertNotIn('report_watch_store', construct.call_args.kwargs)

    def test_background_observation_commits_take_the_same_owner_before_store_lock_as_ui(self):
        for kind in ('empty', 'error'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                b, store = self.session(folder)
                self.configure(b)
                owned = []
                original = store.checked

                def checked(*args, **kwargs):
                    owned.append(b.session.guard._is_owned())
                    return original(*args, **kwargs)

                value = report()
                value.update(window_start=100, window_end=400)
                self.now = 400
                with (
                    patch.object(store, 'checked', side_effect=checked),
                    patch.object(
                        b.chats.catalog,
                        'report',
                        return_value=value,
                        side_effect=RuntimeError('PRIVATE') if kind == 'error' else None,
                    ),
                ):
                    b.reminder_tick()
                    b.chats.watch_ui.scheduler.worker.join(3)
                    self.assertFalse(b.chats.watch_ui.scheduler.worker.is_alive())
                self.assertEqual(owned, [True])
                fields = self.request(b, 'watch_state')
                self.assertTrue(fields['ok'])
                self.assertEqual(
                    store.snapshot()['watch']['last_check']['status'],
                    'empty' if kind == 'empty' else 'read_error',
                )

    def test_shared_reader_rejects_ui_overlap_and_discards_a_late_read_after_home(self):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.session(folder)
            self.configure(b)
            entered, release = threading.Event(), threading.Event()
            value = report()
            value.update(window_end=400)

            def read(source, window, **kwargs):
                entered.set()
                self.assertTrue(release.wait(3))
                self.assertFalse(kwargs['is_current']())
                return value

            self.now = 400
            with patch.object(b.chats.catalog, 'report', side_effect=read):
                b.reminder_tick()
                self.assertTrue(entered.wait(3))
                self.assertTrue(b.chats.busy)
                fields = self.request(b, 'watch_state')
                self.assertFalse(fields['ok'])
                b.session.connection = None
                release.set()
                b.chats.watch_ui.scheduler.worker.join(3)
                self.assertFalse(b.chats.watch_ui.scheduler.worker.is_alive())
            self.assertFalse(b.chats.busy)
            self.assertIsNone(store.snapshot()['watch']['attempt'])
            self.assertIsNone(store.snapshot()['watch']['last_check'])

    def test_actual_quiet_policy_defers_poll_without_changing_its_frozen_start(self):
        from datetime import datetime
        from work_reports import BEIJING

        with tempfile.TemporaryDirectory() as folder:
            b, store = self.session(folder)
            self.configure(b)
            self.now = 400
            b.workspace.now = lambda: datetime.fromtimestamp(self.now, BEIJING)
            b.workspace.reminder_policy.update(
                quiet_enabled=True, quiet_start='08:00', quiet_end='09:00'
            )
            value = report()
            value.update(window_end=400)
            with patch.object(b.chats.catalog, 'report', return_value=value) as read:
                b.reminder_tick()
                read.assert_not_called()
                b.workspace.reminder_policy['quiet_enabled'] = False
                b.reminder_tick()
                b.chats.watch_ui.scheduler.worker.join(3)
                self.assertFalse(b.chats.watch_ui.scheduler.worker.is_alive())
                self.assertEqual(read.call_count, 1)
            self.assertEqual(store.snapshot()['watch']['started_at'], 100)
            self.assertEqual(store.snapshot()['watch']['last_check']['status'], 'empty')

    def test_actual_maintenance_lease_path_blocks_configuration_grant_and_does_not_rearm(self):
        with tempfile.TemporaryDirectory() as folder:
            b, store = self.session(folder)
            lock = Path(folder) / 'automation-test.lock'
            lock.write_bytes(b'')
            lock.chmod(0o600)
            with patch('bridge.PRIVATE', Path(folder)):
                with lock.open('rb') as lease:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.configure(b)
                    self.assertIsNone(b.chats.watch_ui.scheduler.grant)
                self.now = 400
                value = report()
                value.update(window_end=400)
                with patch.object(b.chats.catalog, 'report', return_value=value) as read:
                    b.reminder_tick()
                    read.assert_not_called()
                    b.session.active = True
                    b.session.capture_id = 'fresh-normal-start'
                    b.reminder_tick()
                    b.chats.watch_ui.scheduler.worker.join(3)
                    self.assertFalse(b.chats.watch_ui.scheduler.worker.is_alive())
                    self.assertEqual(read.call_count, 1)
            self.assertEqual(store.snapshot()['watch']['last_check']['status'], 'empty')

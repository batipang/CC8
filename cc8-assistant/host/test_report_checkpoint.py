"""Real report/receipt checkpoints, private stores and actual dispatch; no live chat reads."""

from copy import deepcopy
from pathlib import Path
import json
import stat
import tempfile
import unittest
import uuid
from unittest.mock import patch

from dot_handoff import Handoff
from report_checkpoint import ReportCheckpoints, record, restore, validate_record, describe
from report_progress import ReportProgress, DeliveryPlan, key, version
from protocol import validate_device_event
from reminder_store import ReminderCommitUncertain
import test_work_reports as reports
import test_dot_handoff as sessions


def progress():
    fixture = reports.Fixture()
    fixture.turn_pages[0]['data'] = [reports.turn(f'turn-{i}') for i in range(1, 5)]
    for i in range(1, 5):
        fixture.item_pages[f'turn-{i}'] = [
            dict(
                data=[dict(turnId=f'turn-{i}', item=reports.final(f'item-{i}', f'成果{i}'))],
                nextCursor=None,
            )
        ]
    report = reports.ReportTests().report(fixture)
    result = ReportProgress(reports.ReportTests.source, report)
    first = next(iter(result.entries.values()))
    plan = DeliveryPlan(result, {(key(first.item), version(first.item)): {0, 1}})
    plan.confirm(plan.token(0), result)
    assert result.status()['summary_delivered'] == 0
    plan.confirm(plan.token(1), result)
    assert result.status()['summary_delivered'] == 1
    return result, report, plan


class CheckpointStoreTests(unittest.TestCase):
    def test_explicit_roundtrip_preserves_actual_completed_summary_receipts_and_unheard(self):
        original, source_report, old_plan = progress()
        item = record(original, clock=lambda: 300)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private' / 'progress.json'
            store = ReportCheckpoints(path)
            self.assertEqual(store.snapshot()['items'], [])
            self.assertFalse(path.exists())
            saved = store.apply('save', 0, item=item)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            rebuilt = ReportCheckpoints(path).snapshot()
            self.assertEqual(rebuilt, saved)
            source = Handoff(original.source.source_id, original.source.source_title, (), False)
            restored = restore(rebuilt['items'][0], source)
            self.assertIs(restored.source, source)
            self.assertNotEqual(restored.epoch, original.epoch)
            self.assertEqual(restored.status()['summary_delivered'], 1)
            self.assertEqual(restored.status()['pending'], 3)
            # Restoring does not revive an old playback plan or treat cancelled
            # partial segments as a device-confirmed complete summary.
            old_plan.confirm(old_plan.token(1), restored)
            fresh = deepcopy(source_report)
            fresh['window_end'] = 400
            merged, view = restored.merge(fresh)
            self.assertEqual(view['delivered_items_skipped'], 1)
            self.assertEqual(len(view['items']), 3)
            self.assertEqual(merged.status()['pending'], 3)

    def test_updated_and_cached_unheard_results_survive_restore_without_becoming_current_truth(
        self,
    ):
        original, report, _ = progress()
        restored = restore(record(original), original.source)
        fresh = deepcopy(report)
        fresh['window_end'] = 400
        first = next(iter(restored.entries.values())).item
        for item in fresh['items']:
            if key(item) == key(first):
                item['excerpt'] = '结果已更新'
        fresh['items'] = fresh['items'][:2]
        merged, view = restored.merge(fresh)
        self.assertFalse(view['coverage_complete'])
        self.assertTrue(any(r['since_status'] == 'updated' for r in view['items']))
        self.assertTrue(any(r['since_status'] == 'cached_unheard' for r in view['items']))
        self.assertEqual(merged.status()['pending'], 4)

    def test_record_projects_extra_fields_and_rejects_invalid_evidence_and_duplicate_identity(self):
        original, _, _ = progress()
        entry = next(iter(original.entries.values()))
        entry.item['PRIVATE_RAW'] = 'PRIVATE'
        entry.item['evidence']['PRIVATE_OUTPUT'] = 'PRIVATE'
        value = record(original)
        self.assertNotIn('PRIVATE', json.dumps(value))
        for change in ('duplicate', 'evidence', 'delivered', 'unicode'):
            item = deepcopy(value)
            if change == 'duplicate':
                item['entries'].append(deepcopy(item['entries'][0]))
            elif change == 'evidence':
                item['entries'][0]['item']['evidence']['raw'] = 'PRIVATE'
            elif change == 'unicode':
                item['entries'][0]['item']['excerpt'] = '\ud800'
            else:
                item['entries'][0]['delivered'] = 1
            with self.subTest(change=change), self.assertRaises((ValueError, UnicodeError)):
                validate_record(item)

    def test_source_scope_identity_must_match_exactly_and_group_namespaces_do_not_collide(self):
        from test_report_sources import entry, GroupFixture
        from report_sources import ReportSources, ReportScope
        from group_work_reports import GroupWorkReports
        from work_reports import ReportWindow

        with tempfile.TemporaryDirectory() as folder:
            registry = ReportSources(Path(folder) / 'sources.json')
            registry.apply('register', 0, entry=entry())
            scope = ReportScope.from_state(registry.apply('register', 1, entry=entry('chat-b')))
            data = GroupWorkReports(GroupFixture().factory).read(scope, ReportWindow(100, 200))
            original = ReportProgress(scope, data)
            item = record(original)
            self.assertEqual(describe(restore(item, scope).source), describe(scope))
            self.assertEqual(len(original.entries), 2)
            # Adding then removing a source outside this scope changes the
            # registry hash, but not the selected members' actual identities.
            registry.apply('register', 2, entry=entry('chat-c'))
            extra = registry.snapshot()['items'][-1]['id']
            registry.apply('delete', 3, ident=extra)
            refreshed = ReportScope.from_state(registry.snapshot())
            self.assertNotEqual(scope.source_id, refreshed.source_id)
            rebuilt = restore(item, refreshed)
            self.assertIs(rebuilt.source, refreshed)
            self.assertEqual(len(rebuilt.entries), 2)
            registry.apply('delete', 4, ident=scope.members[0].registration_id)
            with self.assertRaises(ValueError):
                restore(item, ReportScope.from_state(registry.snapshot()))
        original, _, _ = progress()
        with self.assertRaises(ValueError):
            restore(
                record(original), Handoff('another-chat', original.source.source_title, (), False)
            )

    def test_capacity_stale_cas_cancel_corruption_and_uncertain_commit_never_overwrite_silently(
        self,
    ):
        original, _, _ = progress()
        with tempfile.TemporaryDirectory() as folder:
            store = ReportCheckpoints(Path(folder) / 'progress.json')
            for n in range(8):
                store.apply('save', n, item=record(original))
            raw = store.path.read_bytes()
            for revision, kwargs in ((8, {}), (7, {}), (8, dict(current=lambda: False))):
                with self.assertRaises(ValueError):
                    store.apply('save', revision, item=record(original), **kwargs)
                self.assertEqual(store.path.read_bytes(), raw)
            store.apply('delete', 8, ident=store.snapshot()['items'][0]['id'])
            self.assertEqual(len(store.snapshot()['items']), 7)
            with patch('reminder_store.os.fsync', side_effect=OSError('private')):
                with self.assertRaises((OSError, ReminderCommitUncertain)):
                    store.apply('clear', 9)
            self.assertEqual(len(store.snapshot()['items']), 7)
            store.path.write_text('{PRIVATE-corrupt')
            with self.assertRaises(ValueError):
                store.apply('clear', 9)
            self.assertEqual(store.path.read_text(), '{PRIVATE-corrupt')

    def test_store_symlink_and_total_byte_limit_fail_without_eviction(self):
        original, _, _ = progress()
        with tempfile.TemporaryDirectory() as folder:
            outside = Path(folder) / 'outside'
            outside.write_text('PRIVATE')
            linked = Path(folder) / 'linked'
            linked.symlink_to(outside)
            with self.assertRaises((ValueError, OSError)):
                ReportCheckpoints(linked).snapshot()
            self.assertEqual(outside.read_text(), 'PRIVATE')
            store = ReportCheckpoints(Path(folder) / 'progress.json')
            first = record(original)
            store.apply('save', 0, item=first)
            raw = store.path.read_bytes()
            large = record(original)
            large['entries'] = []
            template = first['entries'][0]
            for i in range(256):
                row = deepcopy(template)
                row['item']['item_id'] = f'item-{i}'
                row['item']['excerpt'] = '😀' * 500
                large['entries'].append(row)
            with self.assertRaises(ValueError):
                store.apply('save', 1, item=large)
            self.assertEqual(store.path.read_bytes(), raw)

    def test_two_store_instances_serialize_cas_and_uncertain_durable_commit_is_not_retried(self):
        import concurrent.futures
        import threading

        original, _, _ = progress()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'progress.json'
            barrier = threading.Barrier(2)

            def save_one():
                barrier.wait()
                try:
                    ReportCheckpoints(path).apply('save', 0, item=record(original))
                    return 'saved'
                except ValueError:
                    return 'conflict'

            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: save_one(), range(2)))
            self.assertCountEqual(results, ['saved', 'conflict'])
            store = ReportCheckpoints(path)
            self.assertEqual(store.snapshot()['revision'], 1)
            import os

            real = os.fsync
            calls = []

            def uncertain(fd):
                calls.append(fd)
                if len(calls) == 2:
                    raise OSError('directory sync unconfirmed')
                real(fd)

            with patch('reminder_store.os.fsync', side_effect=uncertain):
                with self.assertRaises(ReminderCommitUncertain):
                    store.apply('clear', 1)
            self.assertEqual(store.snapshot(), dict(schema=1, revision=2, items=[]))
            with self.assertRaises(ValueError):
                store.apply('clear', 1)


class CheckpointControllerTests(unittest.TestCase):
    def test_bridge_injects_private_checkpoint_store_without_startup_read_or_write(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ReportCheckpoints(Path(folder) / 'progress.json')
            mini = type('Mini', (), {'cancel': lambda self: None})()
            with (
                patch('threading.Thread.start'),
                patch.object(store, 'snapshot', side_effect=AssertionError('No startup read')),
            ):
                bridge = sessions.Bridge(
                    None,
                    sessions.BrainRouter(mini, sessions.DotBrain(sessions.FakeRpc())),
                    preference_memory=sessions.isolated_memory(),
                    report_checkpoint_store=store,
                )
            self.assertIs(bridge.chats.progress_ui.store, store)
            self.assertFalse(store.path.exists())
            self.assertIsNone(bridge.chats.voice.progress)

    def session(self, folder):
        b = sessions.ControllerTests().session()
        b.chats.progress_ui.store = ReportCheckpoints(Path(folder) / 'progress.json')
        b.chats.selection = reports.ReportTests.source
        p, report, _ = progress()
        self.assertTrue(b.chats.voice.remember(b.chats.selection, report, progress=p))
        return b

    def request(self, b, action, **fields):
        data = dict(event='dot_chat_request', request_id=uuid.uuid4().hex, action=action, **fields)
        validate_device_event(data)
        b.on_event(data)
        if b.chats.worker is not None:
            b.chats.worker.join(3)
            self.assertFalse(b.chats.worker.is_alive())
        return b.events[-1][1]

    def offer(self, b):
        return self.request(b, 'progress_checkpoints')['offer_id']

    def save(self, b, offer):
        return self.request(b, 'save_progress', offer_id=offer, revision=0, confirmed=True)

    def test_actual_save_restart_explicit_restore_has_no_capture_rpc_task_or_auto_source_selection(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            old_progress = b.chats.voice.progress
            saved = self.save(b, self.offer(b))
            self.assertTrue(saved['ok'])
            ident = saved['progress_checkpoints']['items'][0]['id']
            self.assertIs(b.chats.voice.progress, old_progress)
            new = sessions.ControllerTests().session()
            new.chats.progress_ui.store = ReportCheckpoints(Path(folder) / 'progress.json')
            self.request(new, 'progress_checkpoints')
            self.assertIsNone(new.chats.selection)
            self.assertIsNone(new.chats.voice.progress)
            failed = self.request(
                new, 'restore_progress', checkpoint_id=ident, revision=1, confirmed=True
            )
            self.assertFalse(failed['ok'])
            self.assertIsNone(new.chats.voice.progress)
            selected = Handoff(
                old_progress.source.source_id,
                old_progress.source.source_title,
                (('user', 'existing'),),
                False,
            )
            new.chats.selection = selected
            with patch.object(new.chats.catalog, 'report', side_effect=AssertionError('No RPC')):
                detail = self.request(new, 'progress_checkpoint', checkpoint_id=ident)
                self.assertTrue(detail['can_restore'])
                result = self.request(
                    new, 'restore_progress', checkpoint_id=ident, revision=1, confirmed=True
                )
            self.assertTrue(result['progress_restored'])
            self.assertIs(new.chats.selection, selected)
            self.assertIs(new.chats.voice.progress.source, selected)
            self.assertEqual(new.chats.voice.progress.status()['pending'], 3)
            self.assertIsNone(new.chats.voice.snapshot)
            self.assertFalse(new.chats.voice.followup)
            self.assertIsNone(new.chats.voice.delivery_plan)
            self.assertFalse(new.session.active)
            self.assertEqual(new.workspace.tasks, [])

    def test_save_offer_is_frozen_and_cannot_survive_route_generation_context_progress_or_lifetime_change(
        self,
    ):
        for change in ('route', 'generation', 'source', 'receipt', 'forget', 'deadline', 'capture'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                b = self.session(folder)
                offer = self.offer(b)
                if change == 'route':
                    b.session.connection = object()
                elif change == 'generation':
                    b.session.generation += 1
                elif change == 'source':
                    b.chats.selection = Handoff('different', '另一个', (), False)
                elif change == 'receipt':
                    next(iter(b.chats.voice.progress.entries.values())).delivered = None
                elif change == 'forget':
                    b.chats.voice.forget()
                elif change == 'deadline':
                    b.chats.progress_ui.clock = lambda: float('inf')
                else:
                    b.session.active = True
                self.assertFalse(self.save(b, offer)['ok'])
                self.assertEqual(b.chats.progress_ui.store.snapshot()['items'], [])

    def test_cancel_before_store_commit_and_deleted_checkpoint_cannot_restore_old_state(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            offer = self.offer(b)
            store = b.chats.progress_ui.store
            apply = store.apply

            def changed(*args, **kwargs):
                b.session.connection = object()
                return apply(*args, **kwargs)

            with patch.object(store, 'apply', side_effect=changed):
                self.save(b, offer)
            self.assertEqual(store.snapshot()['items'], [])
            saved = self.save(b, self.offer(b))
            ident = saved['progress_checkpoints']['items'][0]['id']
            old = b.chats.voice.progress
            self.request(b, 'delete_progress', checkpoint_id=ident, revision=1, confirmed=True)
            self.assertFalse(
                self.request(
                    b, 'restore_progress', checkpoint_id=ident, revision=1, confirmed=True
                )['ok']
            )
            self.assertIs(b.chats.voice.progress, old)

    def test_invalid_live_progress_cannot_hide_saved_records_or_offer_a_bad_save(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            self.assertTrue(self.save(b, self.offer(b))['ok'])
            next(iter(b.chats.voice.progress.entries.values())).item['result_kind'] = 'unknown'
            result = self.request(b, 'progress_checkpoints')
            self.assertTrue(result['ok'])
            self.assertNotIn('offer_id', result)
            self.assertEqual(len(result['progress_checkpoints']['items']), 1)
            self.assertTrue(self.request(b, 'clear_progress', revision=1, confirmed=True)['ok'])

    def test_restore_rechecks_busy_capture_and_playback(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            saved = self.save(b, self.offer(b))
            ident = saved['progress_checkpoints']['items'][0]['id']
            original = b.chats.voice.progress
            for busy in ('capture', 'playback'):
                b.session.active = busy == 'capture'
                b.response.busy = busy == 'playback'
                result = self.request(
                    b, 'restore_progress', checkpoint_id=ident, revision=1, confirmed=True
                )
                self.assertFalse(result['ok'])
                self.assertIs(b.chats.voice.progress, original)

    def test_registered_membership_removed_during_restore_cannot_install_old_progress(self):
        from report_sources import ReportSources, ReportScope
        from test_report_sources import entry, GroupFixture
        from group_work_reports import GroupWorkReports
        from work_reports import ReportWindow
        from report_checkpoint import restore as real_restore

        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            b.chats.sources = ReportSources(Path(folder) / 'sources.json')
            b.chats.sources.apply('register', 0, entry=entry())
            scope = ReportScope.from_state(b.chats.sources.snapshot())
            b.chats.report_scope = scope
            report = GroupWorkReports(GroupFixture().factory).read(scope, ReportWindow(100, 200))
            self.assertTrue(b.chats.voice.remember(scope, report))
            saved = self.save(b, self.offer(b))
            ident = saved['progress_checkpoints']['items'][0]['id']

            def removed(item, source):
                result = real_restore(item, source)
                b.chats.sources.apply('clear', 1)
                return result

            with patch('report_checkpoint_controller.restore', side_effect=removed):
                result = self.request(
                    b, 'restore_progress', checkpoint_id=ident, revision=1, confirmed=True
                )
            self.assertFalse(result['ok'])
            self.assertNotIn('progress_restored', result)
            self.assertIsNone(b.chats.voice.progress)
            self.assertEqual(len(b.chats.progress_ui.store.snapshot()['items']), 1)

    def test_wire_rejects_arbitrary_bodies_paths_targets_and_implicit_consent(self):
        base = dict(
            event='dot_chat_request',
            request_id='a' * 32,
            action='restore_progress',
            checkpoint_id='b' * 32,
            revision=1,
            confirmed=True,
        )
        validate_device_event(base)
        for changes in (
            dict(confirmed=1),
            dict(confirmed=False),
            dict(revision=True),
            dict(checkpoint_id='../x'),
            dict(thread_id='other'),
            dict(entries=[]),
            dict(path='/tmp/private'),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_device_event(dict(base, **changes))
        for action in (
            'progress_checkpoints',
            'progress_checkpoint',
            'save_progress',
            'delete_progress',
            'clear_progress',
        ):
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_device_event(dict(base, action=action, text='PRIVATE'))

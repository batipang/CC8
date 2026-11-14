"""Real private metadata storage, frozen scope and bounded read-only aggregation."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from dot_client import Cancelled, DotError
from dot_handoff import Handoff
from group_work_reports import GroupWorkReports
from reminder_store import ReminderCommitUncertain
from report_progress import ReportProgress, key
from report_sources import ReportSources, ReportScope, validate
from report_voice import ReportCommand, ReportVoice, ReportVoiceHooks, report_source, report_text
from work_reports import ReportWindow, WorkReportReader
from test_work_reports import turn, final


def entry(thread='chat-a'):
    return dict(
        provider='codex', host='local', thread_id=thread, title='项目 ' + thread, project='项目'
    )


def scope(*threads):
    rows = [
        dict(entry(thread), id=f'{index + 1:016x}', registered_at=100)
        for index, thread in enumerate(threads or ('chat-a', 'chat-b'))
    ]
    return ReportScope.from_state(dict(schema=1, revision=1, items=rows))


class StoreTests(unittest.TestCase):
    def test_absent_snapshot_does_not_create_directories_or_private_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'absent' / 'sources.json'
            self.assertEqual(ReportSources(path).snapshot(), dict(schema=1, revision=0, items=[]))
            self.assertFalse(path.parent.exists())

    def test_explicit_metadata_only_and_defensive_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private' / 'sources.json'
            store = ReportSources(path, clock=lambda: 100)
            value = store.apply('register', 0, entry=entry())
            self.assertEqual(
                (path.stat().st_mode & 0o777, path.parent.stat().st_mode & 0o777), (0o600, 0o700)
            )
            self.assertEqual(
                set(value['items'][0]),
                {'provider', 'host', 'thread_id', 'title', 'project', 'id', 'registered_at'},
            )
            value['items'][0]['title'] = 'mutated UI'
            self.assertEqual(store.snapshot()['items'][0]['title'], '项目 chat-a')
            frozen = ReportScope.from_state(store.snapshot())
            with self.assertRaises(FrozenInstanceError):
                frozen.members[0].title = 'changed'
            store.apply('delete', 1, ident=store.snapshot()['items'][0]['id'])
            self.assertEqual(frozen.members[0].thread_id, 'chat-a')
            self.assertEqual(store.snapshot()['items'], [])

    def test_invalid_metadata_duplicate_and_stale_revision_preserve_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sources.json'
            store = ReportSources(path)
            store.apply('register', 0, entry=entry())
            original = path.read_bytes()
            for data in (
                entry(),
                dict(entry('chat-b'), provider='chatgpt'),
                dict(entry('chat-b'), host='remote'),
                dict(entry('chat-b'), text='PRIVATE_CHAT'),
                dict(entry('chat-b'), title='bad\nname'),
            ):
                with self.subTest(data=data), self.assertRaises(ValueError):
                    store.apply('register', 1, entry=data)
                self.assertEqual(path.read_bytes(), original)
            for revision in (0, True, -1, 2**31):
                with self.assertRaises(ValueError):
                    store.apply('clear', revision)
                self.assertEqual(path.read_bytes(), original)

    def test_capacity_delete_and_explicit_clear(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sources.json'
            store = ReportSources(path)
            for i in range(8):
                store.apply('register', i, entry=entry('chat-' + str(i)))
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                store.apply('register', 8, entry=entry('ninth'))
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(ValueError):
                store.apply('delete', 8, ident='0' * 16)
            self.assertEqual(store.apply('clear', 8)['items'], [])
            self.assertEqual(store.snapshot()['revision'], 9)

    def test_cancel_before_commit_never_writes_a_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sources.json'
            checks = iter((True, False))
            with self.assertRaises(ValueError):
                ReportSources(path).apply(
                    'register', 0, entry=entry(), current=lambda: next(checks)
                )
            self.assertFalse(path.exists())

    def test_cross_instance_revision_check_is_serialized_by_real_file_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sources.json'
            barrier = threading.Barrier(2)
            outcomes = []

            def write(name):
                store = ReportSources(path)
                barrier.wait(timeout=5)
                try:
                    store.apply('register', 0, entry=entry(name))
                    outcomes.append('saved')
                except ValueError:
                    outcomes.append('stale')

            workers = [
                threading.Thread(target=write, args=(name,)) for name in ('chat-a', 'chat-b')
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(5)
                self.assertFalse(worker.is_alive())
            self.assertCountEqual(outcomes, ['saved', 'stale'])
            self.assertEqual(len(ReportSources(path).snapshot()['items']), 1)

    def test_durability_uncertainty_is_not_retried_or_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sources.json'
            store = ReportSources(path)
            original = os.fsync

            def fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError('injected directory fsync failure')
                original(fd)

            with patch('os.fsync', side_effect=fsync), self.assertRaises(ReminderCommitUncertain):
                store.apply('register', 0, entry=entry())
            self.assertEqual(store.snapshot()['revision'], 1)
            with self.assertRaises(ValueError):
                store.apply('register', 0, entry=entry())
            self.assertEqual(len(store.snapshot()['items']), 1)

    def test_unsafe_linked_or_public_state_and_lock_are_rejected(self):
        for variant in ('symlink', 'hardlink', 'public', 'lock_link'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                path = root / 'sources.json'
                target = root / 'target'
                target.write_text(json.dumps(dict(schema=1, revision=0, items=[])))
                target.chmod(0o600)
                if variant == 'symlink':
                    path.symlink_to(target)
                elif variant == 'hardlink':
                    os.link(target, path)
                elif variant == 'public':
                    path.write_bytes(target.read_bytes())
                    path.chmod(0o644)
                else:
                    (root / '.work-report-sources.lock').symlink_to(target)
                with self.assertRaises((OSError, ValueError)):
                    ReportSources(path).apply('register', 0, entry=entry())
                self.assertEqual(json.loads(target.read_text())['items'], [])

    def test_duplicate_json_unknown_fields_and_duplicate_identity_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sources.json'
            path.write_text('{"schema":1,"schema":1,"revision":0,"items":[]}')
            path.chmod(0o600)
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                ReportSources(path).snapshot()
            self.assertEqual(path.read_bytes(), original)
        good = dict(schema=1, revision=0, items=[])
        for value in (dict(good, history=[]), dict(good, revision=True), dict(good, schema=True)):
            with self.assertRaises(ValueError):
                validate(value)
        row = dict(entry(), id='1' * 16, registered_at=100)
        with self.assertRaises(ValueError):
            validate(dict(good, items=[row, dict(row, id='2' * 16)]))


class GroupFixture:
    def __init__(self):
        self.calls = []
        self.clients = []
        self.failed = set()
        self.partial = set()
        self.count = 1
        self.now = 0
        self.advance = 0
        self.completed = 150
        self.after_request = lambda: None

    def factory(self):
        fixture = self

        class Rpc:
            def __init__(self):
                self.closed = False
                self.started = False

            def start(self):
                self.started = True

            def close(self):
                self.closed = True

            def request(self, method, params, **kwargs):
                fixture.calls.append((method, dict(params), kwargs))
                fixture.now += fixture.advance
                self.thread = params['threadId']
                if self.thread in fixture.failed:
                    raise DotError('private unavailable detail')
                if method == 'thread/turns/list':
                    self.response = dict(
                        data=[
                            turn(f'turn-{i + 1}', completed=fixture.completed)
                            for i in range(fixture.count)
                        ],
                        nextCursor=str(int(params.get('cursor', '0')) + 1)
                        if self.thread in fixture.partial
                        else None,
                    )
                elif method == 'thread/items/list':
                    self.response = dict(
                        data=[
                            dict(turnId=params['turnId'], item=final(text=self.thread + ' 完成'))
                        ],
                        nextCursor=None,
                    )
                else:
                    raise AssertionError('Source report attempted a write or unrelated RPC')
                fixture.after_request()
                return self.response

        client = Rpc()
        fixture.clients.append(client)
        return client


class GroupTests(unittest.TestCase):
    def read(self, fixture, selected=None, **kwargs):
        return GroupWorkReports(fixture.factory, clock=lambda: fixture.now).read(
            selected or scope(), ReportWindow(100, 200), **kwargs
        )

    def test_same_range_only_registered_sources_and_cross_chat_item_ids_do_not_collide(self):
        f = GroupFixture()
        selected = scope()
        report = self.read(f, selected)
        self.assertEqual(len(report['items']), 2)
        self.assertEqual(len({key(row) for row in report['items']}), 2)
        self.assertEqual({p['threadId'] for _, p, _ in f.calls}, {'chat-a', 'chat-b'})
        self.assertTrue(ReportVoice.valid_report(selected, report))
        self.assertTrue(report['coverage_complete'])
        self.assertTrue(all(c.closed for c in f.clients))
        self.assertNotIn('private unavailable', report_text(report))

    def test_unavailable_source_is_visible_and_cannot_be_reported_as_zero(self):
        f = GroupFixture()
        f.failed.add('chat-a')
        selected = scope()
        report = self.read(f, selected)
        self.assertEqual(report['unavailable_sources'], 1)
        self.assertFalse(report['coverage_complete'])
        self.assertFalse(report['empty_confirmed'])
        self.assertIn('不可读取，不能视为零成果', report_text(report))
        self.assertTrue(ReportVoice.valid_report(selected, report))
        f.failed.add('chat-b')
        report = self.read(f, selected)
        self.assertEqual(report['items'], [])
        self.assertFalse(report['empty_confirmed'])

    def test_global_deadline_marks_unread_sources_without_starting_another_client(self):
        f = GroupFixture()
        f.advance = 25
        report = self.read(f, scope('one', 'two', 'three', 'four'))
        self.assertEqual(len(f.clients), 2)
        self.assertEqual(
            [s['status'] for s in report['sources']],
            ['unavailable', 'unavailable', 'not_read_budget', 'not_read_budget'],
        )
        self.assertFalse(report['coverage_complete'])
        self.assertTrue(all(c.closed for c in f.clients))

    def test_expired_source_deadline_never_starts_the_client(self):
        f = GroupFixture()
        with self.assertRaises(DotError):
            WorkReportReader(f.factory, clock=lambda: 0).read(
                Handoff('one', '一', (), False), ReportWindow(100, 200), deadline=0
            )
        self.assertFalse(f.clients[0].started)
        self.assertTrue(f.clients[0].closed)
        self.assertEqual(f.calls, [])

    def test_cancellation_cannot_continue_to_the_next_registered_source(self):
        f = GroupFixture()
        current = [True]
        f.after_request = lambda: current.__setitem__(0, False)
        with self.assertRaises(Cancelled):
            self.read(f, is_current=lambda: current[0])
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(len(f.clients), 1)
        self.assertTrue(f.clients[0].closed)

    def test_delivery_ledger_retains_unheard_failed_source_as_old_snapshot(self):
        f = GroupFixture()
        selected = scope()
        original = self.read(f, selected)
        progress = ReportProgress(selected, original)
        heard = original['items'][0]
        progress.confirm(key(heard), progress.entries[key(heard)].fingerprint)
        unheard = original['items'][1]
        f.failed.add(unheard['source_thread_id'])
        updated, view = progress.merge(self.read(f, selected))
        self.assertEqual(len(view['items']), 1)
        self.assertEqual(view['items'][0]['since_status'], 'cached_unheard')
        self.assertTrue(ReportVoice.valid_report(selected, view))
        self.assertFalse(view['coverage_complete'])
        self.assertEqual(updated.status()['pending'], 1)

        context = (selected, object(), 0)
        voice = ReportVoice(
            ReportVoiceHooks(
                threading.RLock(),
                lambda: context,
                lambda *a: self.fail('Follow-up re-read sources'),
            ),
            clock=lambda: 0,
        )
        self.assertTrue(voice.remember(selected, view))
        detail = voice.answer(ReportCommand('item', 1), lambda: True)
        self.assertIn('尚未重新核验', detail.text)
        self.assertEqual(detail.provenance['thread_id'], unheard['source_thread_id'])
        for command in (
            ReportCommand('continue', 1),
            voice.command('继续第一项，下一步：检查构建结果'),
        ):
            result = voice.answer(command, lambda: True)
            self.assertEqual(result.operation, 'clarification')
            self.assertIsNone(result.provenance)
            self.assertIn('请重新读取原来源', result.text)
            self.assertNotIn('检查构建结果', result.text)
        self.assertTrue(voice.remember(selected, original))
        handoff = voice.answer(voice.command('继续第二项，下一步：检查构建结果'), lambda: True)
        self.assertEqual(handoff.operation, 'desktop_handoff')
        self.assertIn('检查构建结果', handoff.text)
        self.assertIn(unheard['item_id'], handoff.text)

    def test_numbered_detail_uses_frozen_real_chat_provenance_not_a_fake_collection_thread(self):
        f = GroupFixture()
        selected = scope()
        report = self.read(f, selected)
        context = (selected, object(), 0)
        voice = ReportVoice(
            ReportVoiceHooks(
                threading.RLock(), lambda: context, lambda *a: self.fail('Detail re-read sources')
            ),
            clock=lambda: 0,
        )
        self.assertTrue(voice.remember(selected, report))
        report['items'].reverse()
        reply = voice.answer(ReportCommand('item', 1), lambda: True)
        self.assertEqual(reply.provenance['thread_id'], 'chat-b')
        self.assertIn('chat-b', reply.text)
        handoff = voice.answer(ReportCommand('continue', 1), lambda: True)
        self.assertEqual(handoff.provenance['thread_id'], 'chat-b')
        self.assertEqual(handoff.source_title, '项目 chat-b')
        self.assertIn('待桌面接续', handoff.text)
        self.assertIsNone(report_source(selected))

    def test_unregistered_or_spoofed_card_source_cannot_enter_the_snapshot(self):
        selected = scope()
        original = self.read(GroupFixture(), selected)
        for changes in (
            dict(source_thread_id='unregistered'),
            dict(source_title='spoofed'),
            dict(source_host='remote'),
        ):
            report = deepcopy(original)
            report['items'][0].update(changes)
            self.assertFalse(ReportVoice.valid_report(selected, report))

    def test_read_but_partial_source_is_distinct_from_unavailable_and_zero(self):
        fixture = GroupFixture()
        fixture.partial.add('chat-a')
        selected = scope()
        report = self.read(fixture, selected)
        self.assertEqual(report['unavailable_sources'], 0)
        self.assertEqual(len(report['items']), 2)
        self.assertFalse(report['coverage_complete'])
        self.assertIn('已读取（覆盖不完整）', report_text(report))
        self.assertTrue(ReportVoice.valid_report(selected, report))

    def test_result_cap_is_stable_visible_and_never_claims_complete_coverage(self):
        fixture = GroupFixture()
        fixture.count = 11
        selected = scope()
        report = self.read(fixture, selected)
        again = self.read(fixture, selected)
        self.assertEqual(len(report['items']), 20)
        self.assertTrue(report['items_truncated'])
        self.assertFalse(report['coverage_complete'])
        self.assertEqual(
            [key(row) for row in report['items']], [key(row) for row in again['items']]
        )
        self.assertIn('仍有未展示结果', report_text(report))
        self.assertTrue(ReportVoice.valid_report(selected, report))


if __name__ == '__main__':
    unittest.main()

"""Actual protocol/controller admission, metadata consent and source ownership."""

import json
from dataclasses import replace
import os
import stat
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from dot_handoff import ChatCatalog, Handoff
from dot_client import Cancelled, DotError
from report_sources import ReportSources, ReportScope
from report_voice import ReportCommand
from protocol import validate_device_event
import test_dot_handoff as old
from test_report_sources import entry, GroupFixture


class SourcesControllerTests(unittest.TestCase):
    def session(self, folder):
        self.helper = old.ControllerTests()
        bridge = self.helper.session()
        bridge.chats.sources = ReportSources(Path(folder) / 'sources.json')
        self.catalog = old.CatalogFixture()
        bridge.chats.catalog = ChatCatalog(self.catalog.factory)
        return bridge

    def request(self, bridge, action, *, nonce=None, wait=True, **fields):
        request = dict(
            event='dot_chat_request', request_id=nonce or uuid.uuid4().hex, action=action, **fields
        )
        validate_device_event(request)
        bridge.on_event(request)
        if wait and bridge.chats.worker is not None:
            bridge.chats.worker.join(3)
            self.assertFalse(bridge.chats.worker.is_alive())
        return request['request_id']

    def test_metadata_registration_does_not_read_history_change_brain_or_start_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            self.request(b, 'sources')
            self.assertFalse((Path(folder) / 'sources.json').exists())
            self.request(b, 'source_candidates')
            self.request(b, 'register_source', revision=0, source_id='chat-2', confirmed=True)
            self.assertTrue(b.events[-1][1]['ok'])
            self.assertEqual(b.chats.sources.snapshot()['items'][0]['thread_id'], 'chat-2')
            self.assertEqual({method for method, _ in self.catalog.calls}, {'thread/list'})
            self.assertIsNone(b.chats.selection)
            self.assertIsNone(b.brain.dot.handoff)
            self.assertFalse(b.session.active)
            self.assertNotIn('HIDDEN', json.dumps(b.events, ensure_ascii=False))

    def test_registration_requires_paused_capture_current_offer_revision_and_unique_nonce(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            self.request(b, 'source_candidates')
            b.session.active = True
            self.request(b, 'register_source', revision=0, source_id='chat-1', confirmed=True)
            self.assertFalse(b.events[-1][1]['ok'])
            self.assertEqual(b.chats.sources.snapshot()['items'], [])
            b.session.active = False
            self.request(b, 'register_source', revision=0, source_id='unoffered', confirmed=True)
            self.assertFalse(b.events[-1][1]['ok'])
            nonce = self.request(
                b, 'register_source', revision=0, source_id='chat-1', confirmed=True
            )
            raw = (Path(folder) / 'sources.json').read_bytes()
            self.request(b, 'clear_sources', revision=1, confirmed=True, nonce=nonce)
            self.assertFalse(b.events[-1][1]['ok'])
            self.assertEqual((Path(folder) / 'sources.json').read_bytes(), raw)
            self.request(b, 'clear_sources', revision=0, confirmed=True)
            self.assertFalse(b.events[-1][1]['ok'])
            self.assertEqual((Path(folder) / 'sources.json').read_bytes(), raw)

    def test_protocol_rejects_missing_consent_extra_data_and_wrong_field_types(self):
        base = dict(
            event='dot_chat_request',
            request_id='a' * 32,
            action='register_source',
            revision=0,
            source_id='chat-a',
            confirmed=True,
        )
        for changes in (
            dict(confirmed=False),
            dict(confirmed=1),
            dict(revision=True),
            dict(text='PRIVATE_HISTORY'),
            dict(source_id='../other'),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_device_event(dict(base, **changes))
        for action in ([], {}, True, None):
            with self.assertRaises(ValueError):
                validate_device_event(dict(base, action=action))

    def test_text_owner_and_pending_wake_keep_registry_mutations_out_of_startup_gaps(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            self.request(b, 'source_candidates')
            b.capture.endpoint.hooks = replace(
                b.capture.endpoint.hooks, complete_chinese_pending=lambda ticket: True
            )
            for owner in ('text', 'wake'):
                b.session.text_generation = b.session.generation if owner == 'text' else None
                b.capture.endpoint.complete_wake.vad.talking = owner == 'wake'
                self.request(b, 'register_source', revision=0, source_id='chat-1', confirmed=True)
                self.assertFalse(b.events[-1][1]['ok'])
                self.assertFalse((Path(folder) / 'sources.json').exists())

    def test_unrelated_registration_preserves_the_frozen_scope_and_unheard_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            store = b.chats.sources
            store.apply('register', 0, entry=entry('chat-a'))
            self.request(b, 'use_sources', revision=1)
            frozen = b.chats.report_source()
            marker = object()
            b.chats.voice.progress = marker
            self.request(b, 'source_candidates')
            self.request(b, 'register_source', revision=1, source_id='chat-1', confirmed=True)
            self.assertIs(b.chats.report_source(), frozen)
            self.assertIs(b.chats.voice.progress, marker)
            self.assertTrue(b.chats.scope_current(frozen))
            self.request(
                b,
                'delete_source',
                revision=2,
                registration_id=store.snapshot()['items'][0]['id'],
                confirmed=True,
            )
            self.assertIsNone(b.chats.report_scope)
            self.assertIsNone(b.chats.voice.progress)

    def test_scope_is_explicit_ram_only_and_does_not_replace_summary_handoff(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            store = b.chats.sources
            store.apply('register', 0, entry=entry())
            handoff = Handoff('summary-only', '摘要聊天', (), False)
            b.chats.selection = handoff
            self.request(b, 'use_sources', revision=1)
            source = b.chats.report_source()
            self.assertIsInstance(source, ReportScope)
            self.assertIs(b.chats.selection, handoff)
            self.assertIsNone(b.brain.dot.handoff)
            self.assertEqual(b.chats.state()['report_scope'], 'registered')
            with patch.object(b.chats.voice, 'clear') as clear:
                self.request(b, 'use_sources', revision=1)
                self.assertIs(b.chats.report_source(), source)
                clear.assert_not_called()
            self.request(b, 'use_selected_report')
            self.assertIs(b.chats.report_source(), handoff)
            self.assertEqual(store.snapshot()['revision'], 1)
            fresh = self.session(folder)
            self.assertIsNone(fresh.chats.report_scope)
            self.assertEqual(len(fresh.chats.sources.snapshot()['items']), 1)

    def test_selected_chat_preview_does_not_read_registered_scope_or_replace_its_snapshot(self):
        from test_work_reports import Fixture
        from work_reports import ReportWindow, WorkReportReader

        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            b.chats.sources.apply('register', 0, entry=entry())
            selected = Handoff('selected-chat', '所选聊天', (), False)
            b.chats.selection = selected
            self.request(b, 'use_sources', revision=1)
            scope = b.chats.report_scope
            sentinel = object()
            b.chats.display_snapshot.value = sentinel
            report = WorkReportReader(Fixture().factory).read(selected, ReportWindow(100, 200))
            revision = b.chats.voice.revision
            with patch.object(b.chats.catalog, 'report', return_value=report) as read:
                self.request(b, 'report', period='yesterday', report_scope='selected_chat')
            self.assertIs(read.call_args.args[0], selected)
            self.assertIs(b.chats.report_scope, scope)
            self.assertIs(b.chats.display_snapshot.value, sentinel)
            self.assertEqual(b.chats.voice.revision, revision)
            self.assertIs(b.events[-1][1]['ok'], True)
            self.assertIs(b.events[-1][1]['report_snapshot_adopted'], False)
            self.assertIn('本机 Codex 原聊天：selected-chat', b.events[-1][1]['report_text'])

    def test_cancel_before_metadata_commit_discards_the_late_offer(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            self.request(b, 'source_candidates')
            entered, release = threading.Event(), threading.Event()
            original = b.chats.catalog.registration

            def slow(ident):
                entered.set()
                self.assertTrue(release.wait(3))
                return original(ident)

            with patch.object(b.chats.catalog, 'registration', side_effect=slow):
                nonce = self.request(
                    b, 'register_source', revision=0, source_id='chat-1', confirmed=True, wait=False
                )
                self.assertTrue(entered.wait(3))
                b.chats.cancel(nonce)
                count = len(b.events)
                release.set()
                b.chats.worker.join(3)
                self.assertFalse(b.chats.worker.is_alive())
                self.assertEqual(len(b.events), count)
            self.assertFalse((Path(folder) / 'sources.json').exists())

    def test_real_group_reader_runs_under_ui_controller_and_fixed_voice_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            store = b.chats.sources
            store.apply('register', 0, entry=entry('chat-a'))
            store.apply('register', 1, entry=entry('chat-b'))
            self.request(b, 'use_sources', revision=2)
            fixture = GroupFixture()
            fixture.completed = int(time.time()) - 10
            b.chats.catalog = ChatCatalog(fixture.factory)
            self.request(b, 'report', period='half_hour')
            value = b.events[-1][1]
            self.assertTrue(value['ok'])
            self.assertTrue(value['report_snapshot_adopted'])
            self.assertEqual({p['threadId'] for _, p, _ in fixture.calls}, {'chat-a', 'chat-b'})
            self.assertEqual(len(value['report']['items']), 2)
            self.assertIsNotNone(b.chats.voice.snapshot)
            self.assertFalse(b.session.active)
            self.assertIsNone(b.brain.dot.handoff)

    def test_external_deregistration_cancels_before_more_history_or_next_source(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            store = b.chats.sources
            store.apply('register', 0, entry=entry('chat-a'))
            store.apply('register', 1, entry=entry('chat-b'))
            self.request(b, 'use_sources', revision=2)
            fixture = GroupFixture()
            fixture.after_request = lambda: store.apply('clear', 2)
            b.chats.catalog = ChatCatalog(fixture.factory)
            with self.assertRaises(Cancelled):
                b.chats.read_voice_report(b.chats.report_source(), 'today', lambda: True)
            self.assertEqual(len(fixture.calls), 1)
            self.assertFalse(b.chats.busy)
            self.assertTrue(all(c.closed for c in fixture.clients))

    def test_numbered_queue_voice_followup_reads_only_the_committed_card_source(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            store = b.chats.sources
            store.apply('register', 0, entry=entry('chat-a'))
            store.apply('register', 1, entry=entry('chat-b'))
            self.request(b, 'use_sources', revision=2)
            fixture = GroupFixture()
            fixture.completed = int(time.time()) - 10
            b.chats.catalog = ChatCatalog(fixture.factory)
            self.request(b, 'report', period='half_hour')
            snapshot = b.chats.voice.snapshot
            wanted = snapshot[2]['items'][1]['source_thread_id']
            calls, closed = [], []
            failure = None

            class QueueRpc:
                def start(self, *, timeout):
                    assert 0 < timeout <= 20

                def request(self, method, params, **kwargs):
                    calls.append((method, params))
                    if failure == 'unavailable' or failure == 'partial' and params.get('cursor'):
                        raise DotError('PRIVATE_RPC_ERROR')
                    if failure == 'revoke':
                        store.apply('clear', 2)
                    items = (
                        []
                        if failure == 'empty'
                        else [
                            dict(
                                id='queue-id',
                                clientUserMessageId='client-id',
                                input=[
                                    dict(type='text', text='PRIVATE_QUEUE_BODY'),
                                    dict(type='image', url='PRIVATE_ATTACHMENT'),
                                ],
                            )
                        ]
                    )
                    return dict(data=items, nextCursor='next' if failure == 'partial' else None)

                def close(self):
                    closed.append(True)

            b.chats.catalog.rpc_factory = QueueRpc
            command = b.chats.voice.command('大黄蜂，查看这次汇报的第二项待办队列')
            self.assertEqual(command, ReportCommand('item_queue', 2))
            self.assertIn(
                '没有该编号',
                b.chats.voice.answer(ReportCommand('item_queue', 3), lambda: True).text,
            )
            self.assertEqual(calls, [])
            for cause, text in (
                (None, '看到1条排队请求'),
                ('empty', '看到0条排队请求'),
                ('partial', '至少看到1条排队请求'),
                ('unavailable', '未能确认待办队列'),
            ):
                with (
                    self.subTest(cause=cause),
                    patch.object(
                        b.brain.dot, 'clauses', side_effect=AssertionError('Must stay local')
                    ),
                ):
                    failure = cause
                    events = []
                    spoken = list(
                        b.brain.clauses(
                            '查看这次汇报的第二项待办队列',
                            [],
                            lambda: True,
                            lambda kind, data: events.append((kind, data)),
                        )
                    )
                    self.assertIn(text, ''.join(spoken))
                    self.assertIn(wanted, dict(events)['dot_result']['text'])
                    self.assertNotIn('PRIVATE_', str((spoken, events)))
                    self.assertIs(b.chats.voice.snapshot, snapshot)
                    self.assertFalse(b.chats.busy)
            self.assertEqual(len(closed), 4)
            self.assertTrue(
                all(
                    method == 'thread/queue/list' and p['threadId'] == wanted for method, p in calls
                )
            )
            failure = 'revoke'
            with self.assertRaises(Cancelled):
                b.chats.voice.answer(command, lambda: True)
            self.assertFalse(b.chats.busy)
            self.assertEqual(len(closed), 5)

    def test_uncertain_delete_revokes_old_numbered_body_without_retrying_the_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            store = b.chats.sources
            original = store.apply('register', 0, entry=entry('chat-a'))
            self.request(b, 'use_sources', revision=1)
            fixture = GroupFixture()
            fixture.completed = int(time.time()) - 10
            b.chats.catalog = ChatCatalog(fixture.factory)
            self.request(b, 'report', period='half_hour')
            self.assertIsNotNone(b.chats.voice.snapshot)
            real_fsync = os.fsync

            def fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError('injected directory sync failure')
                real_fsync(fd)

            with patch('os.fsync', side_effect=fsync):
                self.request(
                    b,
                    'delete_source',
                    revision=1,
                    registration_id=original['items'][0]['id'],
                    confirmed=True,
                )
            self.assertFalse(b.events[-1][1]['ok'])
            self.assertEqual(store.snapshot()['revision'], 2)
            self.assertEqual(store.snapshot()['items'], [])
            self.assertIsNone(b.chats.voice.snapshot)
            reply = b.chats.voice.answer(ReportCommand('item', 1), lambda: True)
            self.assertEqual(reply.operation, 'clarification')
            self.assertNotIn('chat-a 完成', reply.text)


if __name__ == '__main__':
    unittest.main()

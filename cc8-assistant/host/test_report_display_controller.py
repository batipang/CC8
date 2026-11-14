"""Exercise actual controller/protocol/frame boundaries; no real chats or device media."""

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from protocol import Packet, validate_device_event
from report_archive import ReportArchive, historical_text
from report_voice import report_text
from test_report_voice import SOURCE, report
import test_dot_handoff as sessions


class DisplayControllerTests(unittest.TestCase):
    def session(self):
        b = sessions.ControllerTests().session()
        b.chats.selection = SOURCE
        return b

    def request(self, b, action, **fields):
        data = dict(event='dot_chat_request', request_id=uuid.uuid4().hex, action=action, **fields)
        validate_device_event(data)
        before = len(b.events)
        b.on_event(data)
        b.chats.worker.join(3)
        self.assertFalse(b.chats.worker.is_alive())
        self.assertGreater(len(b.events), before)
        result = b.events[-1][1]
        packet = Packet.event('dot_chat_state', **result)
        packet.encode()
        packet.bind_route('b' * 32).encode()
        return result

    def read(self, b):
        value = report(*(['😀' * 500] * 20))
        with patch.object(b.chats.catalog, 'report', return_value=value):
            result = self.request(b, 'report', period='today')
        self.assertTrue(result['ok'])
        self.assertGreater(result['report_pages'], 1)
        return value, result

    def test_pages_reconstruct_full_server_snapshot_without_rpc_or_ledger_changes(self):
        b = self.session()
        value, first = self.read(b)
        original = b.chats.voice.snapshot
        ledger = b.chats.voice.progress
        deadline = b.chats.display_snapshot.value[1]
        parts = []
        with patch.object(b.chats.catalog, 'report', side_effect=AssertionError('No new RPC')):
            for page in range(first['report_pages']):
                fields = self.request(b, 'report_page', snapshot_id=value['snapshot_id'], page=page)
                self.assertTrue(fields['ok'])
                parts.append(fields['report_text'][len(fields['report_page_marker']) :])
        self.assertEqual(''.join(parts), report_text(value, include_identity=True))
        self.assertIn(f'本机 Codex 原聊天：{SOURCE.source_id}', ''.join(parts))
        self.assertIn('回合：turn-19；结果：final-19', ''.join(parts))
        self.assertEqual(b.chats.voice.snapshot, original)
        self.assertIs(b.chats.voice.progress, ledger)
        self.assertEqual(b.chats.display_snapshot.value[1], deadline)
        self.assertFalse(b.session.active)
        self.assertEqual(b.chats.voice.snapshot[2], value)

    def test_old_context_routes_voice_revisions_expiry_and_unknown_ids_cannot_page(self):
        for changed in ('route', 'generation', 'source', 'voice', 'expired', 'unknown'):
            with self.subTest(changed=changed):
                b = self.session()
                value, first = self.read(b)
                ident = value['snapshot_id']
                if changed == 'route':
                    b.session.connection = object()
                elif changed == 'generation':
                    b.session.generation += 1
                elif changed == 'source':
                    from dataclasses import replace

                    b.chats.selection = replace(SOURCE)  # Equal value is still a different owner.
                elif changed == 'voice':
                    b.chats.voice.forget()
                    self.assertIsNone(b.chats.display_snapshot.value)
                elif changed == 'expired':
                    b.chats.display_snapshot.clock = lambda: float('inf')
                else:
                    ident = 'f' * 32
                fields = self.request(b, 'report_page', snapshot_id=ident, page=1)
                self.assertFalse(fields['ok'])
                self.assertNotIn('report', fields)

    def test_new_read_revokes_previous_display_even_when_reader_fails(self):
        b = self.session()
        value, _ = self.read(b)
        with patch.object(b.chats.catalog, 'report', side_effect=RuntimeError('PRIVATE')):
            fields = self.request(b, 'report', period='today')
        self.assertFalse(fields['ok'])
        fields = self.request(b, 'report_page', snapshot_id=value['snapshot_id'], page=0)
        self.assertFalse(fields['ok'])
        self.assertNotIn('PRIVATE', str(fields))

    def test_external_registered_member_removal_revokes_cached_pages(self):
        from report_sources import ReportSources, ReportScope
        from group_work_reports import GroupWorkReports
        from test_report_sources import entry, GroupFixture
        from work_reports import ReportWindow

        with tempfile.TemporaryDirectory() as folder:
            b = self.session()
            b.chats.sources = ReportSources(Path(folder) / 'sources.json')
            state = b.chats.sources.apply('register', 0, entry=entry())
            b.chats.report_scope = source = ReportScope.from_state(state)
            value = GroupWorkReports(GroupFixture().factory).read(source, ReportWindow(100, 200))
            with patch.object(b.chats.catalog, 'report', return_value=value):
                fields = self.request(b, 'report', period='today')
            self.assertTrue(fields['ok'])
            b.chats.sources.apply('delete', 1, ident=state['items'][0]['id'])
            fields = self.request(b, 'report_page', snapshot_id=value['snapshot_id'], page=0)
            self.assertFalse(fields['ok'])
            self.assertIsNone(b.chats.display_snapshot.value)
            self.assertNotIn('report_text', fields)

    def test_archive_keeps_complete_text_pages_are_revision_bound_and_readonly(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session()
            b.chats.archive = ReportArchive(Path(folder) / 'saved.json')
            value, first = self.read(b)
            fields = self.request(
                b,
                'save_report',
                snapshot_id=value['snapshot_id'],
                revision=first['archive_revision'],
                confirmed=True,
            )
            self.assertTrue(fields['ok'])
            item = b.chats.archive.snapshot()['items'][0]
            self.assertEqual(item['text'], report_text(value))
            raw = b.chats.archive.path.read_bytes()
            parts = []
            fields = self.request(b, 'saved_report', saved_id=item['id'])
            self.assertGreater(fields['report_pages'], 1)
            for page in range(fields['report_pages']):
                fields = self.request(b, 'saved_report', saved_id=item['id'], revision=1, page=page)
                self.assertTrue(fields['ok'])
                parts.append(fields['saved_report_text'][len(fields['report_page_marker']) :])
            self.assertEqual(''.join(parts), historical_text(item))
            self.assertEqual(b.chats.archive.path.read_bytes(), raw)
            self.request(b, 'delete_report', saved_id=item['id'], revision=1, confirmed=True)
            fields = self.request(b, 'saved_report', saved_id=item['id'], revision=1, page=1)
            self.assertFalse(fields['ok'])
            self.assertNotIn('saved_report_text', fields)

    def test_protocol_page_cannot_submit_bodies_paths_or_implicit_revisions(self):
        live = dict(
            event='dot_chat_request',
            action='report_page',
            request_id='a' * 32,
            snapshot_id='b' * 32,
            page=1,
        )
        saved = dict(
            event='dot_chat_request',
            action='saved_report',
            request_id='a' * 32,
            saved_id='b' * 32,
            page=1,
            revision=0,
        )
        for base in (live, saved):
            for changes in (
                dict(page=True),
                dict(page=-1),
                dict(page=16),
                dict(text='PRIVATE'),
                dict(path='/tmp/a'),
            ):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    validate_device_event(dict(base, **changes))
        for changes in (dict(revision=True), dict(revision=-1)):
            with self.assertRaises(ValueError):
                validate_device_event(dict(saved, **changes))
        for field in ('revision', 'page'):
            invalid = dict(saved)
            del invalid[field]
            with self.assertRaises(ValueError):
                validate_device_event(invalid)

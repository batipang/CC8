"""Actual wire/controller ownership for explicit historical saves, not fixture-only stores."""

from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from report_archive import ReportArchive
from protocol import validate_device_event
import test_dot_handoff as sessions
import test_work_reports as reports


class ArchiveControllerTests(unittest.TestCase):
    def session(self, folder, fixture=None):
        b = sessions.ControllerTests().session()
        b.chats.archive = ReportArchive(Path(folder) / 'reports.json')
        b.chats.selection = reports.ReportTests.source
        r = reports.ReportTests().report(fixture or reports.Fixture())
        with patch.object(b.chats.catalog, 'report', return_value=r):
            self.request(b, 'report', period='today')
        self.assertTrue(b.events[-1][1]['ok'])
        self.snapshot_id = r['snapshot_id']
        return b

    def request(self, b, action, *, nonce=None, wait=True, **fields):
        data = dict(
            event='dot_chat_request', action=action, request_id=nonce or uuid.uuid4().hex, **fields
        )
        validate_device_event(data)
        b.on_event(data)
        if wait and b.chats.worker is not None:
            b.chats.worker.join(3)
            self.assertFalse(b.chats.worker.is_alive())
        return data

    def save(self, b, **kwargs):
        return self.request(
            b, 'save_report', revision=0, confirmed=True, snapshot_id=self.snapshot_id, **kwargs
        )

    def test_explicit_current_snapshot_saved_restart_list_detail_delete_without_rpc_or_voice_restore(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            progress = b.chats.voice.progress
            with patch.object(b.chats.catalog, 'report', side_effect=AssertionError('No RPC')):
                self.save(b)
                self.assertTrue(b.events[-1][1]['ok'])
                state = b.chats.archive.snapshot()
                ident = state['items'][0]['id']
                self.assertIs(b.chats.voice.progress, progress)
                self.request(b, 'saved_report', saved_id=ident)
                self.assertIn('非实时结果', b.events[-1][1]['saved_report_text'])
                self.assertFalse(b.session.active)
            new = sessions.ControllerTests().session()
            new.chats.archive = ReportArchive(Path(folder) / 'reports.json')
            self.request(new, 'saved_reports')
            self.assertEqual(len(new.events[-1][1]['saved_reports']['items']), 1)
            self.assertIsNone(new.chats.selection)
            self.assertIsNone(new.chats.voice.snapshot)
            self.assertIsNone(new.chats.voice.progress)
            self.assertFalse(new.session.active)
            self.request(new, 'delete_report', saved_id=ident, revision=1, confirmed=True)
            self.assertEqual(new.chats.archive.snapshot()['items'], [])

    def test_unknown_expired_cleared_context_and_capture_busy_cannot_save(self):
        for change in ('unknown', 'expired', 'forgotten', 'route', 'generation', 'active', 'busy'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                b = self.session(folder)
                if change == 'unknown':
                    self.snapshot_id = 'f' * 32
                elif change == 'expired':
                    b.chats.voice.clock = lambda: float('inf')
                elif change == 'forgotten':
                    b.chats.voice.forget()
                elif change == 'route':
                    b.session.connection = object()
                elif change == 'generation':
                    b.session.generation += 1
                elif change == 'active':
                    b.session.active = True
                elif change == 'busy':
                    b.response.busy = True
                self.save(b)
                self.assertEqual(b.chats.archive.snapshot()['items'], [])

    def test_protocol_cannot_supply_content_path_or_implicit_consent(self):
        data = dict(
            event='dot_chat_request',
            request_id='a' * 32,
            action='save_report',
            revision=0,
            confirmed=True,
            snapshot_id='b' * 32,
        )
        for changes in (
            dict(confirmed=1),
            dict(confirmed=False),
            dict(revision=True),
            dict(snapshot_id='../x'),
            dict(text='PRIVATE'),
            dict(path='/tmp/x'),
            dict(include_receipts=1),
            dict(include_receipts=False),
            dict(include_receipts=True, execution_receipts=['injected']),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_device_event(dict(data, **changes))
        for action in ('delete_report', 'clear_reports'):
            wrong = dict(data, action=action)
            with self.assertRaises(ValueError):
                validate_device_event(wrong)

    def test_receipts_require_explicit_current_save_and_survive_restart(self):
        fixture = reports.Fixture()
        fixture.item_pages['turn-1'][0]['data'].append(
            dict(
                turnId='turn-1',
                item=dict(
                    id='patch-123', type='fileChange', status='failed', changes=['PRIVATE_DIFF']
                ),
            )
        )
        for consent in ({}, {'include_receipts': True}):
            with self.subTest(consent=consent), tempfile.TemporaryDirectory() as folder:
                b = self.session(folder, fixture)
                live = b.events[-1][1]
                self.assertIs(live['archive_receipts_supported'], True)
                self.assertNotIn('patch-123', str(live))
                self.save(b, **consent)
                self.assertTrue(b.events[-1][1]['ok'])
                archive = ReportArchive(Path(folder) / 'reports.json')
                state = archive.snapshot()
                self.assertEqual(state['schema'], 1)
                ident = state['items'][0]['id']
                restarted = sessions.ControllerTests().session()
                restarted.chats.archive = archive
                with patch.object(restarted.chats.catalog, 'report', side_effect=AssertionError):
                    self.request(restarted, 'saved_report', saved_id=ident)
                body = restarted.events[-1][1]['saved_report_text']
                self.assertEqual('文件变更失败 · patch-123' in body, bool(consent))
                self.assertNotIn('PRIVATE_DIFF', body)
                self.assertIn('非实时结果', body)
                self.assertIsNone(restarted.chats.voice.snapshot)

    def test_duplicate_nonce_and_stale_revision_cannot_delete_saved_records(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            data = self.save(b)
            raw = b.chats.archive.path.read_bytes()
            self.request(b, 'clear_reports', revision=1, confirmed=True, nonce=data['request_id'])
            self.assertFalse(b.events[-1][1]['ok'])
            self.assertEqual(b.chats.archive.path.read_bytes(), raw)
            self.request(b, 'clear_reports', revision=0, confirmed=True)
            self.assertFalse(b.events[-1][1]['ok'])
            self.assertEqual(b.chats.archive.path.read_bytes(), raw)

    def test_owner_cancelled_after_admission_before_commit_does_not_write(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            original = b.chats.archive.apply

            def cancelled(*args, **kwargs):
                b.session.connection = object()
                return original(*args, **kwargs)

            with patch.object(b.chats.archive, 'apply', side_effect=cancelled):
                self.save(b)
            self.assertEqual(b.chats.archive.snapshot()['items'], [])

    def test_corrupt_archive_does_not_poison_valid_live_report_or_expose_bad_body(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            b.chats.archive.path.write_text('{PRIVATE-corrupt')
            b.chats.archive.path.chmod(0o600)
            r = reports.ReportTests().report(reports.Fixture())
            with patch.object(b.chats.catalog, 'report', return_value=r):
                self.request(b, 'report', period='today')
            fields = b.events[-1][1]
            self.assertTrue(fields['ok'])
            self.assertNotIn('archive_revision', fields)
            self.assertIn('report_text', fields)
            self.assertNotIn('PRIVATE-corrupt', str(fields))

    def test_commit_current_callback_rechecks_snapshot_expiry_and_registered_membership(self):
        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            original = b.chats.archive.apply

            def expired(*args, **kwargs):
                b.chats.voice.forget()
                return original(*args, **kwargs)

            with patch.object(b.chats.archive, 'apply', side_effect=expired):
                self.save(b)
            self.assertEqual(b.chats.archive.snapshot()['items'], [])

    def test_removed_registered_source_cannot_be_saved_from_an_old_report_window(self):
        from report_sources import ReportSources, ReportScope
        from test_report_sources import entry, GroupFixture
        from group_work_reports import GroupWorkReports
        from work_reports import ReportWindow

        with tempfile.TemporaryDirectory() as folder:
            b = self.session(folder)
            b.chats.sources = ReportSources(Path(folder) / 'sources.json')
            state = b.chats.sources.apply('register', 0, entry=entry('chat-a'))
            source = ReportScope.from_state(state)
            b.chats.report_scope = source
            report = GroupWorkReports(GroupFixture().factory).read(source, ReportWindow(100, 200))
            with patch.object(b.chats.catalog, 'report', return_value=report):
                self.request(b, 'report', period='today')
            self.assertTrue(b.events[-1][1]['ok'])
            self.snapshot_id = report['snapshot_id']
            original = b.chats.archive.apply

            def removed(*args, **kwargs):
                b.chats.sources.apply('delete', 1, ident=state['items'][0]['id'])
                return original(*args, **kwargs)

            with patch.object(b.chats.archive, 'apply', side_effect=removed):
                self.save(b)
            self.assertEqual(b.chats.archive.snapshot()['items'], [])

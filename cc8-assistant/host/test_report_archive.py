"""Actual private files, revision races and durability failures; no live chat or media."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from report_archive import (
    ReportArchive,
    archived_receipts,
    record,
    validate,
    historical_text,
    LIMIT,
)
from reminder_store import ReminderCommitUncertain
import test_work_reports as reports
from test_report_sources import scope, GroupFixture
from group_work_reports import GroupWorkReports
from work_reports import ReportWindow


def snapshot():
    return record(
        reports.ReportTests.source,
        reports.ReportTests().report(reports.Fixture()),
        clock=lambda: 500,
    )


class ArchiveTests(unittest.TestCase):
    def test_absent_is_lazy_and_explicit_save_restart_get_delete_do_not_execute(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private' / 'reports.json'
            store = ReportArchive(path)
            self.assertEqual(store.snapshot(), dict(schema=1, revision=0, items=[]))
            self.assertFalse(path.parent.exists())
            item = snapshot()
            state = store.apply('save', 0, item=item)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            item['text'] = 'MUTATED'
            restored = ReportArchive(path).snapshot()
            self.assertEqual(state, restored)
            body = historical_text(restored['items'][0])
            self.assertIn('非实时结果', body)
            self.assertIn('selected-chat', body)
            self.assertIn('不会恢复', body)
            restored['items'].clear()
            self.assertEqual(len(store.snapshot()['items']), 1)
            store.apply('delete', 1, ident=state['items'][0]['id'])
            self.assertEqual(store.snapshot()['items'], [])

    def test_projection_never_retains_extra_raw_fields_and_group_keeps_source_namespaces(self):
        fixture = reports.Fixture()
        report = reports.ReportTests().report(fixture)
        report['raw_chat'] = 'PRIVATE_FULL_HISTORY'
        report['items'][0]['raw_tools'] = 'PRIVATE_TOOL_OUTPUT'
        item = record(reports.ReportTests.source, report)
        self.assertNotIn('PRIVATE_', json.dumps(item))
        grouped = GroupWorkReports(GroupFixture().factory).read(scope(), ReportWindow(100, 200))
        item = record(scope(), grouped)
        self.assertEqual(len(item['sources']), 2)
        self.assertEqual({r['thread_id'] for r in item['index']}, {'chat-a', 'chat-b'})
        self.assertEqual(
            len({(r['thread_id'], r['turn_id'], r['item_id']) for r in item['index']}), 2
        )

    def test_receipt_appendix_capacity_preserves_summary_and_whole_result_blocks(self):
        r = {
            'source_id': 'chat-c',
            'items': [
                dict(
                    turn_id=f'turn-{index}',
                    item_id=f'answer-{index}',
                    execution_receipts=[
                        dict(item_id=f'patch-{index}' + 'x' * 84, outcome='file_change_failures')
                    ],
                )
                for index in (1, 2)
            ],
        }
        prefix = '汉' * 23750
        text = archived_receipts(prefix, r)
        self.assertTrue(text.startswith(prefix))
        self.assertLessEqual(len(text), 24000)
        self.assertIn('原聊天：chat-c；回合：turn-1；结果：answer-1', text)
        self.assertIn('文件变更失败 · patch-1' + 'x' * 84, text)
        self.assertNotIn('patch-2', text)
        self.assertIn('因快照长度限制未保存', text)

    def test_revision_current_duplicate_unknown_and_spoofed_index_preserve_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'reports.json'
            store = ReportArchive(path)
            item = snapshot()
            state = store.apply('save', 0, item=item)
            raw = path.read_bytes()
            for revision in (0, True, -1, 2**31):
                with self.assertRaises(ValueError):
                    store.apply('clear', revision)
                self.assertEqual(path.read_bytes(), raw)
            with self.assertRaises(ValueError):
                store.apply('clear', 1, current=lambda: False)
            with self.assertRaises(ValueError):
                store.apply('save', 1, item=item)
            for transform in (
                lambda r: r.update(raw_history='PRIVATE'),
                lambda r: r['index'][0].update(thread_id='other-source'),
                lambda r: r['index'][0].update(completed_at=True),
                lambda r: r.update(saved_at=True),
                lambda r: r.update(text='bad\x00text'),
                lambda r: r['sources'].append(deepcopy(r['sources'][0])),
            ):
                wrong = deepcopy(state)
                transform(wrong['items'][0])
                with self.assertRaises(ValueError):
                    validate(wrong)
            self.assertEqual(path.read_bytes(), raw)

    def test_real_cross_instance_concurrency_has_one_winner_and_no_lost_write(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'reports.json'
            gate = threading.Barrier(2)
            outcomes = []

            def write():
                store = ReportArchive(path)
                item = snapshot()
                gate.wait()
                try:
                    store.apply('save', 0, item=item)
                    outcomes.append('saved')
                except ValueError:
                    outcomes.append('stale')

            threads = [threading.Thread(target=write) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(3)
                self.assertFalse(t.is_alive())
            self.assertCountEqual(outcomes, ['saved', 'stale'])
            self.assertEqual(len(ReportArchive(path).snapshot()['items']), 1)

    def test_uncertain_directory_sync_commits_once_and_never_silently_retries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'reports.json'
            store = ReportArchive(path)
            item = snapshot()
            original = os.fsync
            import stat

            def sync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError('PRIVATE_PATH')
                return original(fd)

            with patch('os.fsync', side_effect=sync), self.assertRaises(ReminderCommitUncertain):
                store.apply('save', 0, item=item)
            self.assertEqual(len(store.snapshot()['items']), 1)
            with self.assertRaises(ValueError):
                store.apply('save', 0, item=item)
            self.assertEqual(len(store.snapshot()['items']), 1)

    def test_count_and_byte_quotas_do_not_evict_previous_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'reports.json'
            store = ReportArchive(path)
            for i in range(LIMIT):
                store.apply('save', i, item=snapshot())
            raw = path.read_bytes()
            with self.assertRaises(ValueError):
                store.apply('save', LIMIT, item=snapshot())
            self.assertEqual(path.read_bytes(), raw)
            store.apply('clear', LIMIT)
            item = snapshot()
            item['text'] = '汉' * 24000
            for i in range(3):
                item['id'] = uuid.uuid4().hex
                item['snapshot_id'] = uuid.uuid4().hex
                store.apply('save', LIMIT + 1 + i, item=item)
            raw = path.read_bytes()
            item['id'] = uuid.uuid4().hex
            item['snapshot_id'] = uuid.uuid4().hex
            with self.assertRaises(ValueError):
                store.apply('save', LIMIT + 4, item=item)
            self.assertEqual(path.read_bytes(), raw)

    def test_unsafe_links_modes_malformed_duplicate_and_unknown_fields_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            path = folder / 'reports.json'
            store = ReportArchive(path)
            for raw in (
                '{"schema":1,"schema":1,"revision":0,"items":[]}',
                '{"schema":1,"revision":0,"items":[],"raw":"PRIVATE"}',
                '{broken',
            ):
                path.write_text(raw)
                path.chmod(0o600)
                with self.assertRaises(ValueError):
                    store.snapshot()
                self.assertEqual(path.read_text(), raw)
            path.unlink()
            target = folder / 'target'
            target.write_text('PRIVATE')
            target.chmod(0o600)
            path.symlink_to(target)
            with self.assertRaises((OSError, ValueError)):
                store.snapshot()
            path.unlink()
            os.link(target, path)
            with self.assertRaises((OSError, ValueError)):
                store.snapshot()
            path.unlink()
            store.apply('save', 0, item=snapshot())
            path.chmod(0o644)
            with self.assertRaises((OSError, ValueError)):
                store.snapshot()
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

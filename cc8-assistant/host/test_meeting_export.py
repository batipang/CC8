"""Real private export transactions and interruption receipts; synthetic text only."""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from meeting_asr import Transcript
from meeting_export import MeetingExports, CommitUncertain, render
from meeting_session import CaptureOwner, MeetingSession


class MeetingExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'exports'
        self.store = MeetingExports(self.path)

    def model(self, text='中英 English 😀'):
        model = MeetingSession('a' * 32)
        model.start(CaptureOwner(1, 1, 'b' * 32))
        model.receive(model.owner, 0, 0, b'\x00\x20' * 1600)
        chunk = model.end()[0]
        model.complete(chunk, Transcript(text, 100))
        return model, chunk.ident

    def save(self, model, format='json', **kwargs):
        return self.store.save(model, model.ident, model.revision, format, **kwargs)

    def content(self, item):
        return self.path / item['id'] / ('meeting.' + item['format'])

    def test_constructor_and_missing_directory_listing_are_passive(self):
        self.assertEqual(self.store.snapshot(), dict(items=[], unknown_files=False))
        self.assertFalse(self.path.exists())

    def test_complete_unicode_json_preserves_original_correction_gaps_and_no_pcm(self):
        model, row = self.model('原始 English 😀')
        model.edit(row, model.revision, '人工校对 English 😀')
        item = self.save(model)
        raw = self.content(item).read_bytes()
        value = json.loads(raw)['meeting']
        self.assertEqual(value['rows'][0]['text'], '人工校对 English 😀')
        self.assertEqual(value['rows'][0]['original'], '原始 English 😀')
        self.assertFalse(value['raw_audio_saved'])
        self.assertNotIn('pcm', raw.decode())
        self.assertEqual(item['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertTrue(item['integrity_confirmed'])
        for path, mode in (
            (self.path, 0o700),
            (self.path / item['id'], 0o700),
            (self.content(item), 0o600),
            (self.path / item['id'] / 'receipt.json', 0o600),
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
        self.assertEqual(MeetingExports(self.path).snapshot()['items'], [item])

    def test_text_exports_whole_content_and_uncertainty_without_invented_word_timing(self):
        model, row = self.model('完整文字' * 2000)
        model.edit(row, model.revision, '人工校对' * 2000)
        raw = render(model, model.ident, model.revision, 'txt').decode()
        self.assertIn('人工校对' * 2000, raw)
        self.assertIn('完整文字' * 2000, raw)
        self.assertIn('不是逐字时间戳', raw)
        model.rows[0].update(state='unconfirmed', reason='route_lost')
        raw = render(model, model.ident, model.revision, 'txt').decode()
        self.assertIn('[结果未确认]', raw)
        self.assertIn('route_lost', raw)
        self.assertIn('存在中断', raw)

    def test_pending_live_stale_revision_and_oversized_exports_never_create_directory(self):
        model, row = self.model()
        for state in ('capturing', 'paused'):
            model.state = state
            with self.assertRaises(ValueError):
                self.save(model)
        model.state = 'ended'
        model.rows[0]['state'] = 'pending'
        with self.assertRaises(ValueError):
            self.save(model)
        model.rows[0]['state'] = 'recognized'
        with self.assertRaises(ValueError):
            self.store.save(model, model.ident, model.revision - 1, 'json')
        with patch('meeting_export.MAX_BYTES', 8), self.assertRaises(ValueError):
            self.save(model)
        self.assertFalse(self.path.exists())

    def test_duplicate_is_idempotent_but_changed_content_is_never_overwritten(self):
        model, row = self.model()
        first = self.save(model)
        original = self.content(first).read_bytes()
        self.assertEqual(self.save(model), first)
        model.rows[0]['text'] = '模拟同修订不一致'
        with self.assertRaises(OSError):
            self.save(model)
        self.assertEqual(self.content(first).read_bytes(), original)
        model.edit(row, model.revision, '新的人工修订')
        self.assertNotEqual(first['id'], self.save(model)['id'])
        self.assertEqual(len(self.store.snapshot()['items']), 2)

    def test_cancellation_or_revision_change_before_publish_leaves_no_export(self):
        model, _ = self.model()
        with self.assertRaises(ValueError):
            self.save(model, current=lambda: False)
        self.assertEqual(self.store.snapshot()['items'], [])
        original = self.store._write

        def write(directory, name, raw):
            original(directory, name, raw)
            if name == 'receipt.json':
                model.revision += 1

        with patch.object(self.store, '_write', side_effect=write), self.assertRaises(ValueError):
            self.save(model)
        self.assertEqual(self.store.snapshot()['items'], [])

    def test_publish_fsync_failure_is_uncertain_and_explicit_refresh_finds_full_export(self):
        model, _ = self.model()
        fsync = os.fsync

        def fail_parent(fd):
            if os.fstat(fd).st_ino == self.path.stat().st_ino:
                raise OSError('synthetic durability failure')
            fsync(fd)

        with (
            patch('meeting_export.os.fsync', side_effect=fail_parent),
            self.assertRaises(CommitUncertain),
        ):
            self.save(model)
        rows = self.store.snapshot()['items']
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['integrity_confirmed'])
        self.assertEqual(self.save(model), rows[0])

    def test_write_failure_keeps_original_error_even_if_temporary_cleanup_fails(self):
        model, _ = self.model()
        with (
            patch.object(self.store, '_write', side_effect=OSError('original write failure')),
            patch.object(self.store, '_remove', side_effect=OSError('cleanup failure')),
            self.assertRaisesRegex(OSError, 'original write failure'),
        ):
            self.save(model)
        rows = self.store.snapshot()['items']
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['integrity_confirmed'])
        self.store.delete(rows[0]['id'], rows[0]['sha256'])
        self.assertEqual(self.store.snapshot()['items'], [])

    def test_finder_metadata_keeps_existing_export_readable_and_deletable(self):
        model, _ = self.model()
        item = self.save(model)
        (self.path / '.DS_Store').write_bytes(b'fixture metadata')
        (self.path / item['id'] / '.DS_Store').write_bytes(b'fixture metadata')
        self.assertEqual(self.store.snapshot()['items'][0]['sha256'], item['sha256'])
        self.assertTrue(self.save(model, 'txt')['integrity_confirmed'])
        self.assertTrue(self.store.delete(item['id'], item['sha256'])['deleted'])

    def test_delete_requires_exact_current_receipt_and_leaves_other_exports(self):
        model, _ = self.model()
        first, second = self.save(model), self.save(model, 'txt')
        for name, checksum, current in (
            ('../outside', first['sha256'], True),
            (first['id'], '0' * 64, True),
            (first['id'], first['sha256'], False),
        ):
            with self.assertRaises(ValueError):
                self.store.delete(name, checksum, current=lambda: current)
        self.assertEqual(len(self.store.snapshot()['items']), 2)
        self.assertTrue(self.store.delete(first['id'], first['sha256'])['deleted'])
        self.assertEqual(self.store.snapshot()['items'], [second])

    def test_corrupted_metadata_and_partial_delete_are_visible_and_deletable(self):
        model, _ = self.model()
        item = self.save(model)
        (self.path / item['id'] / 'receipt.json').write_text('{broken')
        stale = self.store.snapshot()['items'][0]
        self.assertFalse(stale['integrity_confirmed'])
        with self.assertRaises(ValueError):
            self.store.delete(item['id'], item['sha256'])
        unlink = os.unlink
        calls = []

        def fail_second(name, **kwargs):
            calls.append(name)
            if len(calls) == 2:
                raise OSError('synthetic unlink failure')
            return unlink(name, **kwargs)

        with (
            patch('meeting_export.os.unlink', side_effect=fail_second),
            self.assertRaises(CommitUncertain),
        ):
            self.store.delete(stale['id'], stale['sha256'])
        remaining = self.store.snapshot()['items'][0]
        self.assertFalse(remaining['integrity_confirmed'])
        self.store.delete(remaining['id'], remaining['sha256'])
        self.assertEqual(self.store.snapshot()['items'], [])

    def test_symlink_hardlink_fifo_permission_and_foreign_bundle_entries_fail_closed(self):
        model, _ = self.model()
        item = self.save(model)
        target = self.content(item)
        original = target.read_bytes()
        outside = Path(self.directory.name) / 'outside'
        outside.write_bytes(original)
        for kind in ('symlink', 'hardlink', 'fifo', 'permissions'):
            with self.subTest(kind=kind):
                target.unlink()
                if kind == 'symlink':
                    target.symlink_to(outside)
                elif kind == 'hardlink':
                    os.link(outside, target)
                elif kind == 'fifo':
                    os.mkfifo(target, 0o600)
                else:
                    target.write_bytes(original)
                    target.chmod(0o644)
                with self.assertRaises(OSError):
                    self.store.snapshot()
        target.unlink()
        target.write_bytes(original)
        target.chmod(0o600)
        (target.parent / 'unrelated').write_text('keep')
        with self.assertRaises(OSError):
            self.store.delete(item['id'], item['sha256'])
        self.assertEqual(outside.read_bytes(), original)
        self.assertTrue((target.parent / 'unrelated').exists())

    def test_capacity_and_unknown_entries_never_remove_existing_exports(self):
        model, _ = self.model()
        first = self.save(model)
        with patch('meeting_export.MAX_FILES', 1), self.assertRaises(ValueError):
            self.save(model, 'txt')
        with patch('meeting_export.MAX_TOTAL', first['bytes']), self.assertRaises(ValueError):
            self.save(model, 'txt')
        unexpected = self.path / 'do-not-touch'
        unexpected.write_text('keep')
        self.assertTrue(self.store.snapshot()['unknown_files'])
        with self.assertRaises(OSError):
            self.save(model, 'txt')
        self.assertEqual(unexpected.read_text(), 'keep')
        self.assertTrue(self.content(first).exists())

    def test_replaced_export_root_cannot_return_a_path_to_another_directory(self):
        model, _ = self.model()
        write = self.store._write
        moved = self.path.with_name('moved')

        def replace(directory, name, raw):
            write(directory, name, raw)
            if name == 'receipt.json':
                self.path.rename(moved)
                self.path.mkdir(mode=0o700)

        with (
            patch.object(self.store, '_write', side_effect=replace),
            self.assertRaises(CommitUncertain),
        ):
            self.save(model)
        self.assertEqual(self.store.snapshot()['items'], [])
        self.assertTrue(any(moved.glob('*/meeting.json')))

    def test_duplicate_export_durability_failure_is_not_reported_as_success(self):
        model, _ = self.model()
        first = self.save(model)
        with (
            patch('meeting_export.os.fsync', side_effect=OSError('sync failed')),
            self.assertRaises(CommitUncertain),
        ):
            self.save(model)
        self.assertEqual(self.store.snapshot()['items'], [first])


if __name__ == '__main__':
    unittest.main()

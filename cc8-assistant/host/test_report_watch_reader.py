"""Actual read/claim/store path with synthetic report readers; never sends a notice."""

from pathlib import Path
import tempfile
import unittest
from copy import deepcopy

from dot_client import Cancelled
from report_checkpoint import describe
from report_watch_store import ReportWatchStore
from report_watch_reader import WatchReader
from test_report_voice import SOURCE, report


class WatchReaderTests(unittest.TestCase):
    def setup_reader(self, folder, value=None):
        self.now, self.current, self.source_ok = 100, True, True
        self.calls = []
        self.value = value if value is not None else report('PRIVATE-RESULT')
        store = ReportWatchStore(Path(folder) / 'watch.json', clock=lambda: self.now)

        def read(source, window, current):
            self.assertTrue(current())
            self.calls.append((source.source_id, window.start, window.end))
            return deepcopy(self.value)

        reader = WatchReader(store, read, lambda source: self.source_ok)
        return store, reader

    def configure(self, store):
        store.configure(0, describe(SOURCE), 300, 'cc8')
        self.now = 200

    def test_absent_paused_or_unconfirmed_subscription_never_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            store, reader = self.setup_reader(folder)
            self.assertIsNone(reader.prepare(200, lambda: True))
            self.assertFalse(store.path.exists())
            self.configure(store)
            store.pause(1)
            self.assertIsNone(reader.prepare(200, lambda: True))
            self.assertEqual(self.calls, [])

    def test_prepare_is_body_free_readonly_then_claim_is_durable_not_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            store, reader = self.setup_reader(folder)
            self.configure(store)
            raw = store.path.read_bytes()
            notice = reader.prepare(200, lambda: self.current)
            self.assertEqual(self.calls, [(SOURCE.source_id, 100, 200)])
            self.assertNotIn('PRIVATE-RESULT', notice.text)
            self.assertEqual(store.path.read_bytes(), raw)
            claimed = reader.claim(notice, lambda: self.current)
            watch = claimed['watch']
            self.assertEqual(watch['seen'], [])
            self.assertEqual(watch['attempt']['status'], 'claimed')
            self.assertIsNone(reader.prepare(200, lambda: True))
            self.assertEqual(len(self.calls), 1)
            self.assertNotIn('PRIVATE-RESULT', store.path.read_text())
            store.settle(watch['id'], watch['attempt']['id'], True, detail='completed')
            self.assertIsNone(reader.prepare(200, lambda: True))
            self.assertEqual(store.snapshot()['watch']['last_check']['status'], 'empty')

    def test_reversion_dedup_uses_all_confirmed_versions_and_never_changes_manual_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            store, reader = self.setup_reader(folder)
            self.configure(store)
            original = deepcopy(self.value)
            for text in ('PRIVATE-RESULT', 'updated version'):
                self.value['items'][0]['excerpt'] = text
                notice = reader.prepare(200, lambda: True)
                state = reader.claim(notice, lambda: True)
                w = state['watch']
                store.settle(w['id'], w['attempt']['id'], True, detail='completed')
            self.value = original
            self.assertIsNone(reader.prepare(200, lambda: True))
            self.assertEqual(len(store.snapshot()['watch']['seen']), 2)

    def test_partial_read_without_results_cannot_claim_confirmed_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            value = report()
            value.update(coverage_complete=False, empty_confirmed=False)
            store, reader = self.setup_reader(folder, value)
            self.configure(store)
            self.assertIsNone(reader.prepare(200, lambda: True))
            check = store.snapshot()['watch']['last_check']
            self.assertEqual((check['status'], check['confirmed_new']), ('partial', None))

    def test_known_partial_results_are_bounded_and_never_promoted_to_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            value = report(*(['PRIVATE'] * 5))
            value['coverage_complete'] = False
            store, reader = self.setup_reader(folder, value)
            self.configure(store)
            notice = reader.prepare(200, lambda: True)
            self.assertEqual((len(notice.pairs), notice.known_new, notice.partial), (3, 5, True))
            self.assertIn('覆盖不完整', notice.text)
            self.assertNotIn('PRIVATE', notice.text)

    def test_cancel_source_change_stale_store_and_old_proposals_cannot_claim_or_send(self):
        for changed in ('cancel', 'source', 'clear'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as folder:
                store, reader = self.setup_reader(folder)
                self.configure(store)
                notice = reader.prepare(200, lambda: self.current)
                if changed == 'cancel':
                    self.current = False
                elif changed == 'source':
                    self.source_ok = False
                else:
                    store.clear(1)
                raw = store.path.read_bytes()
                with self.assertRaises(ValueError):
                    reader.claim(notice, lambda: self.current)
                self.assertEqual(store.path.read_bytes(), raw)

    def test_bounds_invalid_read_or_error_do_not_invent_completed_results(self):
        for changed in ('clock', 'age', 'source', 'invalid', 'error', 'cancel_in_read'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as folder:
                store, reader = self.setup_reader(folder)
                self.configure(store)
                end = 200
                expected = 'read_error'
                if changed == 'clock':
                    end, expected = 99, 'clock_error'
                elif changed == 'age':
                    end, expected = 100 + 32 * 86400, 'age_limit'
                elif changed == 'source':
                    self.source_ok, expected = False, 'source_changed'
                elif changed == 'invalid':
                    self.value['window_start'] = 101
                else:

                    def bad_read(source, window, current):
                        if changed == 'cancel_in_read':
                            self.current = False
                            self.assertFalse(current())
                            raise Cancelled('cancelled')
                        raise RuntimeError('PRIVATE error')

                    reader.read = bad_read
                if changed == 'cancel_in_read':
                    raw = store.path.read_bytes()
                    with self.assertRaises(Cancelled):
                        reader.prepare(end, lambda: self.current)
                    self.assertEqual(store.path.read_bytes(), raw)
                else:
                    self.assertIsNone(reader.prepare(end, lambda: self.current))
                    state = store.snapshot()['watch']
                    self.assertEqual(state['last_check']['status'], expected)
                    self.assertIsNone(state['last_check']['confirmed_new'])
                    self.assertIsNone(state['attempt'])

"""Private subscriptions, CAS and actual durable claims; never read real chat content."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import threading
import concurrent.futures
import unittest
from unittest.mock import patch

from dot_handoff import Handoff
from report_checkpoint import describe
from report_sources import ReportScope, ReportMember
from report_watch_store import ReportWatchStore, validate
from reminder_store import ReminderCommitUncertain


SOURCE = describe(Handoff('original-chat', '工作来源', (), False))


def item(number=1, text='known result version'):
    return dict(
        key=['turn-1', f'item-{number}'], fingerprint=hashlib.sha256(text.encode()).hexdigest()
    )


class WatchStoreTests(unittest.TestCase):
    def test_absence_stays_absent_and_explicit_configuration_contains_no_chat_or_result_body(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private' / 'watch.json'
            store = ReportWatchStore(path, clock=lambda: 100)
            self.assertEqual(store.snapshot(), dict(schema=1, revision=0, watch=None))
            self.assertFalse(path.parent.exists())
            state = store.configure(0, SOURCE, 900, 'cc8')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(state, ReportWatchStore(path).snapshot())
            self.assertTrue(state['watch']['enabled'])
            self.assertEqual(state['watch']['seen'], [])
            self.assertEqual(
                set(state['watch']),
                {
                    'id',
                    'source',
                    'started_at',
                    'period_seconds',
                    'channel',
                    'enabled',
                    'seen',
                    'attempt',
                    'last_check',
                },
            )

    def test_persisted_claim_is_not_a_delivery_and_reopening_does_not_change_it(self):
        with tempfile.TemporaryDirectory() as folder:
            now = [100]
            store = ReportWatchStore(Path(folder) / 'watch.json', clock=lambda: now[0])
            store.configure(0, SOURCE, 300, 'cc8')
            now[0] = 200
            claim = store.claim(1, [item()], 180)
            self.assertEqual(claim['watch']['seen'], [])
            self.assertEqual(claim['watch']['attempt']['status'], 'claimed')
            original = store.path.read_bytes()
            self.assertEqual(ReportWatchStore(store.path).snapshot(), claim)
            self.assertEqual(store.path.read_bytes(), original)
            with self.assertRaises(ValueError):
                store.claim(2, [item(2)], 180)
            self.assertEqual(store.path.read_bytes(), original)

    def test_confirmed_delivery_deduplicates_versions_without_replaying_old_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            now = [100]
            store = ReportWatchStore(Path(folder) / 'watch.json', clock=lambda: now[0])
            configured = store.configure(0, SOURCE, 900, 'cc8')
            now[0] = 200
            claim = store.claim(1, [item()], 180)
            watch = claim['watch']
            delivered = store.settle(watch['id'], watch['attempt']['id'], True, detail='completed')
            self.assertEqual(delivered['watch']['seen'], [item()])
            self.assertIsNone(delivered['watch']['attempt'])
            self.assertEqual(delivered['watch']['last_check']['status'], 'delivered')
            original = store.path.read_bytes()
            self.assertIsNone(store.settle(watch['id'], watch['attempt']['id'], True))
            with self.assertRaises(ValueError):
                store.claim(3, [item()], 190)
            self.assertEqual(store.path.read_bytes(), original)
            changed = store.claim(3, [item(text='updated result version')], 190)
            self.assertEqual(len(changed['watch']['seen']), 1)
            now[0] = 300
            reset = store.configure(4, SOURCE, 1800, 'desktop')
            self.assertNotEqual(reset['watch']['id'], configured['watch']['id'])
            original = store.path.read_bytes()
            self.assertIsNone(
                store.settle(changed['watch']['id'], changed['watch']['attempt']['id'], True)
            )
            self.assertEqual(store.path.read_bytes(), original)

    def test_unconfirmed_delivery_disables_subscription_and_late_receipt_cannot_claim_success(self):
        with tempfile.TemporaryDirectory() as folder:
            now = [100]
            store = ReportWatchStore(Path(folder) / 'watch.json', clock=lambda: now[0])
            store.configure(0, SOURCE, 900, 'cc8')
            now[0] = 200
            claim = store.claim(1, [item()], 180)['watch']
            state = store.settle(claim['id'], claim['attempt']['id'], False, detail='cancelled')
            self.assertFalse(state['watch']['enabled'])
            self.assertEqual(state['watch']['seen'], [])
            self.assertEqual(state['watch']['attempt']['status'], 'unconfirmed')
            self.assertEqual(state['watch']['last_check']['status'], 'delivery_unconfirmed')
            self.assertIsNone(store.settle(claim['id'], claim['attempt']['id'], True))
            with self.assertRaises(ValueError):
                store.claim(3, [item(2)], 190)
            store.clear(3)
            self.assertIsNone(store.snapshot()['watch'])

    def test_partial_or_failed_read_never_records_zero_results_and_old_revision_does_not_write(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            store = ReportWatchStore(Path(folder) / 'watch.json', clock=lambda: 200)
            store.configure(0, SOURCE, 900, 'cc8')
            store.checked(1, 'partial')
            self.assertIsNone(store.snapshot()['watch']['last_check']['confirmed_new'])
            original = store.path.read_bytes()
            for changes in [dict(confirmed_new=0), dict(confirmed_new=False)]:
                with self.assertRaises(ValueError):
                    store.checked(2, 'read_error', **changes)
            for revision in (None, True, 0):
                with self.assertRaises(ValueError):
                    store.pause(revision)
            with self.assertRaises(ValueError):
                store.pause(2, current=lambda: False)
            self.assertEqual(store.path.read_bytes(), original)
            store.checked(2, 'empty', confirmed_new=0)
            self.assertEqual(store.snapshot()['watch']['last_check']['confirmed_new'], 0)

    def test_capacity_source_namespace_and_bad_fields_fail_without_eviction(self):
        with tempfile.TemporaryDirectory() as folder:
            now = [100]
            store = ReportWatchStore(Path(folder) / 'watch.json', clock=lambda: now[0])
            state = store.configure(0, SOURCE, 900, 'cc8')
            now[0] = 200
            state['watch']['seen'] = [item(i) for i in range(256)]
            validate(state)
            store.path.write_text(json.dumps(state))
            store.path.chmod(0o600)
            original = store.path.read_bytes()
            with self.assertRaises(ValueError):
                store.claim(1, [item(300)], 180)
            self.assertEqual(store.path.read_bytes(), original)
            grouped = describe(
                ReportScope(
                    0,
                    (ReportMember('a' * 16, 'member-chat', '成员', '项目'),),
                    'registered_group',
                    '登记范围',
                )
            )
            state = store.configure(1, grouped, 300, 'cc8')
            with self.assertRaises(ValueError):
                store.claim(
                    2, [dict(item(), key=['local', 'another-chat', 'turn-1', 'item-1'])], 180
                )
            broken = deepcopy(state)
            broken['watch']['last_check'] = dict(at=200, status=[], confirmed_new=None)
            with self.assertRaises(ValueError):
                validate(broken)

    def test_concurrent_cas_corruption_symlink_and_uncertain_claim_preserve_actual_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'watch.json'
            barrier = threading.Barrier(2)

            def configure():
                barrier.wait()
                try:
                    ReportWatchStore(path, clock=lambda: 100).configure(0, SOURCE, 900, 'cc8')
                    return True
                except ValueError:
                    return False

            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                self.assertEqual(sum(pool.map(lambda _: configure(), range(2))), 1)
            store = ReportWatchStore(path, clock=lambda: 200)
            import os

            real = os.fsync
            calls = []

            def fail_directory(fd):
                calls.append(fd)
                if len(calls) == 2:
                    raise OSError('sync unconfirmed')
                real(fd)

            with patch('reminder_store.os.fsync', side_effect=fail_directory):
                with self.assertRaises(ReminderCommitUncertain):
                    store.claim(1, [item()], 180)
            current = store.snapshot()
            self.assertEqual(current['revision'], 2)
            self.assertEqual(current['watch']['attempt']['status'], 'claimed')
            with self.assertRaises(ValueError):
                store.claim(2, [item(2)], 190)
            path.write_text('{corrupt')
            with self.assertRaises(ValueError):
                store.clear(2)
            self.assertEqual(path.read_text(), '{corrupt')
            link = Path(folder) / 'linked'
            link.symlink_to(path)
            with self.assertRaises((OSError, ValueError)):
                ReportWatchStore(link).snapshot()

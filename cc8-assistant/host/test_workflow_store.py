"""Real private files, occurrence ownership and distinct execution/delivery receipts."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import concurrent.futures
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from workflow_definition import builtins, definition, fingerprint, validate
from workflow_schedule import due_slot
from workflow_store import WorkflowStore
from work_reports import BEIJING


def flow():
    value = builtins()[0]
    value['steps'] = value['steps'][:1]
    return value


def scheduled():
    value = flow()
    value['schedule'] = dict(kind='interval', enabled=True, configured_at=0, seconds=300)
    return value


class DefinitionTests(unittest.TestCase):
    def test_builtins_are_passive_unbound_and_camera_cannot_be_scheduled(self):
        values = builtins()
        for value in values:
            self.assertIs(definition(value), value)
            self.assertIsNone(value['schedule'])
        self.assertIsNone(values[0]['steps'][1]['source'])
        values[0]['name'] = 'changed'
        self.assertNotEqual(values, builtins())
        for value in builtins():
            value['schedule'] = scheduled()['schedule']
            with self.assertRaises(ValueError):
                definition(value)

    def test_fingerprint_is_order_independent_but_binds_inputs(self):
        value = flow()
        self.assertEqual(fingerprint(value), fingerprint(dict(reversed(list(value.items())))))
        changed = deepcopy(value)
        changed['steps'][0]['text'] += ' 中文'
        self.assertNotEqual(fingerprint(value), fingerprint(changed))

    def test_malformed_and_ambiguous_definitions_fail(self):
        cases = []
        for path, replacement in (
            (('name',), ' '),
            (('name',), '\ud800'),
            (('steps',), []),
            (('id',), True),
            (('extra',), 'unexpected'),
            (('schedule',), dict(kind='interval', enabled=1, configured_at=0, seconds=300)),
        ):
            value = flow()
            value[path[0]] = replacement
            cases.append(value)
        duplicate = flow()
        duplicate['steps'] *= 2
        cases.append(duplicate)
        later_camera = flow()
        later_camera['steps'] += builtins()[2]['steps']
        cases.append(later_camera)
        for value in cases:
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                definition(value)

    def test_interval_has_no_startup_or_backlog_and_daily_uses_beijing(self):
        value = scheduled()
        value['schedule']['configured_at'] = 100
        for now, expected in (
            (99, None),
            (100, None),
            (399, None),
            (400, 400),
            (699, 400),
            (700, 700),
            (1301, 1300),
        ):
            self.assertEqual(due_slot(value, now), expected)
        stamp = lambda hour, minute: int(
            datetime(2026, 11, 10, hour, minute, tzinfo=BEIJING).timestamp()
        )
        value['schedule'] = dict(
            kind='daily',
            enabled=True,
            configured_at=stamp(7, 0),
            minute_of_day=8 * 60,
            weekdays=[1],
        )
        self.assertIsNone(due_slot(value, stamp(7, 59)))
        self.assertEqual(due_slot(value, stamp(8, 15)), stamp(8, 0))
        self.assertIsNone(due_slot(value, stamp(8, 16)))
        value['schedule']['weekdays'] = [2]
        self.assertIsNone(due_slot(value, stamp(8, 0)))
        for now in (True, float('nan'), float('inf'), -1):
            with self.assertRaises(ValueError):
                due_slot(value, now)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'private' / 'workflows.json'
        self.now = 100
        self.store = WorkflowStore(self.path, clock=lambda: self.now)

    def configure(self):
        return self.store.apply('put', 0, value=scheduled())

    def claim(self):
        state = self.configure()
        self.now = 400
        return self.store.claim(state['items'][0]['id'], 1, 400)

    def test_read_never_creates_and_explicit_put_is_owner_private(self):
        before = self.store.snapshot()
        self.assertFalse(self.path.parent.exists())
        state = self.configure()
        self.assertEqual(state['revision'], before['revision'] + 1)
        self.assertEqual(state, WorkflowStore(self.path).snapshot())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(state['items'][0]['schedule']['configured_at'], self.now)

    def test_stale_revision_owner_and_duplicate_name_cannot_write(self):
        self.configure()
        original = self.path.read_bytes()
        for revision, current in ((0, lambda: True), (1, lambda: False), (1, lambda: 1)):
            with self.assertRaises(ValueError):
                self.store.apply('reset', revision, current=current)
            self.assertEqual(self.path.read_bytes(), original)
        other = flow()
        other['id'] = 'a' * 32
        with self.assertRaises(ValueError):
            self.store.apply('put', 1, value=other)
        self.assertEqual(self.path.read_bytes(), original)

    def test_claim_is_durable_unique_and_does_not_claim_delivery(self):
        row = self.claim()
        self.assertEqual((row['execution'], row['delivery']), ('claimed', 'not_started'))
        reopened = WorkflowStore(self.path, clock=lambda: self.now)
        self.assertEqual(reopened.snapshot()['attempts'], [row])
        self.assertIsNone(reopened.claim(row['workflow_id'], 2, 400))
        with self.assertRaises(ValueError):
            reopened.claim(row['workflow_id'], 2, 100)

    def test_two_independent_writers_only_one_can_claim(self):
        state = self.configure()
        self.now = 400

        def claim(_):
            try:
                return WorkflowStore(self.path, clock=lambda: self.now).claim(
                    state['items'][0]['id'], 1, 400
                )
            except (ValueError, BlockingIOError):
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(len(self.store.snapshot()['attempts']), 1)

    def test_execution_survives_unconfirmed_delivery_and_late_ack_cannot_upgrade_it(self):
        row = self.claim()
        self.assertTrue(self.store.finish_execution(row['id'], 'completed'))
        self.assertTrue(self.store.finish_delivery(row['id'], False))
        self.assertFalse(self.store.finish_delivery(row['id'], True))
        self.assertFalse(self.store.finish_execution(row['id'], 'cancelled'))
        state = self.store.snapshot()
        self.assertEqual(
            (state['attempts'][0]['execution'], state['attempts'][0]['delivery']),
            ('completed', 'unconfirmed'),
        )
        self.assertIsNone(self.store.claim(row['workflow_id'], state['revision'], 400))

    def test_failure_never_accepts_delivery_or_reset_replays_claim(self):
        row = self.claim()
        self.store.finish_execution(row['id'], 'unconfirmed')
        self.assertFalse(self.store.finish_delivery(row['id'], True))
        state = self.store.apply('reset', 3)
        self.assertEqual(state['attempts'][0]['id'], row['id'])
        state = self.store.apply('put', state['revision'], value=scheduled())
        self.assertIsNone(due_slot(state['items'][0], self.now))

    def test_explicit_history_clear_disables_all_schedules_before_forgetting_claims(self):
        self.claim()
        state = self.store.apply('clear_history', 2)
        self.assertEqual(state['attempts'], [])
        self.assertFalse(state['items'][0]['schedule']['enabled'])
        self.assertIsNone(due_slot(state['items'][0], 1000))

    def test_unsafe_lock_and_symlink_storage_are_not_followed(self):
        self.configure()
        target = self.path.parent / 'target'
        target.write_text('untouched')
        os.chmod(target, 0o644)
        lock = self.path.parent / '.workflows.lock'
        lock.unlink()
        os.link(target, lock)
        with self.assertRaises(OSError):
            self.store.apply('reset', 1)
        self.assertEqual(target.read_text(), 'untouched')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.store.snapshot()

    def test_mutated_persisted_delivery_and_future_claim_are_rejected(self):
        self.claim()
        for field, value in (('delivery', 'completed'), ('slot', 999999)):
            state = self.store.snapshot()
            state['attempts'][0][field] = value
            with self.assertRaises(ValueError):
                validate(state)

    def test_journal_capacity_keeps_old_claim_and_requires_explicit_paused_clear(self):
        row = self.claim()
        original = self.path.read_bytes()
        self.now = 700
        with patch('workflow_store.MAX_ATTEMPTS', 1):
            with self.assertRaisesRegex(ValueError, 'capacity'):
                self.store.claim(row['workflow_id'], 2, 700)
        self.assertEqual(self.path.read_bytes(), original)
        state = self.store.apply('clear_history', 2)
        self.assertEqual(state['attempts'], [])
        self.assertFalse(state['items'][0]['schedule']['enabled'])

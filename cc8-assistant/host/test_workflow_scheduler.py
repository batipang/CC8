"""Real durable claims with explicit foreground grants and no startup/backlog replay."""

from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from workflow_scheduler import WorkflowScheduler, ScheduleHooks
from workflow_store import WorkflowStore
from reminder_store import ReminderStore, ReminderCommitUncertain
from test_workflow_store import scheduled


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = 100
        self.owner, self.activation = object(), None
        self.ready, self.blocked = True, False
        self.calls, self.errors = [], []
        self.store = WorkflowStore(Path(folder.name) / 'workflows.json', clock=lambda: self.now)
        self.store.apply('put', 0, value=scheduled())

        def submit(flow, claim):
            self.assertEqual(self.store.snapshot()['attempts'][-1], claim)
            self.calls.append((deepcopy(flow), deepcopy(claim)))
            return dict(ok=True)

        self.scheduler = WorkflowScheduler(
            self.store,
            ScheduleHooks(
                threading.RLock(),
                lambda: self.owner,
                lambda: self.activation,
                lambda: self.blocked,
                lambda: self.ready,
                submit,
                self.errors.append,
            ),
            clock=lambda: self.now,
        )

    def test_no_constructor_start_or_implicit_grant_and_no_activation_effect(self):
        original = self.store.path.read_bytes()
        self.now = 400
        self.scheduler.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.path.read_bytes(), original)
        self.activation = 'explicit-normal-start-1'
        self.scheduler.tick()
        self.assertEqual(self.calls, [])
        self.now = 700
        self.scheduler.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]['slot'], 700)

    def test_once_per_slot_and_restart_keeps_claim_without_replaying(self):
        self.scheduler.arm()
        self.now = 400
        self.scheduler.tick()
        self.scheduler.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.snapshot()['attempts'][0]['execution'], 'claimed')
        self.scheduler.revoke()
        self.scheduler.arm()
        self.scheduler.tick()
        self.assertEqual(len(self.calls), 1)
        self.now = 700
        self.scheduler.tick()
        self.assertEqual(len(self.calls), 2)

    def test_reconnect_and_maintenance_end_never_restore_grant(self):
        self.scheduler.arm()
        self.owner = object()
        self.now = 400
        self.scheduler.tick()
        self.assertIsNone(self.scheduler.grant)
        self.scheduler.tick()
        self.assertEqual(self.calls, [])
        self.activation = 'fresh-explicit-start'
        self.scheduler.tick()
        self.assertIsNotNone(self.scheduler.grant)
        self.blocked = True
        self.scheduler.tick()
        self.blocked = False
        self.now = 700
        self.scheduler.tick()
        self.assertEqual(self.calls, [])

    def test_busy_and_quiet_admission_do_not_make_a_claim(self):
        self.scheduler.arm()
        self.now = 400
        self.ready = False
        self.scheduler.tick()
        self.assertEqual(self.store.snapshot()['attempts'], [])
        self.ready = True
        self.scheduler.tick()
        self.assertEqual(len(self.calls), 1)

    def test_old_slots_are_not_replayed_when_an_idle_window_returns(self):
        self.scheduler.arm()
        self.now = 1400
        self.scheduler.tick()
        self.assertEqual([c['slot'] for f, c in self.calls], [1300])

    def test_changed_definition_without_new_consent_revokes_grant(self):
        self.scheduler.arm()
        changed = scheduled()
        changed['steps'][0]['text'] = 'different search'
        self.store.apply('put', 1, value=changed)
        self.now = 400
        self.scheduler.tick()
        self.assertIsNone(self.scheduler.grant)
        self.assertEqual(self.calls, [])

    def test_clock_rollback_revokes_without_claim_or_retry(self):
        self.scheduler.arm()
        self.now = 99
        self.scheduler.tick()
        self.assertIsNone(self.scheduler.grant)
        self.assertEqual(self.store.snapshot()['attempts'], [])

    def test_ambiguous_queue_failure_claims_once_and_is_not_a_retry_ticket(self):
        def submit(flow, claim):
            self.calls.append((flow, claim))
            raise OSError('queue ACK lost')

        self.scheduler.hooks = ScheduleHooks(
            threading.RLock(),
            lambda: self.owner,
            lambda: self.activation,
            lambda: False,
            lambda: True,
            submit,
            self.errors.append,
        )
        self.scheduler.arm()
        self.now = 400
        self.scheduler.tick()
        row = self.store.snapshot()['attempts'][0]
        self.assertEqual(row['execution'], 'unconfirmed')
        self.assertEqual(row['delivery'], 'not_started')
        self.assertIsNone(self.scheduler.grant)
        self.scheduler.arm()
        self.scheduler.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.errors, ['OSError'])

    def test_owner_loss_after_durable_claim_never_submits(self):
        original = self.store.claim

        def claim(*args, **kwargs):
            result = original(*args, **kwargs)
            self.owner = object()
            return result

        self.store.claim = claim
        self.scheduler.arm()
        self.now = 400
        self.scheduler.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.snapshot()['attempts'][0]['execution'], 'unconfirmed')

    def test_corrupt_store_at_new_start_and_failed_optional_log_do_not_kill_tick(self):
        def fail(*args):
            raise OSError('corrupt private store')

        self.store.snapshot = fail
        self.activation = 'start'
        self.scheduler.hooks = ScheduleHooks(
            threading.RLock(),
            lambda: self.owner,
            lambda: self.activation,
            lambda: False,
            lambda: True,
            fail,
            fail,
        )
        self.scheduler.tick()
        self.assertIsNone(self.scheduler.grant)
        self.assertEqual(self.calls, [])

    def test_post_rename_durability_error_never_executes_or_loses_the_claim(self):
        self.scheduler.arm()
        self.now = 400
        original = ReminderStore.save

        def uncertain(disk, value):
            original(disk, value)
            raise ReminderCommitUncertain('directory fsync receipt lost')

        with patch.object(ReminderStore, 'save', uncertain):
            self.scheduler.tick()
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.scheduler.grant)
        state = WorkflowStore(self.store.path, clock=lambda: self.now).snapshot()
        self.assertEqual(len(state['attempts']), 1)
        self.assertEqual(state['attempts'][0]['execution'], 'claimed')
        self.assertIsNone(
            self.store.claim(state['attempts'][0]['workflow_id'], state['revision'], 400)
        )

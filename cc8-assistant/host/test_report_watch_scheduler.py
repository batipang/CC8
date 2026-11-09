"""Real threaded poll/read/claim/send/receipt path with private temporary stores."""

from pathlib import Path
from types import SimpleNamespace
import fcntl
import tempfile
import threading
import unittest
from report_watch_scheduler import WatchScheduler, WatchHooks
from report_watch_guard import automation_active
import test_report_watch_reader as fixtures


class WatchSchedulerTests(unittest.TestCase):
    def session(self, folder):
        helper = self.helper = fixtures.WatchReaderTests()
        store, reader = helper.setup_reader(folder)
        original_read = reader.read

        def read(source, window, current):
            value = original_read(source, window, current)
            value.update(window_start=window.start, window_end=window.end)
            return value

        reader.read = read
        helper.configure(store)
        self.owner, self.activation, self.generation = object(), None, 0
        self.blocked, self.idle, self.ready = False, True, True
        self.sends, self.errors, self.jobs = [], [], []
        self.monotonic = 0

        def send(channel, ident, text, current):
            self.assertTrue(current())
            state = store.snapshot()['watch']
            self.assertEqual(state['attempt']['id'], ident)
            self.assertEqual(state['seen'], [])
            self.sends.append((channel, ident, text))
            # Announcements start a new generation and become busy themselves.
            self.generation += 1
            self.idle = False
            self.assertTrue(current())
            job = SimpleNamespace(done=threading.Event(), result='unconfirmed')
            self.jobs.append(job)
            return job

        scheduler = WatchScheduler(
            store,
            reader,
            WatchHooks(
                threading.RLock(),
                lambda: self.owner,
                lambda: self.activation,
                lambda: self.generation,
                lambda: self.blocked,
                lambda: self.idle,
                lambda: self.ready,
                send,
                self.errors.append,
            ),
            clock=lambda: helper.now,
            monotonic=lambda: self.monotonic,
        )
        return store, scheduler

    def tick(self, scheduler):
        scheduler.tick()
        if scheduler.worker is not None:
            scheduler.worker.join(3)
            self.assertFalse(scheduler.worker.is_alive())

    def due(self):
        self.helper.now = 400

    def test_configuration_alone_and_reconnection_cannot_scan_but_normal_start_can(self):
        with tempfile.TemporaryDirectory() as folder:
            store, scheduler = self.session(folder)
            self.due()
            self.tick(scheduler)
            self.assertEqual(self.helper.calls, [])
            self.activation = 'normal-start-a'
            self.tick(scheduler)
            self.assertEqual(len(self.sends), 1)
            self.assertEqual(store.snapshot()['watch']['seen'], [])
            self.jobs[0].result = 'completed'
            self.jobs[0].done.set()
            self.idle = True
            self.tick(scheduler)
            self.assertEqual(len(store.snapshot()['watch']['seen']), 1)
            self.owner = object()
            self.helper.now, self.monotonic = 800, 400
            self.tick(scheduler)
            self.assertEqual(len(self.helper.calls), 1)
            self.activation = 'normal-start-b'
            self.tick(scheduler)
            self.assertEqual(len(self.helper.calls), 2)
            self.assertEqual(len(self.sends), 1)

    def test_explicit_arm_works_paused_and_quiet_busy_test_guard_prevent_reads(self):
        for gate in ('blocked', 'idle', 'ready'):
            with self.subTest(gate=gate), tempfile.TemporaryDirectory() as folder:
                _, scheduler = self.session(folder)
                scheduler.arm()
                self.due()
                setattr(self, gate, gate == 'blocked')
                self.tick(scheduler)
                self.assertEqual(self.helper.calls, [])
                setattr(self, gate, gate != 'blocked')
                if gate == 'blocked':
                    self.tick(scheduler)
                    self.assertEqual(self.helper.calls, [])
                    scheduler.arm()
                self.tick(scheduler)
                self.assertEqual(len(self.sends), 1)

    def test_test_activation_does_not_rearm_after_maintenance_ends(self):
        with tempfile.TemporaryDirectory() as folder:
            _, scheduler = self.session(folder)
            self.due()
            self.blocked, self.activation = True, 'test-start'
            self.tick(scheduler)
            self.blocked = False
            self.tick(scheduler)
            self.assertEqual(self.helper.calls, [])
            self.activation = 'normal-start'
            self.tick(scheduler)
            self.assertEqual(len(self.sends), 1)

    def test_lost_receipt_failed_send_and_restart_claim_freeze_without_replay(self):
        for kind in (
            'timeout',
            'failed',
            'restart',
            'send_error',
            'permission_denied',
            'malformed_detail',
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                store, scheduler = self.session(folder)
                scheduler.arm()
                self.due()
                if kind == 'send_error':
                    from dataclasses import replace

                    def bad_send(*args):
                        raise RuntimeError('PRIVATE provider error')

                    scheduler.hooks = replace(scheduler.hooks, send=bad_send)
                self.tick(scheduler)
                if kind == 'timeout':
                    self.monotonic = 91
                elif kind == 'failed':
                    self.jobs[0].done.set()
                elif kind == 'permission_denied':
                    self.jobs[0].detail = 'permission_denied'
                    self.jobs[0].done.set()
                elif kind == 'malformed_detail':
                    self.jobs[0].detail = {'untrusted': 'PRIVATE'}
                    self.jobs[0].done.set()
                elif kind == 'restart':
                    scheduler.pending = None
                self.idle = True
                self.tick(scheduler)
                watch = store.snapshot()['watch']
                self.assertFalse(watch['enabled'])
                self.assertEqual(watch['seen'], [])
                self.assertEqual(watch['attempt']['status'], 'unconfirmed')
                if kind == 'permission_denied':
                    self.assertEqual(watch['attempt']['detail'], 'not_authorized')
                sends = len(self.sends)
                self.helper.now, self.monotonic = 1000, 1000
                self.tick(scheduler)
                self.assertEqual(len(self.sends), sends)
                self.assertNotIn('PRIVATE', str(self.errors))

    def test_late_read_after_home_or_new_generation_is_discarded_without_sending(self):
        for kind in ('owner', 'generation', 'blocked', 'clear'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                store, scheduler = self.session(folder)
                original = scheduler.reader.read

                def changed(source, window, current):
                    value = original(source, window, current)
                    if kind == 'owner':
                        self.owner = None
                    elif kind == 'generation':
                        self.generation += 1
                    elif kind == 'blocked':
                        self.blocked = True
                    else:
                        store.clear(1)
                    return value

                scheduler.reader.read = changed
                scheduler.arm()
                self.due()
                self.tick(scheduler)
                self.assertEqual(self.sends, [])
                state = store.snapshot()['watch']
                self.assertTrue(state is None or state['attempt'] is None)

    def test_minimum_interval_no_result_polling_and_desktop_receipt_are_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            store, scheduler = self.session(folder)
            self.helper.now = 100
            from report_checkpoint import describe
            from test_report_voice import SOURCE

            store.configure(1, describe(SOURCE), 300, 'desktop')
            scheduler.arm()
            self.helper.now = 399
            self.tick(scheduler)
            self.assertEqual(self.helper.calls, [])
            self.due()
            self.tick(scheduler)
            self.assertEqual(self.sends[0][0], 'desktop')
            self.jobs[0].result = 'notification_center'
            self.jobs[0].done.set()
            self.idle = True
            self.tick(scheduler)
            self.assertEqual(len(store.snapshot()['watch']['seen']), 1)
            self.helper.now, self.monotonic = 699, 299
            self.tick(scheduler)
            self.assertEqual(len(self.helper.calls), 1)
            self.helper.now, self.monotonic = 700, 300
            self.tick(scheduler)
            self.assertEqual(len(self.helper.calls), 2)
            self.assertEqual(len(self.sends), 1)

    def test_automation_lease_is_readonly_private_nonlinked_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'automation-test.lock'
            self.assertFalse(automation_active(path))
            self.assertFalse(path.exists())
            path.write_bytes(b'')
            path.chmod(0o600)
            self.assertFalse(automation_active(path))
            with path.open('rb') as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(automation_active(path))
            self.assertFalse(automation_active(path))
            target = Path(folder) / 'target'
            path.rename(target)
            path.symlink_to(target)
            self.assertTrue(automation_active(path))
            path.unlink()
            target.rename(path)
            path.chmod(0o644)
            self.assertTrue(automation_active(path))
            path.chmod(0o600)
            import os

            alias = Path(folder) / 'alias'
            os.link(path, alias)
            self.assertTrue(automation_active(path))
            alias.unlink()
            self.assertFalse(automation_active(path))

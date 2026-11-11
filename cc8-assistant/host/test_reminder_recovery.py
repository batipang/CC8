"""Exercise recovery against real files and runtime locks in an isolated checkout."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from process_lock import ProcessLease
from reminder_recovery import recover
from workspace import Workspace


class ReminderRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(folder).resolve() / 'checkout'
        self.store = self.root / 'device-access/private/voice-runtime/reminders.json'
        self.store.parent.mkdir(parents=True, mode=0o700)
        self.lease = Path(folder) / 'owner.lock'
        self.enterContext(patch('runtime_guard.owner_lease_path', return_value=self.lease))
        self.enterContext(patch('runtime_guard.active_roots', return_value=set()))
        self.enterContext(patch('runtime_guard.enabled_service_roots', return_value=set()))
        self.enterContext(patch('reminder_recovery.active_roots', return_value=set()))
        self.enterContext(patch('reminder_recovery.enabled_service_roots', return_value=set()))

    def put(self, raw):
        self.store.write_bytes(raw)
        self.store.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()

    def test_corruption_blocks_workspace_and_explicit_recovery_preserves_bytes(self):
        raw = b'{"version":3,"reminders":'
        digest = self.put(raw)
        with self.assertRaises(ValueError):
            Workspace(self.store)
        self.assertEqual(recover(self.root), {'state': 'unreadable', 'sha256': digest})
        self.assertEqual(self.store.read_bytes(), raw)
        result = recover(self.root, digest)
        self.assertEqual(result['state'], 'quarantined')
        archive = Path(result['archive'])
        self.assertEqual(archive.read_bytes(), raw)
        self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.store.exists())
        self.assertEqual(Workspace(self.store).reminders, [])
        self.assertEqual(recover(self.root), {'state': 'missing'})

    def test_readable_store_and_stale_confirmation_are_refused(self):
        good = b'{"version":1,"reminders":[]}'
        digest = self.put(good)
        self.assertEqual(recover(self.root)['state'], 'readable')
        with self.assertRaises(ValueError):
            recover(self.root, digest)
        self.assertEqual(self.store.read_bytes(), good)
        self.put(b'{"version":99,"reminders":[]}')
        with self.assertRaises(ValueError):
            recover(self.root, digest)
        self.assertEqual(list(self.store.parent.glob('reminders-recovery-*')), [])

    def test_running_or_enabled_backend_and_symlink_are_refused(self):
        digest = self.put(b'')
        with ProcessLease(self.lease), self.assertRaises(BlockingIOError):
            recover(self.root, digest)
        with patch('reminder_recovery.enabled_service_roots', return_value={self.root}):
            with self.assertRaises(RuntimeError):
                recover(self.root, digest)
        target = self.store.with_name('untouched.json')
        self.store.rename(target)
        self.store.symlink_to(target)
        with self.assertRaises(OSError):
            recover(self.root, digest)
        self.assertTrue(self.store.is_symlink())
        self.assertEqual(target.read_bytes(), b'')

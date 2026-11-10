"""Use actual private files/leases; cancellation and archives survive stale callbacks."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from login_check_store import LoginCheckStore
from login_receipt import LoginCheckFailure
from test_login_receipt import baseline


class LoginStoreTests(unittest.TestCase):
    def test_linked_lease_cannot_change_permissions_on_an_unrelated_owner_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'owner-config'
            target.write_text('owner-original')
            target.chmod(0o640)
            os.link(target, root / 'login-check.lock')
            with self.assertRaises(OSError):
                LoginCheckStore(root).arm(baseline())
            self.assertEqual(target.read_text(), 'owner-original')
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_missing_status_creates_nothing_and_cancel_fences_the_observer(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LoginCheckStore(Path(folder) / 'missing')
            self.assertIsNone(store.load())
            self.assertFalse(store.private.exists())
            value = baseline()
            store.arm(value)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            value.update(state='observing')
            self.assertTrue(store.replace(value['id'], value, expected='armed'))
            old = dict(value, state='passed')
            value.update(state='interrupted', reason='owner_cancelled_login_check')
            self.assertTrue(store.replace(value['id'], value))
            self.assertFalse(store.replace(value['id'], old))
            self.assertEqual(store.load()['state'], 'interrupted')

    def test_terminal_failure_is_archived_without_rewriting_before_new_arm(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LoginCheckStore(Path(folder))
            first = baseline()
            store.arm(first)
            with self.assertRaisesRegex(LoginCheckFailure, 'already_pending'):
                store.arm(first)
            first.update(state='failed', reason='login_ready_deadline')
            self.assertTrue(store.replace(first['id'], first, expected='armed'))
            new = dict(baseline(), id='b' * 32)
            store.arm(new)
            archive = store.private / 'login-check-history' / ('login-' + first['id'] + '.json')
            self.assertEqual(json.loads(archive.read_text()), first)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.load()['id'], new['id'])
            self.assertFalse(
                store.replace(first['id'], dict(first, state='passed'), expected='armed')
            )

    def test_linked_or_corrupt_pending_file_is_never_replaced_or_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = LoginCheckStore(root)
            target = root / 'owner-config'
            target.write_text('owner-original')
            target.chmod(0o600)
            store.path.symlink_to(target)
            with self.assertRaises(OSError):
                store.arm(baseline())
            self.assertEqual(target.read_text(), 'owner-original')
            store.path.unlink()
            store.path.write_text('{}')
            store.path.chmod(0o600)
            with self.assertRaises(LoginCheckFailure):
                store.arm(baseline())
            self.assertEqual(store.path.read_text(), '{}')

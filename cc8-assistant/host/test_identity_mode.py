"""Real private mode persistence, stale writers and failed commits; no device or service."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from identity_mode import IdentityModeStore, ModeState, ModeConflict, ModeCommitUncertain, validate
from reminder_store import ReminderCommitUncertain


class IdentityModeTests(unittest.TestCase):
    def store(self, folder):
        return IdentityModeStore(Path(folder) / 'private' / 'identity-mode.json')

    def test_passive_default_and_reboot_follow_explicit_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.store(folder)
            self.assertFalse(store.path.parent.exists())
            self.assertEqual(store.snapshot(), ModeState())
            self.assertFalse(store.path.parent.exists())
            guest = store.change(ModeState(), 'guest')
            self.assertEqual(guest, ModeState('guest', 1))
            self.assertEqual(self.store(folder).snapshot(), guest)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.path.parent.stat().st_mode & 0o777, 0o700)
            owner = store.change(guest, 'owner')
            self.assertEqual(self.store(folder).snapshot(), owner)

    def test_stale_revision_or_role_never_overwrites_saved_guest(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.store(folder)
            store.change(ModeState(), 'guest')
            before = store.path.read_bytes()
            for expected in (ModeState(), ModeState('owner', 1)):
                with self.assertRaises(ModeConflict):
                    store.change(expected, 'owner')
                self.assertEqual(store.path.read_bytes(), before)

    def test_invalid_schema_and_private_path_fail_without_overwrite(self):
        for value in (
            {'schema': True, 'mode': 'owner', 'revision': 0},
            {'schema': 1, 'mode': 'owner', 'revision': True},
            {'schema': 1, 'mode': 'unknown', 'revision': 1},
            {'schema': 1, 'mode': 'guest', 'revision': 1, 'extra': True},
        ):
            with self.assertRaises(ValueError):
                validate(value)
        with tempfile.TemporaryDirectory() as folder:
            store = self.store(folder)
            store.path.parent.mkdir(mode=0o700)
            store.path.write_text('{"schema":1,"mode":"guest","revision":true}')
            store.path.chmod(0o600)
            before = store.path.read_bytes()
            with self.assertRaises(ValueError):
                store.change(ModeState(), 'owner')
            self.assertEqual(store.path.read_bytes(), before)

    def test_file_and_lock_links_never_change_the_referenced_target(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.store(folder)
            store.path.parent.mkdir(mode=0o700)
            target = Path(folder) / 'target'
            target.write_text('unchanged')
            store.path.symlink_to(target)
            with self.assertRaises(OSError):
                store.change(ModeState(), 'guest')
            self.assertEqual(target.read_text(), 'unchanged')
            store.path.unlink()
            (store.path.parent / '.identity-mode.lock').unlink()
            (store.path.parent / '.identity-mode.lock').symlink_to(target)
            with self.assertRaises(OSError):
                store.change(ModeState(), 'guest')
            self.assertEqual(target.read_text(), 'unchanged')

    def test_failure_before_rename_keeps_previous_state_and_uncertain_rename_is_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.store(folder)
            guest = store.change(ModeState(), 'guest')
            before = store.path.read_bytes()
            with patch('reminder_store.os.replace', side_effect=OSError('synthetic failure')):
                with self.assertRaises(OSError):
                    store.change(guest, 'owner')
            self.assertEqual(store.path.read_bytes(), before)
            actual_save = __import__('reminder_store').ReminderStore.save

            def uncertain(disk, value):
                actual_save(disk, value)
                raise ReminderCommitUncertain('synthetic durability failure')

            with patch('identity_mode.ReminderStore.save', uncertain):
                with self.assertRaises(ModeCommitUncertain) as result:
                    store.change(guest, 'owner')
            self.assertEqual(result.exception.state, ModeState('owner', 2))
            self.assertEqual(store.snapshot(), result.exception.state)

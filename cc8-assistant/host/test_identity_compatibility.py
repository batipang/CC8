"""Real private guest state blocks legacy recovery, without changing stores."""

from pathlib import Path
import tempfile
import unittest

from identity_mode import IdentityModeStore, ModeState
from identity_compatibility import storage_gate


class IdentityCompatibilityTests(unittest.TestCase):
    def test_legacy_owner_is_allowed_but_guest_requires_reviewed_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            before, after = Path(folder) / 'before', Path(folder) / 'after'
            store = IdentityModeStore(
                before / 'device-access/private/voice-runtime/identity-mode.json'
            )
            source = after / 'cc8-assistant/host/bridge.py'
            source.parent.mkdir(parents=True)
            source.write_text('IDENTITY_READER_VERSION = 1\n')
            self.assertTrue(storage_gate(before, after)['compatible'])
            store.change(ModeState(), 'guest')
            original = store.path.read_bytes()
            self.assertFalse(storage_gate(before, after)['compatible'])
            target = IdentityModeStore(
                after / 'device-access/private/voice-runtime/identity-mode.json'
            )
            target.change(ModeState(), 'guest')
            self.assertTrue(storage_gate(before, after)['compatible'])
            for content in (
                '# legacy backend\n',
                'IDENTITY_READER_VERSION = True\n',
                'IDENTITY_READER_VERSION = 2\n',
            ):
                source.write_text(content)
                self.assertFalse(storage_gate(before, after)['compatible'])
            self.assertEqual(store.path.read_bytes(), original)
            store.change(ModeState('guest', 1), 'owner')
            self.assertFalse(storage_gate(before, after)['compatible'])
            target.change(ModeState('guest', 1), 'owner')
            self.assertTrue(storage_gate(before, after)['compatible'])

    def test_corrupt_guest_state_never_becomes_legacy_compatible(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = IdentityModeStore(
                root / 'device-access/private/voice-runtime/identity-mode.json'
            )
            store.change(ModeState(), 'guest')
            store.path.write_bytes(b'corrupt')
            with self.assertRaises(ValueError):
                storage_gate(root, root)
            self.assertEqual(store.path.read_bytes(), b'corrupt')

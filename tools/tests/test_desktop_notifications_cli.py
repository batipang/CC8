"""Verify helper identity, ownership and replacement without opening an app."""

import hashlib
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from tools.desktop_notifications_cli import install, build
from desktop_notifications import APP_NAME, BUNDLE_ID, source_hashes, verify_bundle, directory_lock

ROOT = Path(__file__).resolve().parents[2]


class NotifierArtifacts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.stage = self.folder / 'stage'
        self.stage.mkdir(mode=0o700)
        self.make(self.stage)

    def make(self, target):
        bundle = target / APP_NAME
        binary = bundle / 'Contents/MacOS/CC8Notifier'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'public inert fixture, not executable')
        signature = bundle / 'Contents/_CodeSignature/CodeResources'
        signature.parent.mkdir()
        signature.write_bytes(b'public inert signature fixture')
        info = bundle / 'Contents/Info.plist'
        info.write_bytes(
            plistlib.dumps(
                {
                    'CFBundleIdentifier': BUNDLE_ID,
                    'CFBundleExecutable': 'CC8Notifier',
                    'LSUIElement': True,
                }
            )
        )
        receipt = {
            'protocol': 1,
            'sources': source_hashes(ROOT),
            'files': {
                str(p.relative_to(bundle)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in bundle.rglob('*')
                if p.is_file()
            },
        }
        (target / 'build.json').write_text(json.dumps(receipt))
        (target / 'build.json').chmod(0o600)

    def test_current_source_and_exact_artifact_inventory_verified_before_codesign(self):
        with patch('desktop_notifications.subprocess.run') as run:
            self.assertEqual(verify_bundle(self.stage), self.stage / APP_NAME)
            run.assert_called_once()

    def test_tampered_binary_or_source_refused_before_running_anything(self):
        binary = self.stage / APP_NAME / 'Contents/MacOS/CC8Notifier'
        binary.write_bytes(b'tampered')
        with patch('desktop_notifications.subprocess.run') as run, self.assertRaises(ValueError):
            verify_bundle(self.stage)
        run.assert_not_called()

    def test_changed_source_receipt_is_not_accepted_as_current(self):
        p = self.stage / 'build.json'
        d = json.loads(p.read_text())
        d['sources']['mac-notifier/Package.swift'] = '0' * 64
        p.write_text(json.dumps(d))
        with patch('desktop_notifications.subprocess.run') as run, self.assertRaises(ValueError):
            verify_bundle(self.stage)
        run.assert_not_called()

    def test_symlink_binary_and_world_writable_directory_refused(self):
        p = self.stage / APP_NAME / 'Contents/MacOS/CC8Notifier'
        p.unlink()
        p.symlink_to(self.folder / 'missing')
        with self.assertRaises(OSError):
            verify_bundle(self.stage)
        self.stage.chmod(0o777)
        with self.assertRaises(OSError):
            verify_bundle(self.stage)

    def test_busy_owned_helper_cannot_be_replaced(self):
        with directory_lock(self.stage), self.assertRaises(BlockingIOError):
            with directory_lock(self.stage):
                pass

    def test_unknown_existing_install_is_preserved(self):
        target = self.folder / 'installed'
        target.mkdir(mode=0o700)
        app = target / APP_NAME
        app.mkdir()
        (app / 'keep.txt').write_text('public fixed original')
        with (
            patch('desktop_notifications.subprocess.run'),
            patch('tools.desktop_notifications_cli.install_directory', return_value=target),
            self.assertRaises(OSError),
        ):
            install(self.stage)
        self.assertEqual((app / 'keep.txt').read_text(), 'public fixed original')

    def test_known_bundle_with_unknown_owner_file_is_not_deleted_by_setup(self):
        target = self.folder / 'installed'
        target.mkdir(mode=0o700)
        self.make(target)
        note = target / APP_NAME / 'Contents/owner-note'
        note.write_text('public owner fixture')
        with (
            patch('desktop_notifications.subprocess.run'),
            patch('tools.desktop_notifications_cli.install_directory', return_value=target),
            self.assertRaises(ValueError),
        ):
            install(self.stage)
        self.assertEqual(note.read_text(), 'public owner fixture')

    def build_fixture(self, mutate):
        root = self.folder / 'build-root'
        (root / 'mac-notifier/Sources/CC8Notifier').mkdir(parents=True)
        (root / 'mac-notifier/Package.swift').write_text('public package fixture')
        source = root / 'mac-notifier/Sources/CC8Notifier/main.swift'
        source.write_text('public source fixture')
        binary = root / 'bin/CC8Notifier'
        binary.parent.mkdir()
        binary.write_bytes(b'public binary fixture')

        def run(argv, **kwargs):
            if argv[0] == '/usr/bin/swift' and mutate:
                source.write_text('changed while compiling')
            if argv[0] == '/usr/bin/codesign' and '--sign' in argv:
                signature = Path(argv[-1]) / 'Contents/_CodeSignature/CodeResources'
                signature.parent.mkdir()
                signature.write_bytes(b'public signature fixture')

        with (
            patch('tools.desktop_notifications_cli.platform.system', return_value='Darwin'),
            patch('tools.desktop_notifications_cli.subprocess.run', side_effect=run),
            patch(
                'tools.desktop_notifications_cli.subprocess.check_output',
                return_value=str(binary.parent),
            ),
        ):
            return build(root)

    def test_unchanged_build_can_publish_exact_verified_app_without_launching(self):
        stage = self.build_fixture(False)
        self.assertTrue((stage / APP_NAME).is_dir())

    def test_source_changed_during_compile_cannot_publish_as_new_source(self):
        with self.assertRaises(ValueError):
            self.build_fixture(True)
        self.assertFalse(
            (self.folder / 'build-root/.toolchains/cc8/desktop-notifier' / APP_NAME).exists()
        )

    def test_install_does_not_ask_permission_or_launch_or_delete_unrelated_files(self):
        target = self.folder / 'installed'
        target.mkdir(mode=0o700)
        keep = target / 'owner-note'
        keep.write_text('public fixed fixture')
        with (
            patch('desktop_notifications.subprocess.run') as run,
            patch('tools.desktop_notifications_cli.install_directory', return_value=target),
        ):
            install(self.stage)
        self.assertTrue((target / APP_NAME / 'Contents/MacOS/CC8Notifier').is_file())
        self.assertEqual(keep.read_text(), 'public fixed fixture')
        self.assertTrue(all(call.args[0][0] == '/usr/bin/codesign' for call in run.call_args_list))
        self.assertEqual((target / 'build.json').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()

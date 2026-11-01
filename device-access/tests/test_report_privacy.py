"""Installer report permissions are checked with fake devices and a permissive umask."""

from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'private_report_installer', Path(__file__).resolve().parents[1] / 'cc8_install.py'
)
access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(access)


class ReportPrivacyTests(unittest.TestCase):
    def scope(self, root, *, install_result=None):
        stack = ExitStack()
        stack.enter_context(patch.object(access, 'ROOT', root))
        tools = root / 'fake-tool'
        tools.write_text('fixture')
        for name in ('ADB', 'AAPT', 'APKSIGNER'):
            stack.enter_context(patch.object(access, name, tools))
        target = stack.enter_context(patch.object(access, 'target', return_value='7'))
        stack.enter_context(
            patch.object(
                access,
                'probe',
                return_value={'shell_verified': True, 'uid': 2000, 'model': 'AILABS_S6', 'sdk': 27},
            )
        )
        install = stack.enter_context(
            patch.object(
                access,
                'install',
                return_value=install_result
                or {'installed': True, 'installed_apk_hash_verified': True},
            )
        )
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack, target, install

    def test_report_is_private_even_under_permissive_umask(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'device-access').mkdir()
            stack, target, install = self.scope(root)
            old = os.umask(0)
            try:
                with stack:
                    access.main(['--install', str(root / 'candidate.apk')])
            finally:
                os.umask(old)
            private = root / 'device-access/private'
            reports = list(private.glob('apk-install-result*.json'))
            self.assertEqual(len(reports), 1)
            self.assertEqual(private.stat().st_mode & 0o777, 0o700)
            self.assertEqual(reports[0].stat().st_mode & 0o777, 0o600)
            self.assertTrue(json.loads(reports[0].read_text())['installed'])
            self.assertEqual(install.call_count, 1)
            self.assertEqual(target.call_count, 1)

    def test_public_private_directory_is_rejected_before_any_device_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            private = root / 'device-access/private'
            private.mkdir(parents=True)
            private.chmod(0o755)
            stack, target, install = self.scope(root)
            with stack, self.assertRaises(SystemExit):
                access.main(['--install', str(root / 'candidate.apk')])
            target.assert_not_called()
            install.assert_not_called()
            self.assertEqual(private.stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(private.iterdir()), [])

    def test_symlink_directory_and_existing_reports_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            outside = root / 'outside'
            outside.mkdir(mode=0o700)
            device = root / 'device-access'
            device.mkdir()
            private = device / 'private'
            private.symlink_to(outside, target_is_directory=True)
            stack, target, install = self.scope(root)
            with stack, self.assertRaises(SystemExit):
                access.main(['--install', str(root / 'candidate.apk')])
            target.assert_not_called()
            install.assert_not_called()
            self.assertEqual(list(outside.iterdir()), [])
            private.unlink()
            private.mkdir(mode=0o700)
            old = private / 'apk-install-result.json'
            old.write_text('old report')
            old.chmod(0o600)
            stack, target, install = self.scope(root)
            with stack:
                access.main(['--install', str(root / 'candidate.apk')])
                access.main(['--install', str(root / 'candidate.apk')])
            self.assertEqual(old.read_text(), 'old report')
            self.assertEqual(len(list(private.glob('apk-install-result*.json'))), 3)
            self.assertEqual(install.call_count, 2)

    def test_private_receipt_is_reserved_before_device_probe_and_unknown_on_failed_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'device-access').mkdir()
            stack, target, install = self.scope(root)

            def before_probe():
                reports = list((root / 'device-access/private').glob('apk-install-result*.json'))
                self.assertEqual(len(reports), 1)
                self.assertEqual(
                    json.loads(reports[0].read_text())['installation_result'], 'not_started'
                )
                return '7'

            target.side_effect = before_probe
            install.side_effect = RuntimeError('fixture failed after dispatch')
            with stack, self.assertRaises(SystemExit):
                access.main(['--install', str(root / 'candidate.apk')])
            report = next((root / 'device-access/private').glob('apk-install-result*.json'))
            data = json.loads(report.read_text())
            self.assertEqual(data['report_state'], 'result_unconfirmed')
            self.assertEqual(data['installation_result'], 'unconfirmed')
            self.assertNotIn('installed', data)
            self.assertNotIn('fixture failed', report.read_text())
            self.assertEqual(install.call_count, 1)

    def test_report_save_failure_after_install_does_not_retry_or_claim_no_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'device-access').mkdir()
            stack, target, install = self.scope(root)
            write = access.PrivateOutput.write
            calls = []

            def fail_final(output, value):
                calls.append(value)
                if len(calls) == 3:
                    raise OSError('PRIVATE disk error')
                return write(output, value)

            with (
                stack,
                patch.object(access.PrivateOutput, 'write', fail_final),
                self.assertRaises(SystemExit) as result,
            ):
                access.main(['--install', str(root / 'candidate.apk')])
            self.assertIn('may have completed', str(result.exception))
            self.assertNotIn('PRIVATE', str(result.exception))
            self.assertEqual(install.call_count, 1)
            self.assertEqual(target.call_count, 1)
            report = next((root / 'device-access/private').glob('apk-install-result*.json'))
            self.assertEqual(json.loads(report.read_text())['installation_result'], 'unconfirmed')

    def test_read_only_probe_does_not_create_private_report_or_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stack, target, install = self.scope(root)
            with stack:
                access.main(['--probe'])
            self.assertFalse((root / 'device-access/private').exists())
            install.assert_not_called()
            self.assertEqual(target.call_count, 1)

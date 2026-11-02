"""Signature preflight failures must precede every device write."""

import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'signed_install', Path(__file__).parents[1] / 'cc8_install.py'
)
access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(access)


class PreflightTests(unittest.TestCase):
    def exercise(
        self, *, signers=None, changed=False, installed=True, blocked=False, read_error=False
    ):
        data = b'installed APK fixture'
        digest = hashlib.sha256(data).hexdigest()
        written = []

        def shell(transport, command):
            if command.startswith('pm path'):
                return 'package:/data/app/example/base.apk' if installed else ''
            if command.startswith('pm list'):
                return 'Error: blocked' if blocked else ''
            if command.startswith('sha256sum'):
                return ('0' * 64 if changed else digest) + '  base.apk'
            raise AssertionError('Unexpected write: ' + command)

        def read(args, **kwargs):
            self.assertIn('toybox head -c ', args[-1])
            self.assertEqual(kwargs['stdout'].fileno() > 0, True)
            if read_error:
                raise subprocess.TimeoutExpired(args, 30)
            kwargs['stdout'].write(data)
            written.append(kwargs['stdout'].name)
            return subprocess.CompletedProcess(args, 0)

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            candidate = directory / 'candidate.apk'
            candidate.write_bytes(b'candidate APK')
            with (
                patch.object(access, 'require_java', return_value=Path('/jdk/bin')),
                patch.object(access, 'shell', side_effect=shell),
                patch.object(
                    access,
                    'signer_fingerprints',
                    side_effect=signers or [frozenset({'a'}), frozenset({'a'})],
                ),
                patch.object(access, 'apk_package', return_value=('com.example.app', None)),
                patch.object(access.subprocess, 'run', side_effect=read),
            ):
                result = access.signature_preflight('7', 'com.example.app', candidate, directory)
            for name in written:
                self.assertEqual(Path(name).stat().st_mode & 0o777, 0o600)
            return result

    def test_matching_installed_signers_and_confirmed_new_install(self):
        self.assertEqual(self.exercise()['signature_continuity'], 'matching_installed_signers')
        self.assertEqual(self.exercise(installed=False)['signature_continuity'], 'new_package')

    def test_mismatch_changed_unreadable_or_ambiguous_absence_fail_closed(self):
        for options in (
            {'signers': [frozenset({'a'}), frozenset({'b'})]},
            {'changed': True},
            {'read_error': True},
            {'installed': False, 'blocked': True},
        ):
            with self.subTest(options=options), self.assertRaises(RuntimeError):
                self.exercise(**options)

    def test_invalid_split_or_error_paths_are_rejected(self):
        for value in (
            'Error: denied',
            'package:relative.apk',
            'package:/a.apk\npackage:/b.apk',
            'package:/a.apk\x00x',
        ):
            with patch.object(access, 'shell', return_value=value), self.assertRaises(RuntimeError):
                access.package_paths('7', 'com.example.app')

    def test_oem_absent_path_status_one_requires_independent_successful_empty_list(self):
        absent = RuntimeError('CC8 shell command failed (1): ')
        with patch.object(access, 'shell', side_effect=[absent, '']) as shell:
            self.assertEqual(access.package_paths('7', 'com.example.app'), [])
            self.assertEqual(shell.call_args.args[1], 'pm list packages com.example.app')
        for second in ('package:com.example.app', 'Error: blocked', RuntimeError('closed')):
            with (
                self.subTest(second=type(second).__name__),
                patch.object(access, 'shell', side_effect=[absent, second]),
                self.assertRaises(RuntimeError),
            ):
                access.package_paths('7', 'com.example.app')
        for error in (
            'CC8 shell command failed (1): denied',
            'CC8 shell command failed (2): ',
            'closed',
        ):
            with (
                patch.object(access, 'shell', side_effect=RuntimeError(error)) as shell,
                self.assertRaises(RuntimeError),
            ):
                access.package_paths('7', 'com.example.app')
            self.assertEqual(shell.call_count, 1)

    def test_failed_preflight_never_sends_helper_or_apk(self):
        with tempfile.TemporaryDirectory() as folder:
            apk = Path(folder) / 'candidate.apk'
            apk.write_bytes(b'apk')
            with (
                patch.object(access, 'apk_package', return_value=('com.example.app', None)),
                patch.object(
                    access, 'signature_preflight', side_effect=access.SignatureError('mismatch')
                ),
                patch.object(access, 'install_stream') as install,
                patch.object(access, 'shell') as shell,
            ):
                with self.assertRaises(access.SignatureError):
                    access.install('7', apk, False)
            install.assert_not_called()
            shell.assert_not_called()

    def test_candidate_snapshot_is_same_bytes_that_are_installed(self):
        with tempfile.TemporaryDirectory() as folder:
            apk = Path(folder) / 'candidate.apk'
            apk.write_bytes(b'original')
            snapshots = []

            def verify(transport, package, snapshot, directory):
                self.assertEqual(snapshot.read_bytes(), b'original')
                snapshots.append(snapshot)
                apk.write_bytes(b'replaced build output')
                return {'signature_continuity': 'matching_installed_signers'}

            with (
                patch.object(access, 'apk_package', return_value=('com.example.app', None)),
                patch.object(access, 'signature_preflight', side_effect=verify),
                patch.object(access, '_install_verified', return_value={}) as install,
            ):
                access.install('7', apk, False)
            self.assertEqual(install.call_args.args[3], b'original')
            self.assertFalse(snapshots[0].parent.exists())


if __name__ == '__main__':
    unittest.main()

"""Exercise installer trust boundaries without contacting a real device."""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'cc8_install', Path(__file__).resolve().parents[1] / 'cc8_install.py'
)
access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(access)


class InstallerTests(unittest.TestCase):
    def devices(self, text):
        return patch.object(
            access.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, text, '')
        )

    def test_target_accepts_only_one_online_usb_model(self):
        with self.devices(
            'List of devices attached\nFAKE123 device product:cc8 model:AILABS_S6 transport_id:7\n'
        ):
            self.assertEqual(access.target(), '7')
        for text in [
            'FAKE123 offline model:AILABS_S6 transport_id:7\n',
            'FAKE123 device model:OTHER transport_id:7\n',
            '192.168.1.8:5555 device model:AILABS_S6 transport_id:7\n',
            'host:5555 device model:AILABS_S6 transport_id:7\n',
            'FAKE123 device model:AILABS_S6 transport_id:7\nFAKE456 device model:AILABS_S6 transport_id:8\n',
        ]:
            with self.subTest(text=text), self.devices(text), self.assertRaises(RuntimeError):
                access.target()

    def test_probe_rejects_wrong_uid_model_sdk_or_closed_channel(self):
        for result in [
            'closed',
            'CC8_SHELL_OK\n0\nAILABS_S6\n27\n',
            'CC8_SHELL_OK\n2000\nOTHER\n27\n',
            'CC8_SHELL_OK\n2000\nAILABS_S6\n28\n',
        ]:
            with (
                patch.object(access, 'shell', return_value=result),
                self.assertRaises(RuntimeError),
            ):
                access.probe('7')
        with patch.object(access, 'shell', return_value='CC8_SHELL_OK\n2000\nAILABS_S6\n27\n'):
            self.assertEqual(access.probe('7')['uid'], 2000)

    def test_helper_and_source_must_match_metadata(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(access, 'ROOT', Path(folder)):
            helper = Path(folder) / 'artifacts/cc8-installer-shell-1.0.jar'
            source = Path(folder) / 'device-access/installer-helper/Cc8Installer.java'
            helper.parent.mkdir()
            source.parent.mkdir(parents=True)
            helper.write_bytes(b'dex helper')
            source.write_bytes(b'java source')
            helper.with_suffix('.json').write_text(
                json.dumps(
                    {
                        'sha256': hashlib.sha256(helper.read_bytes()).hexdigest(),
                        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                )
            )
            self.assertEqual(access.helper_artifact(), b'dex helper')
            source.write_bytes(b'changed source')
            with self.assertRaises(ValueError):
                access.helper_artifact()
            source.write_bytes(b'java source')
            helper.write_bytes(b'changed binary')
            with self.assertRaises(ValueError):
                access.helper_artifact()

    def stream(self, callback=True, hash_ok=True):
        calls = []

        def shell(transport, command, data=None, timeout=30):
            calls.append((command, data))
            if command.startswith('stat -c'):
                return '2000 2000 771'
            if command.startswith('sha256sum'):
                return hashlib.sha256(b'helper').hexdigest() if hash_ok else 'bad'
            if command.startswith('CLASSPATH'):
                return 'CC8_APK_STREAM_VERIFIED bytes=3\n' + (
                    'CC8_PACKAGE_INSTALLED com.example.app version=1\n' if callback else ''
                )
            return ''

        with (
            patch.object(access, 'helper_artifact', return_value=b'helper'),
            patch.object(access, 'shell', side_effect=shell),
        ):
            if callback and hash_ok:
                access.install_stream(
                    '7', 'com.example.app', b'apk', hashlib.sha256(b'apk').hexdigest()
                )
            else:
                with self.assertRaises(RuntimeError):
                    access.install_stream(
                        '7', 'com.example.app', b'apk', hashlib.sha256(b'apk').hexdigest()
                    )
        self.assertTrue(calls[-1][0].startswith('rm -f /data/local/tmp/.cc8-installer-'))
        return calls

    def test_stream_waits_for_real_install_callback_and_cleans_on_failure(self):
        self.stream()
        self.stream(callback=False)

    def test_bad_helper_transfer_hash_prevents_install_and_still_cleans(self):
        calls = self.stream(hash_ok=False)
        self.assertFalse(any(command.startswith('CLASSPATH') for command, _ in calls))

    def test_closed_console_pipe_cannot_interrupt_install_verification(self):
        payload = b'apk'
        digest = hashlib.sha256(payload).hexdigest()
        for closed_stream in ('stdout', 'stderr'):
            commands = []

            def shell(transport, command, data=None, timeout=30):
                commands.append(command)
                if command.startswith('sha256sum /sdcard/'):
                    return hashlib.sha256(b'helper').hexdigest()
                if command.startswith('CLASSPATH='):
                    self.assertEqual(data, payload)
                    pipe_reader.close()
                    return (
                        'CC8_APK_STREAM_VERIFIED bytes=3\n'
                        'CC8_PACKAGE_INSTALLED com.example.app version=1\n'
                    )
                if command == 'pm path com.example.app':
                    return 'package:/data/app/test/base.apk'
                if command == 'sha256sum /data/app/test/base.apk':
                    return digest
                return ''

            reader, writer = os.pipe()
            with (
                self.subTest(closed_stream=closed_stream),
                os.fdopen(reader, 'rb') as pipe_reader,
                io.TextIOWrapper(
                    os.fdopen(writer, 'wb', buffering=0), write_through=True
                ) as broken,
                patch.object(
                    access.sys, 'stdout', broken if closed_stream == 'stdout' else io.StringIO()
                ),
                patch.object(
                    access.sys, 'stderr', broken if closed_stream == 'stderr' else io.StringIO()
                ),
                patch.object(access, 'helper_artifact', return_value=b'helper'),
                patch.object(access, 'shell', side_effect=shell),
            ):
                if closed_stream == 'stderr':
                    pipe_reader.close()
                receipt = access._install_verified(
                    '7', 'com.example.app', None, payload, False, True, {}, lambda: None
                )
                self.assertTrue(receipt['installed_apk_hash_verified'])
                self.assertEqual(sum(command.startswith('CLASSPATH=') for command in commands), 1)
                self.assertEqual(sum(command.startswith('rm -f ') for command in commands), 1)
                open_stream = access.sys.stderr if closed_stream == 'stdout' else access.sys.stdout
                if closed_stream == 'stdout':
                    self.assertIn('system install callback verified', open_stream.getvalue())
                else:
                    self.assertEqual(open_stream.getvalue(), '')

    def test_shell_uses_argument_array_engineering_prefix_and_binary_stdin(self):
        result = subprocess.CompletedProcess([], 0, b'ok', b'')
        with patch.object(access.subprocess, 'run', return_value=result) as run:
            self.assertEqual(access.shell('7', 'cat > /safe', b'\x00\xff'), 'ok')
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], access.PREFIX + 'cat > /safe')
        self.assertEqual(kwargs['input'], b'\x00\xff')
        self.assertNotIn('shell', kwargs)

    def test_installed_hash_missing_wrong_or_unreadable_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as folder:
            apk = Path(folder) / 'app.apk'
            apk.write_bytes(b'signed test apk')
            metadata = (
                "package: name='com.example.app'\nlaunchable-activity: name='com.example.Main'\n"
            )
            for checksum in ['', 'bad hash', RuntimeError('read denied')]:
                with (
                    self.subTest(checksum=repr(checksum)),
                    patch.object(access.subprocess, 'check_output', return_value=metadata),
                    patch.object(access, 'signature_preflight', return_value={}),
                    patch.object(access, 'install_stream'),
                    patch.object(
                        access, 'shell', side_effect=['package:/data/app/test/base.apk', checksum]
                    ) as shell,
                ):
                    with self.assertRaises(RuntimeError):
                        access.install('7', apk, True)
                    self.assertEqual(shell.call_count, 2)
            digest = hashlib.sha256(apk.read_bytes()).hexdigest()
            with (
                patch.object(access.subprocess, 'check_output', return_value=metadata),
                patch.object(access, 'signature_preflight', return_value={}),
                patch.object(access, 'install_stream'),
                patch.object(
                    access,
                    'shell',
                    side_effect=['package:/data/app/test/base.apk', digest + '  base.apk'],
                ),
            ):
                result = access.install('7', apk, False)
                self.assertTrue(result['installed'])
                self.assertTrue(result['installed_apk_hash_verified'])

    def test_empty_transferred_helper_hash_is_a_controlled_failure(self):
        calls = []

        def shell(transport, command, data=None, timeout=30):
            calls.append(command)
            if command.startswith('stat -c'):
                return '2000 2000 771'
            return ''

        with (
            patch.object(access, 'helper_artifact', return_value=b'helper'),
            patch.object(access, 'shell', side_effect=shell),
        ):
            with self.assertRaisesRegex(RuntimeError, 'helper hash mismatch'):
                access.install_stream('7', 'com.example.app', b'apk', '0' * 64)
        self.assertTrue(calls[-1].startswith('rm -f '))

    def test_staging_rejects_shared_or_unverified_directory_permissions(self):
        for output in ['', '2000 2000 777', '2000 1000 771', '1000 2000 771', '2000 2000 400']:
            with (
                self.subTest(output=output),
                patch.object(access, 'shell', return_value=output),
                self.assertRaises(RuntimeError),
            ):
                access.private_staging('7')
        with patch.object(access, 'shell', return_value='2000 2000 771'):
            self.assertEqual(access.private_staging('7'), '/data/local/tmp')

    def test_denied_protected_write_aborts_without_running_installer(self):
        calls = []

        def shell(transport, command, data=None, timeout=30):
            calls.append(command)
            if command.startswith('umask'):
                raise RuntimeError('Permission denied')
            return ''

        with (
            patch.object(access, 'private_staging', return_value='/data/local/tmp'),
            patch.object(access, 'helper_artifact', return_value=b'helper'),
            patch.object(access, 'shell', side_effect=shell),
        ):
            with self.assertRaisesRegex(RuntimeError, 'installation was not attempted'):
                access.install_stream('7', 'com.example.app', b'apk', '0' * 64)
        self.assertFalse(any(command.startswith('CLASSPATH') for command in calls))
        self.assertFalse(any('/sdcard/' in command for command in calls))
        self.assertTrue(calls[-1].startswith('rm -f '))

    def test_protected_staging_probe_only_verifies_empty_marker_creation_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:

            def local_shell(_transport, command, *, timeout):
                self.assertNotIn('app_process', command)
                self.assertNotIn('/sdcard', command)
                return subprocess.check_output(
                    ['/bin/sh', '-c', command], timeout=timeout, text=True
                )

            with (
                patch.object(access, 'private_staging', return_value=directory),
                patch.object(access, 'shell', side_effect=local_shell),
            ):
                self.assertEqual(access.probe_protected_staging('7'), 'verified')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_staging_preflight_stops_on_creation_cleanup_or_channel_failure(self):
        for outcome, expected in (
            ('CC8_STAGING_CREATE_FAILED\n', 'create_failed'),
            ('CC8_STAGING_CLEANUP_UNCONFIRMED\n', 'cleanup_unconfirmed'),
            ('unexpected\nCC8_STAGING_OK\n', 'unconfirmed'),
            (subprocess.TimeoutExpired('adb', 10), 'unconfirmed'),
        ):
            stdout = io.StringIO()
            with (
                self.subTest(expected=expected),
                patch.object(access, 'target', return_value='7'),
                patch.object(access, 'probe', return_value={'shell_verified': True}),
                patch.object(access, 'shell', side_effect=['2000 2000 771', outcome]) as shell,
                patch.object(access.sys, 'stdout', stdout),
                self.assertRaises(SystemExit) as failure,
            ):
                access._perform_actions(SimpleNamespace(probe_staging=True))
            self.assertEqual(failure.exception.code, 1)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report['protected_staging'], expected)
            self.assertIs(report['installation_started'], False)
            self.assertEqual(shell.call_count, 2)

    def test_failure_receipt_distinguishes_staging_refusal_from_dispatched_install(self):
        args = SimpleNamespace(
            probe_staging=False,
            verify_apk=None,
            install=Path('candidate.apk'),
            launch=False,
            shared_staging=False,
            expect_installed_sha256=None,
            expect_apk_sha256=None,
        )
        for dispatched in (False, True):
            with (
                self.subTest(dispatched=dispatched),
                tempfile.TemporaryDirectory() as folder,
                patch.object(access, 'target', return_value='7'),
                patch.object(access, 'probe', return_value={'shell_verified': True}),
            ):
                path = Path(folder) / 'install.json'

                def install(*args, before_dispatch, **kwargs):
                    if dispatched:
                        before_dispatch()
                        raise RuntimeError('private details from a lost receipt')
                    raise access.ProtectedStagingDenied('private details from device')

                with (
                    access.PrivateOutput(path) as output,
                    patch.object(access, 'install', side_effect=install),
                    patch('sys.stdout', new_callable=io.StringIO),
                    self.assertRaises(RuntimeError),
                ):
                    access._perform_actions(args, output)
                receipt = json.loads(path.read_text())
                self.assertNotIn('private details', path.read_text())
                self.assertEqual(
                    receipt['installation_result'], 'unconfirmed' if dispatched else 'not_started'
                )
                self.assertEqual(
                    receipt['report_state'],
                    'result_unconfirmed' if dispatched else 'pre_dispatch_failed',
                )
                self.assertEqual(
                    receipt.get('failure_reason'),
                    None if dispatched else 'protected_staging_denied',
                )

    def test_shared_path_is_used_only_when_explicitly_selected(self):
        calls = []

        def shell(transport, command, data=None, timeout=30):
            calls.append(command)
            if command.startswith('sha256sum'):
                return hashlib.sha256(b'helper').hexdigest()
            if command.startswith('CLASSPATH'):
                return 'CC8_APK_STREAM_VERIFIED bytes=3\nCC8_PACKAGE_INSTALLED com.example.app version=1\n'
            return ''

        with (
            patch.object(
                access,
                'private_staging',
                side_effect=AssertionError('Explicit mode should choose its path'),
            ),
            patch.object(access, 'helper_artifact', return_value=b'helper'),
            patch.object(access, 'shell', side_effect=shell),
        ):
            access.install_stream('7', 'com.example.app', b'apk', '0' * 64, shared_staging=True)
        self.assertTrue(
            any(command.startswith('umask') and '/sdcard/Download/' in command for command in calls)
        )


if __name__ == '__main__':
    unittest.main()

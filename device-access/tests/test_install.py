"""Exercise installer trust boundaries without contacting a real device."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('cc8_install', Path(__file__).resolve().parents[1]/'cc8_install.py')
access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(access)


class InstallerTests(unittest.TestCase):
    def devices(self, text):
        return patch.object(access.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, text, ''))

    def test_target_accepts_only_one_online_usb_model(self):
        with self.devices('List of devices attached\nFAKE123 device product:cc8 model:AILABS_S6 transport_id:7\n'):
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
        for result in ['closed', 'CC8_SHELL_OK\n0\nAILABS_S6\n27\n',
                       'CC8_SHELL_OK\n2000\nOTHER\n27\n', 'CC8_SHELL_OK\n2000\nAILABS_S6\n28\n']:
            with patch.object(access, 'shell', return_value=result), self.assertRaises(RuntimeError):
                access.probe('7')
        with patch.object(access, 'shell', return_value='CC8_SHELL_OK\n2000\nAILABS_S6\n27\n'):
            self.assertEqual(access.probe('7')['uid'], 2000)

    def test_helper_and_source_must_match_metadata(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(access, 'ROOT', Path(folder)):
            helper = Path(folder)/'artifacts/cc8-installer-shell-1.0.jar'
            source = Path(folder)/'device-access/installer-helper/Cc8Installer.java'
            helper.parent.mkdir(); source.parent.mkdir(parents=True)
            helper.write_bytes(b'dex helper'); source.write_bytes(b'java source')
            helper.with_suffix('.json').write_text(json.dumps({
                'sha256': hashlib.sha256(helper.read_bytes()).hexdigest(),
                'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}))
            self.assertEqual(access.helper_artifact(), b'dex helper')
            source.write_bytes(b'changed source')
            with self.assertRaises(ValueError): access.helper_artifact()
            source.write_bytes(b'java source'); helper.write_bytes(b'changed binary')
            with self.assertRaises(ValueError): access.helper_artifact()

    def stream(self, callback=True, hash_ok=True):
        calls = []
        def shell(transport, command, data=None, timeout=30):
            calls.append((command, data))
            if command.startswith('sha256sum'):
                return hashlib.sha256(b'helper').hexdigest() if hash_ok else 'bad'
            if command.startswith('CLASSPATH'):
                return 'CC8_APK_STREAM_VERIFIED bytes=3\n'+('CC8_PACKAGE_INSTALLED com.example.app version=1\n' if callback else '')
            return ''
        with patch.object(access, 'helper_artifact', return_value=b'helper'), patch.object(access, 'shell', side_effect=shell):
            if callback and hash_ok:
                access.install_stream('7', 'com.example.app', b'apk', hashlib.sha256(b'apk').hexdigest())
            else:
                with self.assertRaises(RuntimeError):
                    access.install_stream('7', 'com.example.app', b'apk', hashlib.sha256(b'apk').hexdigest())
        self.assertTrue(calls[-1][0].startswith('rm -f /sdcard/Download/.cc8-installer-'))
        return calls

    def test_stream_waits_for_real_install_callback_and_cleans_on_failure(self):
        self.stream(); self.stream(callback=False)

    def test_bad_helper_transfer_hash_prevents_install_and_still_cleans(self):
        calls = self.stream(hash_ok=False)
        self.assertFalse(any(command.startswith('CLASSPATH') for command, _ in calls))

    def test_shell_uses_argument_array_engineering_prefix_and_binary_stdin(self):
        result = subprocess.CompletedProcess([], 0, b'ok', b'')
        with patch.object(access.subprocess, 'run', return_value=result) as run:
            self.assertEqual(access.shell('7', 'cat > /safe', b'\x00\xff'), 'ok')
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], access.PREFIX+'cat > /safe')
        self.assertEqual(kwargs['input'], b'\x00\xff')
        self.assertNotIn('shell', kwargs)


if __name__ == '__main__':
    unittest.main()

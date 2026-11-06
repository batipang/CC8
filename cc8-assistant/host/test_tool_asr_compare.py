import hashlib
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from tool_asr_compare import compare_local, verified_whisper_snapshot


class ToolAsrCompareTests(unittest.TestCase):
    def test_fixed_model_checks_snapshot_hash_and_file_type(self):
        with TemporaryDirectory() as directory:
            cache = Path(directory) / 'cache'
            snapshot = cache / 'snapshots' / 'pinned'
            snapshot.mkdir(parents=True)
            blobs = cache / 'blobs'
            blobs.mkdir()
            data = b'fixed weights'
            model = blobs / 'weights'
            model.write_bytes(data)
            artifact = snapshot / 'weights.safetensors'
            artifact.symlink_to(model)
            manifest = {
                'revision': 'pinned',
                'files': {
                    'weights.safetensors': {
                        'size': len(data),
                        'sha256': hashlib.sha256(data).hexdigest(),
                    }
                },
            }
            expected = verified_whisper_snapshot(cache, manifest)
            self.assertEqual(expected[0], snapshot.resolve())
            self.assertEqual(
                verified_whisper_snapshot(cache, manifest, verify_hashes=False), expected
            )
            shared = cache.parent / 'blobs' / '78' / 'weights'
            shared.parent.mkdir(parents=True)
            model.rename(shared)
            model.symlink_to(shared)
            self.assertEqual(verified_whisper_snapshot(cache, manifest)[0], snapshot.resolve())
            model.write_bytes(b'wrong weights')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                verified_whisper_snapshot(cache, manifest)
            model.unlink()
            os.mkfifo(model)
            with self.assertRaisesRegex(ValueError, 'invalid size'):
                verified_whisper_snapshot(cache, manifest)
            outside = Path(directory) / 'outside'
            outside.write_bytes(data)
            artifact.unlink()
            artifact.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'outside its cache'):
                verified_whisper_snapshot(cache, manifest)
            with self.assertRaises(FileNotFoundError):
                verified_whisper_snapshot(cache, {**manifest, 'revision': 'missing'})

    def child(self, code):
        processes = []
        popen = subprocess.Popen

        def start(_argv, **kwargs):
            process = popen([sys.executable, '-c', code], **kwargs)
            processes.append(process)
            return process

        return processes, patch('tool_asr_compare.subprocess.Popen', side_effect=start)

    def test_pipe_sends_expected_pcm_and_exports_only_a_known_category(self):
        audio = np.array([0.5, -0.25], dtype='<f4')
        for answer, expected in (
            ('matched', 'matched'),
            ('empty', 'empty'),
            ('private transcript', 'error'),
        ):
            with self.subTest(answer=answer):
                code = (
                    'import sys; '
                    f'assert sys.stdin.buffer.read() == {audio.tobytes()!r}; '
                    f'print({answer!r})'
                )
                processes, mock = self.child(code)
                with mock:
                    self.assertEqual(compare_local(5, audio, lambda: True), expected)
                self.assertEqual(processes[0].returncode, 0)

    def test_cancellation_and_deadline_reap_the_child(self):
        for expires, expected in ((False, None), (True, 'timeout')):
            with self.subTest(expires=expires):
                states = iter((True, True, False))
                processes, mock = self.child('import time; time.sleep(5)')
                with mock, patch('tool_asr_compare.TIMEOUT_SECONDS', 0.08 if expires else 2):
                    current = (lambda: True) if expires else (lambda: next(states, False))
                    self.assertEqual(compare_local(5, np.zeros(16), current), expected)
                self.assertIsNotNone(processes[0].returncode)
                self.assertTrue(processes[0].stdin.closed)
                self.assertTrue(processes[0].stdout.closed)

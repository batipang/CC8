"""Strict certificate set parsing and tool failures, with no SDK requirement."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import apk_signing as signing


class SignerTests(unittest.TestCase):
    def verify(self, report, status=0):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / 'tool'
            executable.touch()
            result = subprocess.CompletedProcess([], status, report, '')
            with patch.object(signing.subprocess, 'run', return_value=result) as run:
                value = signing.signer_fingerprints(Path('/candidate.apk'), executable, executable)
            self.assertIn('--max-sdk-version', run.call_args.args[0])
            return value

    def test_all_signers_are_compared_without_order_dependency(self):
        a, b = 'a' * 64, 'b' * 64
        report = f'Number of signers: 2\nSigner #1 certificate SHA-256 digest: {a}\nSigner #2 certificate SHA-256 digest: {b}\n'
        result = self.verify(report)
        self.assertEqual(result, frozenset({a, b}))
        signing.require_same_signers(result, frozenset({b, a}))
        with self.assertRaises(signing.SignatureError):
            signing.require_same_signers(result, frozenset({a}))

    def test_missing_partial_duplicate_or_malformed_signers_are_rejected(self):
        a = 'a' * 64
        for report in (
            '',
            f'Signer #1 certificate SHA-256 digest: {a}\n',
            'Number of signers: 0\n',
            'Number of signers: 17\n',
            f'Number of signers: 2\nSigner #1 certificate SHA-256 digest: {a}\n',
            f'Number of signers: 2\nSigner #1 certificate SHA-256 digest: {a}\nSigner #2 certificate SHA-256 digest: {a}\n',
        ):
            with self.subTest(report=report), self.assertRaises(signing.SignatureError):
                self.verify(report)

    def test_failed_tool_does_not_expose_its_output_or_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tool'
            path.touch()
            with patch.object(
                signing.subprocess,
                'run',
                side_effect=subprocess.CalledProcessError(1, 'SECRET_ARG', output='SECRET_CERT'),
            ):
                with self.assertRaises(signing.SignatureError) as error:
                    signing.signer_fingerprints(path, path, path)
            self.assertNotIn('SECRET', str(error.exception))


if __name__ == '__main__':
    unittest.main()

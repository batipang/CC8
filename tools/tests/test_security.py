"""Fail-closed scanner and download boundaries without networking or model imports."""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import ci_tools, security


class AuditTests(unittest.TestCase):
    def run_audit(self, folder, data, returncode=0):
        def run(command, **kwargs):
            Path(command[command.index('--output') + 1]).write_text(json.dumps(data))
            return subprocess.CompletedProcess(command, returncode)

        with (
            patch.object(security, 'ROOT', Path(folder)),
            patch.object(security, 'REQUIREMENTS', ('requirements.txt',)),
            patch('sys.stdout', new_callable=io.StringIO),
            patch('sys.stderr', new_callable=io.StringIO),
        ):
            return security.audit(run=run)

    def test_clean_inventory_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(
                self.run_audit(folder, {'dependencies': [{'name': 'x', 'vulns': []}]}), 0
            )

    def test_unknown_package_cannot_pass_by_being_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(
                self.run_audit(folder, {'dependencies': [{'name': 'x', 'skip_reason': 'missing'}]}),
                1,
            )

    def test_advisory_causes_failure_even_if_cli_returns_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(
                self.run_audit(
                    folder, {'dependencies': [{'name': 'x', 'vulns': [{'id': 'CVE-test'}]}]}
                ),
                1,
            )

    def test_missing_or_stale_report_cannot_mask_audit_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / 'artifacts/security/audit-0.json'
            report.parent.mkdir(parents=True)
            report.write_text('{"dependencies": []}')
            with (
                patch.object(security, 'ROOT', Path(folder)),
                patch.object(security, 'REQUIREMENTS', ('requirements.txt',)),
                patch('sys.stderr', new_callable=io.StringIO),
            ):
                result = security.audit(
                    run=lambda *args, **kwargs: subprocess.CompletedProcess(args, 1)
                )
            self.assertEqual(result, 1)
            self.assertFalse(report.exists())

    def test_each_environment_is_audited_separately_and_failure_continues(self):
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            Path(command[command.index('--output') + 1]).write_text('{"dependencies": []}')
            return subprocess.CompletedProcess(command, 1 if len(commands) == 1 else 0)

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(security, 'ROOT', Path(folder)):
                self.assertEqual(security.audit(run=run), 1)
        self.assertEqual(len(commands), 3)
        self.assertTrue(
            all(c.count('-r') == 1 and '--disable-pip' in c and '--fix' not in c for c in commands)
        )

    def test_secret_scan_redacts_and_includes_all_history(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 1))
        with patch.object(security, 'binary', return_value=Path('/gitleaks')):
            self.assertEqual(security.secrets(run=run), 1)
        command = run.call_args.args[0]
        self.assertIn('--redact=100', command)
        self.assertIn('--log-opts=--all', command)


class CliDownloadTests(unittest.TestCase):
    def archive(self, folder, link=False):
        archive = Path(folder) / 'tool.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            member = tarfile.TarInfo('gitleaks')
            if link:
                member.type = tarfile.SYMTYPE
                member.linkname = '/etc/passwd'
                output.addfile(member)
            else:
                member.size = 4
                output.addfile(member, io.BytesIO(b'tool'))
        return archive

    def test_changed_executable_is_not_trusted_even_with_verified_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = self.archive(folder)
            executable = Path(folder) / 'gitleaks'
            executable.write_bytes(b'tool')
            info = (
                {'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()},
                '',
                archive,
                executable,
            )
            with patch.object(ci_tools, 'tool_info', return_value=info):
                self.assertEqual(ci_tools.binary('gitleaks'), executable)
                executable.write_bytes(b'changed')
                with self.assertRaisesRegex(RuntimeError, 'does not match'):
                    ci_tools.binary('gitleaks')

    def test_archive_cannot_install_a_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = self.archive(folder, link=True)
            payload = archive.read_bytes()
            executable = Path(folder) / 'gitleaks'
            info = (
                {'sha256': hashlib.sha256(payload).hexdigest()},
                'https://example.test',
                archive,
                executable,
            )
            with (
                patch.object(ci_tools, 'tool_info', return_value=info),
                patch('tools.ci_tools.urllib.request.urlopen', return_value=io.BytesIO(payload)),
            ):
                with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                    ci_tools.setup('gitleaks')
            self.assertFalse(executable.exists())

    def test_checksum_failure_installs_nothing_and_cleans_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'tool.tar.gz'
            executable = Path(folder) / 'gitleaks'
            with (
                patch.object(
                    ci_tools,
                    'tool_info',
                    return_value=({'sha256': '0' * 64}, '', archive, executable),
                ),
                patch('tools.ci_tools.urllib.request.urlopen', return_value=io.BytesIO(b'bad')),
            ):
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    ci_tools.setup('gitleaks')
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == '__main__':
    unittest.main()

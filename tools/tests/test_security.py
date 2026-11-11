"""Fail-closed scanner and download boundaries without networking or model imports."""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from tools import ci_tools, security


class AuditTests(unittest.TestCase):
    def run_audit(self, folder, data, returncode=0):
        (Path(folder) / 'requirements.txt').write_text('x==1 --hash=sha256:' + 'a' * 64 + '\n')

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
                self.run_audit(
                    folder, {'dependencies': [{'name': 'x', 'version': '1', 'vulns': []}]}
                ),
                0,
            )

    def test_unknown_package_cannot_pass_by_being_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(
                self.run_audit(
                    folder,
                    {'dependencies': [{'name': 'x', 'version': '1', 'skip_reason': 'missing'}]},
                ),
                1,
            )

    def test_advisory_causes_failure_even_if_cli_returns_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(
                self.run_audit(
                    folder,
                    {
                        'dependencies': [
                            {'name': 'x', 'version': '1', 'vulns': [{'id': 'CVE-test'}]}
                        ]
                    },
                ),
                1,
            )

    def test_missing_or_stale_report_cannot_mask_audit_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'requirements.txt').write_text('x==1 --hash=sha256:' + 'a' * 64 + '\n')
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
            Path(command[command.index('--output') + 1]).write_text(
                '{"dependencies": [{"name":"x","version":"1","vulns":[]}]}'
            )
            return subprocess.CompletedProcess(command, 1 if len(commands) == 1 else 0)

        with tempfile.TemporaryDirectory() as folder:
            for requirements in security.REQUIREMENTS:
                path = Path(folder) / requirements
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('x==1 --hash=sha256:' + 'a' * 64 + '\n')
            with patch.object(security, 'ROOT', Path(folder)):
                self.assertEqual(security.audit(run=run), 1)
        self.assertEqual(len(commands), 3)
        self.assertTrue(
            all(c.count('-r') == 1 and '--disable-pip' in c and '--fix' not in c for c in commands)
        )

    def test_empty_successful_report_cannot_hide_unscanned_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(self.run_audit(folder, {'dependencies': []}), 1)

    def test_wrong_package_or_version_is_rejected_even_without_advisories(self):
        with tempfile.TemporaryDirectory() as folder:
            for dependency in (
                {'name': 'x', 'version': '2', 'vulns': []},
                {'name': 'different', 'version': '1', 'vulns': []},
            ):
                self.assertEqual(self.run_audit(folder, {'dependencies': [dependency]}), 1)

    def test_empty_unpinned_or_include_manifest_is_not_accepted_as_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'requirements.txt'
            path.write_text(
                'Example_Package==1.0 ' + chr(92) + '\n  --hash=sha256:' + 'a' * 64 + '\n'
            )
            self.assertEqual(
                security.pinned_inventory(path, require_hashes=True), {('example-package', '1.0')}
            )
            path.write_text('x==1\n')
            with self.assertRaises(ValueError):
                security.pinned_inventory(path, require_hashes=True)
            for source in (
                '# empty\n',
                'x>=1\n',
                '-r elsewhere.txt\n',
                'x==1 --hash=sha256:bad\n',
                'x==1 ' + chr(92) + '\n',
                'Example_Pkg==1\nexample-pkg==1\n',
                'x==1 --index-url https://example.invalid\n',
            ):
                path.write_text(source)
                with self.assertRaises(ValueError):
                    security.pinned_inventory(path)

    def test_secret_scan_redacts_and_includes_all_history(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 1))
        with patch.object(security, 'binary', return_value=Path('/gitleaks')):
            self.assertEqual(security.secrets(run=run), 1)
        command = run.call_args.args[0]
        self.assertIn('--redact=100', command)
        self.assertIn('--log-opts=--all', command)


class CliDownloadTests(unittest.TestCase):
    def archive(self, folder, link=False, name='gitleaks'):
        archive = Path(folder) / 'tool.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            member = tarfile.TarInfo(name)
            if link:
                member.type = tarfile.SYMTYPE
                member.linkname = '/etc/passwd'
                output.addfile(member)
            else:
                member.size = 4
                output.addfile(member, io.BytesIO(b'tool'))
        return archive

    def test_official_fallback_installs_only_the_pinned_archive_and_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.archive(folder, name='actionlint')
            payload = source.read_bytes()
            source.unlink()
            archive, executable = Path(folder) / 'cached.tar.gz', Path(folder) / 'actionlint'
            asset = {
                'sha256': hashlib.sha256(payload).hexdigest(),
                'size': len(payload),
                'github_asset_id': 384924896,
            }
            error = urllib.error.HTTPError(
                'https://github.com/rhysd/actionlint', 504, 'test gateway', {}, io.BytesIO()
            )
            with (
                patch.object(
                    ci_tools,
                    'tool_info',
                    return_value=(
                        asset,
                        'https://github.com/rhysd/actionlint/releases/download/v1.7.12/tool.tar.gz',
                        archive,
                        executable,
                    ),
                ),
                patch(
                    'tools.ci_tools.urllib.request.urlopen',
                    side_effect=[error, io.BytesIO(payload)],
                ) as network,
                patch('sys.stdout', new_callable=io.StringIO),
            ):
                ci_tools.setup('actionlint')
                self.assertEqual(ci_tools.binary('actionlint'), executable)
            self.assertEqual(executable.read_bytes(), b'tool')
            self.assertEqual(archive.read_bytes(), payload)
            self.assertTrue(error.closed)
            self.assertEqual(network.call_count, 2)
            request = network.call_args_list[1].args[0]
            self.assertEqual(
                request.full_url,
                'https://api.github.com/repos/rhysd/actionlint/releases/assets/384924896',
            )
            self.assertEqual(request.get_header('Accept'), 'application/octet-stream')
            self.assertEqual(list(Path(folder).glob('.ci-tool-*')), [])

    def test_failed_primary_or_api_never_replaces_either_existing_cache_file(self):
        for case in (
            '403',
            '404',
            'no_pin',
            'primary_hash',
            'api_hash',
            'api_size',
            'api_symlink',
            'api_504',
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                source = self.archive(folder, link=case == 'api_symlink', name='actionlint')
                payload = source.read_bytes()
                source.unlink()
                archive, executable = Path(folder) / 'cached.tar.gz', Path(folder) / 'actionlint'
                archive.write_bytes(b'old archive')
                executable.write_bytes(b'old executable')
                asset = {
                    'sha256': hashlib.sha256(payload).hexdigest(),
                    'size': len(payload),
                    'github_asset_id': 384924896,
                }
                error = lambda code: urllib.error.HTTPError(
                    'https://github.com', code, 'test only', {}, None
                )
                if case in ('403', '404'):
                    values = [error(int(case))]
                elif case == 'no_pin':
                    del asset['github_asset_id']
                    values = [error(504)]
                elif case == 'primary_hash':
                    values = [io.BytesIO(b'x' * len(payload))]
                elif case == 'api_hash':
                    values = [error(504), io.BytesIO(b'x' * len(payload))]
                elif case == 'api_size':
                    values = [error(504), io.BytesIO(payload + b'extra')]
                elif case == 'api_symlink':
                    values = [error(504), io.BytesIO(payload)]
                else:
                    values = [error(504), error(504)]
                with (
                    patch.object(
                        ci_tools,
                        'tool_info',
                        return_value=(
                            asset,
                            'https://github.com/rhysd/actionlint',
                            archive,
                            executable,
                        ),
                    ),
                    patch('tools.ci_tools.urllib.request.urlopen', side_effect=values) as network,
                    patch('sys.stdout', new_callable=io.StringIO),
                ):
                    with self.assertRaises((RuntimeError, urllib.error.HTTPError)):
                        ci_tools.setup('actionlint')
                self.assertEqual(network.call_count, len(values))
                self.assertEqual(archive.read_bytes(), b'old archive')
                self.assertEqual(executable.read_bytes(), b'old executable')
                self.assertEqual(list(Path(folder).glob('.ci-tool-*')), [])

    def test_invalid_pinned_size_identity_and_repository_stop_before_network(self):
        for field, values in (
            ('github_asset_id', (True, 0, -1, 2**63, '384924896')),
            ('size', (True, 0, -1, 101 * 1024 * 1024, '2353908')),
        ):
            for value in values:
                with (
                    self.subTest(field=field, value=value),
                    tempfile.TemporaryDirectory() as folder,
                ):
                    p = Path(folder)
                    asset = {field: value, 'sha256': '0' * 64}
                    with (
                        patch.object(
                            ci_tools,
                            'tool_info',
                            return_value=(asset, 'https://github.com', p / 'archive', p / 'binary'),
                        ),
                        patch('tools.ci_tools.urllib.request.urlopen') as network,
                    ):
                        with self.assertRaises(RuntimeError):
                            ci_tools.setup('actionlint')
                        network.assert_not_called()
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'manifest.json'
            p.write_text(json.dumps({'actionlint': {'repo': 'other/repository'}}))
            with patch.object(ci_tools, 'MANIFEST', p):
                with self.assertRaisesRegex(RuntimeError, 'official repository'):
                    ci_tools.tool_info('actionlint')

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

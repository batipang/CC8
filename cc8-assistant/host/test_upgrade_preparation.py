"""Offline versions/configuration workflows never deploy or replace a live profile."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import upgrade_preparation as upgrade

ROOT = Path(__file__).resolve().parents[2]


class UpgradeTests(unittest.TestCase):
    def profile(self, folder, values=None):
        path = Path(folder) / 'original.json'
        path.write_text(json.dumps(values or {'schema_version': 1, 'tts_speed': 1.2}))
        path.chmod(0o600)
        return path

    def manifests(self, folder):
        roots = [Path(folder) / name for name in ('before', 'after')]
        for root in roots:
            root.mkdir()
            (root / 'release.json').write_bytes((ROOT / 'release.json').read_bytes())
        return roots

    def invoke(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = upgrade.main(arguments)
        return code, out.getvalue(), err.getvalue()

    def test_plan_checks_each_protocol_direction_and_marks_no_install_readiness(self):
        with tempfile.TemporaryDirectory() as folder:
            before, after = self.manifests(folder)
            result = upgrade.plan(before, after)
            self.assertTrue(result['declared_protocol_compatible'])
            self.assertFalse(result['ready_for_installation'])
            self.assertFalse(result['installed_signer_checked'])
            data = json.loads((after / 'release.json').read_text())
            data['protocol'] = {'version': 3, 'device_min': 2, 'device_max': 3}
            data['assistant']['version_code'] = 1
            (after / 'release.json').write_text(json.dumps(data))
            result = upgrade.plan(before, after)
            self.assertTrue(result['old_device_new_host_protocol'])
            self.assertFalse(result['new_device_old_host_protocol'])
            self.assertFalse(result['declared_protocol_compatible'])
            self.assertTrue(result['assistant_downgrade'])
            output = Path(folder) / 'plan.json'
            code, _, _ = self.invoke(
                [
                    'plan',
                    '--from-root',
                    str(before),
                    '--to-root',
                    str(after),
                    '--output',
                    str(output),
                ]
            )
            self.assertEqual(code, 2)
            self.assertFalse(json.loads(output.read_text())['ready_for_installation'])

    def test_explicit_apk_option_reuses_the_original_signature_checker(self):
        with tempfile.TemporaryDirectory() as folder:
            before, after = self.manifests(folder)
            with patch('tools.release.check_apk', return_value={'sha256': 'a' * 64}) as check:
                result = upgrade.plan(before, after, Path(folder) / 'candidate.apk')
            check.assert_called_once_with('assistant', Path(folder) / 'candidate.apk', root=after)
            self.assertTrue(result['apk_checked'])
            self.assertFalse(result['installed_signer_checked'])
            self.assertFalse(result['ready_for_installation'])

    def test_backup_stage_roundtrip_keeps_original_bytes_and_key_references(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.profile(folder)
            original = source.read_bytes()
            value = upgrade.backup(source, root)
            target = root / 'backup.json'
            upgrade.output(value, target)
            settings = upgrade.verify_backup(target)
            self.assertEqual(settings['tts_speed'], 1.2)
            self.assertEqual(
                settings['credential_file'], str(root / '.toolchains/cc8/voice-secrets.env')
            )
            self.assertEqual(
                settings['signing_keystore'],
                str(root / 'cc8-diagnostics/signing/cc8-diagnostics-debug.jks'),
            )
            staged = root / 'staged.json'
            code, _, _ = self.invoke(['stage-restore', str(target), '--output', str(staged)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(staged.read_text()), settings)
            self.assertEqual(source.read_bytes(), original)
            for path in (target, staged):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_backup_reads_no_credential_or_key_contents_and_ignores_environment_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.profile(folder)
            with (
                patch('os.open', wraps=os.open) as opened,
                patch.dict(
                    os.environ, {'CC8_TTS_SPEED': '2.0', 'CC8_MINIMAX_API_KEY': 'PRIVATE_KEY_VALUE'}
                ),
            ):
                value = upgrade.backup(source, Path(folder))
            self.assertEqual(len(opened.call_args_list), 1)
            self.assertEqual(Path(opened.call_args.args[0]), source)
            self.assertEqual(value['settings']['tts_speed'], 1.2)
            self.assertNotIn('PRIVATE_KEY_VALUE', json.dumps(value))
            self.assertIn('pairing', value['excluded'])

    def test_legacy_profile_normalizes_to_version_one_without_overwriting_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.profile(folder, {'asr': 'whisper'})
            original = source.read_bytes()
            value = upgrade.backup(source, Path(folder))
            self.assertTrue(value['legacy_source'])
            self.assertEqual(value['settings']['schema_version'], 1)
            self.assertEqual(source.read_bytes(), original)

    def test_invalid_public_or_symlink_source_never_creates_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.profile(folder)
            target = root / 'backup.json'
            source.chmod(0o644)
            code, _, _ = self.invoke(['backup', '--profile', str(source), '--output', str(target)])
            self.assertEqual(code, 2)
            self.assertFalse(target.exists())
            source.chmod(0o600)
            link = root / 'link.json'
            link.symlink_to(source)
            code, _, _ = self.invoke(['backup', '--profile', str(link), '--output', str(target)])
            self.assertEqual(code, 2)
            self.assertFalse(target.exists())

    def test_tamper_unknown_fields_and_duplicates_never_stage_or_modify_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.profile(folder)
            original = source.read_bytes()
            good = upgrade.backup(source, root)
            variants = []
            changed = json.loads(json.dumps(good))
            changed['settings']['tts_speed'] = 1.7
            variants.append(json.dumps(changed))
            variants.append(json.dumps({**good, 'restore_path': str(source)}))
            variants.append('{"schema_version":1,"schema_version":1}')
            variants.append('x' * 32769)
            for raw in variants:
                backup = root / 'supplied.json'
                backup.write_text(raw)
                backup.chmod(0o600)
                output = root / 'staged.json'
                code, _, _ = self.invoke(['stage-restore', str(backup), '--output', str(output)])
                self.assertEqual(code, 2)
                self.assertFalse(output.exists())
                self.assertEqual(source.read_bytes(), original)

    def test_output_never_replaces_existing_profile_or_chmods_public_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.profile(folder)
            original = source.read_bytes()
            with self.assertRaises(FileExistsError):
                upgrade.output(upgrade.backup(source, root), source)
            self.assertEqual(source.read_bytes(), original)
            public = root / 'public'
            public.mkdir(mode=0o755)
            public.chmod(0o755)
            with self.assertRaises(OSError):
                upgrade.output({'schema_version': 1}, public / 'out.json')
            self.assertEqual(public.stat().st_mode & 0o777, 0o755)

    def test_verify_is_read_only_and_errors_never_echo_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.profile(folder, {'unknown': 'PRIVATE_PAYLOAD'})
            code, out, err = self.invoke(
                ['backup', '--profile', str(source), '--output', str(root / 'backup.json')]
            )
            self.assertEqual(code, 2)
            self.assertNotIn('PRIVATE_PAYLOAD', out + err)
            source = self.profile(folder)
            saved = root / 'saved.json'
            upgrade.output(upgrade.backup(source, root), saved)
            before = saved.read_bytes()
            code, _, _ = self.invoke(['verify', str(saved)])
            self.assertEqual(code, 0)
            self.assertEqual(saved.read_bytes(), before)

    def test_root_cli_backup_verify_stage_works_with_minimal_path_and_no_model_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.profile(folder)
            backup, staged = root / 'saved.json', root / 'staged.json'
            for arguments in (
                [
                    'backup',
                    '--profile',
                    str(source),
                    '--source-root',
                    str(root),
                    '--output',
                    str(backup),
                ],
                ['verify', str(backup)],
                ['stage-restore', str(backup), '--output', str(staged)],
            ):
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'cc8'), 'upgrade', *arguments],
                    env={'PATH': '/usr/bin:/bin'},
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(staged.read_text())['tts_speed'], 1.2)


if __name__ == '__main__':
    unittest.main()

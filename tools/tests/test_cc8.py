"""Unified commands fail closed; subprocess smoke tests never touch devices."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import cc8


class CommandTests(unittest.TestCase):
    def args(self, **values):
        return argparse.Namespace(
            profile='dev', python=sys.executable, sensevoice=False, dry_run=False, **values
        )

    def fixture(self, root):
        resources = root / 'cc8-assistant/host/resources'
        resources.mkdir(parents=True)
        (root / 'cc8-assistant/host/voice-config.json').write_text(
            '{"schema_version":1,"asr":"minimax"}'
        )
        (root / 'requirements-dev.txt').write_text('example==1.0\n')
        (root / 'cc8-assistant/host/requirements-runtime.txt').write_text('example==1.0\n')

    def test_pairing_export_preserves_legacy_identity_and_refuses_overwrite(self):
        from transport_pairing import Pairing

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            pairing_path = root / '.toolchains/cc8/transport'
            destination = root / 'device-access/private/cc8-pairing.json'
            destination.parent.mkdir(parents=True, mode=0o700)
            args = argparse.Namespace(output=destination)
            with self.assertRaises(FileNotFoundError):
                cc8.pairing_export(args, root)
            self.assertFalse(pairing_path.exists())
            legacy = Pairing(pairing_path)
            original = {p.name: p.read_bytes() for p in pairing_path.iterdir()}
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(cc8.pairing_export(args, root), 0)
            self.assertNotIn(legacy.token, out.getvalue())
            exported = json.loads(destination.read_text())
            self.assertEqual(exported['token'], legacy.token)
            self.assertEqual(exported['server_id'], legacy.server_id)
            self.assertEqual(exported['certificate_sha256'], legacy.pin)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                cc8.pairing_export(args, root)
            self.assertEqual({p.name: p.read_bytes() for p in pairing_path.iterdir()}, original)

    def test_real_metadata_probe_without_model_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'requirements.txt'
            path.write_text('cc8-missing-test-package==1.0 --hash=sha256:' + 'a' * 64 + '\n')
            result = cc8.python_info(sys.executable, path)
            self.assertEqual(result['version'], list(sys.version_info[:2]))
            self.assertEqual(result['missing_or_wrong'], ['cc8-missing-test-package'])

    def test_setup_dev_plan_is_isolated_and_dry_run_creates_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = self.args()
            args.dry_run = True
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(cc8.setup(args, root), 0)
            plan = json.loads(out.getvalue())
            self.assertEqual(plan['commands'][0][1:3], ['-m', 'venv'])
            self.assertIn('--require-hashes', plan['commands'][1])
            self.assertIn('--only-binary=:all:', plan['commands'][1])
            self.assertIn('requirements-dev.txt', plan['commands'][1][-1])
            self.assertFalse((root / '.toolchains').exists())
            self.assertNotIn('voice-venv', out.getvalue())

    def test_setup_rejects_external_environment_even_if_parent_is_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'repo'
            outside = Path(folder) / 'outside'
            root.mkdir()
            outside.mkdir()
            (root / '.toolchains').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(cc8.SetupError):
                cc8.contained_environment(root, 'dev-venv')

    def test_voice_setup_conflict_cannot_mutate_dependencies(self):
        args = self.args()
        args.profile = 'voice'
        info = {'version': [3, 13], 'system': 'Darwin', 'machine': 'arm64'}
        with tempfile.TemporaryDirectory() as folder:
            with (
                patch.object(cc8, 'python_info', return_value=info),
                patch.object(
                    cc8, 'ensure_available', side_effect=cc8.RuntimeConflict('other runtime')
                ),
                patch.object(cc8.subprocess, 'run') as run,
            ):
                with self.assertRaises(cc8.RuntimeConflict):
                    cc8.setup(args, Path(folder))
            run.assert_not_called()

    def test_dev_doctor_does_not_read_voice_config_credentials_or_models(self):
        with tempfile.TemporaryDirectory() as folder:
            with (
                patch.object(
                    cc8, 'python_info', return_value={'version': [3, 13], 'missing_or_wrong': []}
                ),
                patch.object(cc8, 'require_java', return_value=Path('/jdk')),
                patch.object(
                    cc8, 'load_config', side_effect=AssertionError('voice config accessed')
                ),
                patch.object(cc8, 'verified_models', side_effect=AssertionError('models accessed')),
            ):
                result = cc8.doctor('dev', Path(folder))
            self.assertFalse(result['device_capture_started'])
            self.assertEqual(
                {c['id'] for c in result['checks']},
                {'dev_python', 'jdk', 'openssl', 'cxx', 'java_formatter'},
            )

    def test_invalid_profile_is_reported_without_its_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            profile = root / '.toolchains/cc8/settings/assistant.json'
            profile.parent.mkdir(parents=True)
            profile.write_text('{"brain":"SECRET_VALUE"}')
            profile.chmod(0o600)
            with patch.object(cc8, 'other_runtime', return_value=False):
                result = cc8.doctor('voice', root)
            self.assertFalse(result['ok'])
            self.assertFalse(next(c['ok'] for c in result['checks'] if c['id'] == 'configuration'))
            self.assertNotIn('SECRET_VALUE', json.dumps(result))

    def test_voice_doctor_checks_local_tools_without_cloud_credentials_or_codex(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with (
                patch.dict('os.environ', {'CC8_BRAIN_PROVIDER': 'echo'}, clear=True),
                patch.object(cc8, 'credential_ready') as credential,
                patch.object(cc8, 'dot_ready') as dot,
                patch.object(cc8, 'local_voice_tools', return_value=False),
                patch.object(cc8, 'verified_models', return_value=True) as models,
                patch.object(cc8, 'other_runtime', return_value=False),
            ):
                report = cc8.doctor('voice', root)
            models.assert_called_once_with(root, 'sensevoice')
            credential.assert_not_called()
            dot.assert_not_called()
            checks = {item['id']: item for item in report['checks']}
            self.assertFalse(checks['local_voice_tools']['ok'])
            self.assertTrue(checks['local_voice_tools']['required'])

    def test_voice_doctor_requires_optional_chinese_model_only_when_selected(self):
        for mode in ('off', 'sensevoice', 'sensevoice_complete'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                self.fixture(root)

                def models(root, engine):
                    if engine == 'sensevoice':
                        raise RuntimeError('test-only missing local weights')
                    return True

                with (
                    patch.dict('os.environ', {'CC8_CHINESE_WAKE_VERIFIER': mode}, clear=True),
                    patch.object(
                        cc8,
                        'python_info',
                        return_value={
                            'version': [3, 13],
                            'system': 'Darwin',
                            'machine': 'arm64',
                            'missing_or_wrong': [],
                        },
                    ),
                    patch.object(cc8, 'credential_ready', return_value=True),
                    patch.object(cc8, 'other_runtime', return_value=False),
                    patch.object(cc8, 'verified_models', side_effect=models),
                ):
                    result = cc8.doctor('voice', root)
                checks = [c for c in result['checks'] if c['id'] == 'chinese_wake_model']
                self.assertEqual(len(checks), 0 if mode == 'off' else 1)
                if checks:
                    self.assertFalse(checks[0]['ok'])
                    self.assertIn('--sensevoice', checks[0]['fix'])
                self.assertFalse(result['device_capture_started'])

    def test_english_model_check_uses_minimal_environment_and_no_worker_start(self):
        with patch.object(cc8.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertTrue(cc8.verified_english_wake_model(Path('/work area')))
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], '--check-model')
        self.assertTrue(args[0][0].endswith('/voice-venv/bin/python'))
        self.assertEqual(set(kwargs['env']), {'PATH', 'HOME', 'PYTHONNOUSERSITE'})
        self.assertTrue(kwargs['check'])
        self.assertEqual(kwargs['timeout'], 20)

    def test_voice_doctor_requires_english_weights_only_when_configured(self):
        for mode in ('off', 'whisper_complete'):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                self.fixture(root)
                with (
                    patch.dict('os.environ', {'CC8_ENGLISH_WAKE_VERIFIER': mode}, clear=True),
                    patch.object(
                        cc8,
                        'python_info',
                        return_value={
                            'version': [3, 13],
                            'system': 'Darwin',
                            'machine': 'arm64',
                            'missing_or_wrong': [],
                        },
                    ),
                    patch.object(cc8, 'credential_ready', return_value=True),
                    patch.object(cc8, 'other_runtime', return_value=False),
                    patch.object(cc8, 'verified_models', return_value=True),
                    patch.object(
                        cc8,
                        'verified_english_wake_model',
                        side_effect=RuntimeError('test only missing'),
                    ) as model,
                ):
                    report = cc8.doctor('voice', root)
                checks = [c for c in report['checks'] if c['id'] == 'english_wake_model']
                self.assertEqual(len(checks), int(mode != 'off'))
                self.assertEqual(model.call_count, int(mode != 'off'))
                self.assertFalse(report['device_capture_started'])
                if checks:
                    self.assertFalse(checks[0]['ok'])

    def test_credentials_only_check_owner_readability_and_format_not_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            config = cc8.load_config(root, environ={})
            path = config.credential_file
            path.parent.mkdir(parents=True, exist_ok=True)
            secret = 'sk-cp-fixture-not-a-real-key'
            path.write_text('CC8_MINIMAX_API_KEY=' + secret)
            path.chmod(0o600)
            self.assertTrue(cc8.credential_ready(config, {}))
            path.chmod(0o644)
            with self.assertRaises(OSError):
                cc8.credential_ready(config, {})
            self.assertTrue(cc8.credential_ready(config, {'CC8_MINIMAX_API_KEY': secret}))

    def test_models_are_verified_against_archive_not_just_present(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            tools = root / '.toolchains/cc8'
            path = tools / 'model/tokens.txt'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'verified tokens')
            archive = tools / 'model.tar.bz2'
            with tarfile.open(archive, 'w:bz2') as out:
                out.add(path, arcname='model/tokens.txt')
            manifest = {
                'name': archive.name,
                'digest': 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
            (root / 'cc8-assistant/host/resources/fast-stop-model.json').write_text(
                json.dumps(manifest)
            )
            (root / 'cc8-assistant/host/resources/english-wake-model.json').write_text(
                json.dumps(manifest)
            )
            self.assertTrue(cc8.verified_models(root, 'minimax'))
            path.write_bytes(b'changed')
            self.assertFalse(cc8.verified_models(root, 'minimax'))

    def test_run_reuses_service_and_never_calls_ui_launcher(self):
        args = argparse.Namespace(stop=False, foreground=False)
        run = Mock(return_value=subprocess.CompletedProcess([], 0))
        with (
            patch.object(cc8, 'doctor', return_value={'ok': True}),
            patch.object(cc8.platform, 'system', return_value='Darwin'),
            patch.object(cc8.subprocess, 'run', run),
        ):
            self.assertEqual(cc8.run_runtime(args), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[-1], 'start')
        self.assertTrue(command[-2].endswith('/service.py'))
        self.assertNotIn('launch.py', ' '.join(command))

    def test_failed_doctor_never_spawns_worker(self):
        args = argparse.Namespace(stop=False, foreground=False)
        with (
            patch.object(cc8, 'doctor', return_value={'ok': False}),
            patch('sys.stdout', new_callable=io.StringIO),
            patch.object(cc8.subprocess, 'run') as run,
        ):
            self.assertEqual(cc8.run_runtime(args), 2)
        run.assert_not_called()

    def test_explicit_missing_signing_reference_is_required(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with patch.dict('os.environ', {'CC8_SIGNING_KEYSTORE': str(root / 'missing.jks')}):
                report = cc8.doctor('android', root)
            item = next(c for c in report['checks'] if c['id'] == 'signing_reference')
            self.assertFalse(item['ok'])
            self.assertTrue(item['required'])
            self.assertFalse(report['ok'])

    def test_foreground_and_stop_use_only_current_checkout_entrypoints(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                patch.object(cc8, 'doctor', return_value={'ok': True}),
                patch.object(cc8.subprocess, 'run') as run,
            ):
                run.return_value.returncode = 0
                cc8.run_runtime(argparse.Namespace(stop=False, foreground=True), root)
                self.assertEqual(
                    run.call_args.args[0][-1], str(root / 'cc8-assistant/host/bridge.py')
                )
                cc8.run_runtime(argparse.Namespace(stop=True, foreground=False), root)
                self.assertEqual(
                    run.call_args.args[0][-2:],
                    [str(root / 'cc8-assistant/host/service.py'), 'stop'],
                )

    def test_root_entry_works_with_minimal_path_and_missing_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'tools').mkdir()
            (root / 'cc8-assistant/host').mkdir(parents=True)
            shutil.copy2(cc8.ROOT / 'cc8', root / 'cc8')
            for name in ('cc8.py', 'java_toolchain.py', 'requirement_pins.py'):
                shutil.copy2(cc8.ROOT / 'tools' / name, root / 'tools' / name)
            for name in (
                'configuration.py',
                'config_schema.py',
                'runtime_guard.py',
                'process_lock.py',
                'runtime_env.py',
            ):
                shutil.copy2(
                    cc8.ROOT / 'cc8-assistant/host' / name, root / 'cc8-assistant/host' / name
                )
            env = {
                'PATH': '/usr/bin:/bin',
                'HOME': folder,
                'CC8_MINIMAX_API_KEY': 'SECRET_SENTINEL',
            }
            result = subprocess.run(
                [sys.executable, str(root / 'cc8'), 'doctor', '--profile', 'dev', '--json'],
                cwd='/',
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stdout)['ok'])
            self.assertNotIn('SECRET_SENTINEL', result.stdout + result.stderr)
            self.assertFalse((root / '.toolchains').exists())
            help_result = subprocess.run(
                [sys.executable, str(root / 'cc8'), '--help'],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(help_result.returncode, 0)


if __name__ == '__main__':
    unittest.main()

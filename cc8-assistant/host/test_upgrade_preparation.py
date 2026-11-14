"""Offline versions/configuration workflows never deploy or replace a live profile."""

from contextlib import redirect_stdout, redirect_stderr
import hashlib
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
from test_audit_observation import observation

ROOT = Path(__file__).resolve().parents[2]


class UpgradeTests(unittest.TestCase):
    def test_report_collections_require_the_exact_declared_format_without_export(self):
        with tempfile.TemporaryDirectory() as folder:
            before, after = self.manifests(folder)
            self.assertTrue(upgrade.plan(before, after)['report_collection_storage']['compatible'])
            private = before / 'device-access/private/voice-runtime'
            private.mkdir(parents=True, mode=0o700)
            readers = after / 'cc8-assistant/host'
            readers.mkdir(parents=True)
            originals = {}
            for name, (filename, module) in upgrade.REPORT_STORES.items():
                path = private / filename
                path.write_text(json.dumps(dict(schema=1, revision=1, items=['PRIVATE_FIXTURE'])))
                path.chmod(0o600)
                originals[path] = path.read_bytes()
                (readers / module).write_text((ROOT / 'cc8-assistant/host' / module).read_text())
                gate = upgrade.report_collection_storage_gate(before, ROOT)
                self.assertTrue(gate['stores'][name]['compatible'])
            gate = upgrade.plan(before, after)['report_collection_storage']
            self.assertTrue(gate['compatible'])
            self.assertNotIn('PRIVATE_FIXTURE', str(gate))
            archive = readers / 'report_archive.py'
            archive.write_text(
                "def validate(value):\n    if value['schema'] != 2: raise ValueError()\n"
            )
            gate = upgrade.plan(before, after)['report_collection_storage']
            self.assertEqual(gate['stores']['archive']['reader_version'], 2)
            self.assertFalse(gate['compatible'])
            self.assertEqual(
                self.invoke(['plan', '--from-root', str(before), '--to-root', str(after)])[0], 2
            )
            archive.unlink()
            self.assertIsNone(
                upgrade.plan(before, after)['report_collection_storage']['stores']['archive'][
                    'reader_version'
                ]
            )
            self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))

    def test_report_collection_preflight_rejects_unsafe_and_malformed_files(self):
        with tempfile.TemporaryDirectory() as folder:
            before, _ = self.manifests(folder)
            path = before / 'device-access/private/voice-runtime/work-report-progress.json'
            path.parent.mkdir(mode=0o700, parents=True)
            path.write_text('{"schema":true,"revision":1,"items":[]}')
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                upgrade.report_collection_storage_gate(before, ROOT)
            path.write_text('{"schema":1,"revision":1,"items":[]}')
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                upgrade.report_collection_storage_gate(before, ROOT)
            path.unlink()
            path.symlink_to(before / 'release.json')
            with self.assertRaises(OSError):
                upgrade.report_collection_storage_gate(before, ROOT)

    def test_candidate_report_collections_are_rechecked_before_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            roots, apks, checked, _ = self.transition_fixture(folder)
            base, candidate, recovery = roots
            transition = Path(folder) / 'transition.json'
            with patch('tools.release.check_apk', side_effect=checked):
                report = upgrade.verify_assistant_transition(
                    *roots, apks[0], hashlib.sha256(apks[0].read_bytes()).hexdigest()
                )
                self.assertTrue(report['report_collection_storage']['recovery']['compatible'])
                upgrade.output(report, transition)
                path = base / 'device-access/private/voice-runtime/work-report-sources.json'
                path.parent.mkdir(mode=0o700, parents=True)
                path.write_text('{"schema":1,"revision":1,"items":[]}')
                path.chmod(0o600)
                with self.assertRaisesRegex(ValueError, 'Saved work report collections'):
                    upgrade.verify_assistant_transition(
                        *roots, apks[0], hashlib.sha256(apks[0].read_bytes()).hexdigest()
                    )
            dest = candidate / path.relative_to(base)
            dest.parent.mkdir(mode=0o700, parents=True)
            dest.write_bytes(path.read_bytes())
            dest.chmod(0o600)
            receipt = upgrade.recheck_transition_storage(
                transition, candidate, recovery, 'recovery'
            )
            self.assertFalse(receipt['compatible'])
            self.assertTrue(receipt['report_collection_storage']['stores']['sources']['present'])
            self.assertEqual(dest.read_bytes(), path.read_bytes())

    def test_window_receipt_archive_blocks_older_reader_and_reports_no_content(self):
        from saved_tasks import SavedTasks
        from test_saved_tasks import task

        with tempfile.TemporaryDirectory() as folder:
            before, after = self.manifests(folder)
            path = before / 'device-access/private/voice-runtime/saved-task-results.json'
            path.parent.mkdir(parents=True, mode=0o700)
            reader = after / 'cc8-assistant/host/saved_tasks.py'
            reader.parent.mkdir(parents=True)
            reader.write_text("if value['schema_version'] not in (1, 2): raise ValueError()\n")
            item = {
                **task(),
                'window_input': {
                    'window_id': 9,
                    'application': 'Document',
                    'title': 'PRIVATE_TITLE',
                    'width': 640,
                    'height': 480,
                },
            }
            SavedTasks(path).save(item)
            raw = path.read_bytes()
            plan = upgrade.plan(before, after)
            self.assertFalse(plan['saved_task_storage']['compatible'])
            self.assertEqual(plan['saved_task_storage']['reader_max_version'], 2)
            self.assertNotIn('PRIVATE_TITLE', str(plan))
            code, _, _ = self.invoke(['plan', '--from-root', str(before), '--to-root', str(after)])
            self.assertEqual(code, 2)
            reader.write_text('READER_VERSION = 3\n')
            self.assertTrue(upgrade.plan(before, after)['saved_task_storage']['compatible'])
            self.assertEqual(path.read_bytes(), raw)

    def test_scheduled_summary_format_blocks_old_recovery_without_export_or_migration(self):
        from report_watch_store import ReportWatchStore
        from test_report_watch_store import SOURCE

        with tempfile.TemporaryDirectory() as folder:
            before, after = self.manifests(folder)
            path = before / 'device-access/private/voice-runtime/work-report-watch.json'
            store = ReportWatchStore(path, clock=lambda: 200)
            store.configure(0, SOURCE, 300, 'cc8', mode='summary')
            raw = path.read_bytes()
            import builtins

            original_import = builtins.__import__

            def stdlib_only(name, *args, **kwargs):
                if name == 'report_watch_store':
                    raise AssertionError('Format inspection must not import voice runtime')
                return original_import(name, *args, **kwargs)

            with patch('builtins.__import__', side_effect=stdlib_only):
                gate = upgrade.plan(before, after)['report_watch_storage']
            self.assertFalse(gate['compatible'])
            self.assertFalse(gate['report_content_exported'])
            self.assertNotIn('source', gate)
            code, _, _ = self.invoke(['plan', '--from-root', str(before), '--to-root', str(after)])
            self.assertEqual(code, 2)
            source = after / 'cc8-assistant/host/report_watch_store.py'
            source.parent.mkdir(parents=True)
            source.write_text('READER_VERSION = 2\n')
            self.assertTrue(upgrade.plan(before, after)['report_watch_storage']['compatible'])
            self.assertEqual(path.read_bytes(), raw)
            source.write_text('READER_VERSION = True\n')
            with self.assertRaises(ValueError):
                upgrade.plan(before, after)
            source.write_text('READER_VERSION = 1\n')
            store.clear(1)
            self.assertTrue(upgrade.plan(before, after)['report_watch_storage']['compatible'])

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

    def test_matching_application_protocol_does_not_imply_compatible_usb(self):
        with tempfile.TemporaryDirectory() as folder:
            before, after = self.manifests(folder)
            path = before / 'release.json'
            data = json.loads(path.read_text())
            for declared, expected in (
                (None, 'unverified'),
                ('legacy_plaintext', 'incompatible'),
                ('paired_tls', 'compatible'),
            ):
                with self.subTest(declared=declared):
                    if declared is None:
                        data.pop('transport', None)
                    else:
                        data['transport'] = {'usb_authentication': declared}
                    path.write_text(json.dumps(data))
                    report = upgrade.plan(before, after)
                    self.assertTrue(report['declared_protocol_compatible'])
                    gate = report['transport_compatibility']
                    self.assertEqual(gate['old_device_new_host_usb'], expected)
                    self.assertEqual(gate['new_device_old_host_usb'], expected)
                    self.assertEqual(
                        gate['coordinated_usb_cutover_required'], expected != 'compatible'
                    )
                    self.assertFalse(gate['physical_transport_verified'])
                    self.assertFalse(report['ready_for_installation'])

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS system Python compatibility')
    def test_upgrade_plan_runs_with_system_python_and_minimal_path(self):
        with tempfile.TemporaryDirectory() as folder:
            before, after = self.manifests(folder)
            output = Path(folder) / 'plan.json'
            result = subprocess.run(
                [
                    '/usr/bin/python3',
                    str(ROOT / 'cc8'),
                    'upgrade',
                    'plan',
                    '--from-root',
                    str(before),
                    '--to-root',
                    str(after),
                    '--output',
                    str(output),
                ],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertTrue(report['declared_protocol_compatible'])
            self.assertFalse(report['ready_for_installation'])

    def test_explicit_apk_option_reuses_the_original_signature_checker(self):
        with tempfile.TemporaryDirectory() as folder:
            before, after = self.manifests(folder)
            with patch('tools.release.check_apk', return_value={'sha256': 'a' * 64}) as check:
                result = upgrade.plan(before, after, Path(folder) / 'candidate.apk')
            check.assert_called_once_with('assistant', Path(folder) / 'candidate.apk', root=after)
            self.assertTrue(result['apk_checked'])
            self.assertFalse(result['installed_signer_checked'])
            self.assertFalse(result['ready_for_installation'])

    def test_reminder_schema_gate_blocks_old_reader_without_exporting_or_rewriting_content(self):
        from reminder_policy import defaults
        from reminder_store import ReminderStore

        with tempfile.TemporaryDirectory() as folder:
            before, after = self.manifests(folder)
            path = before / 'device-access/private/voice-runtime/reminders.json'
            ReminderStore(path).save({'version': 2, 'policy': defaults(), 'reminders': []})
            raw = path.read_bytes()
            gate = upgrade.plan(before, after)['reminder_storage']
            self.assertFalse(gate['compatible'])
            self.assertFalse(gate['reminder_content_exported'])
            code, _, _ = self.invoke(['plan', '--from-root', str(before), '--to-root', str(after)])
            self.assertEqual(code, 2)
            source = after / 'cc8-assistant/host/workspace.py'
            source.parent.mkdir(parents=True)
            source.write_text('REMINDER_STORE_VERSION = 2\n')
            self.assertTrue(upgrade.plan(before, after)['reminder_storage']['compatible'])
            self.assertEqual(path.read_bytes(), raw)

    def test_desktop_schema_three_refuses_reader_two_and_preserves_original_bytes(self):
        from reminder_policy import defaults
        from reminder_store import ReminderStore

        with tempfile.TemporaryDirectory() as folder:
            before, after = self.manifests(folder)
            path = before / 'device-access/private/voice-runtime/reminders.json'
            ReminderStore(path).save({'version': 3, 'policy': defaults(), 'reminders': []})
            raw = path.read_bytes()
            source = after / 'cc8-assistant/host/workspace.py'
            source.parent.mkdir(parents=True)
            source.write_text('REMINDER_STORE_VERSION = 2\n')
            self.assertFalse(upgrade.plan(before, after)['reminder_storage']['compatible'])
            source.write_text('REMINDER_STORE_VERSION = 3\n')
            self.assertTrue(upgrade.plan(before, after)['reminder_storage']['compatible'])
            self.assertEqual(path.read_bytes(), raw)

    def transition_fixture(self, folder, *, settings_schema=False, saved_task_reader=False):
        base, candidate, recovery = (Path(folder) / x for x in ('base', 'candidate', 'recovery'))

        def git(root, *args):
            return subprocess.check_output(
                [
                    'git',
                    '-C',
                    str(root),
                    '-c',
                    'user.name=Fixture',
                    '-c',
                    'user.email=fixture@example.test',
                    '-c',
                    'commit.gpgSign=false',
                    *args,
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).strip()

        base.mkdir()
        git(base, 'init', '-q')
        (base / '.gitignore').write_text('artifacts/\n/device-access/private/\n')
        manifest = json.loads((ROOT / 'release.json').read_bytes())
        manifest.pop('transport', None)  # Exercise the published pre-TLS baseline.
        initial_code = manifest['assistant']['version_code']
        (base / 'release.json').write_text(json.dumps(manifest))
        schema_path = Path('cc8-assistant/host/resources/runtime-config.schema.json')
        if settings_schema:
            schema = upgrade.config_schema()
            del schema['properties']['voice_mode']
            del schema['properties']['gpt_live_voice']
            schema['properties']['brain']['enum'] = ['dot', 'minimax']
            (base / schema_path).parent.mkdir(parents=True)
            (base / schema_path).write_text(json.dumps(schema))
        reader_path = Path('cc8-assistant/host/saved_tasks.py')
        if saved_task_reader:
            (base / reader_path).parent.mkdir(parents=True, exist_ok=True)
            (base / reader_path).write_text('READER_VERSION = 2\n')
        git(base, 'add', '.')
        git(base, 'commit', '-qm', 'fixed baseline')
        git(base, 'worktree', 'add', '--detach', str(candidate))
        git(base, 'worktree', 'add', '--detach', str(recovery))
        paths = []
        for i, root in enumerate((base, candidate, recovery)):
            data = json.loads((root / 'release.json').read_bytes())
            if i:
                data['assistant'].update(version=f'9.0.{i}', version_code=initial_code + i)
                data['release_id'] = f'fixture-{i}'
                if i == 1:
                    data['backend']['version'] = '9.0.1'
                    data['transport'] = {'usb_authentication': 'paired_tls'}
                    if settings_schema:
                        (root / schema_path).write_text(json.dumps(upgrade.config_schema()))
                    if saved_task_reader:
                        (root / reader_path).write_text('READER_VERSION = 3\n')
                (root / 'release.json').write_text(json.dumps(data))
                git(root, 'commit', '-qam', 'fixed build')
            (root / 'artifacts').mkdir()
            apk = root / 'artifacts' / f'cc8-assistant-{data["assistant"]["version"]}.apk'
            apk.write_bytes(f'fixture bytes {i}'.encode())
            paths.append(apk)
            apk.with_suffix('.apk.build.json').write_text(
                json.dumps(
                    {
                        'schema_version': 1,
                        'component': 'assistant',
                        'version': data['assistant']['version'],
                        'version_code': initial_code + i,
                        'source_commit': git(root, 'rev-parse', 'HEAD'),
                        'source_dirty': False,
                        'manifest_sha256': hashlib.sha256(
                            (root / 'release.json').read_bytes()
                        ).hexdigest(),
                        'apk_sha256': hashlib.sha256(apk.read_bytes()).hexdigest(),
                    }
                )
            )

        def checked(component, apk, root):
            version = upgrade.load_release(root).assistant
            return {
                'component': component,
                'version': version.version,
                'version_code': version.version_code,
                'package': version.package,
                'signer_sha256': ['a' * 64],
                'sha256': hashlib.sha256(apk.read_bytes()).hexdigest(),
            }

        return (base, candidate, recovery), paths, checked, git

    def test_transition_cli_binds_three_stages_without_claiming_live_install_readiness(self):
        with tempfile.TemporaryDirectory() as folder:
            roots, apks, checked, _ = self.transition_fixture(folder)
            output = Path(folder) / 'transition.json'
            with patch('tools.release.check_apk', side_effect=checked) as probe:
                code, _, err = self.invoke(
                    [
                        'verify-transition',
                        '--from-root',
                        str(roots[0]),
                        '--to-root',
                        str(roots[1]),
                        '--recovery-root',
                        str(roots[2]),
                        '--baseline-apk',
                        str(apks[0]),
                        '--baseline-sha256',
                        hashlib.sha256(apks[0].read_bytes()).hexdigest(),
                        '--output',
                        str(output),
                    ]
                )
            self.assertEqual(code, 0, err)
            self.assertEqual(probe.call_count, 3)
            report = json.loads(output.read_text())
            self.assertTrue(report['verified_against_pinned_baseline'])
            self.assertFalse(report['installed_signer_checked'])
            self.assertFalse(report['ready_for_installation'])
            self.assertEqual(list(report['stages']), ['baseline', 'candidate', 'recovery'])
            gates = report['transport_compatibility']
            self.assertEqual(gates['upgrade']['to_usb_authentication'], 'paired_tls')
            self.assertEqual(gates['rollback']['from_usb_authentication'], 'paired_tls')
            for gate in gates.values():
                self.assertTrue(gate['coordinated_usb_cutover_required'])
                self.assertFalse(gate['physical_transport_verified'])
            self.assertEqual(report['recovery_source']['candidate_matching_build_support'], [])
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_transition_checks_explicit_backup_without_copying_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            roots, apks, checked, _ = self.transition_fixture(folder, settings_schema=True)
            root = Path(folder)
            pin = hashlib.sha256(apks[0].read_bytes()).hexdigest()
            profile = self.profile(folder, {'schema_version': 1, 'brain': 'dot'})
            saved = root / 'settings-backup.json'
            upgrade.output(upgrade.backup(profile, roots[0]), saved)
            original = saved.read_bytes()
            with patch('tools.release.check_apk', side_effect=checked):
                report = upgrade.verify_assistant_transition(
                    *roots, apks[0], pin, settings_backup_path=saved
                )
            self.assertEqual(
                report['settings_compatibility'],
                {
                    'candidate': {'compatible': True, 'omitted_default_fields': []},
                    'recovery': {
                        'compatible': True,
                        'omitted_default_fields': ['gpt_live_voice', 'voice_mode'],
                    },
                },
            )
            self.assertNotIn('credential_file', json.dumps(report))
            self.assertEqual(saved.read_bytes(), original)
            pin_settings = report['settings_sha256']
            self.assertEqual(pin_settings, upgrade.fingerprint(upgrade.verify_backup(saved)))
            staged = root / 'staged.json'
            stage = [
                'stage-restore',
                str(saved),
                '--target-root',
                str(roots[2]),
                '--expect-settings-sha256',
                pin_settings,
                '--output',
                str(staged),
            ]
            code, _, error = self.invoke(stage)
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(staged.read_text())['brain'], 'dot')
            staged.unlink()
            alternate = self.profile(folder, {'schema_version': 1, 'brain': 'minimax'})
            other_backup = root / 'other-backup.json'
            upgrade.output(upgrade.backup(alternate, roots[0]), other_backup)
            stage[1] = str(other_backup)
            code, _, error = self.invoke(stage)
            self.assertEqual(code, 2)
            self.assertIn('do not match the transition digest', error)
            self.assertFalse(staged.exists())
            trial = self.profile(folder, {'schema_version': 1, 'voice_mode': 'gpt-live-1'})
            trial_backup = root / 'trial-backup.json'
            upgrade.output(upgrade.backup(trial, roots[0]), trial_backup)
            output = root / 'blocked-transition.json'
            with patch(
                'tools.release.check_apk',
                side_effect=AssertionError('Must stop before APK tooling'),
            ):
                code, _, error = self.invoke(
                    [
                        'verify-transition',
                        '--from-root',
                        str(roots[0]),
                        '--to-root',
                        str(roots[1]),
                        '--recovery-root',
                        str(roots[2]),
                        '--baseline-apk',
                        str(apks[0]),
                        '--baseline-sha256',
                        pin,
                        '--settings-backup',
                        str(trial_backup),
                        '--output',
                        str(output),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn('incompatible with recovery', error)
            self.assertNotIn(str(trial_backup), error)
            self.assertFalse(output.exists())

    def test_verify_staged_checks_both_targets_and_rejects_a_changed_profile_or_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            roots, apks, checked, git = self.transition_fixture(folder, settings_schema=True)
            base, candidate, recovery = roots
            path = Path(folder)
            profile = self.profile(folder, {'schema_version': 1, 'brain': 'dot'})
            saved, transition = path / 'settings.json', path / 'transition.json'
            upgrade.output(upgrade.backup(profile, base), saved)
            with patch('tools.release.check_apk', side_effect=checked):
                report = upgrade.verify_assistant_transition(
                    *roots,
                    apks[0],
                    hashlib.sha256(apks[0].read_bytes()).hexdigest(),
                    settings_backup_path=saved,
                )
            upgrade.output(report, transition)
            for label, target in (('candidate', candidate), ('recovery', recovery)):
                with self.subTest(label=label):
                    staged = path / (label + '.json')
                    upgrade.output(
                        upgrade.settings_for_target(upgrade.verify_backup(saved), target), staged
                    )
                    args = [
                        'verify-staged',
                        '--transition',
                        str(transition),
                        '--settings-backup',
                        str(saved),
                        '--staged-profile',
                        str(staged),
                        '--target-root',
                        str(target),
                        '--stage',
                        label,
                    ]
                    original = staged.read_bytes()
                    code, output, error = self.invoke(args)
                    self.assertEqual(code, 0, error)
                    self.assertNotIn('signing', output)
                    self.assertEqual(staged.read_bytes(), original)
                    args[-1] = 'recovery' if label == 'candidate' else 'candidate'
                    self.assertEqual(self.invoke(args)[0], 2)
                    args[-1] = label
                    staged.write_bytes(original.replace(b'"dot"', b'"minimax"'))
                    self.assertEqual(self.invoke(args)[0], 2)
                    staged.write_bytes(original)
                    staged.chmod(0o644)
                    self.assertEqual(self.invoke(args)[0], 2)
                    staged.chmod(0o600)
                    runtime = target / 'release.json'
                    runtime.write_bytes(runtime.read_bytes() + b'\n')
                    self.assertEqual(self.invoke(args)[0], 2)
                    git(target, 'restore', 'release.json')
                    self.assertEqual(self.invoke(args)[0], 0)
            original = saved.read_bytes()
            saved.unlink()
            alternate = self.profile(folder, {'schema_version': 1, 'brain': 'minimax'})
            upgrade.output(upgrade.backup(alternate, base), saved)
            self.assertEqual(self.invoke(args)[0], 2)
            saved.write_bytes(original)
            self.assertEqual(self.invoke(args)[0], 0)

    def test_storage_recheck_uses_current_candidate_archive_and_pinned_recovery(self):
        from saved_tasks import SavedTasks
        from test_saved_tasks import task

        with tempfile.TemporaryDirectory() as folder:
            roots, apks, checked, _ = self.transition_fixture(folder, saved_task_reader=True)
            base, candidate, recovery = roots
            transition = Path(folder) / 'transition.json'
            with patch('tools.release.check_apk', side_effect=checked):
                report = upgrade.verify_assistant_transition(
                    *roots, apks[0], hashlib.sha256(apks[0].read_bytes()).hexdigest()
                )
            upgrade.output(report, transition)
            args = [
                'recheck-storage',
                '--transition',
                str(transition),
                '--runtime-root',
                str(candidate),
                '--target-root',
                str(recovery),
                '--stage',
                'recovery',
            ]
            code, output, error = self.invoke(args)
            self.assertEqual(code, 0, error)
            self.assertFalse(json.loads(output)['ready_for_installation'])
            upgrade_args = args[:4] + [
                str(base),
                '--target-root',
                str(candidate),
                '--stage',
                'candidate',
            ]
            self.assertEqual(self.invoke(upgrade_args)[0], 0)

            path = candidate / 'device-access/private/voice-runtime/saved-task-results.json'
            path.parent.mkdir(parents=True, mode=0o700)
            SavedTasks(path).save(
                {
                    **task(),
                    'window_input': {
                        'window_id': 7,
                        'application': 'Synthetic',
                        'title': 'PRIVATE_FIXTURE',
                        'width': 640,
                        'height': 480,
                    },
                }
            )
            raw = path.read_bytes()
            code, output, error = self.invoke(args)
            self.assertEqual(code, 2)
            receipt = json.loads(output)
            self.assertEqual(
                receipt['target_commit'], report['stages']['recovery']['source_commit']
            )
            self.assertFalse(receipt['saved_task_storage']['compatible'])
            self.assertEqual(receipt['saved_task_storage']['store_version'], 3)
            self.assertEqual(receipt['saved_task_storage']['reader_max_version'], 2)
            self.assertNotIn('PRIVATE_FIXTURE', output + error)
            self.assertEqual(path.read_bytes(), raw)
            self.assertTrue(
                upgrade.read_private_json(transition)['saved_task_storage']['recovery'][
                    'compatible'
                ]
            )

            args[4] = str(recovery)
            self.assertEqual(self.invoke(args)[:2], (2, ''))
            args[4] = str(candidate)
            args[6] = str(candidate)
            self.assertEqual(self.invoke(args)[:2], (2, ''))

    def test_transition_accepts_only_candidate_identical_build_support_and_reports_it(self):
        with tempfile.TemporaryDirectory() as folder:
            roots, apks, checked, git = self.transition_fixture(folder)
            for root, apk in zip(roots[1:], apks[1:]):
                for name in upgrade.RECOVERY_BUILD_SUPPORT:
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('fixed CI download support\n')
                git(root, 'add', '.')
                git(root, 'commit', '-qm', 'reviewed build support')
                receipt = apk.with_suffix('.apk.build.json')
                value = json.loads(receipt.read_bytes())
                value['source_commit'] = git(root, 'rev-parse', 'HEAD')
                receipt.write_text(json.dumps(value))
            pin = hashlib.sha256(apks[0].read_bytes()).hexdigest()
            with patch('tools.release.check_apk', side_effect=checked):
                report = upgrade.verify_assistant_transition(*roots, apks[0], pin)
            self.assertEqual(
                report['recovery_source']['candidate_matching_build_support'],
                sorted(upgrade.RECOVERY_BUILD_SUPPORT),
            )
            self.assertFalse(report['ready_for_installation'])

    def test_transition_rejects_divergent_missing_linked_executable_or_unknown_support(self):
        for variant in ('divergent', 'missing', 'link', 'executable', 'unknown', 'deleted'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as folder:
                roots, apks, _, git = self.transition_fixture(folder)
                name = '.github/workflows/ci.yml'
                if variant == 'unknown':
                    name = 'tools/other-download.py'
                if variant == 'deleted':
                    # Start from a shared tracked CI file, then delete it in recovery.
                    for root in roots:
                        path = root / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text('original CI\n')
                        git(root, 'add', '.')
                        git(root, 'commit', '-qm', 'shared CI fixture')
                    (roots[2] / name).unlink()
                    git(roots[2], 'commit', '-qam', 'deleted recovery CI')
                else:
                    for index, root in enumerate(roots[1:]):
                        if variant == 'missing' and index == 0:
                            continue
                        path = root / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if variant == 'link':
                            path.symlink_to('release.json')
                        else:
                            path.write_text(f'CI {index if variant == "divergent" else 0}\n')
                            if variant == 'executable':
                                path.chmod(0o755)
                        git(root, 'add', '.')
                        git(root, 'commit', '-qm', 'invalid recovery support')
                pin = hashlib.sha256(apks[0].read_bytes()).hexdigest()
                with patch('tools.release.check_apk') as probe:
                    with self.assertRaisesRegex(ValueError, 'Recovery'):
                        upgrade.verify_assistant_transition(*roots, apks[0], pin)
                    probe.assert_not_called()

    def test_recovery_cannot_hide_runtime_deletion_as_a_rename_to_allowed_support(self):
        with tempfile.TemporaryDirectory() as folder:
            roots, apks, _, git = self.transition_fixture(folder)
            runtime = 'cc8-assistant/host/baseline_behavior.py'
            support = '.github/workflows/ci.yml'
            for root in roots:
                path = root / runtime
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('same bytes to induce Git rename detection\n')
                git(root, 'add', '.')
                git(root, 'commit', '-qm', 'baseline runtime fixture')
            for root in roots[1:]:
                path = root / support
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((root / runtime).read_bytes())
                if root == roots[2]:
                    (root / runtime).unlink()
                git(root, 'add', '.')
                git(root, 'commit', '-qm', 'support and runtime deletion')
            pin = hashlib.sha256(apks[0].read_bytes()).hexdigest()
            with patch('tools.release.check_apk') as probe:
                with self.assertRaisesRegex(ValueError, 'only release.json'):
                    upgrade.verify_assistant_transition(*roots, apks[0], pin)
                probe.assert_not_called()

    def test_optional_observation_gate_reaudits_private_report_against_the_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            roots, apks, checked, _ = self.transition_fixture(folder)
            recorded = Path(folder) / 'observation.json'
            data = observation()
            window = data['observation']
            backend = upgrade.load_release(roots[0]).backend_version
            for state, expected_backend, expected_code, reason in (
                ('running', backend, 2, 'window_not_completed'),
                ('completed', '9.0.1', 2, 'backend_version_not_bound'),
                ('completed', backend, 0, None),
            ):
                with self.subTest(state=state, backend=expected_backend):
                    window.update(state=state, expected_backend_version=expected_backend)
                    data['window_verified'] = True  # The saved verdict is not authoritative.
                    encoded = json.dumps(data)
                    recorded.write_text(encoded)
                    recorded.chmod(0o600)
                    output = Path(folder) / f'receipt-{state}-{expected_backend}.json'
                    arguments = [
                        'verify-transition',
                        '--from-root',
                        str(roots[0]),
                        '--to-root',
                        str(roots[1]),
                        '--recovery-root',
                        str(roots[2]),
                        '--baseline-apk',
                        str(apks[0]),
                        '--baseline-sha256',
                        hashlib.sha256(apks[0].read_bytes()).hexdigest(),
                        '--observation-report',
                        str(recorded),
                        '--output',
                        str(output),
                    ]
                    with patch('tools.release.check_apk', side_effect=checked):
                        code, _, err = self.invoke(arguments)
                    self.assertEqual(code, expected_code, err)
                    report = json.loads(output.read_text())
                    self.assertTrue(report['verified_against_pinned_baseline'])
                    self.assertFalse(report['ready_for_installation'])
                    gate = report['observation_gate']
                    self.assertEqual(gate['baseline_backend_version'], backend)
                    self.assertEqual(gate['report_sha256'], upgrade.fingerprint(data))
                    self.assertEqual(gate['window_verified'], expected_code == 0)
                    self.assertEqual(gate['reasons'], [reason] if reason else [])
                    self.assertNotIn('samples', gate)
                    self.assertEqual(recorded.read_text(), encoded)
            window['interval_seconds'] = 10**1000
            recorded.write_text(json.dumps(data))
            invalid = Path(folder) / 'invalid-window.json'
            arguments[-1] = str(invalid)
            with patch('tools.release.check_apk', side_effect=checked):
                code, _, err = self.invoke(arguments)
            self.assertEqual(code, 2, err)
            self.assertFalse(invalid.exists())

    def test_transition_rejects_wrong_baseline_signer_or_stale_build_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            roots, apks, checked, _ = self.transition_fixture(folder)
            pin = hashlib.sha256(apks[0].read_bytes()).hexdigest()
            with patch('tools.release.check_apk', side_effect=checked):
                with self.assertRaisesRegex(ValueError, 'recorded installed artifact'):
                    upgrade.verify_assistant_transition(*roots, apks[0], 'b' * 64)

            def wrong_signer(component, apk, root):
                item = checked(component, apk, root)
                if root == roots[2].resolve():
                    item['signer_sha256'] = ['b' * 64]
                return item

            with patch('tools.release.check_apk', side_effect=wrong_signer):
                with self.assertRaisesRegex(RuntimeError, 'Signing certificate mismatch'):
                    upgrade.verify_assistant_transition(*roots, apks[0], pin)
            apks[1].write_bytes(b'new bytes with stale receipt')
            with patch('tools.release.check_apk', side_effect=checked):
                with self.assertRaisesRegex(ValueError, 'build receipt'):
                    upgrade.verify_assistant_transition(*roots, apks[0], pin)

    def test_transition_rechecks_baseline_if_it_changes_while_other_apks_are_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            roots, apks, checked, _ = self.transition_fixture(folder)
            pin = hashlib.sha256(apks[0].read_bytes()).hexdigest()

            def changed(component, apk, root):
                if apk == apks[2].resolve():
                    apks[0].write_bytes(b'different baseline after signature check')
                return checked(component, apk, root)

            with patch('tools.release.check_apk', side_effect=changed):
                with self.assertRaisesRegex(ValueError, 'APK changed'):
                    upgrade.verify_assistant_transition(*roots, apks[0], pin)

    def test_transition_rejects_dirty_changed_or_lower_version_recovery_before_apk_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            roots, apks, _, git = self.transition_fixture(folder)
            pin = hashlib.sha256(apks[0].read_bytes()).hexdigest()
            recovery = roots[2]
            extra = recovery / 'unexpected-code'
            extra.write_text('changed code')
            with patch('tools.release.check_apk') as probe:
                with self.assertRaisesRegex(ValueError, 'clean'):
                    upgrade.verify_assistant_transition(*roots, apks[0], pin)
                git(recovery, 'add', extra.name)
                git(recovery, 'commit', '-qm', 'unrelated code')
                with self.assertRaisesRegex(ValueError, 'only release.json'):
                    upgrade.verify_assistant_transition(*roots, apks[0], pin)
                git(recovery, 'revert', '--no-edit', 'HEAD')
                path = recovery / 'release.json'
                value = json.loads(path.read_bytes())
                value['assistant']['version_code'] -= 1
                path.write_text(json.dumps(value))
                git(recovery, 'commit', '-qam', 'invalid recovery code')
                with self.assertRaisesRegex(ValueError, 'increase versionCode'):
                    upgrade.verify_assistant_transition(*roots, apks[0], pin)
                probe.assert_not_called()

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

    def test_backup_preserves_original_checkout_defaults_and_private_precedence(self):
        from configuration import load_config

        with tempfile.TemporaryDirectory() as folder:
            source_root, target_root = self.manifests(folder)
            source_defaults = source_root / 'cc8-assistant/host/voice-config.json'
            source_defaults.parent.mkdir(parents=True)
            source_defaults.write_text(
                '{"schema_version":1,"asr":"sensevoice","tts_speed":0.9,"tls_port":18880}'
            )
            target_defaults = target_root / 'cc8-assistant/host/voice-config.json'
            target_defaults.parent.mkdir(parents=True)
            target_defaults.write_text('{"schema_version":1,"asr":"minimax","tls_port":18881}')
            source_profile = self.profile(folder)
            raw = source_profile.read_bytes()
            source = load_config(source_root, {'CC8_CONFIG_FILE': str(source_profile)})
            value = upgrade.backup(source_profile, source_root)
            backup, restored = Path(folder) / 'backup.json', Path(folder) / 'restored.json'
            upgrade.output(value, backup)
            code, _, error = self.invoke(['stage-restore', str(backup), '--output', str(restored)])
            self.assertEqual(code, 0, error)
            target = load_config(target_root, {'CC8_CONFIG_FILE': str(restored)})
            self.assertEqual(
                (source.asr, source.tls_port, source.tts_speed), ('sensevoice', 18880, 1.2)
            )
            self.assertEqual(
                (target.asr, target.tls_port, target.tts_speed),
                (source.asr, source.tls_port, source.tts_speed),
            )
            self.assertEqual(source_profile.read_bytes(), raw)

    def test_invalid_source_repository_defaults_stop_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.profile(folder)
            defaults = root / 'cc8-assistant/host/voice-config.json'
            defaults.parent.mkdir(parents=True)
            defaults.write_text('{"schema_version":1,"brain":"unrecognized"}')
            with self.assertRaises(upgrade.ConfigError):
                upgrade.backup(source, root)

    def test_targeted_restore_omits_only_default_fields_missing_in_old_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = self.profile(folder)
            original = profile.read_bytes()
            schema = upgrade.config_schema()
            del schema['properties']['voice_mode']
            del schema['properties']['gpt_live_voice']
            schema['properties']['brain']['enum'] = ['dot', 'minimax']
            declaration = root / 'cc8-assistant/host/resources/runtime-config.schema.json'
            declaration.parent.mkdir(parents=True)
            declaration.write_text(json.dumps(schema))
            saved, staged = root / 'backup.json', root / 'staged.json'
            snapshot = upgrade.backup(profile, root)
            upgrade.output(snapshot, saved)
            args = ['stage-restore', str(saved), '--target-root', str(root), '--output']
            code, _, error = self.invoke([*args, str(staged)])
            self.assertEqual(code, 0, error)
            expected = snapshot['settings'].copy()
            del expected['voice_mode']
            del expected['gpt_live_voice']
            self.assertEqual(json.loads(staged.read_text()), expected)
            self.assertEqual(profile.read_bytes(), original)
            self.assertEqual(upgrade.verify_backup(saved), snapshot['settings'])
            self.assertEqual(
                upgrade.settings_for_target({**expected, 'brain': 'minimax'}, root),
                {**expected, 'brain': 'minimax'},
            )
            with self.assertRaisesRegex(ValueError, 'field contract differs'):
                upgrade.settings_for_target({**expected, 'brain': 'echo'}, root)
            schema['properties']['brain']['maxLength'] = 6
            declaration.write_text(json.dumps(schema))
            with self.assertRaisesRegex(ValueError, 'field contract differs'):
                upgrade.settings_for_target(expected, root)

    def test_targeted_restore_rejects_active_trial_and_different_field_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = self.profile(folder, {'schema_version': 1, 'voice_mode': 'gpt-live-1'})
            schema = upgrade.config_schema()
            declaration = root / 'cc8-assistant/host/resources/runtime-config.schema.json'
            declaration.parent.mkdir(parents=True)
            snapshot = upgrade.backup(profile, root)
            saved, staged = root / 'backup.json', root / 'staged.json'
            upgrade.output(snapshot, saved)
            args = [
                'stage-restore',
                str(saved),
                '--target-root',
                str(root),
                '--output',
                str(staged),
            ]
            for properties in (
                {k: v for k, v in schema['properties'].items() if k != 'voice_mode'},
                {**schema['properties'], 'tts_speed': {'type': 'number', 'maximum': 1.0}},
            ):
                declaration.write_text(json.dumps({**schema, 'properties': properties}))
                code, _, error = self.invoke(args)
                self.assertEqual(code, 2)
                self.assertIn('target configuration schema', error)
                self.assertFalse(staged.exists())
            declaration.write_text(json.dumps(schema))
            code, _, error = self.invoke(args)
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(staged.read_text()), snapshot['settings'])

            staged.unlink()
            declaration.write_text('{"properties":{},' + json.dumps(schema)[1:])
            code, _, error = self.invoke(args)
            self.assertEqual(code, 2)
            self.assertIn('target configuration schema', error)
            self.assertFalse(staged.exists())

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

"""CLI plans need no device/model/configuration; execution stays an explicit opt-in."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import check_camera_routes as cli
from camera_route_check import CameraCheckFailure
from test_camera_route_check import Simulation

ROOT = Path(__file__).resolve().parents[2]


class CameraRouteCommandTests(unittest.TestCase):
    def test_execution_audits_owner_data_before_publishing_any_passed_receipt(self):
        for fault in (None, 'store_changed', 'owner_changed', 'interrupt'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                (root / 'device-access').mkdir(mode=0o700)
                runtime = root / 'runtime'
                private = runtime / 'device-access/private/voice-runtime'
                private.mkdir(mode=0o700, parents=True)
                home = root / 'home'
                (home / '.codex').mkdir(parents=True)
                (home / '.codex/config.toml').write_text('owner-configuration')
                release = SimpleNamespace(
                    backend_version='test',
                    release_id='test',
                    protocol_version=2,
                    assistant=SimpleNamespace(version='test', version_code=134),
                )
                simulation = Simulation()
                initial = dict(
                    pid=100,
                    instance='a' * 32,
                    **{
                        key: 0
                        for key in (
                            'mic_frames',
                            'render_frames',
                            'voice_asr_attempts',
                            'local_wake_count',
                            'device_mic_frames',
                            'device_auto_voice_starts',
                        )
                    },
                )
                calls, checkpoints = [], []

                class Service:
                    plist = root / 'agent.plist'

                    def read_private(self, path):
                        return b'original-agent-with-complete-env'

                    def sample(self, **kwargs):
                        calls.append(kwargs)
                        if len(calls) == 2:
                            if fault == 'interrupt':
                                raise KeyboardInterrupt
                            if fault == 'owner_changed':
                                return dict(initial, instance='b' * 32)
                            if fault == 'store_changed':
                                path = private / 'reminders.json'
                                path.write_text('owner-new-data')
                                path.chmod(0o600)
                        return dict(initial)

                def request(path, payload):
                    value = simulation.request(payload)
                    if payload == {'operation': 'health'}:
                        value.update(bridge_version='test', release_id='test', protocol_version=2)
                    return value

                run_type = cli.CameraRouteRun

                def runner(device, request, probe):
                    return run_type(
                        device, request, probe, clock=lambda: simulation.now, sleep=simulation.sleep
                    )

                from private_output import PrivateOutput

                cli_output_write = PrivateOutput.write

                def write(output, value):
                    checkpoints.append(value)
                    cli_output_write(output, value)

                with (
                    patch.object(cli.sys, 'platform', 'darwin'),
                    patch.object(cli, 'ROOT', root),
                    patch.object(cli, 'CameraRouteRun', side_effect=runner),
                    patch('pathlib.Path.home', return_value=home),
                    patch('release_info.load_release', return_value=release),
                    patch('device_smoke.rpc', side_effect=request),
                    patch('device_smoke.Device', return_value=simulation),
                    patch('service_smoke.LiveService', return_value=Service()),
                    patch(
                        'camera_route_probe.CameraProbe',
                        side_effect=lambda path, instance, generation: simulation.probe(
                            instance, generation
                        ),
                    ),
                    patch.object(PrivateOutput, 'write', write),
                ):
                    state, path = cli.execute(runtime, ('wifi_handover',))
                report = json.loads(path.read_text())
                self.assertEqual(
                    state,
                    'passed'
                    if fault is None
                    else 'interrupted'
                    if fault == 'interrupt'
                    else 'failed',
                )
                self.assertEqual(report['state'], state)
                self.assertTrue(all(value['state'] == 'running' for value in checkpoints[:-1]))
                self.assertEqual(
                    report['original_stores_and_configuration_preserved'], fault is None
                )
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertTrue(all(probe.closed for probe in simulation.probes))
                if fault == 'store_changed':
                    self.assertEqual((private / 'reminders.json').read_text(), 'owner-new-data')

    def test_default_and_each_case_plan_never_invoke_execution(self):
        for selected in (None, 'wifi_handover', 'all_routes_lost'):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / 'missing-runtime'
                args = ['--runtime-root', str(root)] + (['--case', selected] if selected else [])
                with (
                    patch.object(cli, 'execute', side_effect=AssertionError('No capture')),
                    contextlib.redirect_stdout(io.StringIO()) as out,
                ):
                    self.assertEqual(cli.main(args), 0)
                plan = json.loads(out.getvalue())
                self.assertFalse(plan['execute'] or plan['camera_started'])
                self.assertTrue(plan['metadata_only'])
                self.assertEqual(plan['camera_requests_at_most'], 1 if selected else 2)
                self.assertFalse(root.exists())

    def test_explicit_execution_passes_only_selected_cases(self):
        with (
            patch.object(
                cli, 'execute', return_value=('passed', Path('/private/report.json'))
            ) as execute,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli.main(['--runtime-root', '/runtime', '--case', 'all_routes_lost', '--execute']),
                0,
            )
        execute.assert_called_once_with(Path('/runtime'), ('all_routes_lost',))

    def test_interrupt_and_failure_exit_codes_never_claim_passed(self):
        for state, code in (('interrupted', 130), ('failed', 1)):
            with (
                self.subTest(state=state),
                patch.object(cli, 'execute', return_value=(state, Path('/private/report.json'))),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.assertEqual(cli.main(['--runtime-root', '/runtime', '--execute']), code)
                self.assertEqual(json.loads(out.getvalue())['state'], state)
        with (
            patch.object(cli, 'execute', side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(cli.main(['--runtime-root', '/runtime', '--execute']), 130)
            self.assertEqual(json.loads(out.getvalue())['state'], 'interrupted')

    def test_actual_root_cli_works_with_minimal_path_without_voice_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / 'cc8'),
                    'verify-camera-routes',
                    '--runtime-root',
                    str(Path(folder) / 'missing'),
                ],
                cwd=folder,
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout)['cases'], ['wifi_handover', 'all_routes_lost']
            )
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_old_backend_is_rejected_before_android_access_or_lock_creation(self):
        release = SimpleNamespace(backend_version='0.16.24', release_id='old', protocol_version=2)
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder).resolve()
            with (
                patch.object(cli.sys, 'platform', 'darwin'),
                patch('release_info.load_release', return_value=release),
                patch(
                    'device_smoke.rpc',
                    return_value=dict(
                        bridge_version='0.16.24',
                        release_id='old',
                        protocol_version=2,
                        camera_metadata_only_supported=False,
                    ),
                ),
                patch('device_smoke.Device') as device,
                patch('service_smoke.LiveService') as service,
            ):
                with self.assertRaisesRegex(
                    CameraCheckFailure, '^metadata_only_camera_unsupported$'
                ):
                    cli.execute(runtime, ('wifi_handover',))
                device.assert_not_called()
                service.assert_not_called()
            self.assertEqual(list(runtime.iterdir()), [])

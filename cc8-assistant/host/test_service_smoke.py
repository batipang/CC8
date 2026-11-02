"""Fault verification gates are exercised without touching launchd or devices."""

import json
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from device_smoke import CheckFailure
from service_smoke import LiveService, RecoveryRun, process_rows


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Service:
    def __init__(self):
        self.pid = 100
        self.crashes = 0
        self.unready = 0
        self.failure = None
        self.same_instance = False

    def sample(self):
        if self.failure:
            raise CheckFailure(self.failure)
        if self.unready:
            self.unready -= 1
            return None
        return {
            'pid': self.pid,
            'instance': 'constant' if self.same_instance else str(self.pid),
            'mic_frames': 0,
        }

    def crash(self, previous):
        self.crashes += 1
        self.pid += 1
        self.unready = 2


class RecoveryTests(unittest.TestCase):
    def live_fixture(self):
        service = LiveService.__new__(LiveService)
        service.root = Path('/test/project')
        service.private = service.root / 'private'
        service.socket = service.private / 'announce.sock'
        service.plist = service.root / 'agent.plist'
        service.release = SimpleNamespace(
            backend_version='1.0.0',
            protocol_version=2,
            assistant=SimpleNamespace(version='1.0.0', version_code=1),
        )
        health = dict(
            ok=True,
            bridge_version='1.0.0',
            protocol_version=2,
            microphone_active=False,
            playing=False,
            calibrating=False,
            connected=True,
            transport='usb',
            model_ready=True,
            recognition_worker=True,
            response_worker=True,
            runtime_instance='fixture',
            mic_frames=0,
        )
        device = dict(foreground=True, microphone_active=False, vision_active=False)
        ui = dict(camera_open=False)
        processes = {'bridge': [(20, 10)], 'supervisor': [(10, 1)], 'unexpected': []}
        agent = {
            'WorkingDirectory': str(service.root),
            'ProgramArguments': [
                str(service.root / '.toolchains/cc8/voice-venv/bin/python'),
                str(service.root / 'cc8-assistant/host/service.py'),
                'run',
            ],
        }
        supervisor = dict(phase='ready', pid=20)
        enabled = dict(enabled=True)
        requests = []
        service.device = SimpleNamespace(status=lambda: (device, ui), version=lambda: ('1.0.0', 1))
        service.processes = lambda: processes

        def read(path):
            if path.name == 'agent.plist':
                return plistlib.dumps(agent)
            return json.dumps(
                enabled if path.name == 'service-enabled.json' else supervisor
            ).encode()

        service.read_private = read

        def request(path, payload):
            return health if payload['operation'] == 'health' else {'requests': requests}

        return service, health, device, ui, processes, agent, supervisor, enabled, requests, request

    def test_live_gates_reject_busy_foreign_or_unmanaged_targets_before_signal(self):
        for kind in (
            'microphone',
            'playing',
            'calibrating',
            'foreground',
            'camera',
            'vision',
            'version',
            'protocol_type',
            'duplicates',
            'parent',
            'unexpected',
            'agent',
            'owner_stop',
            'pending',
        ):
            (
                service,
                health,
                device,
                ui,
                processes,
                agent,
                supervisor,
                enabled,
                requests,
                request,
            ) = self.live_fixture()
            if kind == 'microphone':
                health['microphone_active'] = True
            elif kind in ('playing', 'calibrating'):
                health[kind] = True
            elif kind == 'foreground':
                device['foreground'] = False
            elif kind == 'camera':
                ui['camera_open'] = True
            elif kind == 'vision':
                device['vision_active'] = True
            elif kind == 'version':
                health['bridge_version'] = '9.9.9'
            elif kind == 'protocol_type':
                health['protocol_version'] = 2.0
            elif kind == 'duplicates':
                processes['bridge'].append((21, 10))
            elif kind == 'parent':
                processes['bridge'] = [(20, 99)]
            elif kind == 'unexpected':
                processes['unexpected'].append((30, 10))
            elif kind == 'agent':
                agent['WorkingDirectory'] = '/another'
            elif kind == 'owner_stop':
                enabled['enabled'] = False
            else:
                requests.append({'outcome': None})
            with (
                self.subTest(kind=kind),
                patch('service_smoke.rpc', side_effect=request),
                patch('service_smoke.os.kill') as kill,
            ):
                with self.assertRaises(CheckFailure):
                    service.crash({'pid': 20, 'instance': 'fixture', 'mic_frames': 0})
                kill.assert_not_called()

    def test_live_ready_requires_both_supervisor_and_health_receipts(self):
        service, health, device, ui, processes, agent, supervisor, enabled, requests, request = (
            self.live_fixture()
        )
        with patch('service_smoke.rpc', side_effect=request):
            self.assertEqual(service.sample(), {'pid': 20, 'instance': 'fixture', 'mic_frames': 0})
            supervisor['pid'] = 99
            self.assertIsNone(service.sample())
            supervisor['pid'] = 20
            health['response_worker'] = False
            self.assertIsNone(service.sample())

    def run_check(self, service, cycles=10):
        clock = Clock()
        reports = []
        result = RecoveryRun(
            service,
            cycles,
            clock=clock,
            sleep=clock.sleep,
            checkpoint=lambda value: reports.append(json.loads(json.dumps(value))),
        ).run()
        return result, reports

    def test_ten_distinct_real_instance_transitions_are_required(self):
        service = Service()
        result, reports = self.run_check(service)
        self.assertEqual(result['state'], 'passed')
        self.assertEqual(service.crashes, 10)
        self.assertEqual(len(result['checks']), 10)
        self.assertTrue(result['recovery_verified'])
        self.assertTrue(all(r['state'] == 'passed' for r in result['checks']))
        self.assertFalse(result['starts_capture'])
        self.assertFalse(result['opens_ui'])
        self.assertNotIn('instance', json.dumps(result))
        self.assertEqual(reports[-1], result)

    def test_pending_task_capture_owner_stop_and_duplicate_abort_before_signalling(self):
        for reason in (
            'owner_request_pending',
            'backend_capture_active',
            'owner_service_stopped',
            'duplicate_backend',
            'device_camera_active',
        ):
            service = Service()
            service.failure = reason
            with self.subTest(reason=reason):
                result, _ = self.run_check(service)
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(service.crashes, 0)

    def test_stale_health_cannot_count_as_a_recovered_instance(self):
        service = Service()
        service.same_instance = True
        result, _ = self.run_check(service, 1)
        self.assertEqual(result['state'], 'recovery_unconfirmed')
        self.assertEqual(service.crashes, 1)
        self.assertEqual(result['checks'][0]['reason'], 'service_recovery_timeout')

    def test_interrupt_after_fault_only_observes_existing_supervisor(self):
        service = Service()
        original = service.sample
        interrupted = False

        def sample():
            nonlocal interrupted
            if service.crashes and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original()

        service.sample = sample
        result, _ = self.run_check(service, 3)
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(service.crashes, 1)
        self.assertTrue(result['recovery_verified'])

    def test_failed_recovery_does_not_override_an_owner_stop(self):
        service = Service()

        def crash(previous):
            service.crashes += 1
            service.failure = 'owner_service_stopped'

        service.crash = crash
        result, _ = self.run_check(service, 1)
        self.assertEqual(result['state'], 'recovery_unconfirmed')
        self.assertFalse(result['recovery_verified'])
        self.assertEqual(service.crashes, 1)

    def test_cycle_bound_and_preflight_not_ready(self):
        for cycles in (0, 11, True, 1.5):
            with self.assertRaises(CheckFailure):
                RecoveryRun(Service(), cycles)
        service = Service()
        service.unready = 1
        result, _ = self.run_check(service)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(service.crashes, 0)

    def test_exact_script_uid_and_arguments_required_for_process_selection(self):
        root = Path('/a/project space')
        bridge = "'/a/project space/cc8-assistant/host/bridge.py'"
        supervisor = "'/a/project space/cc8-assistant/host/service.py' run"
        text = '\n'.join(
            (
                f'20 501 10 /python -u {bridge}',
                f'10 501 1 /python {supervisor}',
                f'21 502 10 /python -u {bridge}',
                f'22 501 10 /python -u {bridge} --unexpected',
                '23 501 10 /python -u /other/bridge.py',
                'broken row',
            )
        )
        rows = process_rows(text, root, 501)
        self.assertEqual(
            rows, {'bridge': [(20, 10)], 'supervisor': [(10, 1)], 'unexpected': [(22, 10)]}
        )

    def test_crash_revalidates_entire_snapshot_before_sigkill(self):
        service = LiveService.__new__(LiveService)
        expected = {'pid': 20, 'instance': 'a', 'mic_frames': 0}
        with (
            patch.object(service, 'sample', return_value={**expected, 'instance': 'b'}),
            patch('service_smoke.os.kill') as kill,
        ):
            with self.assertRaises(CheckFailure):
                service.crash(expected)
            kill.assert_not_called()
        with (
            patch.object(service, 'sample', return_value=expected),
            patch.object(
                service,
                'processes',
                return_value={'bridge': [(20, 10)], 'supervisor': [(10, 1)], 'unexpected': []},
            ),
            patch('service_smoke.os.kill') as kill,
        ):
            service.crash(expected)
            kill.assert_called_once_with(20, signal.SIGKILL)

    def test_private_metadata_rejects_public_permissions_and_symlinks(self):
        service = LiveService.__new__(LiveService)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'state'
            target.write_text('{}')
            target.chmod(0o644)
            with self.assertRaises(CheckFailure):
                service.read_private(target)
            target.chmod(0o600)
            link = Path(folder) / 'link'
            link.symlink_to(target)
            with self.assertRaises(CheckFailure):
                service.read_private(link)
            self.assertEqual(service.read_private(target), b'{}')

    def test_real_minimal_path_plan_never_queries_or_creates_runtime(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'absent'
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / 'cc8'),
                    'verify-service',
                    '--runtime-root',
                    str(target),
                ],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['execute'])
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()

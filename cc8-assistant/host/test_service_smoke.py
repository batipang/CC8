"""Fault verification gates are exercised without touching launchd or devices."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from device_smoke import CheckFailure, Device
from service_smoke import (
    LiveService,
    RecoveryRun,
    SleepRecoveryRun,
    SupervisorRecoveryRun,
    mac_sleep_clocks,
    main,
    process_rows,
)


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
    def test_sleep_plan_is_read_only_and_separate_from_fault_injection(self):
        output = io.StringIO()
        with patch('service_smoke.Device') as device, redirect_stdout(output):
            self.assertEqual(main(['--runtime-root', '/unused', '--observe-sleep']), 0)
            device.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['scope'], 'observed_mac_system_sleep')
        self.assertFalse(plan['forces_sleep'])
        self.assertEqual(plan['wait_clock'], 'awake')
        for flags in (
            ['--observe-sleep', '--fault', 'backend'],
            ['--observe-sleep', '--cycles', '2'],
            ['--sleep-wait', '10'],
            ['--observe-sleep', '--sleep-wait', '9'],
        ):
            with (
                self.subTest(flags=flags),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                main(['--runtime-root', '/unused', *flags])

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS raw clocks')
    def test_macos_sleep_clock_samples_both_kernel_clocks(self):
        instant, low, high = mac_sleep_clocks()
        self.assertGreater(instant, 0)
        self.assertLessEqual(low, high)
        self.assertLessEqual(high - low, 0.011)

    def test_sleep_witness_requires_suspend_idle_runtime_and_stable_recovery(self):
        for case, reason in (
            ('valid', None),
            ('no_sleep', 'resume_sleep_not_observed'),
            ('new_instance', 'resume_runtime_or_activity_changed'),
            ('new_request', 'resume_runtime_or_activity_changed'),
            ('unstable', 'resume_service_not_stable'),
            ('extra_sleep', 'resume_additional_sleep_observed'),
            ('foreign_adb_at_baseline', 'usb_serial_mismatch'),
        ):
            with self.subTest(case=case), redirect_stdout(io.StringIO()):
                fake = Clock()

                def clocks():
                    offset = 15 if fake.now >= 2 and case != 'no_sleep' else 0
                    if case == 'extra_sleep' and fake.now >= 5:
                        offset += 5
                    return fake.now + offset, offset - 0.001, offset

                service = Mock()
                if case == 'foreign_adb_at_baseline':
                    service.device.verify_usb_serial.side_effect = CheckFailure(
                        'usb_serial_mismatch'
                    )
                baseline = dict(
                    pid=20, instance='private-runtime', mic_frames=10, requests=frozenset()
                )
                service.supervisor_sample.return_value = baseline
                second = baseline
                if case == 'new_instance':
                    second = dict(baseline, instance='new-runtime')
                elif case == 'new_request':
                    second = dict(baseline, requests={'private-new-request'})
                service.resume_sample.side_effect = [
                    None,
                    second,
                    None if case == 'unstable' else baseline,
                ]
                checkpoints = []
                runner = SleepRecoveryRun(
                    service,
                    30,
                    clock=clocks,
                    sleep=fake.sleep,
                    checkpoint=lambda report: checkpoints.append(dict(report)),
                    usb_reader=lambda _: (
                        SimpleNamespace(serial='private-serial', location=8),
                        None,
                        None,
                    ),
                )
                result = runner.run()
                self.assertEqual(checkpoints[-1], result)
                self.assertNotIn('private-', json.dumps(result))
                if reason:
                    self.assertEqual(result.get('reason'), reason)
                    self.assertFalse(result['recovery_verified'])
                else:
                    self.assertEqual(result['state'], 'passed')
                    self.assertEqual(result['minimum_sleep_seconds'], 14.999)
                    self.assertEqual(result['unready_samples_after_resume'], 1)
                    self.assertEqual(result['post_resume_ready_observed_seconds'], 1)
                service.crash.assert_not_called()
                service.crash_supervisor.assert_not_called()
                if case == 'foreign_adb_at_baseline':
                    service.supervisor_sample.assert_not_called()
                    service.resume_sample.assert_not_called()

    def test_sleep_budget_excludes_time_suspended_and_does_not_sum_separate_samples(self):
        for case, offset_at, expected in (
            ('long_sleep', lambda awake: 120 if awake >= 2 else 0, 'passed'),
            ('small_separate_gaps', lambda awake: min(int(awake), 2) * 3, 'failed'),
        ):
            with self.subTest(case=case), redirect_stdout(io.StringIO()):
                fake = Clock()
                usb = SimpleNamespace(serial='private-serial', location=8)
                service = Mock()
                baseline = {'pid': 20, 'instance': 'private-runtime'}
                service.supervisor_sample.return_value = baseline
                service.resume_sample.return_value = baseline

                def clocks():
                    offset = offset_at(fake.now)
                    return fake.now + offset, offset, offset

                result = SleepRecoveryRun(
                    service,
                    10,
                    clock=clocks,
                    sleep=fake.sleep,
                    usb_reader=lambda _: (usb, None, None),
                ).run()
                self.assertEqual(result['state'], expected)
                if case == 'long_sleep':
                    self.assertEqual(result['minimum_sleep_seconds'], 120)
                    self.assertTrue(result['recovery_verified'])
                else:
                    self.assertEqual(result['reason'], 'resume_sleep_not_observed')
                    self.assertIsNone(result['minimum_sleep_seconds'])
                    service.resume_sample.assert_not_called()

    def test_sleep_recovery_stops_at_deadline_when_every_sample_is_unready(self):
        fake = Clock()
        service = Mock()
        service.resume_sample.return_value = None
        usb = SimpleNamespace(serial='original', location=8)

        def clocks():
            offset = 15 if fake.now >= 1 else 0
            return fake.now + offset, offset, offset

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            fake.sleep(seconds)

        with redirect_stdout(io.StringIO()):
            result = SleepRecoveryRun(
                service, 30, clock=clocks, sleep=sleep, usb_reader=lambda _: (usb, None, None)
            ).run()
        self.assertEqual(result['reason'], 'resume_service_not_ready')
        self.assertEqual(result['unready_samples_after_resume'], 91)
        self.assertFalse(result['recovery_verified'])

    def test_resume_requires_same_usb_enumeration_through_ready_sample(self):
        service = LiveService.__new__(LiveService)
        original = SimpleNamespace(serial='original', location=8, entry=21)
        remounted = SimpleNamespace(serial='original', location=8, entry=22)
        snapshot = {'pid': 20, 'instance': 'private-runtime'}
        for final, expected in ((remounted, None), (original, snapshot)):
            with (
                self.subTest(final=final),
                patch(
                    'service_smoke.read_usb',
                    side_effect=[(d, None, None) for d in (original, original, final)],
                ),
                patch('service_smoke.Device'),
                patch.object(service, 'supervisor_sample', return_value=snapshot),
            ):
                self.assertEqual(service.resume_sample(('original', 8)), expected)

    def test_resume_never_targets_a_different_usb_identity(self):
        service = LiveService.__new__(LiveService)
        original = SimpleNamespace(serial='original', location=9, entry=21)
        for unexpected in (
            SimpleNamespace(serial='other', location=9, entry=22),
            SimpleNamespace(serial='original', location=8, entry=22),
        ):
            for stage in range(3):
                observations = [original] * stage + [unexpected]
                with (
                    self.subTest(unexpected=unexpected, stage=stage),
                    patch(
                        'service_smoke.read_usb',
                        side_effect=[(d, None, None) for d in observations],
                    ),
                    patch('service_smoke.Device') as device,
                    patch.object(service, 'supervisor_sample', return_value=None) as sample,
                ):
                    with self.assertRaisesRegex(CheckFailure, 'resume_usb_identity_changed'):
                        service.resume_sample(('original', 9))
                    self.assertEqual(device.call_count, int(stage > 0))
                    self.assertEqual(sample.call_count, int(stage == 2))

    def test_resume_checks_usb_identity_even_when_adb_target_disappears(self):
        service = LiveService.__new__(LiveService)
        original = SimpleNamespace(serial='original', location=9, entry=21)
        unexpected = SimpleNamespace(serial='other', location=9, entry=22)
        for during_probe in (True, False):
            observations = [original] * (1 if during_probe else 2) + [unexpected]
            with (
                self.subTest(during_probe=during_probe),
                patch(
                    'service_smoke.read_usb',
                    side_effect=[(d, None, None) for d in observations],
                ),
                patch('service_smoke.Device', side_effect=OSError() if during_probe else None),
                patch.object(
                    service, 'supervisor_sample', side_effect=CheckFailure('usb_target_changed')
                ) as sample,
            ):
                with self.assertRaisesRegex(CheckFailure, 'resume_usb_identity_changed'):
                    service.resume_sample(('original', 9))
                self.assertEqual(sample.call_count, int(not during_probe))

    def test_resume_rejects_mismatched_adb_target_even_when_usb_registry_is_stable(self):
        service = LiveService.__new__(LiveService)
        original = SimpleNamespace(serial='original', location=9, entry=21)
        device = Mock()
        device.verify_usb_serial.side_effect = CheckFailure('usb_serial_mismatch')
        with (
            patch('service_smoke.read_usb', return_value=(original, None, None)),
            patch('service_smoke.Device', return_value=device),
            patch.object(service, 'supervisor_sample') as sample,
        ):
            with self.assertRaisesRegex(CheckFailure, 'usb_serial_mismatch'):
                service.resume_sample(('original', 9))
            device.verify_usb_serial.assert_called_once_with('original')
            sample.assert_not_called()

    def test_adb_serial_verification_addresses_only_the_bound_transport(self):
        device = Device.__new__(Device)
        device.access = SimpleNamespace(ADB='/tools/adb')
        device.transport = '8'
        with patch('device_smoke.subprocess.check_output', return_value='original\n') as read:
            device.verify_usb_serial('original')
            with self.assertRaisesRegex(CheckFailure, '^usb_serial_mismatch$'):
                device.verify_usb_serial('other')
            for call in read.call_args_list:
                self.assertEqual(call.args[0], ['/tools/adb', '-t', '8', 'get-serialno'])

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

    def test_live_distinguishes_missing_and_duplicate_supervisors_before_signalling(self):
        service, _, _, _, processes, _, _, _, _, _ = self.live_fixture()
        for supervisors, reason in (
            ([], 'supervisor_missing'),
            ([(10, 1), (11, 1)], 'duplicate_supervisor'),
        ):
            with self.subTest(reason=reason), patch('service_smoke.os.kill') as kill:
                processes['supervisor'] = supervisors
                with self.assertRaisesRegex(CheckFailure, f'^{reason}$'):
                    service.crash({'pid': 20, 'instance': 'fixture', 'mic_frames': 0})
                kill.assert_not_called()

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

    def test_failed_cycle_records_the_gate_that_rejected_it(self):
        for sample_number, stage in enumerate(
            ('fault_gate', 'waiting_for_ready', 'checking_stability'), start=2
        ):
            with self.subTest(stage=stage):
                service = Service()
                old_sample = service.sample
                samples = 0

                def sample():
                    nonlocal samples
                    samples += 1
                    if samples == sample_number:
                        raise CheckFailure('supervisor_missing')
                    return old_sample()

                def crash(previous):
                    service.sample()
                    service.crashes += 1
                    service.pid += 1

                service.sample = sample
                service.crash = crash
                result, _ = self.run_check(service, 2)
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(result['checks'][0].get('failure_stage'), stage)
                self.assertEqual(result['checks'][0]['reason'], 'supervisor_missing')
                self.assertEqual(service.crashes, int(stage != 'fault_gate'))
                self.assertEqual(result['recovery_verified'], stage != 'fault_gate')

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
        bridge = str(root / 'cc8-assistant/host/bridge.py')
        supervisor = str(root / 'cc8-assistant/host/service.py')
        args = {
            20: ['python', '-u', bridge],
            10: ['python', supervisor, 'run'],
            22: ['python', '-u', bridge, '--unexpected'],
            24: ['python', '-u', '/a/project', 'space/cc8-assistant/host/bridge.py'],
            25: [],
        }
        text = '\n'.join(
            (
                f'20 501 10 /python -u {bridge}',
                f'10 501 1 /python {supervisor} run',
                f'21 502 10 /python -u {bridge}',
                f'22 501 10 /python -u {bridge} --unexpected',
                '23 501 10 /python -u /other/bridge.py',
                f'24 501 10 /python -u {bridge}',
                f'25 501 10 /python -u {bridge}',
                'broken row',
            )
        )
        with patch('service_smoke.process_arguments', side_effect=args.__getitem__):
            rows = process_rows(text, root, 501)
        self.assertEqual(
            rows,
            {'bridge': [(20, 10)], 'supervisor': [(10, 1)], 'unexpected': [(22, 10), (25, 10)]},
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

    def test_adopted_backend_needs_fresh_ready_and_exact_launchd_supervisor(self):
        for kind in ('valid', 'stale', 'future', 'boolean', 'nonfinite', 'other_agent', 'foreign'):
            service, _, _, _, processes, _, supervisor, _, _, request = self.live_fixture()
            processes['bridge'] = [(20, 99 if kind == 'foreign' else 1)]
            supervisor['updated_at'] = {
                'stale': 90,
                'future': 101,
                'boolean': True,
                'nonfinite': float('nan'),
            }.get(kind, 99)
            with (
                self.subTest(kind=kind),
                patch('service_smoke.rpc', side_effect=request),
                patch('service_smoke.time.time', return_value=100),
                patch.object(
                    service, 'launchd_pid', return_value=11 if kind == 'other_agent' else 10
                ),
                patch('service_smoke.os.kill') as kill,
            ):
                if kind == 'valid':
                    self.assertEqual(service.sample()['pid'], 20)
                else:
                    with self.assertRaises(CheckFailure):
                        service.sample()
                kill.assert_not_called()

    def test_launchd_pid_requires_one_bounded_receipt_for_exact_agent(self):
        service = LiveService.__new__(LiveService)
        service.label = 'owned.agent'
        for text in ('pid = 20\n', 'pid = 0\n', 'pid = 20\n pid = 21\n', 'x' * 65537):
            with (
                self.subTest(receipt_length=len(text)),
                patch(
                    'service_smoke.subprocess.run', return_value=SimpleNamespace(stdout=text)
                ) as run,
            ):
                if text == 'pid = 20\n':
                    self.assertEqual(service.launchd_pid(), 20)
                else:
                    with self.assertRaises(CheckFailure):
                        service.launchd_pid()
                self.assertEqual(run.call_args.args[0][-1], f'gui/{os.getuid()}/owned.agent')

    def test_supervisor_sample_waits_for_a_post_fault_ready_write(self):
        service, _, _, _, _, _, supervisor, _, _, request = self.live_fixture()
        supervisor['updated_at'] = 99
        with (
            patch('service_smoke.rpc', side_effect=request),
            patch.object(service, 'launchd_pid', return_value=10),
        ):
            self.assertIsNone(service.supervisor_sample(ready_after=100))
            supervisor['updated_at'] = 101
            snapshot = service.supervisor_sample(ready_after=100)
            self.assertEqual(snapshot['supervisor_pid'], 10)
            self.assertFalse(snapshot['adopted_bridge'])

    def test_supervisor_signal_revalidates_snapshot_and_never_targets_bridge(self):
        service = LiveService.__new__(LiveService)
        snapshot = {'pid': 20, 'instance': 'a', 'mic_frames': 0, 'supervisor_pid': 10}
        for changed in (False, True):
            actual = {**snapshot, 'supervisor_pid': 11} if changed else snapshot
            with (
                self.subTest(changed=changed),
                patch.object(service, 'supervisor_sample', return_value=actual),
                patch('service_smoke.os.kill') as kill,
            ):
                if changed:
                    with self.assertRaises(CheckFailure):
                        service.crash_supervisor(snapshot)
                    kill.assert_not_called()
                else:
                    service.crash_supervisor(snapshot)
                    kill.assert_called_once_with(10, signal.SIGKILL)

    def test_only_exact_surviving_backend_may_wait_for_delayed_adoption_ready(self):
        for kind in ('same', 'other_pid', 'other_instance', 'other_parent', 'duplicate'):
            service, health, _, _, processes, _, supervisor, _, _, request = self.live_fixture()
            processes['bridge'] = [
                (21 if kind == 'other_pid' else 20, 99 if kind == 'other_parent' else 1)
            ]
            supervisor['updated_at'] = 80
            if kind == 'other_instance':
                health['runtime_instance'] = 'other'
            if kind == 'duplicate':
                processes['supervisor'].append((11, 1))
            with (
                self.subTest(kind=kind),
                patch('service_smoke.rpc', side_effect=request),
                patch('service_smoke.time.time', return_value=100),
                patch('service_smoke.os.kill') as kill,
            ):
                if kind == 'same':
                    self.assertIsNone(
                        service.supervisor_sample(
                            ready_after=90, previous={'pid': 20, 'instance': 'fixture'}
                        )
                    )
                else:
                    with self.assertRaises(CheckFailure):
                        service.supervisor_sample(
                            ready_after=90, previous={'pid': 20, 'instance': 'fixture'}
                        )
                kill.assert_not_called()

    def test_supervisor_recovery_requires_new_supervisor_and_consistent_bridge(self):
        for mode in ('adopt', 'replace', 'same_supervisor', 'changed_instance_only'):
            clock = Clock()
            sample = {
                'pid': 20,
                'instance': 'a',
                'mic_frames': 0,
                'supervisor_pid': 10,
                'adopted_bridge': False,
            }
            signals, ready_after = [], []

            def snapshot(**kwargs):
                if kwargs:
                    ready_after.append(kwargs['ready_after'])
                return dict(sample)

            def crash(previous):
                signals.append(dict(previous))
                if mode != 'same_supervisor':
                    sample['supervisor_pid'] = 11
                if mode == 'replace':
                    sample.update(pid=21, instance='b')
                elif mode == 'changed_instance_only':
                    sample['instance'] = 'b'
                else:
                    sample['adopted_bridge'] = True

            service = SimpleNamespace(supervisor_sample=snapshot, crash_supervisor=crash)
            with self.subTest(mode=mode), patch('service_smoke.time.time', return_value=100):
                result = SupervisorRecoveryRun(service, 1, clock=clock, sleep=clock.sleep).run()
                self.assertEqual(len(signals), 1)
                self.assertTrue(ready_after and all(t == 100 for t in ready_after))
                if mode in ('adopt', 'replace'):
                    self.assertEqual(result['state'], 'passed')
                    self.assertEqual(result['checks'][0]['backend_reused'], mode == 'adopt')
                else:
                    self.assertEqual(result['state'], 'recovery_unconfirmed')
                self.assertEqual(result['fault'], 'owned_supervisor_sigkill')
                self.assertNotIn('supervisor_pid', json.dumps(result))

    def test_missing_supervisor_is_transient_only_after_our_fault(self):
        def missing(**kwargs):
            raise CheckFailure('supervisor_missing')

        service = SimpleNamespace(
            supervisor_sample=missing,
            processes=lambda: {'bridge': [(20, 1)], 'supervisor': [], 'unexpected': []},
        )
        run = SupervisorRecoveryRun(service, 1)
        with self.assertRaisesRegex(CheckFailure, '^supervisor_missing$'):
            run.sample()

        run.fault_sent = True
        run.fault_at = 100
        run.fault_previous = {'pid': 20, 'instance': 'a'}
        self.assertIsNone(run.sample())
        service.processes = lambda: {
            'bridge': [(20, 1)],
            'supervisor': [(11, 1)],
            'unexpected': [],
        }
        try:
            transition = run.sample()
        except CheckFailure as error:
            self.fail(f'A single replacement between snapshots must be re-sampled: {error}')
        self.assertIsNone(transition)
        service.processes = lambda: {
            'bridge': [(20, 1)],
            'supervisor': [(10, 1), (11, 1)],
            'unexpected': [],
        }
        with self.assertRaisesRegex(CheckFailure, '^duplicate_supervisor$'):
            run.sample()

        service.supervisor_sample = Mock(side_effect=CheckFailure('duplicate_supervisor'))
        service.processes = Mock()
        with self.assertRaisesRegex(CheckFailure, '^duplicate_supervisor$'):
            run.sample()
        service.processes.assert_not_called()

    def test_post_fault_ready_boundary_is_taken_after_the_verified_signal(self):
        events = []
        service = SimpleNamespace(crash_supervisor=lambda previous: events.append('signal'))
        run = SupervisorRecoveryRun(service, 1)

        def timestamp():
            events.append('timestamp')
            return 100

        with patch('service_smoke.time.time', side_effect=timestamp):
            run.crash({'pid': 20, 'instance': 'a'})
        self.assertEqual(events, ['signal', 'timestamp'])
        self.assertEqual(run.fault_at, 100)

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

    def test_supervisor_minimal_path_plan_has_no_runtime_side_effects(self):
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
                    '--fault',
                    'supervisor',
                    '--cycles',
                    '1',
                ],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertFalse(plan['execute'])
            self.assertEqual(plan['fault'], 'owned_supervisor_sigkill')
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()

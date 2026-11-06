"""Service lifecycle boundaries; never calls launchctl or loads real voice models."""

import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from process_lock import ProcessLease, atomic_private_json
import service


class ServiceTests(unittest.TestCase):
    def test_ten_process_crashes_release_lease_without_stale_pid_ownership(self):
        code = 'import sys,time;from process_lock import ProcessLease\nwith ProcessLease(sys.argv[1]):\n print("locked",flush=True);time.sleep(20)'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bridge.lock'
            for _ in range(10):
                child = subprocess.Popen(
                    [sys.executable, '-c', code, str(path)],
                    cwd=Path(__file__).parent,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    self.assertEqual(child.stdout.readline().strip(), 'locked')
                    with self.assertRaises(BlockingIOError):
                        with ProcessLease(path):
                            pass
                    child.send_signal(signal.SIGKILL)
                    child.wait(timeout=3)
                    with ProcessLease(path):
                        pass
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                    child.stdout.close()
                    child.stderr.close()

    def test_lease_rejects_symlink_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'keep'
            target.write_text('original')
            path = Path(folder) / 'lease'
            path.symlink_to(target)
            with self.assertRaises(OSError):
                with ProcessLease(path):
                    pass
            self.assertEqual(target.read_text(), 'original')

    def test_private_marker_and_corruption_default_to_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'enabled.json'
            self.assertFalse(service.enabled(path))
            atomic_private_json(path, {'enabled': True})
            self.assertTrue(service.enabled(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            atomic_private_json(path, {'enabled': False})
            self.assertFalse(service.enabled(path))
            path.write_text('broken')
            self.assertFalse(service.enabled(path))
            self.assertEqual(list(Path(folder).glob('.enabled.json-*')), [])

    def test_agent_only_runs_supervisor_and_has_crash_throttle(self):
        value = service.agent_payload()
        self.assertEqual(value['ProgramArguments'][-1], 'run')
        self.assertNotIn('launch.py', ' '.join(value['ProgramArguments']))
        self.assertEqual(value['KeepAlive'], {'SuccessfulExit': False})
        self.assertGreaterEqual(value['ThrottleInterval'], 10)
        self.assertEqual(set(value['EnvironmentVariables']), {'PATH'})
        self.assertIn('/opt/homebrew/bin', value['EnvironmentVariables']['PATH'])

    def test_backoff_is_bounded_and_not_a_tight_restart_loop(self):
        self.assertEqual([service.retry_delay(n) for n in range(1, 8)], [1, 2, 4, 8, 16, 30, 30])

    def test_ten_worker_failures_restart_with_backoff_and_no_request_replay(self):
        count = [0]
        waits = []

        class Stop:
            def is_set(self):
                return count[0] >= 10

            def wait(self, seconds):
                waits.append(seconds)
                return self.is_set()

        class Child:
            def poll(self):
                return 1

            def wait(self, timeout):
                return 1

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(service, 'PRIVATE', Path(folder)),
            patch.object(service, 'enabled', return_value=True),
            patch.object(service, 'read_pid', return_value=None),
            patch.object(service, 'own_process', return_value=False),
            patch.object(service, 'ensure_available'),
        ):
            supervisor = service.Supervisor()
            supervisor.stop = Stop()

            def spawn():
                count[0] += 1
                supervisor.child = Child()
                supervisor.pid = 100 + count[0]

            with patch.object(supervisor, 'start_child', side_effect=spawn) as start:
                supervisor.run()
            self.assertEqual(start.call_count, 10)
            self.assertIn(30, waits)
            self.assertNotIn(0, waits)
            self.assertEqual(
                json.loads((Path(folder) / 'supervisor.json').read_text())['phase'], 'stopped'
            )

    def test_disabled_supervisor_never_spawns_worker(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(service, 'PRIVATE', Path(folder)),
            patch.object(service, 'enabled', return_value=False),
        ):
            supervisor = service.Supervisor()
            with patch.object(supervisor, 'start_child') as spawn:
                supervisor.run()
            spawn.assert_not_called()
            self.assertEqual(
                json.loads((Path(folder) / 'supervisor.json').read_text())['phase'], 'stopped'
            )

    def test_state_write_failure_keeps_healthy_worker_and_recovers_receipt(self):
        for error in (PermissionError('private path'), OSError(errno.ENOSPC, 'private path')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                private = Path(folder)
                status = private / 'supervisor.json'
                replace = os.replace
                health_checks = []
                publications = []
                stopped = []
                with (
                    patch.object(service, 'PRIVATE', private),
                    patch.object(service, 'enabled', return_value=True),
                    patch.object(service, 'read_pid', return_value=123),
                    patch.object(service, 'own_process', return_value=True) as owns,
                ):
                    supervisor = service.Supervisor()
                    supervisor.state('starting', 0)
                    previous = status.read_bytes()
                    supervisor.stop.wait = lambda _seconds: supervisor.stop.is_set()

                    def publish(source, target):
                        if target == status:
                            publications.append(json.loads(Path(source).read_bytes())['phase'])
                            if len(publications) == 1:
                                raise error
                        return replace(source, target)

                    def health(*, operation):
                        self.assertEqual(operation, 'health')
                        health_checks.append(supervisor.pid)
                        if len(health_checks) == 2:
                            self.assertEqual(status.read_bytes(), previous)
                        if len(health_checks) == 3:
                            recovered = json.loads(status.read_bytes())
                            self.assertEqual((recovered['phase'], recovered['pid']), ('ready', 123))
                            supervisor.stop.set()
                        return dict.fromkeys(
                            ('model_ready', 'recognition_worker', 'response_worker'), True
                        )

                    def stop_worker(_pid, _sig):
                        stopped.append(supervisor.stop.is_set())
                        owns.return_value = False

                    with (
                        patch.object(os, 'replace', side_effect=publish),
                        patch.object(service, 'query', side_effect=health),
                        patch.object(supervisor, 'start_child') as start,
                        patch.object(os, 'kill', side_effect=stop_worker) as kill,
                    ):
                        supervisor.run()
                        start.assert_not_called()
                        kill.assert_called_once_with(123, signal.SIGTERM)
                self.assertEqual(health_checks, [123, 123, 123])
                self.assertEqual(stopped, [True])
                self.assertEqual(publications, ['ready', 'ready', 'ready', 'stopped'])
                self.assertEqual(list(private.glob('.supervisor.json-*')), [])
                self.assertEqual(json.loads(status.read_bytes())['phase'], 'stopped')
                log = (private / 'service.log').read_text()
                self.assertIn(
                    'supervisor_state_write_failed error_type=' + type(error).__name__, log
                )
                self.assertNotIn('private path', log)

    def test_stop_during_runtime_preflight_does_not_spawn_worker(self):
        for trigger in ('marker', 'signal'):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as folder:
                private = Path(folder)
                marker = private / 'enabled.json'
                atomic_private_json(marker, {'enabled': True})
                read_enabled = service.enabled
                with (
                    patch.object(service, 'PRIVATE', private),
                    patch.object(service, 'ENABLED', marker),
                    patch.object(service, 'PLIST', private / 'unused.plist'),
                    patch.object(service, 'enabled', side_effect=lambda: read_enabled(marker)),
                    patch.object(service, 'read_pid', return_value=None),
                ):
                    supervisor = service.Supervisor()

                    def stop_during_preflight(_root):
                        if trigger == 'marker':
                            service.disable()
                        else:
                            supervisor.stop.set()

                    with (
                        patch.object(
                            service, 'ensure_available', side_effect=stop_during_preflight
                        ) as preflight,
                        patch.object(supervisor, 'start_child') as spawn,
                    ):
                        supervisor.run()
                    preflight.assert_called_once()
                    spawn.assert_not_called()
                self.assertEqual(
                    json.loads((private / 'supervisor.json').read_text())['phase'], 'stopped'
                )

    def test_other_checkout_blocks_supervisor_without_spawning_or_stopping_it(self):
        from runtime_guard import RuntimeConflict

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(service, 'PRIVATE', Path(folder)),
            patch.object(service, 'enabled', return_value=True),
            patch.object(service, 'read_pid', return_value=None),
            patch.object(service, 'ensure_available', side_effect=RuntimeConflict('other')),
        ):
            supervisor = service.Supervisor()
            supervisor.stop.wait = lambda seconds: True
            with (
                patch.object(supervisor, 'start_child') as spawn,
                patch.object(supervisor, 'state') as state,
            ):
                supervisor.run()
            spawn.assert_not_called()
            self.assertIn(unittest.mock.call('other_runtime', 0), state.call_args_list)

    def test_log_rotation_is_bounded(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(service, 'PRIVATE', Path(folder)),
        ):
            supervisor = service.Supervisor()
            handler = supervisor.log.handlers[0]
            handler.maxBytes = 100
            try:
                for _ in range(20):
                    supervisor.log.info('x' * 80)
                self.assertLessEqual(len(list(Path(folder).glob('service.log*'))), 4)
            finally:
                handler.close()

    def test_clear_stop_is_written_before_launchctl_bootout(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'enabled.json'
            plist = Path(folder) / 'agent.plist'
            plist.touch()

            def run(*args, **kwargs):
                self.assertFalse(json.loads(marker.read_text())['enabled'])
                return subprocess.CompletedProcess(args, 0)

            with (
                patch.object(service, 'PRIVATE', Path(folder)),
                patch.object(service, 'ENABLED', marker),
                patch.object(service, 'PLIST', plist),
                patch.object(service.subprocess, 'run', side_effect=run),
            ):
                service.disable()
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)

    def test_install_rolls_back_failed_registration_without_removing_existing_job(self):
        for fault in ('domain', 'bootstrap', 'bootstrap_timeout', 'kickstart', 'existing'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                private = root / 'private'
                agents = root / 'LaunchAgents'
                agents.mkdir()
                # Give the isolated fixture a known initial mode even when the
                # test suite runs under a private umask.
                agents.chmod(0o755)
                marker, plist = private / 'enabled.json', agents / 'fixture.plist'
                loaded = fault == 'existing'
                previous_marker = b'{ "enabled": false }\n' if loaded else None
                previous_plist = b'previous private plist\n' if loaded else None
                if loaded:
                    atomic_private_json(marker, {'enabled': False})
                    marker.write_bytes(previous_marker)
                    plist.write_bytes(previous_plist)
                    plist.chmod(0o600)
                calls = []
                domain = f'gui/{service.os.getuid()}'
                target = f'{domain}/{service.LABEL}'

                def launchctl(command, *, check=False, **_kwargs):
                    nonlocal loaded
                    verb, destination, *_ = command[1:]
                    calls.append(verb)
                    if verb == 'print':
                        code = int(not loaded) if destination == target else int(fault == 'domain')
                        stdout = f'    path = {plist}\n' if loaded and destination == target else ''
                    elif verb == 'bootstrap':
                        self.assertTrue(service.enabled(marker))
                        if fault == 'bootstrap_timeout':
                            loaded = True
                            raise subprocess.TimeoutExpired(command, 15)
                        code = int(fault in ('domain', 'bootstrap'))
                        loaded = code == 0
                        stdout = ''
                    elif verb == 'kickstart':
                        code, stdout = 1, ''
                    elif verb == 'bootout':
                        self.assertEqual(destination, target)
                        self.assertFalse(marker.exists())
                        self.assertTrue(loaded)
                        loaded = False
                        code, stdout = 0, ''
                    else:
                        raise AssertionError('Unexpected fixture command')
                    if code and check:
                        raise subprocess.CalledProcessError(code, command)
                    return subprocess.CompletedProcess(command, code, stdout)

                with (
                    patch.object(service, 'PRIVATE', private),
                    patch.object(service, 'ENABLED', marker),
                    patch.object(service, 'PLIST', plist),
                    patch.object(service, 'PYTHON', Path(sys.executable)),
                    patch.object(service, 'BRIDGE', Path(__file__)),
                    patch.object(service, 'agent_payload', return_value={'Label': service.LABEL}),
                    patch.object(service, 'ensure_available'),
                    patch.object(service.subprocess, 'run', side_effect=launchctl),
                ):
                    with self.assertRaises((RuntimeError, subprocess.SubprocessError)):
                        service.install()

                self.assertEqual(marker.read_bytes() if marker.exists() else None, previous_marker)
                self.assertEqual(plist.read_bytes() if plist.exists() else None, previous_plist)
                self.assertEqual(loaded, fault == 'existing')
                self.assertEqual(
                    calls.count('bootout'), int(fault in ('kickstart', 'bootstrap_timeout'))
                )
                if fault == 'domain':
                    self.assertEqual(calls, ['print', 'print'])
                self.assertEqual(agents.stat().st_mode & 0o777, 0o755)

    def test_disable_does_not_overwrite_marker_during_registration(self):
        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder)
            marker = private / 'enabled.json'
            atomic_private_json(marker, {'enabled': True})
            with (
                patch.object(service, 'PRIVATE', private),
                patch.object(service, 'ENABLED', marker),
                patch.object(service, 'PLIST', private / 'unused.plist'),
                ProcessLease(private / 'service-change.lock'),
            ):
                with self.assertRaises(BlockingIOError):
                    service.disable()
            self.assertTrue(service.enabled(marker))


if __name__ == '__main__':
    unittest.main()

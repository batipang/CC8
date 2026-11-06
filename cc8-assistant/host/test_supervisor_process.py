"""Exercise the real supervisor with isolated synthetic worker processes.

Wait gates are accelerated; the unhealthy case advances its startup budget. No launchctl, models, global runtime lease,
real backend, device UI or capture is used; fixture PIDs are path-verified.
"""

import json
import errno
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import diagnose
import launch
import service
from process_lock import ProcessLease, atomic_private_bytes, atomic_private_json

HOST = Path(__file__).resolve().parent
DUMMY = """
import json, os, socket
from pathlib import Path
from process_lock import ProcessLease, atomic_private_json
root = Path(__file__).resolve().parent
private = root / 'p'
with (private / 'pids').open('a') as journal:
    journal.write(str(os.getpid()) + '\\n')
try:
    lease = ProcessLease(private / 'worker.lock')
    lease.__enter__()
except BlockingIOError:
    (private / 'overlap').write_text('overlap')
    raise SystemExit(99)
(private / 'bridge.pid').write_text(str(os.getpid()))
path = private / 'announce.sock'
path.unlink(missing_ok=True)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(str(path))
path.chmod(0o600)
server.listen(1)
mode = (private / 'mode').read_text()
print('synthetic backend only', flush=True)
index = 0
while True:
    with server.accept()[0] as peer:
        peer.settimeout(1)
        data = bytearray()
        while b'\\n' not in data and len(data) <= 4096:
            block = peer.recv(4096)
            if not block:
                raise SystemExit(98)
            data.extend(block)
        if json.loads(data)['operation'] != 'health':
            raise SystemExit(97)
        peer.sendall((json.dumps({'ok': True, 'model_ready': mode != 'unhealthy', 'recognition_worker': True, 'response_worker': True}) + '\\n').encode())
    if mode == 'disable':
        atomic_private_json(private / 'enabled.json', {'enabled': False})
        while True:
            import time
            time.sleep(1)
    index += 1
    if mode == 'crash' and index >= 2:
        os._exit(7)
"""


def fixture_owned(pid, script):
    if type(pid) is not int or pid <= 0:
        return False
    result = subprocess.run(
        ['ps', '-p', str(pid), '-o', 'uid=', '-o', 'command='],
        capture_output=True,
        text=True,
        timeout=2,
    )
    try:
        owner, command = result.stdout.strip().split(None, 1)
        args = shlex.split(command)
        return (
            result.returncode == 0 and int(owner) == os.getuid() and args[1:] == ['-u', str(script)]
        )
    except ValueError:
        return False


def cleanup(root):
    try:
        pids = (root / 'p/pids').read_text().splitlines()
    except OSError:
        return
    for raw in pids[:20]:
        try:
            pid = int(raw)
        except ValueError:
            continue
        if fixture_owned(pid, root / 'bridge.py'):
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and fixture_owned(pid, root / 'bridge.py'):
                time.sleep(0.02)
            if fixture_owned(pid, root / 'bridge.py'):
                os.kill(pid, signal.SIGKILL)


def exercise(root):
    private = root / 'p'
    marker = private / 'enabled.json'
    original_enabled = service.enabled
    mode = (private / 'mode').read_text()
    clock_offset = [0]
    stop = threading.Event()
    backoffs, phases, spawned = [], [], []
    pending_backoff = [False]
    deadline = time.monotonic() + 10

    class Gate:
        def is_set(self):
            return stop.is_set()

        def wait(self, seconds):
            if time.monotonic() > deadline:
                raise TimeoutError('Synthetic supervisor deadline')
            if pending_backoff[0]:
                pending_backoff[0] = False
                backoffs.append(seconds)
                if len(backoffs) == 10:
                    stop.set()
            return stop.wait(min(seconds, 0.02))

    def health(operation):
        result = diagnose.query(private / 'announce.sock', operation, timeout=0.5)
        if result.get('model_ready') is False:
            clock_offset[0] = 200
        return result

    with (
        patch.object(service, 'ROOT', root),
        patch.object(service, 'PRIVATE', private),
        patch.object(service, 'BRIDGE', root / 'bridge.py'),
        patch.object(service, 'PYTHON', Path(sys.executable)),
        patch.object(launch, 'BRIDGE', root / 'bridge.py'),
        patch.object(service, 'enabled', side_effect=lambda: original_enabled(marker)),
        # Global ownership is tested elsewhere; a fake worker must never use or
        # contend with the real assistant's model/runtime lease.
        patch.object(service, 'ensure_available'),
        patch.object(service, 'query', side_effect=health),
        patch.object(
            service,
            'time',
            SimpleNamespace(
                monotonic=lambda: time.monotonic() + clock_offset[0],
                time=time.time,
                sleep=time.sleep,
            ),
        ),
    ):
        supervisor = service.Supervisor()
        supervisor.stop = Gate()
        original_start, original_state = supervisor.start_child, supervisor.state

        def start():
            if any(fixture_owned(pid, root / 'bridge.py') for pid in spawned):
                raise AssertionError('Previous synthetic worker still alive')
            with patch.dict(
                os.environ,
                {'PATH': '/usr/bin:/bin', 'HOME': str(root), 'PYTHONPATH': str(HOST)},
                clear=True,
            ):
                original_start()
            spawned.append(supervisor.pid)

        def state(phase, failures):
            original_state(phase, failures)
            phases.append(phase)
            pending_backoff[0] = phase == 'backoff'
            if mode == 'unhealthy' and phase == 'backoff':
                (private / 'mode').write_text('disable')

        supervisor.start_child = start
        supervisor.state = state
        supervisor.run()
        alive = sum(fixture_owned(pid, root / 'bridge.py') for pid in spawned)
    print(
        json.dumps(
            {
                'spawned': len(spawned),
                'backoffs': backoffs,
                'ready': phases.count('ready'),
                'unhealthy': phases.count('unhealthy'),
                'final': phases[-1],
                'alive': alive,
                'overlap': (private / 'overlap').exists(),
                'enabled': original_enabled(marker),
            }
        )
    )


class SupervisorProcessTests(unittest.TestCase):
    def test_pid_publication_failure_reaps_worker_before_supervisor_retries(self):
        with tempfile.TemporaryDirectory(prefix='cc8-pid-write-', dir='/tmp') as folder:
            root = Path(folder)
            private = root / 'p'
            private.mkdir(mode=0o700)
            script = root / 'bridge.py'
            script.write_text('import time\ntime.sleep(30)\n')
            pids, delays = [], []
            stop = threading.Event()
            deadline = time.monotonic() + 5

            def publish(path, value, **kwargs):
                self.assertEqual(path, private / 'bridge.pid')
                with self.assertRaises(BlockingIOError), ProcessLease(private / 'launcher.lock'):
                    self.fail('PID write must hold the launcher lease')
                pids.append(int(value))
                if len(pids) == 1:
                    raise OSError(errno.ENOSPC, 'PRIVATE_PATH')
                with self.assertRaises(ProcessLookupError):
                    os.kill(pids[0], 0)
                return atomic_private_bytes(path, value, **kwargs)

            class Gate:
                def is_set(self):
                    return stop.is_set()

                def wait(self, seconds):
                    if time.monotonic() > deadline:
                        raise TimeoutError('PID write recovery test deadline')
                    if seconds == 1:
                        delays.append(seconds)
                    return stop.wait(min(seconds, 0.02))

            def ready(*, operation):
                self.assertEqual(operation, 'health')
                self.assertEqual(len(pids), 2)
                self.assertEqual(int((private / 'bridge.pid').read_text()), pids[-1])
                stop.set()
                return dict(model_ready=True, recognition_worker=True, response_worker=True)

            with (
                patch.object(service, 'ROOT', root),
                patch.object(service, 'PRIVATE', private),
                patch.object(service, 'BRIDGE', script),
                patch.object(service, 'PYTHON', Path(sys.executable)),
                patch.object(launch, 'BRIDGE', script),
                patch.object(service, 'enabled', return_value=True),
                patch.object(service, 'ensure_available'),
                patch.object(service, 'atomic_private_bytes', side_effect=publish),
                patch.object(launch, 'atomic_private_bytes', side_effect=publish),
                patch.object(service, 'query', side_effect=ready),
            ):
                supervisor = service.Supervisor()
                supervisor.stop = Gate()
                error = None
                try:
                    supervisor.run()
                except OSError as failure:
                    error = type(failure).__name__
                self.assertIsNone(error, 'PID write failure escaped the supervisor')
            self.assertEqual(delays, [1])
            self.assertEqual(len(pids), 2)
            self.assertNotIn('PRIVATE_PATH', (private / 'service.log').read_text())
            for pid in pids:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_manual_pid_publication_failure_reaps_worker_before_releasing_lease(self):
        with tempfile.TemporaryDirectory(prefix='cc8-manual-pid-', dir='/tmp') as folder:
            root = Path(folder)
            private = root / 'p'
            private.mkdir(mode=0o700)
            script = root / 'bridge.py'
            script.write_text('import time\ntime.sleep(30)\n')
            child = None
            original_spawn = subprocess.Popen

            def spawn(*args, **kwargs):
                nonlocal child
                child = original_spawn(*args, **kwargs)
                return child

            try:
                with (
                    patch.object(launch, 'ROOT', root),
                    patch.object(launch, 'PRIVATE', private),
                    patch.object(launch, 'BRIDGE', script),
                    patch.object(launch, 'PYTHON', Path(sys.executable)),
                    patch.object(launch, 'ensure_available'),
                    patch.object(launch, 'load_config'),
                    patch.object(launch.subprocess, 'Popen', side_effect=spawn),
                    patch.object(
                        launch, 'atomic_private_bytes', side_effect=PermissionError('PRIVATE_PATH')
                    ),
                    patch.object(sys, 'argv', ['launch.py', '--no-open']),
                ):
                    error = None
                    try:
                        launch.main()
                    except (SystemExit, OSError) as failure:
                        error = failure
                    self.assertIsNotNone(child)
                    self.assertIsNotNone(child.poll(), 'unpublished worker survived the launcher')
                    self.assertIsInstance(error, SystemExit)
                    self.assertNotIn('PRIVATE_PATH', str(error))
            finally:
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
            with ProcessLease(private / 'launcher.lock'):
                self.assertFalse((private / 'bridge.pid').exists())

    def test_manual_open_reuses_supervisor_pid_while_real_worker_is_still_loading(self):
        with tempfile.TemporaryDirectory(prefix='cc8 load ', dir='/tmp') as folder:
            root = Path(folder)
            private = root / 'p'
            private.mkdir(mode=0o700)
            script = root / 'bridge.py'
            script.write_text('import time\ntime.sleep(30)\n')
            with (
                patch.object(service, 'ROOT', root),
                patch.object(service, 'PRIVATE', private),
                patch.object(service, 'BRIDGE', script),
                patch.object(service, 'PYTHON', Path(sys.executable)),
                patch.object(launch, 'ROOT', root),
                patch.object(launch, 'PRIVATE', private),
                patch.object(launch, 'BRIDGE', script),
                patch.object(launch, 'PYTHON', Path(sys.executable)),
                patch.object(launch, 'ensure_available') as preflight,
                patch.object(sys, 'argv', ['launch.py', '--no-open']),
            ):
                supervisor = service.Supervisor()
                try:
                    with ProcessLease(private / 'launcher.lock'):
                        supervisor.start_child()
                    pidfile = private / 'bridge.pid'
                    self.assertTrue(
                        pidfile.is_file(), 'supervisor did not publish the starting PID'
                    )
                    self.assertEqual(int(pidfile.read_text()), supervisor.child.pid)
                    self.assertEqual(pidfile.stat().st_mode & 0o777, 0o600)
                    launch.main()
                    preflight.assert_not_called()
                    self.assertIsNone(supervisor.child.poll())
                finally:
                    supervisor.terminate()
                    for handler in supervisor.log.handlers:
                        handler.close()

    def test_supervisor_waits_for_manual_launch_lease_and_adopts_published_worker(self):
        with tempfile.TemporaryDirectory(prefix='cc8-launch-race-', dir='/tmp') as folder:
            root = Path(folder)
            private = root / 'p'
            private.mkdir(mode=0o700)
            script = root / 'bridge.py'
            script.write_text('import time\ntime.sleep(30)\n')
            waiting, adopted, stop = threading.Event(), threading.Event(), threading.Event()
            errors = []
            child = None
            original_read = service.read_pid

            class Gate:
                def is_set(self):
                    return stop.is_set()

                def wait(self, seconds):
                    if seconds == 0.5:
                        waiting.set()
                    return stop.wait(min(0.02, seconds))

            def read_pid():
                pid = original_read()
                if child is not None and pid == child.pid:
                    adopted.set()
                return pid

            with (
                patch.object(service, 'PRIVATE', private),
                patch.object(service, 'enabled', return_value=True),
                patch.object(service, 'read_pid', side_effect=read_pid),
                patch.object(service, 'ensure_available') as preflight,
                patch.object(launch, 'BRIDGE', script),
            ):
                supervisor = service.Supervisor()
                supervisor.stop = Gate()

                def run():
                    try:
                        supervisor.run()
                    except BaseException as error:
                        errors.append(type(error).__name__)

                thread = threading.Thread(target=run, daemon=True)
                with patch.object(supervisor, 'start_child') as spawn:
                    try:
                        with ProcessLease(private / 'launcher.lock'):
                            thread.start()
                            self.assertTrue(waiting.wait(2))
                            spawn.assert_not_called()
                            child = subprocess.Popen([sys.executable, '-u', str(script)])
                            atomic_private_bytes(private / 'bridge.pid', f'{child.pid}\n'.encode())
                        self.assertTrue(adopted.wait(2))
                    finally:
                        stop.set()
                        thread.join(3)
                        if child is not None:
                            if child.poll() is None:
                                child.terminate()
                            child.wait(timeout=3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    preflight.assert_not_called()
                    spawn.assert_not_called()

    def check_mode(self, mode):
        with tempfile.TemporaryDirectory(prefix='cc8-sv-', dir='/tmp') as folder:
            root = Path(folder)
            private = root / 'p'
            private.mkdir(mode=0o700)
            atomic_private_json(private / 'enabled.json', {'enabled': True})
            (private / 'mode').write_text(mode)
            (root / 'bridge.py').write_text(textwrap.dedent(DUMMY))
            try:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), '--worker', str(root)],
                    cwd='/',
                    env={'PATH': '/usr/bin:/bin', 'HOME': folder, 'PYTHONPATH': str(HOST)},
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                report = json.loads(result.stdout)
                self.assertEqual(report['alive'], 0)
                self.assertFalse(report['overlap'])
                self.assertEqual(report['final'], 'stopped')
                return report
            finally:
                cleanup(root)

    def test_ten_real_child_crashes_recover_serially_with_bounded_backoff(self):
        report = self.check_mode('crash')
        self.assertEqual(report['spawned'], 10)
        self.assertEqual(report['backoffs'], [1, 2, 4, 8, 16, 30, 30, 30, 30, 30])
        self.assertGreaterEqual(report['ready'], 10)

    def test_unhealthy_live_child_is_replaced_before_a_healthy_disabled_stop(self):
        report = self.check_mode('unhealthy')
        self.assertEqual(report['spawned'], 2)
        self.assertEqual(report['backoffs'], [1])
        self.assertGreaterEqual(report['unhealthy'], 2)
        self.assertGreaterEqual(report['ready'], 1)
        self.assertFalse(report['enabled'])

    def test_disable_marker_terminates_real_child_without_restart(self):
        report = self.check_mode('disable')
        self.assertEqual(report['spawned'], 1)
        self.assertEqual(report['backoffs'], [])
        self.assertFalse(report['enabled'])


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        exercise(Path(sys.argv[2]))
    else:
        unittest.main()

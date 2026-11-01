"""Resource and observer tests use only the owner process and isolated health IPC."""

import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from observation_run import ObservationRun, health_sample
from observe import run, validate
from runtime_resources import ResourceSampler

ROOT = Path(__file__).resolve().parents[2]
SECRET = 'PRIVATE-provider-body-or-device-id'
INSTANCE = 'f' * 32


def health(rss=100, *, instance=INSTANCE, uptime=10, cpu=1):
    return {
        'ok': True,
        'model_ready': True,
        'connected': False,
        'transport': 'none',
        'microphone_active': False,
        'playing': False,
        'calibrating': False,
        'recognition_worker': True,
        'response_worker': True,
        'uptime_seconds': uptime,
        'mic_frames': 0,
        'render_frames': 0,
        'waiting_for_wake': False,
        'runtime_instance': instance,
        'resources': {
            'rss_bytes': rss,
            'peak_rss_bytes': 1000,
            'python_threads': 3,
            'open_fds': 7,
            'cpu_seconds': cpu,
        },
        'provider_body': SECRET,
    }


class ResourceSamplerTests(unittest.TestCase):
    def test_units_cache_copy_and_minimal_ps_environment(self):
        for system, factor in [('Darwin', 1), ('Linux', 1024)]:
            now = [0.0]
            sampler = ResourceSampler(clock=lambda: now[0])
            usage = SimpleNamespace(ru_maxrss=1234, ru_utime=1.0, ru_stime=0.5)
            with (
                patch('runtime_resources.platform.system', return_value=system),
                patch('runtime_resources.resource.getrusage', return_value=usage),
                patch('runtime_resources.subprocess.check_output', return_value='10\n') as ps,
            ):
                result = sampler.sample()
                self.assertEqual(result['rss_bytes'], 10240)
                self.assertEqual(result['peak_rss_bytes'], 1234 * factor)
                self.assertEqual(result['cpu_seconds'], 1.5)
                result['rss_bytes'] = 999
                self.assertEqual(sampler.sample()['rss_bytes'], 10240)
                self.assertEqual(ps.call_count, 1)
                now[0] = 1.1
                sampler.sample()
                self.assertEqual(ps.call_count, 2)
                self.assertEqual(
                    ps.call_args.args[0], ['/bin/ps', '-p', str(os.getpid()), '-o', 'rss=']
                )
                self.assertEqual(set(ps.call_args.kwargs['env']), {'PATH', 'LC_ALL'})
                self.assertLessEqual(ps.call_args.kwargs['timeout'], 0.5)

    def test_missing_or_malformed_gauges_are_null_not_fabricated_zero(self):
        for output in ['nan', '-1', '１２', '1 2', SECRET, '9' * 30]:
            with (
                patch('runtime_resources.subprocess.check_output', return_value=output),
                patch('runtime_resources.resource.getrusage', side_effect=OSError),
                patch('runtime_resources.os.scandir', side_effect=OSError),
            ):
                result = ResourceSampler().sample()
                self.assertIsNone(result['rss_bytes'])
                self.assertIsNone(result['peak_rss_bytes'])
                self.assertIsNone(result['cpu_seconds'])
                self.assertIsNone(result['open_fds'])
                self.assertGreaterEqual(result['python_threads'], 1)
        with patch(
            'runtime_resources.subprocess.check_output',
            side_effect=subprocess.TimeoutExpired('fixture', 0.5),
        ):
            self.assertIsNone(ResourceSampler().sample()['rss_bytes'])

    def test_actual_owner_gauges_are_finite_without_model_or_device_access(self):
        result = ResourceSampler().sample()
        self.assertIsInstance(result['rss_bytes'], int)
        self.assertGreater(result['rss_bytes'], 0)
        self.assertGreater(result['python_threads'], 0)
        self.assertNotIn('pid', result)
        self.assertNotIn('files', result)


class ObservationTests(unittest.TestCase):
    def args(self, path, **overrides):
        values = dict(seconds=0.3, interval=0.1, output=path, socket=path.parent / 'fixture.sock')
        values.update(overrides)
        return argparse.Namespace(**values)

    def execute(self, args, query):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        with redirect_stdout(io.StringIO()) as output:
            code = run(
                args,
                query_fn=query,
                clock=lambda: now[0],
                wall_clock=lambda: 1000 + now[0],
                sleep=sleep,
            )
        return code, output.getvalue()

    def test_whitelist_privacy_copy_and_runtime_segments_avoid_cross_restart_slope(self):
        book = ObservationRun(60)
        for index, (rss, instance, cpu) in enumerate(
            [(100, INSTANCE, 1), (200, INSTANCE, 3), (900, 'e' * 32, 0), (800, 'e' * 32, 2)]
        ):
            book.add(health(rss, instance=instance, cpu=cpu), index * 60, index * 60)
        report = book.report()
        self.assertEqual(report['observed_runtime_segments'], 2)
        self.assertEqual(report['segments'][0]['gauges']['rss_bytes']['delta'], 100)
        self.assertEqual(report['segments'][0]['gauges']['rss_bytes']['slope_per_hour'], 6000)
        self.assertEqual(report['segments'][1]['gauges']['rss_bytes']['delta'], -100)
        self.assertIsNone(report['samples'][2]['cpu_percent_of_one_core'])
        self.assertAlmostEqual(report['samples'][1]['cpu_percent_of_one_core'], 3.333, places=3)
        encoded = json.dumps(report)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn(INSTANCE, encoded)
        self.assertFalse(report['long_run_acceptance_asserted'])
        report['samples'].clear()
        self.assertEqual(len(book.report()['samples']), 4)

    def test_legacy_backend_missing_resources_is_explicitly_incomplete(self):
        payload = health()
        payload.pop('runtime_instance')
        payload.pop('resources')
        payload.pop('waiting_for_wake')
        book = ObservationRun(1)
        book.add(payload, 0, 0)
        book.add(payload, 1, 1)
        report = book.report()
        self.assertEqual(report['resource_coverage_incomplete_polls'], 2)
        self.assertEqual(report['observed_runtime_segments'], 0)
        self.assertIsNone(report['samples'][1]['cpu_percent_of_one_core'])

    def test_gaps_clock_and_uptime_discontinuity_are_not_claimed_continuous(self):
        book = ObservationRun(1)
        book.add(health(uptime=10), 0, 0)
        book.add(health(uptime=11), 10, 100)
        book.add(health(uptime=1), 11, 101)
        report = book.report()
        self.assertEqual(report['poll_gaps'], 1)
        self.assertEqual(report['wall_clock_discontinuities'], 1)
        self.assertEqual(report['uptime_discontinuities'], 1)
        self.assertEqual(report['observed_runtime_segments'], 2)
        with self.assertRaises(ValueError):
            book.add(health(), 9, 102)
        self.assertEqual(len(book.rows), 3)

    def test_invalid_snapshot_and_capacity_fail_before_record_mutation(self):
        for key, value in [
            ('uptime_seconds', float('nan')),
            ('runtime_instance', SECRET),
            ('connected', 1),
            ('mic_frames', True),
            ('transport', SECRET),
        ]:
            payload = health()
            payload[key] = value
            book = ObservationRun(1)
            with self.assertRaises(ValueError):
                book.add(payload, 0, 0)
            self.assertEqual(book.rows, [])
        payload = health()
        payload['resources']['rss_bytes'] = True
        with self.assertRaises(ValueError):
            health_sample(payload)
        book = ObservationRun(1, limit=1)
        book.add(health(), 0, 0)
        with self.assertRaises(ValueError):
            book.add(health(), 1, 1)
        self.assertEqual(len(book.rows), 1)

    def test_bounded_run_only_queries_health_and_writes_private_curves(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'observe.json'
            calls = []

            def query(target, *, operation, timeout):
                calls.append(operation)
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 5)
                return health()

            code, printed = self.execute(self.args(path), query)
            self.assertEqual(code, 0)
            result = json.loads(path.read_text())
            self.assertEqual(result['observation']['state'], 'completed')
            self.assertGreaterEqual(result['polls'], 2)
            self.assertEqual(set(calls), {'health'})
            self.assertFalse(result['device_capture_started'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(SECRET, path.read_text() + printed)
            self.assertEqual(printed.strip(), str(path))

    def test_outage_invalid_and_unhealthy_samples_survive_as_partial_evidence(self):
        for mode in ('outage', 'invalid', 'unhealthy', 'legacy'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'partial.json'
                count = [0]

                def query(*args, **kwargs):
                    count[0] += 1
                    payload = health()
                    if count[0] == 1:
                        if mode == 'outage':
                            raise OSError(SECRET)
                        if mode == 'invalid':
                            return {'ok': True, 'body': SECRET}
                        if mode == 'unhealthy':
                            payload['recognition_worker'] = False
                        if mode == 'legacy':
                            payload.pop('resources')
                    return payload

                code, _ = self.execute(self.args(path), query)
                self.assertEqual(code, 2)
                result = json.loads(path.read_text())
                self.assertEqual(result['observation']['state'], 'partial')
                self.assertGreaterEqual(count[0], 2)
                self.assertNotIn(SECRET, path.read_text())

    def test_cancel_and_invalid_options_do_not_forge_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cancel.json'
            code, _ = self.execute(
                self.args(path), lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
            )
            self.assertEqual(code, 130)
            self.assertEqual(json.loads(path.read_text())['observation']['state'], 'cancelled')
            for seconds, interval in [
                (True, 1),
                (0, 1),
                (259201, 60),
                (259200, 1),
                (float('nan'), 1),
                (1, 0),
                (1, 301),
            ]:
                with self.assertRaises(ValueError):
                    validate(seconds, interval)
            validate(72 * 3600, 60)

    def test_real_root_cli_uses_only_owner_health_ipc_from_minimal_path(self):
        with tempfile.TemporaryDirectory(prefix='cc8-ob-') as folder:
            root = Path(folder)
            address = root / 'health.sock'
            output = root / 'observe.json'
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(address))
            address.chmod(0o600)
            server.listen()
            server.settimeout(0.1)
            stopped = threading.Event()
            operations = []
            errors = []

            def serve():
                try:
                    while not stopped.is_set():
                        try:
                            connection, _ = server.accept()
                        except socket.timeout:
                            continue
                        with connection:
                            connection.settimeout(1)
                            request = json.loads(connection.recv(4096))
                            operations.append(request)
                            connection.sendall((json.dumps(health()) + '\n').encode())
                except Exception as error:
                    errors.append(type(error).__name__)

            thread = threading.Thread(target=serve)
            thread.start()
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / 'cc8'),
                        'observe',
                        '--seconds',
                        '0.15',
                        '--interval',
                        '0.05',
                        '--socket',
                        str(address),
                        '--output',
                        str(output),
                    ],
                    cwd='/',
                    env={'PATH': '/usr/bin:/bin'},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(operations)
                self.assertTrue(all(request == {'operation': 'health'} for request in operations))
                self.assertNotIn(SECRET, output.read_text() + result.stdout + result.stderr)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            finally:
                stopped.set()
                thread.join(2)
                server.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()

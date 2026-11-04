"""Read-only measurement runs and private output, using synthetic receipts only."""

import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from diagnostics import TraceBook
from diagnostic_run import DiagnosticRun
from measure import audit_main, run
from private_output import PrivateOutput

ROOT = Path(__file__).resolve().parents[2]


class PrivateOutputTests(unittest.TestCase):
    def test_atomic_private_updates_reject_existing_output_and_external_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.json'
            with PrivateOutput(path) as output:
                output.write({'version': 1})
                output.write({'version': 2})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(path.read_text()), {'version': 2})
                with self.assertRaises(ValueError):
                    output.write({'bad': float('nan')})
                self.assertEqual(json.loads(path.read_text()), {'version': 2})
                self.assertEqual(list(Path(folder).iterdir()), [path])
                with self.assertRaises(FileExistsError), PrivateOutput(path):
                    pass
                path.unlink()
                target = Path(folder) / 'untouched.json'
                target.write_text('untouched')
                path.symlink_to(target)
                with self.assertRaises(OSError):
                    output.write({'bad': 1})
                self.assertEqual(target.read_text(), 'untouched')

    def test_nonprivate_or_symlink_directory_is_rejected_without_chmod(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public = root / 'public'
            public.mkdir(mode=0o755)
            public.chmod(0o755)
            with self.assertRaises(OSError), PrivateOutput(public / 'run.json'):
                pass
            self.assertEqual(public.stat().st_mode & 0o777, 0o755)
            link = root / 'link'
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError), PrivateOutput(link / 'run.json'):
                pass
            self.assertFalse((root / 'run.json').exists())


class MeasurementTests(unittest.TestCase):
    def test_confirmed_voice_latency_keeps_failure_and_missing_evidence_visible(self):
        book = DiagnosticRun()
        before = TraceBook().report()
        book.ingest(before)
        stages = {
            'utterance_end': 100,
            'brain_start': 150,
            'playout_ack': 350,
            'playback_done': 650,
        }
        confirmed = dict(stages, confirmed_playout_ack=375, confirmed_playback_done=700)
        variants = [
            {},
            {'stages': {k: v for k, v in confirmed.items() if k != 'utterance_end'}},
            {'stages': dict(confirmed, utterance_end=400)},
            {'stages': stages},
            {'outcome': 'cancelled'},
            {'outcome': 'failed', 'error': {'stage': 'playback', 'kind': 'timeout'}},
            {'outcome': 'rejected', 'gate': 'wake_closed', 'generation': None},
            {'outcome': None},
            {'confirmed_playback_clauses': None, 'stages': stages},
            {'task_outcome': None},
            {'origin': 'text', 'gate': None},
            {'scenario': 'web', 'stages': dict(confirmed, confirmed_playout_ack=600)},
        ]
        rows = []
        for index, changes in enumerate(variants, start=1):
            row = {
                'request_id': f'{index:032x}',
                'origin': 'voice',
                'scenario': 'qa',
                'outcome': 'completed',
                'gate': 'accepted',
                'generation': index,
                'task_outcome': 'completed',
                'error': None,
                'confirmed_playback_clauses': 1,
                'stages': confirmed,
            }
            row.update(changes)
            for field in ('gate', 'task_outcome', 'confirmed_playback_clauses'):
                if row[field] is None:
                    row.pop(field)
            rows.append(row)
        book.ingest(dict(before, requests=rows))
        report = book.report()
        coverage = report['voice_playout_coverage']
        qa = coverage['qa']
        self.assertEqual(qa['observed_voice_requests'], 10)
        self.assertEqual(qa['terminal_for_rate'], 8)
        self.assertEqual(qa['confirmed_response_rate'], 4 / 8)
        self.assertEqual(
            qa['outcomes'],
            {
                'completed_with_confirmed_playout': 4,
                'cancelled': 1,
                'failed': 1,
                'rejected': 1,
                'pending': 1,
                'completed_without_playout_confirmation': 1,
                'completed_without_response_receipt': 1,
            },
        )
        self.assertEqual(qa['confirmed_without_valid_timing'], 3)
        self.assertEqual(qa['utterance_to_first_playout_ack_ms'], dict(samples=1, p50=275, p95=275))
        self.assertEqual(coverage['web']['utterance_to_first_playout_ack_ms']['p95'], 500)
        self.assertIsNone(coverage['tool']['confirmed_response_rate'])
        self.assertGreater(
            report['summary']['latencies']['utterance_to_playout_ack_ms']['samples'], 1
        )

        # A historical file may lack the field or contain a stale aggregate.
        report['voice_playout_coverage'] = {'incorrect': True}
        report['capture'] = dict(state='partial', query_failures=2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prior.json'
            with PrivateOutput(path) as output:
                output.write(report)
            original = path.read_bytes()
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(audit_main([str(path)]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result['voice_playout_coverage'], coverage)
            self.assertEqual(result['capture_state'], 'partial')
            self.assertEqual(result['query_failures'], 2)
            self.assertFalse(result['playout_ack_is_audible_onset'])
            self.assertFalse(result['full_p0_acceptance'])
            self.assertNotIn(rows[0]['request_id'], output.getvalue())
            self.assertEqual(path.read_bytes(), original)

    def test_saved_audit_refuses_duplicate_fields_and_untrusted_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.json'
            path.write_text('{"schema":1,"schema":1}')
            path.chmod(0o600)
            link = path.with_name('link.json')
            link.symlink_to(path)
            for report in (path, link):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                    audit_main([str(report)])
                self.assertEqual(stopped.exception.code, 2)

    def args(self, path):
        return argparse.Namespace(
            seconds=0.3,
            interval=0.1,
            output=path,
            socket=path.parent / 'unused.sock',
            condition='quiet',
            distance='1m',
            partition='acceptance',
        )

    def execute(self, args, query):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            result = run(args, query_fn=query, clock=lambda: now[0], sleep=sleep)
        return result, output.getvalue()

    def test_updates_merge_and_output_is_content_free_without_starting_devices(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.json'
            book = TraceBook()
            count = [0]

            def query(target, *, operation, timeout):
                self.assertEqual(operation, 'diagnostics')
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 5)
                count[0] += 1
                if count[0] == 2:
                    ident = book.begin('text', 101)
                    book.finish(ident, 'completed')
                result = book.report()
                result['provider_body'] = 'SECRET'
                return result

            code, printed = self.execute(self.args(path), query)
            self.assertEqual(code, 0)
            result = json.loads(path.read_text())
            self.assertEqual(result['summary']['samples'], 1)
            self.assertEqual(result['summary']['outcomes'], {'completed': 1})
            self.assertEqual(result['capture']['state'], 'completed')
            self.assertFalse(result['capture']['device_capture_started'])
            self.assertNotIn('SECRET', path.read_text() + printed)
            self.assertEqual(printed.strip(), str(path))

    def test_unavailable_or_invalid_backend_stops_with_partial_private_report(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'run.json'
                args = self.args(path)
                args.seconds = 1

                def query(*a, **k):
                    if invalid:
                        return {'contains_content': True, 'secret': 'SECRET'}
                    raise OSError('SECRET')

                code, printed = self.execute(args, query)
                result = json.loads(path.read_text())
                self.assertEqual(code, 2)
                self.assertEqual(
                    result['capture']['state'], 'invalid_snapshot' if invalid else 'ipc_unavailable'
                )
                self.assertEqual(result['summary']['samples'], 0)
                self.assertNotIn('SECRET', path.read_text() + printed)

    def test_changed_backend_instance_or_version_preserves_only_the_original_run(self):
        original = {'bridge_version': '0.14.40', 'runtime_instance': 'a' * 32}
        for replacement in (
            {**original, 'runtime_instance': 'b' * 32},
            {**original, 'bridge_version': '0.14.41'},
            {},
        ):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'run.json'
                book = TraceBook()
                first = {'health': original, **book.report()}
                ident = book.begin('text', 101)
                book.finish(ident, 'completed')
                second = {'health': original, **book.report()}
                other = TraceBook()
                other.finish(other.begin('text', 102), 'completed')
                snapshots = iter((first, second, {'health': replacement, **other.report()}))
                code, _ = self.execute(self.args(path), lambda *a, **k: next(snapshots))

                result = json.loads(path.read_text())
                self.assertEqual(code, 2)
                self.assertEqual(result['capture']['state'], 'runtime_changed')
                self.assertEqual(result['capture']['backend_version'], '0.14.40')
                self.assertTrue(result['capture']['runtime_bound'])
                self.assertEqual(result['coverage']['polls'], 2)
                self.assertEqual([row['request_id'] for row in result['requests']], [ident])
                self.assertNotIn(original['runtime_instance'], path.read_text())

    def test_expected_version_requires_verifiable_source_and_rejects_wrong_version(self):
        for health, expected_state in (
            (
                {'bridge_version': '0.14.39', 'runtime_instance': 'a' * 32},
                'backend_version_mismatch',
            ),
            ({'bridge_version': '0.14.40'}, 'runtime_unconfirmed'),
            ({'bridge_version': '0.14.40', 'runtime_instance': 'SECRET'}, 'invalid_snapshot'),
        ):
            with self.subTest(state=expected_state), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'run.json'
                args = self.args(path)
                args.expect_backend = '0.14.40'
                payload = {'health': health, **TraceBook().report()}
                code, _ = self.execute(args, lambda *a, **k: payload)
                result = json.loads(path.read_text())
                self.assertEqual(code, 2)
                self.assertEqual(result['capture']['state'], expected_state)
                self.assertEqual(result['coverage']['polls'], 0)
                self.assertNotIn('SECRET', path.read_text())

    def test_interrupt_saves_completed_receipts_and_returns_cancelled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.json'
            calls = [0]
            book = TraceBook()

            def query(*a, **k):
                calls[0] += 1
                if calls[0] == 3:
                    raise KeyboardInterrupt()
                if calls[0] == 2:
                    ident = book.begin('text', 101)
                    book.finish(ident, 'completed')
                return book.report()

            code, _ = self.execute(self.args(path), query)
            result = json.loads(path.read_text())
            self.assertEqual(code, 130)
            self.assertEqual(result['capture']['state'], 'cancelled')
            self.assertEqual(result['summary']['samples'], 1)
            self.assertEqual(result['summary']['outcomes'], {'completed': 1})

    def test_a_query_gap_is_not_reported_as_clean_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.json'
            calls = [0]

            def query(*a, **k):
                calls[0] += 1
                if calls[0] == 2:
                    raise OSError('synthetic gap')
                return TraceBook().report()

            code, _ = self.execute(self.args(path), query)
            result = json.loads(path.read_text())
            self.assertEqual(code, 2)
            self.assertEqual(result['capture']['state'], 'partial')
            self.assertEqual(result['capture']['query_failures'], 1)

    def test_bad_arguments_cannot_create_files_or_read_socket(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.json'
            for value in (float('nan'), 0, True, 10**1000):
                args = self.args(path)
                args.seconds = value
                with patch('measure.default_output') as default, patch('measure.query') as query:
                    with self.assertRaises(ValueError):
                        self.execute(args, query)
                    default.assert_not_called()
                    query.assert_not_called()
                self.assertFalse(path.exists())

    def test_real_minimal_path_cli_reads_only_diagnostic_ipc(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path, output = root / 'test.sock', root / 'run.json'
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            path.chmod(0o600)
            server.listen(1)
            server.settimeout(0.1)
            stop = threading.Event()
            errors, operations = [], []
            book = TraceBook()

            def serve():
                try:
                    while not stop.is_set():
                        try:
                            peer, _ = server.accept()
                        except socket.timeout:
                            continue
                        with peer:
                            peer.settimeout(1)
                            data = bytearray()
                            while b'\n' not in data and len(data) <= 4096:
                                block = peer.recv(4096)
                                if not block:
                                    raise OSError('incomplete synthetic request')
                                data.extend(block)
                            operations.append(json.loads(data)['operation'])
                            if len(operations) == 2:
                                ident = book.begin('text', 101)
                                at = book.clock()
                                book.dot_stage(ident, 'dot_rpc_start', at)
                                book.dot_stage(ident, 'dot_rpc_ready', at, False)
                                book.dot_stage(ident, 'dot_context_ready', at, False)
                                book.finish(ident, 'completed')
                            peer.sendall(
                                (
                                    json.dumps(
                                        {
                                            'ok': True,
                                            'health': {
                                                'bridge_version': '0.14.40',
                                                'runtime_instance': 'a' * 32,
                                            },
                                            **book.report(),
                                        }
                                    )
                                    + '\n'
                                ).encode()
                            )
                except OSError:
                    if not stop.is_set():
                        errors.append('socket')
                except Exception as error:
                    errors.append(type(error).__name__)

            # This integration proves real CLI/IPC and deduplication, not a
            # 200 ms CI filesystem budget. Clock-injected cases cover deadlines.
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / 'cc8'),
                        'measure',
                        '--seconds',
                        '2',
                        '--interval',
                        '0.05',
                        '--socket',
                        str(path),
                        '--expect-backend',
                        '0.14.40',
                        '--output',
                        str(output),
                    ],
                    cwd='/',
                    env={'PATH': '/usr/bin:/bin', 'HOME': folder},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            finally:
                stop.set()
                worker.join(2)
                server.close()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreaterEqual(len(operations), 2)
            self.assertEqual(set(operations), {'diagnostics'})
            self.assertEqual(errors, [])
            self.assertFalse(worker.is_alive())
            self.assertEqual(json.loads(output.read_text())['summary']['samples'], 1)
            self.assertTrue(json.loads(output.read_text())['capture']['runtime_bound'])
            self.assertEqual(
                json.loads(output.read_text())['dot_contexts']['thread']['new']['samples'], 1
            )
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()

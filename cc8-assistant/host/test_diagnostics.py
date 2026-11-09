"""Pipeline correlation/privacy regressions, without devices or cloud requests."""

import json
import io
import os
from contextlib import redirect_stdout
import socket
import tempfile
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from announce import AnnouncementServer
from bridge import Bridge
from diagnostics import TraceBook, error_kind, percentiles
from diagnose import query
import diagnose
from minimax_client import MiniMaxError
from protocol import Packet, RENDER
import numpy as np


class DiagnosticsTests(unittest.TestCase):
    def test_clock_latency_and_missing_stage_are_not_fabricated(self):
        now = [10.0]
        book = TraceBook(clock=lambda: now[0])
        ident = book.begin('voice')
        book.mark(ident, 'utterance_end')
        book.mark(ident, 'asr_start')
        now[0] += 0.3
        book.mark(ident, 'asr_end')
        book.bind(ident, 101)
        book.mark(101, 'brain_start')
        now[0] += 0.2
        book.mark(101, 'first_clause')
        book.finish(101, 'completed')
        report = book.report()
        group = report['groups']['voice']
        self.assertEqual(group['latencies']['asr_ms'], {'samples': 1, 'p50': 300.0, 'p95': 300.0})
        self.assertEqual(group['latencies']['brain_first_clause_ms']['p50'], 200.0)
        self.assertIsNone(group['latencies']['utterance_to_render_ack_ms']['p95'])
        self.assertEqual(group['success_rate'], 1)
        self.assertNotIn('started', report['requests'][0])
        self.assertFalse(report['render_ack_is_audible_onset'])

    def test_report_cannot_contain_exception_messages_or_arbitrary_stages(self):
        book = TraceBook()
        key = book.begin('text', 4)
        secret = 'sk-secret-user-photo-transcript'
        book.failure(4, 'brain', RuntimeError(secret))
        book.link_turn(4, 'turn_123')
        with self.assertRaises(ValueError):
            book.mark(key, secret)
        with self.assertRaises(ValueError):
            book.begin(secret)
        with self.assertRaises(ValueError):
            book.finish(key, secret)
        report = json.dumps(book.report())
        self.assertNotIn(secret, report)
        self.assertIn('turn_123', report)
        self.assertIn('provider_or_runtime', report)

    def test_request_eviction_and_late_receipt_are_bounded(self):
        book = TraceBook(limit=2)
        old = book.begin('voice', 1)
        book.begin('text', 2)
        book.begin('announcement', 3)
        book.mark(old, 'asr_end')
        book.mark(1, 'playback_done')
        book.failure(old, 'tts', RuntimeError())
        self.assertEqual(len(book.rows), 2)
        self.assertNotIn(1, book.generations)

    def test_first_event_and_terminal_receipt_do_not_regress(self):
        now = [0.0]
        book = TraceBook(clock=lambda: now[0])
        book.begin('text', 1)
        book.mark(1, 'first_pcm')
        now[0] = 1
        book.mark(1, 'first_pcm')
        book.finish(1, 'completed')
        book.finish(1, 'cancelled')
        row = book.report()['requests'][0]
        self.assertEqual(row['stages']['first_pcm'], 0)
        self.assertEqual(row['outcome'], 'completed')

    def test_task_completion_survives_narration_failure_and_rebinding_cleans_map(self):
        book = TraceBook(limit=1)
        ident = book.begin('text', 1)
        book.bind(ident, 2)
        self.assertNotIn(1, book.generations)
        book.scenario(2, 'tool')
        book.task_result(2, 'completed')
        book.failure(2, 'tts', TimeoutError('not exported'))
        book.task_result(2, 'failed')
        report = book.report()
        row = report['requests'][0]
        self.assertEqual(row['outcome'], 'failed')
        self.assertEqual(row['task_outcome'], 'completed')
        self.assertEqual(report['scenarios']['tool']['samples'], 1)
        book.begin('voice', 3)
        self.assertNotIn(2, book.generations)

    def test_error_categories_never_guess_vendor_codes(self):
        self.assertEqual(error_kind(MiniMaxError('secret', http_status=429)), 'rate_limit')
        self.assertEqual(
            error_kind(MiniMaxError('secret', http_status=401)), 'credential_or_permission'
        )
        self.assertEqual(error_kind(MiniMaxError('secret', code=9999)), 'provider_or_runtime')
        self.assertEqual(error_kind(TimeoutError('secret')), 'timeout')
        self.assertEqual(error_kind(EOFError('secret')), 'transport_or_io')

    def test_later_clause_tts_failure_is_not_mislabeled_as_playback(self):
        book = TraceBook()
        book.begin('text', 1)
        for stage in ('tts_start', 'first_pcm', 'tts_start'):
            book.mark(1, stage)
        self.assertEqual(book.current_stage(1), 'tts')
        book.failure(1, book.current_stage(1), TimeoutError())
        self.assertEqual(book.report()['requests'][0]['error']['stage'], 'tts')

    def test_unconfirmed_barge_asr_error_does_not_fail_previous_answer(self):
        bridge = Bridge(None, None)
        bridge.log = lambda *a, **k: None
        bridge.status = lambda *a: None
        old = bridge.diagnostics.begin('text', bridge.session.generation)
        candidate = bridge.diagnostics.begin('voice')
        bridge.diagnostics.mark(candidate, 'asr_start')
        bridge.failure('识别失败', TimeoutError(), trace_key=candidate)
        rows = {r['request_id']: r for r in bridge.diagnostics.report()['requests']}
        self.assertIsNone(rows[old]['outcome'])
        self.assertEqual(rows[candidate]['outcome'], 'failed')

    def test_dropped_recognition_request_is_not_reported_as_still_pending(self):
        with patch('threading.Thread.start'):
            bridge = Bridge(None, None)
        ids = []
        for _ in range(3):
            ident = bridge.diagnostics.begin('voice')
            ids.append(ident)
            bridge.enqueue_recognition((1, [], False, [], None, None, ident))
        rows = {r['request_id']: r for r in bridge.diagnostics.report()['requests']}
        self.assertEqual(rows[ids[0]]['outcome'], 'cancelled')
        self.assertIsNone(rows[ids[-1]]['outcome'])

    def test_percentiles_use_actual_samples(self):
        self.assertEqual(percentiles([]), {'samples': 0, 'p50': None, 'p95': None})
        self.assertEqual(percentiles([100, 200, 300, 400])['p95'], 385.0)

    def test_threaded_collection_and_snapshot_are_independent(self):
        book = TraceBook()

        def add():
            for _ in range(20):
                ident = book.begin('voice')
                book.mark(ident, 'asr_start')
                book.finish(ident, 'rejected')

        threads = [threading.Thread(target=add) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        result = book.report()
        self.assertEqual(len(result['requests']), 80)
        result['requests'].clear()
        self.assertEqual(len(book.report()['requests']), 80)

    def test_private_ipc_health_and_diagnostics_never_enable_capture(self):
        bridge = Bridge(None, None)
        bridge.metrics['last_asr'] = {'text': 'SECRET'}
        bridge.metrics.update(local_wake_count=2, voice_asr_attempts=3)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'owner.sock'
            server = AnnouncementServer(bridge, path)
            server.start()
            try:
                health = query(path, 'health')
                report = query(path, 'diagnostics')
                self.assertFalse(health['microphone_active'])
                self.assertFalse(health['connected'])
                self.assertEqual(health['health_owner_version'], 1)
                self.assertIs(type(health['runtime_process_id']), int)
                self.assertEqual(health['runtime_process_id'], os.getpid())
                self.assertEqual(health['local_wake_count'], 2)
                self.assertEqual(health['voice_asr_attempts'], 3)
                self.assertNotIn('SECRET', json.dumps(health))
                self.assertNotIn('SECRET', json.dumps(report))
                self.assertFalse(bridge.session.active)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                server.close()

    def test_incomplete_diagnostic_reply_fails_without_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.sock'
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            path.chmod(0o600)
            server.listen()

            def serve():
                with server.accept()[0] as conn:
                    conn.recv(4096)
                    conn.sendall(b'{"ok":true}')

            t = threading.Thread(target=serve)
            t.start()
            try:
                with self.assertRaises(OSError):
                    query(path)
            finally:
                t.join()
                server.close()

    def test_cli_exports_owner_only_json_and_no_content(self):
        with tempfile.TemporaryDirectory() as folder:
            result = {'ok': True, **TraceBook().report()}
            stdout = io.StringIO()
            with (
                patch.object(diagnose, 'ROOT', Path(folder)),
                patch.object(diagnose, 'query', return_value=result),
                patch('sys.argv', ['diagnose.py']),
                redirect_stdout(stdout),
            ):
                diagnose.main()
            path = Path(stdout.getvalue().strip())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(path.read_text()), result)

    def test_real_answer_pipeline_links_render_and_completion_without_device(self):
        class Speech:
            def stream(self, text, current):
                yield np.ones(320, dtype=np.int16)

        bridge = Bridge(Speech(), None)
        bridge.session.connection = object()
        bridge.session.text_generation = bridge.session.generation
        bridge.save_status = lambda: None

        def event(name, **fields):
            if name == 'play_end':
                bridge.on_event({'event': 'play_done', 'generation': fields['generation']})

        bridge.event = event
        bridge.send = lambda p: bridge.on_packet(
            Packet(RENDER, p.generation, p.sequence, p.frame, 1_000_000_000, p.data)
        )
        bridge.answer('PRIVATE-UTTERANCE', bridge.session.generation)
        report = bridge.diagnostics_report()
        row = report['requests'][0]
        self.assertEqual(row['outcome'], 'completed')
        self.assertTrue(
            {
                'brain_start',
                'first_clause',
                'tts_start',
                'first_pcm',
                'render_ack',
                'playback_done',
                'task_result',
            }
            <= row['stages'].keys()
        )
        self.assertNotIn('PRIVATE-UTTERANCE', json.dumps(report))


if __name__ == '__main__':
    unittest.main()

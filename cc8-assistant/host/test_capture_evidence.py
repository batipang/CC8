"""Real endpoint/recognition observations; no capture, providers or stored PCM."""

from copy import deepcopy
from dataclasses import replace
import json
import gc
import weakref
from pathlib import Path
import subprocess
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from bridge import Bridge
from capture_evidence import CaptureEvidence, validate
from diagnostic_run import DiagnosticRun
from diagnostics import TraceBook
from dsp import FRAME, RATE
from echo_pipeline import CleanFrame
from test_barge_in import Sink


def evidence():
    result = CaptureEvidence()
    near = SimpleNamespace(correlation=0.9, residual_correlation=0.7)
    raw, clean, ref = np.full(FRAME, 0.1), np.full(FRAME, 0.01), np.full(FRAME, 0.2)
    result.feed(
        CleanFrame(0, raw, ref, clean, clean),
        near,
        voiced=True,
        credible=True,
        pause_requested=False,
    )
    zero = np.zeros(FRAME)
    near.correlation = near.residual_correlation = 0
    result.feed(
        CleanFrame(1, zero, zero, zero, zero),
        near,
        voiced=False,
        credible=False,
        pause_requested=True,
    )
    return result.snapshot(echo_risk=True, during_playback=True, local_wake=False)


class CaptureDiagnosticsTests(unittest.TestCase):
    def test_numeric_observations_roundtrip_without_pcm_or_aliases(self):
        value = evidence()
        self.assertEqual(
            {
                k: value[k]
                for k in (
                    'frames',
                    'voiced_frames',
                    'credible_frames',
                    'render_frames',
                    'pause_requested_frames',
                    'voiced_after_pause_request_frames',
                )
            },
            dict(
                frames=2,
                voiced_frames=1,
                credible_frames=1,
                render_frames=1,
                pause_requested_frames=1,
                voiced_after_pause_request_frames=0,
            ),
        )
        self.assertEqual(value['raw_correlated_frames'], 1)
        self.assertEqual(value['residual_correlated_frames'], 1)
        self.assertAlmostEqual(value['raw_peak_dbfs'], -20, places=1)
        book = TraceBook()
        run = DiagnosticRun()
        run.ingest(book.report())
        key = book.begin('voice', 7)
        book.capture(key, value)
        book.gate(key, 'accepted')
        run.ingest(book.report())
        value['frames'] = 8
        report = run.report()
        self.assertEqual(report['requests'][0]['capture']['frames'], 2)
        report['requests'][0]['capture']['frames'] = 9
        self.assertEqual(book.report()['requests'][0]['capture']['frames'], 2)
        self.assertEqual(run.report()['requests'][0]['capture']['frames'], 2)
        self.assertNotIn('array', json.dumps(run.report()))

    def test_aggregation_does_not_retain_frame_arrays(self):
        stats = CaptureEvidence()
        array = np.zeros(FRAME)
        tracked = weakref.ref(array)
        frame = CleanFrame(0, array, array, array, array)
        near = SimpleNamespace(correlation=0.0, residual_correlation=0.0)
        stats.feed(frame, near, voiced=False, credible=False, pause_requested=False)
        del frame, array
        gc.collect()
        self.assertIsNone(tracked())
        self.assertEqual(
            stats.snapshot(echo_risk=False, during_playback=False, local_wake=False)['frames'], 1
        )

    def test_nonfinite_frame_observations_cannot_fabricate_valid_correlations_or_counts(self):
        stats = CaptureEvidence()
        array = np.zeros(FRAME)
        frame = CleanFrame(0, array, array, array, array)
        near = SimpleNamespace(correlation=0.0, residual_correlation=0.0)
        stats.feed(frame, near, voiced=False, credible=False, pause_requested=False)
        before = deepcopy(stats.values)
        for value in (float('nan'), float('inf'), -0.1, 1.1):
            near.correlation = value
            with self.assertRaises(ValueError):
                stats.feed(frame, near, voiced=True, credible=True, pause_requested=True)
            self.assertEqual(stats.values, before)
        near.correlation = 0.0
        invalid = replace(frame, cleaned=np.full(FRAME, float('nan')))
        with self.assertRaises(ValueError):
            stats.feed(invalid, near, voiced=True, credible=True, pause_requested=True)
        self.assertEqual(stats.values, before)

    def test_unknown_content_nonfinite_and_impossible_counts_are_rejected_atomically(self):
        book = TraceBook()
        key = book.begin('voice')
        mutations = [
            ('text', 'SECRET'),
            ('frames', True),
            ('frames', 0),
            ('frames', 2001),
            ('voiced_frames', 3),
            ('credible_frames', 2),
            ('render_frames', 0),
            ('voiced_after_pause_request_frames', 2),
            ('max_raw_correlation', float('nan')),
            ('max_residual_correlation', float('inf')),
            ('raw_peak_dbfs', 'SECRET'),
            ('raw_peak_dbfs', -121),
            ('raw_peak_dbfs', 10**1000),
            ('local_wake', 1),
        ]
        for name, value in mutations:
            with self.subTest(name=name):
                invalid = evidence()
                invalid[name] = value
                before = book.report()
                with self.assertRaises(ValueError):
                    book.capture(key, invalid)
                self.assertEqual(book.report(), before)
        with self.assertRaises(ValueError):
            book.gate(key, 'SECRET')
        self.assertNotIn('SECRET', json.dumps(book.report()))

    def test_terminal_enrichment_preserves_outcome_and_cannot_rewrite_observations(self):
        book = TraceBook(limit=1)
        key = book.begin('voice', 5)
        book.finish(key, 'cancelled')
        value = evidence()
        book.capture(key, value)
        book.gate(key, 'stale')
        self.assertEqual(book.report()['requests'][0]['outcome'], 'cancelled')
        changed = {**value, 'frames': 3}
        with self.assertRaises(ValueError):
            book.capture(key, changed)
        with self.assertRaises(ValueError):
            book.gate(key, 'accepted')
        new = book.begin('text', 6)
        book.capture(key, value)
        book.gate(key, 'accepted')
        self.assertNotIn('capture', book.report()['requests'][0])
        with self.assertRaises(ValueError):
            book.capture(new, value)
        with self.assertRaises(ValueError):
            book.gate(new, 'accepted')

    def test_collector_rejects_changed_or_missing_evidence_without_partial_updates(self):
        book = TraceBook()
        run = DiagnosticRun()
        run.ingest(book.report())
        key = book.begin('voice')
        book.capture(key, evidence())
        book.gate(key, 'accepted')
        run.ingest(book.report())
        for field in ('capture', 'gate'):
            for remove in (False, True):
                changed = deepcopy(book.report())
                row = changed['requests'][0]
                if remove:
                    del row[field]
                elif field == 'capture':
                    row[field]['frames'] += 1
                else:
                    row[field] = 'echo_text'
                before = run.report()
                with self.assertRaises(ValueError):
                    run.ingest(changed)
                self.assertEqual(run.report(), before)
        changed = deepcopy(book.report())
        changed['requests'][0]['origin'] = 'text'
        with self.assertRaises(ValueError):
            DiagnosticRun().ingest(changed)

    def test_plain_system_python_collectors_do_not_load_audio_dependencies(self):
        program = "import sys;import diagnostics,diagnostic_run;assert 'numpy' not in sys.modules;print('lightweight')"
        result = subprocess.run(
            ['/usr/bin/python3', '-c', program],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'lightweight\n')

    def endpoint(self):
        class Boundaries:
            talking = False

            def reset(self):
                self.talking = False

            def feed(self, pcm):
                self.talking = not self.talking
                return self.talking, None if self.talking else pcm.copy(), -30

        # Endpoint/queue assertions own both queues. Overriding Bridge.work
        # only quiets the response worker, not the independently started ASR.
        with (
            patch('recognition_worker.RecognitionWorker.start'),
            patch('response_worker.ResponseWorker.start'),
        ):
            b = Bridge(None, None)
        b.session.connection = Sink()
        b.session.active = True
        b.session.wake = False
        b.session.awake = True
        b.playback.playing = True
        b.save_status = lambda: None
        endpoint = b.capture.endpoint
        endpoint.fast_stop = None
        endpoint.vad = Boundaries()
        endpoint.near_speech = SimpleNamespace(
            feed=lambda *args: (True, True),
            reset=lambda: None,
            correlation=0.9,
            residual_correlation=0.7,
        )
        raw, cleaned, reference = np.full(FRAME, 0.1), np.full(FRAME, 0.01), np.full(FRAME, 0.2)
        frame = CleanFrame(1_000_000_000, raw, reference, cleaned, cleaned)
        return b, frame, Boundaries

    def test_endpoint_fixture_has_no_background_queue_consumers(self):
        b, _, _ = self.endpoint()
        self.assertIsNone(b.recognition.worker)
        self.assertIsNone(b.response.worker)

    def test_actual_endpoint_binds_capture_to_its_queued_trace_without_changing_pcm(self):
        b, frame, _ = self.endpoint()
        b.capture.endpoint.consume(frame)
        b.capture.endpoint.consume(replace(frame, nanos=frame.nanos + 10_000_000))
        job = b.recognition.queue.get_nowait()
        row = next(r for r in b.diagnostics.report()['requests'] if r['request_id'] == job[6])
        self.assertEqual(row['capture']['frames'], 2)
        self.assertEqual(row['capture']['voiced_frames'], 2)
        self.assertTrue(row['capture']['echo_risk'])
        self.assertTrue(row['capture']['during_playback'])
        self.assertEqual(job[1].tobytes(), frame.cleaned.tobytes())
        self.assertFalse(job[4]['local_wake'])
        self.assertEqual(validate(row['capture']), row['capture'])

    def test_transport_session_and_activity_resets_drop_partial_capture_counts(self):
        for name in ('reset_transport', 'reset_session', 'reset_voice_activity'):
            with self.subTest(reset=name):
                b, frame, boundaries = self.endpoint()
                endpoint = b.capture.endpoint
                endpoint.consume(frame)
                getattr(endpoint, name)()
                endpoint.vad = boundaries()
                endpoint.consume(replace(frame, nanos=frame.nanos + 10_000_000))
                endpoint.consume(replace(frame, nanos=frame.nanos + 20_000_000))
                job = b.recognition.queue.get_nowait()
                row = next(
                    r for r in b.diagnostics.report()['requests'] if r['request_id'] == job[6]
                )
                self.assertEqual(row['capture']['frames'], 2)

    def test_abandoned_partial_capture_finishes_on_each_lifecycle_reset(self):
        for reset in ('reset_transport', 'reset_session', 'reset_voice_activity'):
            with self.subTest(reset=reset):
                b, frame, _ = self.endpoint()
                endpoint = b.capture.endpoint
                endpoint.consume(frame)
                key = endpoint.voice_trace
                getattr(endpoint, reset)()
                getattr(endpoint, reset)()
                row = next(r for r in b.diagnostics.report()['requests'] if r['request_id'] == key)
                self.assertEqual(row['outcome'], 'cancelled')
                self.assertEqual(row['gate'], 'stale')
                self.assertIsNone(row['generation'])
                self.assertNotIn('utterance_end', row['stages'])
                self.assertIsNone(endpoint.voice_trace)
                self.assertTrue(b.recognition.queue.empty())

    def test_reset_does_not_finish_trace_already_handed_to_recognition(self):
        for reset in ('reset_transport', 'reset_session', 'reset_voice_activity'):
            with self.subTest(reset=reset):
                b, frame, _ = self.endpoint()
                endpoint = b.capture.endpoint
                endpoint.consume(frame)
                endpoint.consume(replace(frame, nanos=frame.nanos + 10_000_000))
                job = b.recognition.queue.get_nowait()
                getattr(endpoint, reset)()
                row = next(
                    r for r in b.diagnostics.report()['requests'] if r['request_id'] == job[6]
                )
                self.assertIsNone(row['outcome'])
                self.assertNotIn('gate', row)
                self.assertIn('utterance_end', row['stages'])
                self.assertIsNone(endpoint.voice_trace)

    def test_fast_stop_finishes_only_partial_capture_without_erasing_completed_tasks(self):
        b, frame, _ = self.endpoint()
        endpoint = b.capture.endpoint
        done = b.diagnostics.begin('text', b.session.generation)
        b.diagnostics.finish(done, 'completed')
        endpoint.consume(frame)
        partial = endpoint.voice_trace
        endpoint.apply_fast_stop('停一下', frame.nanos + 10_000_000)
        rows = {r['request_id']: r for r in b.diagnostics.report()['requests']}
        self.assertEqual(rows[partial]['outcome'], 'cancelled')
        self.assertEqual(rows[partial]['gate'], 'stale')
        self.assertEqual(rows[done]['outcome'], 'completed')
        self.assertIsNone(endpoint.voice_trace)
        self.assertTrue(b.recognition.queue.empty())

    def test_home_stop_cancels_partial_and_releases_capture_without_enqueuing_asr(self):
        b, frame, _ = self.endpoint()
        b.capture.endpoint.consume(frame)
        key = b.capture.endpoint.voice_trace
        b.session.stop()
        row = next(r for r in b.diagnostics.report()['requests'] if r['request_id'] == key)
        self.assertEqual(row['outcome'], 'cancelled')
        self.assertEqual(row['gate'], 'stale')
        self.assertFalse(b.session.active)
        self.assertTrue(b.recognition.queue.empty())
        self.assertIsNone(b.capture.endpoint.capture_evidence)
        self.assertIsNone(b.capture.endpoint.voice_trace)

    def test_fast_stop_finishes_discarded_queue_without_touching_inflight_trace(self):
        b, _, _ = self.endpoint()
        inflight = b.diagnostics.begin('voice')
        queued = []
        for _ in range(2):
            key = b.diagnostics.begin('voice')
            queued.append(key)
            b.recognition.queue.put(
                (b.session.generation, np.zeros(FRAME), False, [], None, None, key)
            )
        b.recognition.discard()
        rows = {r['request_id']: r for r in b.diagnostics.report()['requests']}
        self.assertIsNone(rows[inflight]['outcome'])
        self.assertNotIn('gate', rows[inflight])
        for key in queued:
            self.assertEqual(rows[key]['outcome'], 'cancelled')
            self.assertEqual(rows[key]['gate'], 'stale')
        self.assertTrue(b.recognition.queue.empty())

    def test_real_recognition_worker_exports_only_its_gate_classification(self):
        cases = [
            ('', None, 'empty'),
            ('这是参考内容', None, 'echo_text'),
            ('说个笑话', None, 'accepted'),
            (TimeoutError('SECRET'), None, 'error'),
            ('另一个参考', '另一个参考', 'echo_render'),
        ]
        for recognized, rendered, expected in cases:
            with self.subTest(decision=expected):

                class Speech:
                    def recognize(self, pcm):
                        if isinstance(recognized, Exception):
                            raise recognized
                        return rendered if np.mean(pcm) > 0.5 else recognized

                b = Bridge(Speech(), None)
                b.session.connection = Sink()
                b.session.active = True
                b.session.awake = True
                b.session.wake = False
                b.playback.playing = True
                b.save_status = lambda: None
                replies = []
                b.recognition.hooks = replace(b.recognition.hooks, enqueue_response=replies.append)
                key = b.diagnostics.begin('voice')
                generation = b.session.generation
                b.recognition.queue.put(
                    (
                        generation,
                        np.zeros(RATE),
                        True,
                        ['这是参考内容'],
                        {'echo_risk': True, 'captured_at': time.monotonic()},
                        np.ones(RATE) if rendered else None,
                        key,
                    )
                )
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    row = next(
                        r for r in b.diagnostics.report()['requests'] if r['request_id'] == key
                    )
                    if 'gate' in row:
                        break
                    threading.Event().wait(0.01)
                self.assertEqual(row.get('gate'), expected)
                self.assertEqual(len(replies), int(expected == 'accepted'))
                self.assertNotIn('SECRET', json.dumps(b.diagnostics.report()))


if __name__ == '__main__':
    unittest.main()

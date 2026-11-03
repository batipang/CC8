"""Pause receipts remain attached to the initiating utterance after ASR handoff."""

from dataclasses import replace
import json
import threading
import unittest

import test_capture_evidence as fixture
from test_pause_confirmation import Span


class BargeReceiptTests(unittest.TestCase):
    def paused_endpoint(self):
        b, frame, _ = fixture.CaptureDiagnosticsTests().endpoint()
        b.playback.device_can_pause = True
        b.capture.endpoint.vad = Span(12)
        for index in range(12):
            b.capture.endpoint.consume(replace(frame, nanos=frame.nanos + index * 10_000_000))
        key = b.capture.endpoint.voice_trace
        token = b.playback.pause_token
        generation = b.session.generation
        b.capture.endpoint.consume(replace(frame, nanos=frame.nanos + 120_000_000))
        b.recognition.queue.get_nowait()
        self.assertIsNone(b.capture.endpoint.voice_trace)
        return b, frame, key, token, generation

    def test_ack_after_queue_handoff_enriches_only_the_original_rejected_trace(self):
        b, _, key, token, generation = self.paused_endpoint()
        b.playback.device_event(
            dict(event='playback_paused', token=token, generation=generation, nanos=1)
        )
        b.diagnostics.gate(key, 'echo_text')
        b.diagnostics.finish(key, 'rejected')
        b.resume_after_rejection(token, generation)
        b.playback.device_event(
            dict(event='playback_resumed', token=token, generation=generation, nanos=2)
        )
        row = next(r for r in b.diagnostics.report()['requests'] if r['request_id'] == key)
        self.assertEqual(row['outcome'], 'rejected')
        self.assertTrue({'pause_request', 'pause_ack', 'resume_ack'} <= set(row['stages']))

    def test_late_ack_does_not_attach_to_a_new_partial_or_new_owner(self):
        b, frame, key, token, generation = self.paused_endpoint()
        b.capture.endpoint.consume(replace(frame, nanos=frame.nanos + 130_000_000))
        newer = b.capture.endpoint.voice_trace
        self.assertNotEqual(key, newer)
        b.playback.device_event(
            dict(event='playback_paused', token=token, generation=generation, nanos=1)
        )
        rows = {r['request_id']: r for r in b.diagnostics.report()['requests']}
        self.assertIn('pause_ack', rows[key]['stages'])
        self.assertNotIn('pause_ack', rows[newer]['stages'])
        b.cancel('new owner')
        before = b.diagnostics.report()
        b.playback.device_event(
            dict(event='playback_resumed', token=token, generation=generation, nanos=2)
        )
        self.assertEqual(b.diagnostics.report(), before)

    def test_delayed_pause_publication_cannot_pause_new_owner(self):
        b, _, _ = fixture.CaptureDiagnosticsTests().endpoint()
        b.playback.device_can_pause = True
        b.capture.endpoint.voice_trace = b.diagnostics.begin('voice')
        original = b.diagnostics.mark
        entered, release = threading.Event(), threading.Event()

        def mark(key, stage):
            if stage == 'pause_request':
                entered.set()
                release.wait(2)
            original(key, stage)

        b.diagnostics.mark = mark
        thread = threading.Thread(target=b.pause_for_voice)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            b.cancel('new owner')
            before = len(b.session.connection.packets)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        events = [json.loads(p.data) for p in b.session.connection.packets[before:]]
        self.assertFalse(any(e['event'] == 'playback_pause' for e in events))
        self.assertIsNone(b.playback.pause_state)


if __name__ == '__main__':
    unittest.main()

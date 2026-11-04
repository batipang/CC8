"""Pause receipts remain attached to the initiating utterance after ASR handoff."""

from dataclasses import replace
import json
import threading
import unittest
import numpy as np

import test_capture_evidence as fixture
from test_pause_preservation import Span


class BargeReceiptTests(unittest.TestCase):
    def paused_endpoint(self):
        b, frame, _ = fixture.CaptureDiagnosticsTests().endpoint()
        b.playback.device_can_pause = True
        b.capture.endpoint.vad = Span(12)
        for index in range(12):
            b.capture.endpoint.consume(replace(frame, nanos=frame.nanos + index * 10_000_000))
        key = b.capture.endpoint.voice_trace
        token = b.capture.endpoint.turn_pause_token
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

    def test_ordered_resume_receipt_preserves_render_between_two_pauses(self):
        for delay_first_pause in (False, True):
            with self.subTest(delay_first_pause=delay_first_pause):
                b, _, _ = fixture.CaptureDiagnosticsTests().endpoint()
                b.playback.device_can_pause = True
                generation = b.generation
                timeline = b.capture.echo.timeline
                timeline.add(generation, 0, 1_000_000_000, np.ones(3200))
                first_key = b.diagnostics.begin('voice')
                b.capture.endpoint.voice_trace = first_key
                first = b.pause_for_voice()

                def receipt(event, token, millis):
                    b.on_event(
                        dict(
                            event=event,
                            token=token,
                            generation=generation,
                            nanos=millis * 1_000_000,
                        )
                    )

                if not delay_first_pause:
                    receipt('playback_paused', first, 1020)
                b.resume_after_rejection(first, generation)
                # Force the host interleaving without depending on wall time.
                b.playback.pause_cooldown = 0
                second_key = b.diagnostics.begin('voice')
                b.capture.endpoint.voice_trace = second_key
                second = b.pause_for_voice()
                self.assertIsNotNone(second)
                self.assertNotEqual(first, second)
                if delay_first_pause:
                    receipt('playback_paused', first, 1020)
                receipt('playback_resumed', first, 1220)
                receipt('playback_paused', second, 1280)
                self.assertEqual(timeline.paused_at, 1_280_000_000)
                self.assertTrue(np.all(timeline.at(1_130_000_000, 160) == 0))
                np.testing.assert_allclose(timeline.at(1_240_000_000, 160), 0.35)
                self.assertEqual(b.playback.pause_state['token'], second)
                rows = {r['request_id']: r['stages'] for r in b.diagnostics.report()['requests']}
                self.assertTrue({'pause_ack', 'resume_ack'} <= set(rows[first_key]))
                self.assertIn('pause_ack', rows[second_key])
                self.assertNotIn('resume_ack', rows[second_key])

    def test_unsolicited_backwards_and_replayed_receipts_cannot_retime_reference(self):
        b, _, key, token, generation = self.paused_endpoint()
        timeline = b.capture.echo.timeline

        def receipt(event, nanos):
            b.on_event(dict(event=event, token=token, generation=generation, nanos=nanos))

        receipt('playback_paused', 100)
        receipt('playback_resumed', 200)  # Host has not requested resume yet.
        self.assertEqual(timeline.paused_at, 100)
        b.resume_after_rejection(token, generation)
        receipt('playback_resumed', 90)
        self.assertEqual(timeline.paused_at, 100)
        receipt('playback_resumed', 300)
        self.assertIsNone(timeline.paused_at)
        receipt('playback_paused', 400)
        self.assertIsNone(timeline.paused_at)
        row = next(r for r in b.diagnostics.report()['requests'] if r['request_id'] == key)
        self.assertIn('resume_ack', row['stages'])

    def test_pending_receipts_are_bounded_and_new_ack_retires_older_tokens(self):
        b, _, _ = fixture.CaptureDiagnosticsTests().endpoint()
        b.playback.device_can_pause = True
        b.log = lambda *_args, **_fields: None
        generation = b.generation
        timeline = b.capture.echo.timeline
        tokens = []
        for _ in range(9):
            b.playback.pause_cooldown = 0
            token = b.pause_for_voice()
            self.assertIsNotNone(token)
            tokens.append(token)
            b.resume_after_rejection(token, generation)

        def receipt(event, token, nanos):
            b.on_event(dict(event=event, token=token, generation=generation, nanos=nanos))

        receipt('playback_paused', tokens[0], 10)
        self.assertIsNone(timeline.paused_at)
        receipt('playback_paused', tokens[-2], 20)
        receipt('playback_paused', tokens[-1], 30)
        receipt('playback_resumed', tokens[-2], 40)
        self.assertEqual(timeline.paused_at, 20)
        receipt('playback_resumed', tokens[-1], 50)
        self.assertIsNone(timeline.paused_at)

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

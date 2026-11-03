"""Short confirmed bursts must not suspend playback; recognition still receives them."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

import test_capture_evidence as fixture


class Span:
    def __init__(self, frames):
        self.frames = frames
        self.index = 0
        self.talking = False

    def reset(self):
        self.index = 0
        self.talking = False

    def feed(self, pcm):
        began = self.index == 0
        self.index += 1
        self.talking = self.index <= self.frames
        if not self.talking:
            self.index = 0
        return began, None if self.talking else pcm.copy(), -30


class PauseConfirmationTests(unittest.TestCase):
    def setup(self, sequence):
        b, frame, _ = fixture.CaptureDiagnosticsTests().endpoint()
        b.playback.device_can_pause = True
        self.install(b, sequence)
        return b, frame

    def install(self, b, sequence):
        values = iter([*sequence, False])
        b.capture.endpoint.vad = Span(len(sequence))
        b.capture.endpoint.near_speech = SimpleNamespace(
            feed=lambda *args: (True, next(values)),
            reset=lambda: None,
            correlation=0.5,
            residual_correlation=0.5,
        )

    def feed(self, b, frame, index):
        b.capture.endpoint.consume(replace(frame, nanos=frame.nanos + index * 10_000_000))

    def test_observed_short_burst_shapes_do_not_pause_or_drop_recognition(self):
        for count in (2, 5, 10):
            with self.subTest(credible_frames=count):
                b, frame = self.setup([True] * count)
                for i in range(count + 1):
                    self.feed(b, frame, i)
                self.assertIsNone(b.playback.pause_state)
                job = b.recognition.queue.get_nowait()
                row = next(
                    r for r in b.diagnostics.report()['requests'] if r['request_id'] == job[6]
                )
                self.assertEqual(row['capture']['credible_frames'], count)
                self.assertEqual(row['capture']['pause_requested_frames'], 0)
                self.assertTrue(b.recognition.queue.empty())

    def test_confirmed_voice_pauses_at_twelfth_frame_and_counter_saturates(self):
        b, frame = self.setup([True] * 20)
        for i in range(11):
            self.feed(b, frame, i)
            self.assertIsNone(b.playback.pause_state)
        self.feed(b, frame, 11)
        self.assertIsNotNone(b.playback.pause_state)
        for i in range(12, 21):
            self.feed(b, frame, i)
        self.assertEqual(b.capture.endpoint.pause_confirmed_frames, 12)
        self.assertEqual(b.metrics.get('barge_in_pauses'), 1)
        job = b.recognition.queue.get_nowait()
        row = next(r for r in b.diagnostics.report()['requests'] if r['request_id'] == job[6])
        self.assertEqual(row['capture']['credible_frames'], 20)

    def test_lifecycle_resets_cannot_carry_confirmation_into_another_utterance(self):
        for method in ('reset_transport', 'reset_session', 'reset_voice_activity'):
            with self.subTest(reset=method):
                b, frame = self.setup([True] * 8)
                for i in range(8):
                    self.feed(b, frame, i)
                getattr(b.capture.endpoint, method)()
                self.install(b, [True] * 12)
                for i in range(11):
                    self.feed(b, frame, 20 + i)
                    self.assertIsNone(b.playback.pause_state)
                self.feed(b, frame, 31)
                self.assertIsNotNone(b.playback.pause_state)

    def test_streaming_stop_still_cancels_before_confirmation(self):
        b, frame = self.setup([False])
        b.capture.endpoint.fast_stop = SimpleNamespace(feed=lambda pcm: '停一下')
        b.capture.endpoint.consume(replace(frame, nanos=2_000_000_000))
        self.assertFalse(b.playback.playing)
        self.assertEqual(b.capture.endpoint.pause_confirmed_frames, 0)
        events = [json.loads(p.data) for p in b.session.connection.packets]
        self.assertTrue(any(e['event'] == 'transcript' and e['text'] == '停一下' for e in events))
        self.assertEqual(b.metrics.get('fast_stop_count'), 1)


if __name__ == '__main__':
    unittest.main()

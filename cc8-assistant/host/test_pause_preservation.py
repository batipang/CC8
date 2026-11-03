"""Preserve the deployed pause path after the twelve-frame experiment was rejected."""

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


class PausePreservationTests(unittest.TestCase):
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

    def test_short_confirmed_segments_pause_and_still_reach_recognition(self):
        for count in (2, 5, 10):
            with self.subTest(credible_frames=count):
                b, frame = self.setup([True] * count)
                self.feed(b, frame, 0)
                self.assertIsNotNone(b.playback.pause_state)
                for i in range(1, count + 1):
                    self.feed(b, frame, i)
                job = b.recognition.queue.get_nowait()
                row = next(
                    r for r in b.diagnostics.report()['requests'] if r['request_id'] == job[6]
                )
                self.assertEqual(row['capture']['credible_frames'], count)
                self.assertGreater(row['capture']['pause_requested_frames'], 0)
                self.assertTrue(b.recognition.queue.empty())

    def test_first_confirmed_frame_pauses_once_without_extra_accumulation(self):
        b, frame = self.setup([False, False, True, True])
        for i in range(2):
            self.feed(b, frame, i)
            self.assertIsNone(b.playback.pause_state)
        self.feed(b, frame, 2)
        self.assertIsNotNone(b.playback.pause_state)
        for i in range(3, 5):
            self.feed(b, frame, i)
        self.assertEqual(b.metrics.get('barge_in_pauses'), 1)
        job = b.recognition.queue.get_nowait()
        row = next(r for r in b.diagnostics.report()['requests'] if r['request_id'] == job[6])
        self.assertEqual(row['capture']['credible_frames'], 2)

    def test_streaming_stop_still_cancels_without_utterance_confirmation(self):
        b, frame = self.setup([False])
        b.capture.endpoint.fast_stop = SimpleNamespace(feed=lambda pcm: '停一下')
        b.capture.endpoint.consume(replace(frame, nanos=2_000_000_000))
        self.assertFalse(b.playback.playing)
        events = [json.loads(p.data) for p in b.session.connection.packets]
        self.assertTrue(any(e['event'] == 'transcript' and e['text'] == '停一下' for e in events))
        self.assertEqual(b.metrics.get('fast_stop_count'), 1)


if __name__ == '__main__':
    unittest.main()

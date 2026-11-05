"""Measure the real endpoint's pre-VAD coverage without changing admission."""

from dataclasses import replace
import gc
import unittest
import weakref

import numpy as np

from capture_evidence import validate
from diagnostic_run import DiagnosticRun
from diagnostics import TraceBook
from dsp import FRAME, VoiceDetector
from echo_pipeline import CleanFrame
import test_capture_evidence as fixture


PREFIX = {
    'onset_prefix_frames': 13,
    'onset_prefix_credible_frames': 13,
    'onset_prefix_render_frames': 13,
    'onset_prefix_credible_with_render_frames': 13,
}


class OnsetPrefixEvidenceTests(unittest.TestCase):
    def endpoint(self):
        bridge, frame, _ = fixture.CaptureDiagnosticsTests().endpoint()
        bridge.capture.endpoint.vad = VoiceDetector()
        return bridge, frame

    def complete(self, bridge, frame, *, leading_render=True, gap_ns=0, gap_index=None):
        endpoint = bridge.capture.endpoint
        onset = VoiceDetector.ONSET_FRAMES
        if gap_index is None:
            gap_index = onset - 1
        voices = iter([True] * onset + [False] * 80)
        endpoint.near_speech.feed = lambda *_: (voice := next(voices), voice)
        for index in range(onset + 80):
            offset = gap_ns if index >= gap_index else 0
            reference = frame.reference if leading_render and index < onset - 1 else np.zeros(FRAME)
            endpoint.consume(
                replace(frame, nanos=frame.nanos + index * 10_000_000 + offset, reference=reference)
            )
        job = bridge.recognition.queue.get_nowait()
        row = next(r for r in bridge.diagnostics.report()['requests'] if r['request_id'] == job[6])
        return job, row['capture']

    def test_real_endpoint_keeps_pre_vad_overlap_separate_from_strict_capture_counts(self):
        bridge, frame = self.endpoint()
        job, capture = self.complete(bridge, frame)
        self.assertEqual({k: capture.get(k) for k in PREFIX}, PREFIX)
        self.assertEqual(capture['frames'], 81)
        self.assertEqual(capture['render_frames'], 0)
        self.assertEqual(capture['credible_with_render_frames'], 0)
        self.assertEqual(capture['device_vad_tail_ms'], 800)
        self.assertEqual(job[4]['input_end_ns'], frame.nanos + 14 * 10_000_000)
        # The recognition job retains precisely the same VAD audio, including
        # its existing pre-roll. The new evidence contains counters only.
        np.testing.assert_array_equal(job[1], np.tile(frame.cleaned, 94))
        self.assertEqual(validate(capture), capture)

    def test_silence_before_actual_onset_cannot_supply_prefix_render_evidence(self):
        bridge, frame = self.endpoint()
        endpoint = bridge.capture.endpoint
        voices = iter([True] * 10 + [False] * 20)
        endpoint.near_speech.feed = lambda *_: (voice := next(voices), voice)
        for index in range(30):
            endpoint.consume(replace(frame, nanos=frame.nanos + index * 10_000_000))
        _, capture = self.complete(
            bridge, replace(frame, nanos=frame.nanos + 300_000_000), leading_render=False
        )
        self.assertEqual(capture.get('onset_prefix_frames'), 13)
        self.assertEqual(capture.get('onset_prefix_render_frames'), 0)
        self.assertEqual(capture.get('onset_prefix_credible_with_render_frames'), 0)

    def test_gap_duplicate_or_reversed_capture_stamp_discards_old_prefix(self):
        for gap in (20_000_000, -10_000_000, -20_000_000):
            with self.subTest(gap_ns=gap):
                bridge, frame = self.endpoint()
                _, capture = self.complete(bridge, frame, gap_ns=gap)
                self.assertEqual({k: capture.get(k) for k in PREFIX}, dict.fromkeys(PREFIX, 0))

    def test_mid_prefix_gap_keeps_only_the_recent_contiguous_run(self):
        bridge, frame = self.endpoint()
        _, capture = self.complete(bridge, frame, gap_ns=20_000_000, gap_index=5)
        self.assertEqual({k: capture.get(k) for k in PREFIX}, dict.fromkeys(PREFIX, 8))

    def test_lifecycle_resets_discard_unfinished_prefix_without_changing_new_audio(self):
        for name in (
            'reset_transport',
            'reset_session',
            'reset_voice_activity',
            'discard_capture_gap',
        ):
            with self.subTest(reset=name):
                bridge, frame = self.endpoint()
                endpoint = bridge.capture.endpoint
                endpoint.near_speech.feed = lambda *_: (True, True)
                for index in range(10):
                    endpoint.consume(replace(frame, nanos=frame.nanos + index * 10_000_000))
                self.assertIsNone(endpoint.voice_trace)
                getattr(endpoint, name)()
                _, capture = self.complete(
                    bridge, replace(frame, nanos=frame.nanos + 100_000_000), leading_render=False
                )
                self.assertEqual(capture.get('onset_prefix_frames'), 13)
                self.assertEqual(capture.get('onset_prefix_render_frames'), 0)

    def test_prefix_is_bounded_to_the_existing_500ms_vad_preroll(self):
        bridge, frame = self.endpoint()
        endpoint = bridge.capture.endpoint

        class DelayedVad(VoiceDetector):
            ONSET_FRAMES = 100

        endpoint.vad = DelayedVad()
        endpoint.near_speech.feed = lambda *_: (True, True)
        for index in range(100):
            endpoint.consume(replace(frame, nanos=frame.nanos + index * 10_000_000))
        endpoint.near_speech.feed = lambda *_: (False, False)
        for index in range(100, 180):
            endpoint.consume(replace(frame, nanos=frame.nanos + index * 10_000_000))
        job = bridge.recognition.queue.get_nowait()
        row = next(r for r in bridge.diagnostics.report()['requests'] if r['request_id'] == job[6])
        self.assertEqual({k: row['capture'].get(k) for k in PREFIX}, dict.fromkeys(PREFIX, 49))

    def test_zero_prefix_and_legacy_reports_remain_distinct_through_real_collectors(self):
        for prefix in (None, dict.fromkeys(PREFIX, 0), PREFIX):
            with self.subTest(prefix=prefix):
                capture = fixture.evidence()
                if prefix is not None:
                    capture.update(prefix)
                self.assertEqual(validate(capture), capture)
                book, run = TraceBook(), DiagnosticRun()
                run.ingest(book.report())
                key = book.begin('voice')
                book.capture(key, capture)
                run.ingest(book.report())
                self.assertEqual(run.report()['requests'][0]['capture'], capture)
                if prefix is not None:
                    capture['onset_prefix_frames'] = 1
                    self.assertEqual(
                        book.report()['requests'][0]['capture']['onset_prefix_frames'],
                        prefix['onset_prefix_frames'],
                    )

    def test_partial_nonfinite_overbound_and_contradictory_prefix_counts_are_rejected(self):
        value = {**fixture.evidence(), **PREFIX}
        for field in PREFIX:
            with self.subTest(missing=field), self.assertRaises(ValueError):
                validate({k: v for k, v in value.items() if k != field})
            for invalid in (True, -1, 50, 1.5, float('nan'), '1'):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    validate({**value, field: invalid})
        for counts in ((3, 4, 3, 3), (3, 3, 4, 3), (3, 2, 1, 2), (3, 3, 3, 0)):
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                validate({**value, **dict(zip(PREFIX, counts))})

    def test_pending_prefix_retains_flags_without_holding_the_input_audio(self):
        from capture_evidence import OnsetPrefix

        prefix = OnsetPrefix()
        references = []
        for index in range(4):
            pcm = np.full(FRAME, 0.1)
            frame = CleanFrame(index * 10_000_000, pcm, pcm, pcm, pcm)
            references.append(weakref.ref(pcm))
            prefix.feed(frame, voiced=True, credible=True)
        del frame, pcm
        gc.collect()
        self.assertTrue(all(ref() is None for ref in references))
        self.assertEqual(prefix.before(40_000_000, voiced=True), dict.fromkeys(PREFIX, 4))


if __name__ == '__main__':
    unittest.main()

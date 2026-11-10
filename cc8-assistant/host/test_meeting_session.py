"""Actual meeting timeline using synthetic PCM only, without a recorder or API."""

import dataclasses
import unittest
from unittest.mock import patch

from meeting_asr import Transcript, Span, CHUNK_SAMPLES
from meeting_session import CaptureOwner, Chunk, MeetingSession, MAX_PACKET_SAMPLES

PCM = (8192).to_bytes(2, 'little', signed=True) * 1600


def owner(value=1, route=1):
    return CaptureOwner(route, value, format(value, '032x'))


class MeetingSessionTests(unittest.TestCase):
    def test_chunk_immutable_pcm_and_declared_sample_bounds_must_agree(self):
        valid = Chunk('a' * 32, 'b' * 32, 0, 1600, PCM)
        for fields in (
            dict(pcm=bytearray(PCM)),
            dict(pcm=PCM[:-2]),
            dict(start_sample=True),
            dict(end_sample=1599),
            dict(end_sample=1601),
            dict(ident='../file'),
        ):
            with self.assertRaises(ValueError):
                dataclasses.replace(valid, **fields)

    def model(self, **kwargs):
        value = MeetingSession('a' * 32, **kwargs)
        value.start(owner())
        return value

    def feed(self, model, count, start=0, who=None):
        ready = []
        for i in range(start, start + count):
            accepted, chunks = model.receive(who or owner(), i, i * 1600, PCM)
            self.assertEqual(accepted, 1600)
            ready.extend(chunks)
        return ready

    def result(self, chunk, text='真实识别'):
        return Transcript(text, round((chunk.end_sample - chunk.start_sample) * 1000 / 16000))

    def test_passive_construction_and_invalid_or_reused_capture_consent(self):
        model = MeetingSession('a' * 32)
        self.assertEqual(model.state, 'ready')
        self.assertFalse(model.snapshot()['processing_complete'])
        for value in (CaptureOwner(0, 0, 'b' * 32), owner()):
            model.start(value)
            model.pause()
            with self.assertRaises(ValueError):
                model.start(value)
        for values in ((True, 1, 'a' * 32), (1, True, 'a' * 32), (1, 1, 'short')):
            with self.assertRaises(ValueError):
                CaptureOwner(*values)

    def test_thirty_second_chunks_have_exact_source_bounds_and_snapshot_contains_no_pcm(self):
        model = self.model()
        chunks = self.feed(model, CHUNK_SAMPLES // 1600)
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0].start_sample, chunks[0].end_sample), (0, CHUNK_SAMPLES))
        self.assertEqual(len(chunks[0].pcm), CHUNK_SAMPLES * 2)
        self.assertEqual(len(model.buffer), 0)
        self.assertNotIn('pcm', repr(model.snapshot()))
        self.assertNotIn('8192', repr(chunks[0]))

    def test_pause_seals_short_leg_and_resume_requires_fresh_owner_and_zero_local_offsets(self):
        model = self.model()
        self.feed(model, 1)
        chunk = model.pause()[0]
        self.assertEqual((chunk.start_sample, chunk.end_sample), (0, 1600))
        self.assertTrue(model.complete(chunk, self.result(chunk)))
        model.start(owner(2))
        before = model.snapshot()
        self.assertEqual(model.receive(owner(), 0, 0, PCM), (0, ()))
        self.assertEqual(model.snapshot(), before)
        self.feed(model, 1, who=owner(2))
        next_chunk = model.end()[0]
        self.assertEqual((next_chunk.start_sample, next_chunk.end_sample), (1600, 3200))

    def test_transport_gap_is_visible_and_never_silently_filled_as_transcribed_speech(self):
        model = self.model()
        self.feed(model, 1)
        accepted, chunks = model.receive(owner(), 2, 3200, PCM)
        self.assertEqual(accepted, 1600)
        self.assertEqual(len(chunks), 1)
        gap = model.snapshot()['rows'][1]
        self.assertEqual(
            (gap['state'], gap['start_sample'], gap['end_sample']), ('gap', 1600, 3200)
        )
        self.assertEqual(model.position, 4800)
        self.assertEqual(model.received_samples, 3200)
        self.assertFalse(model.snapshot()['processing_complete'])

    def test_header_mismatch_interrupts_but_old_owner_or_replayed_packet_does_not_touch_current(
        self,
    ):
        model = self.model()
        self.feed(model, 1)
        before = model.snapshot()
        self.assertEqual(model.receive(owner(2), 999, 100000, b'x'), (0, ()))
        self.assertEqual(model.receive(owner(), 0, 0, PCM), (0, ()))
        self.assertEqual(model.snapshot(), before)
        self.assertEqual(model.receive(owner(), 2, 1600, PCM), (0, ()))
        self.assertEqual((model.state, model.reason), ('paused', 'protocol_error'))
        self.assertEqual(model.rows[0]['state'], 'unconfirmed')

    def test_home_or_route_loss_marks_pending_unknown_and_late_cloud_result_cannot_return(self):
        model = self.model()
        self.feed(model, 1)
        chunk = model.pause()[0]
        model.interrupt('route_lost')
        before = model.snapshot()
        self.assertFalse(model.complete(chunk, self.result(chunk)))
        self.assertEqual(model.snapshot(), before)
        self.assertEqual(model.rows[0]['state'], 'unconfirmed')
        model.start(owner(2, route=2))
        self.assertEqual(model.receive(owner(), 1, 1600, PCM), (0, ()))

    def test_ending_needs_actual_receipts_and_home_cannot_undo_confirmed_work(self):
        model = self.model()
        self.feed(model, 1)
        chunk = model.end()[0]
        self.assertFalse(model.snapshot()['processing_complete'])
        self.assertTrue(model.complete(chunk, self.result(chunk)))
        before = model.snapshot()
        self.assertTrue(before['processing_complete'])
        model.interrupt('background')
        self.assertEqual(model.snapshot(), before)
        with self.assertRaises(ValueError):
            model.start(owner(2))

    def test_ended_pending_interruption_stays_terminal_and_never_reopens_old_capture(self):
        model = self.model()
        self.feed(model, 1)
        chunk = model.end()[0]
        model.interrupt('background')
        self.assertEqual(model.state, 'ended')
        self.assertFalse(model.complete(chunk, self.result(chunk)))
        with self.assertRaises(ValueError):
            model.start(owner(2))

    def test_correction_preserves_original_and_invalidates_old_alignment_without_changing_audio(
        self,
    ):
        model = self.model()
        self.feed(model, 1)
        chunk = model.end()[0]
        model.complete(chunk, Transcript('原文', 100, (Span(0, 100, 'S1', '原文'),), 'available'))
        revision = model.revision
        self.assertFalse(model.edit(chunk.ident, revision, '原文'))
        self.assertEqual(model.revision, revision)
        self.assertTrue(model.edit(chunk.ident, revision, '校对后'))
        row = model.snapshot()['rows'][0]
        self.assertEqual(
            (row['text'], row['original'], row['metadata'], row['spans']),
            ('校对后', '原文', 'edited_text', []),
        )
        with self.assertRaises(ValueError):
            model.edit(chunk.ident, revision, '旧页面')
        self.assertEqual(model.received_samples, 1600)

    def test_delete_erases_text_and_buffer_and_rejects_every_late_result(self):
        model = self.model()
        self.feed(model, 1)
        chunk = model.pause()[0]
        model.delete()
        self.assertEqual((model.rows, model.buffer, model.state), ([], bytearray(), 'deleted'))
        self.assertFalse(model.complete(chunk, self.result(chunk)))
        with self.assertRaises(ValueError):
            model.start(owner(2))

    def test_duration_packet_text_and_row_budgets_fail_closed_without_evicting_results(self):
        model = self.model(maximum_samples=3200)
        self.feed(model, 2)
        self.assertEqual(
            (model.state, model.reason, model.position), ('ended', 'duration_limit', 3200)
        )
        self.assertEqual(model.receive(owner(), 2, 3200, PCM), (0, ()))
        model = self.model()
        self.assertEqual(model.receive(owner(), 0, 0, b'x' * (MAX_PACKET_SAMPLES * 2 + 2)), (0, ()))
        self.assertEqual(model.reason, 'protocol_error')
        model = self.model()
        self.feed(model, 1)
        chunk = model.end()[0]
        with patch('meeting_session.MAX_TEXT_BYTES', 4):
            self.assertFalse(model.complete(chunk, self.result(chunk, '超过预算')))
        self.assertEqual(model.rows[0]['reason'], 'text_limit')

    def test_foreign_or_duplicate_completion_never_adds_content(self):
        model = self.model()
        self.feed(model, 1)
        chunk = model.end()[0]
        self.assertFalse(
            model.complete(dataclasses.replace(chunk, meeting='b' * 32), self.result(chunk))
        )
        self.assertTrue(model.complete(chunk, self.result(chunk)))
        before = model.snapshot()
        self.assertFalse(model.complete(chunk, self.result(chunk, '重复')))
        self.assertEqual(model.snapshot(), before)

    def test_alignment_budget_discards_only_optional_spans_and_row_limit_is_terminal(self):
        model = self.model()
        self.feed(model, 1)
        chunk = model.pause()[0]
        with patch('meeting_session.MAX_ALIGNMENT_BYTES', 3):
            self.assertTrue(
                model.complete(
                    chunk, Transcript('原文', 100, (Span(0, 100, 'S1', '原文'),), 'available')
                )
            )
        self.assertEqual(
            (model.rows[0]['text'], model.rows[0]['metadata']), ('原文', 'resource_limit')
        )
        self.assertEqual(model.rows[0]['spans'], [])
        model.start(owner(2))
        self.feed(model, 1, who=owner(2))
        with patch('meeting_session.MAX_ROWS', 1):
            self.assertEqual(model.pause(), ())
        self.assertEqual((model.state, model.reason), ('ended', 'segment_limit'))
        self.assertEqual(len(model.rows), 1)
        self.assertFalse(model.buffer)


if __name__ == '__main__':
    unittest.main()

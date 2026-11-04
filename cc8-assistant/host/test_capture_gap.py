"""Packet and read gaps end only the interrupted, unfinished microphone input."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from bridge import Bridge
from dsp import FRAME
from echo_pipeline import CleanFrame
from protocol import EVENT, MIC, Packet
from test_barge_in import Sink


class CaptureGapTests(unittest.TestCase):
    def bridge(self):
        with patch('threading.Thread.start'):
            b = Bridge(None, None)
        b.session.connection = Sink()
        b.session.active = b.session.awake = True
        b.session.epoch = 7
        b.playback.playing = b.playback.device_can_pause = True
        b.save_status = lambda: None
        endpoint = b.capture.endpoint
        endpoint.near_speech = SimpleNamespace(
            feed=lambda pcm, *_: (bool(np.any(pcm)), bool(np.any(pcm))),
            reset=Mock(),
            correlation=0,
            residual_correlation=0,
        )
        b.capture.echo.process = lambda nanos, raw: CleanFrame(
            nanos, raw, np.zeros(FRAME), raw, raw
        )
        return b

    def mic(self, b, seq, value):
        b.on_packet(
            Packet(
                MIC,
                7,
                seq,
                seq * FRAME,
                1_000_000_000 + seq * 10_000_000,
                np.full(FRAME, value, dtype='<i2').tobytes(),
            )
        )

    def events(self, b):
        return [json.loads(p.data) for p in b.session.connection.packets if p.kind == EVENT]

    def row(self, b, key):
        return next(r for r in b.diagnostics.report()['requests'] if r['request_id'] == key)

    def test_missing_packet_never_joins_two_utterances_or_keeps_a_partial_pause(self):
        b = self.bridge()
        for seq in range(24):
            self.mic(b, seq, 4000)
        endpoint = b.capture.endpoint
        trace = endpoint.voice_trace
        pause = b.playback.pause_state['token']
        self.assertIsNotNone(trace)
        old_generation = b.generation
        for seq in range(25, 138):
            self.mic(b, seq, 8000 if seq < 47 else 0)
        self.assertEqual(b.metrics['sequence_gaps'], 1)
        self.assertEqual(self.row(b, trace)['outcome'], 'cancelled')
        self.assertNotIn('utterance_end', self.row(b, trace)['stages'])
        job = b.recognition.queue.get_nowait()
        self.assertNotEqual(job[6], trace)
        self.assertEqual(set(np.unique(job[1])), {0.0, 8000 / 32768})
        self.assertTrue(b.recognition.queue.empty())
        resumes = [p for p in self.events(b) if p['event'] == 'playback_resume']
        self.assertEqual([p['token'] for p in resumes], [pause])
        self.assertEqual(b.generation, old_generation)
        self.assertTrue(b.playback.playing)
        endpoint.near_speech.reset.assert_called_once()

    def test_read_gap_is_applied_at_the_matched_packet_after_lookahead(self):
        b = self.bridge()
        for seq in range(24):
            self.mic(b, seq, 4000)
        endpoint = b.capture.endpoint
        before = endpoint.voice_trace
        b.session.wake = True
        b.wake.deadline = 1234.0
        b.wake.pending_prefix = True
        serial = b.wake.serial
        b.on_packet(Packet.event('capture_gap', 7, epoch=7, sequence=24, milliseconds=465))
        self.assertEqual(endpoint.voice_trace, before)
        self.assertTrue(b.wake.pending_prefix)
        self.mic(b, 24, 8000)
        self.assertEqual(endpoint.voice_trace, before)
        for seq in range(25, 33):
            self.mic(b, seq, 8000)
        self.assertIsNone(endpoint.voice_trace)
        self.assertEqual(b.wake.serial, serial + 1)
        self.assertFalse(b.wake.pending_prefix)
        self.assertEqual(b.wake.deadline, 1234.0)
        self.assertEqual(b.metrics['sequence_gaps'], 0)
        self.assertEqual(self.row(b, before)['outcome'], 'cancelled')
        self.assertIsNone(b.playback.pause_state)

    def test_gap_cannot_release_pause_owned_by_an_already_queued_utterance(self):
        b = self.bridge()
        for seq in range(133):
            self.mic(b, seq, 4000 if seq < 22 or seq >= 103 else 0)
        earlier = b.recognition.queue.get_nowait()
        token = b.playback.pause_state['token']
        current = b.capture.endpoint.voice_trace
        self.assertIsNotNone(current)
        self.assertFalse(b.capture.endpoint.owns_turn_pause)
        for seq in range(134, 143):
            self.mic(b, seq, 0)
        self.assertIsNone(self.row(b, earlier[6])['outcome'])
        self.assertEqual(self.row(b, current)['outcome'], 'cancelled')
        self.assertEqual(b.playback.pause_state['token'], token)
        self.assertFalse(any(p['event'] == 'playback_resume' for p in self.events(b)))

    def test_old_epoch_and_wrong_sequence_do_not_reset_a_new_partial_utterance(self):
        b = self.bridge()
        for seq in range(24):
            self.mic(b, seq, 4000)
        trace = b.capture.endpoint.voice_trace
        for epoch, seq in ((6, 24), (7, 25)):
            b.on_packet(Packet.event('capture_gap', epoch, epoch=epoch, sequence=seq))
        for seq in range(24, 34):
            self.mic(b, seq, 4000)
        self.assertEqual(b.capture.endpoint.voice_trace, trace)
        self.assertIsNone(self.row(b, trace)['outcome'])

    def test_gap_event_accepts_legacy_but_rejects_partial_or_invalid_identity(self):
        b = self.bridge()
        b.on_packet(Packet.event('capture_gap', 7, milliseconds=500))
        for fields in ({'epoch': 7}, {'sequence': 1}, {'epoch': 7, 'sequence': True}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                b.on_packet(Packet.event('capture_gap', 7, **fields))

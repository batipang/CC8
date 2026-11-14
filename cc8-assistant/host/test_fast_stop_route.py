"""Route boundaries cannot join partial stop commands from different streams."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from bridge import Bridge
from dsp import FRAME
from echo_pipeline import CleanFrame
from fast_stop import FastStopDetector
from protocol import EVENT
from test_barge_in import Sink


class KeywordStream:
    def __init__(self):
        self.pending = None
        self.prefix = False
        self.result = ''

    def accept_waveform(self, rate, samples):
        assert rate == 16000
        self.pending = float(samples[0])


class KeywordBackend:
    """A deterministic partial-word decoder behind the actual streaming adapter."""

    def __init__(self, keyword='停一下'):
        self.keyword = keyword

    def create_stream(self):
        return KeywordStream()

    def is_ready(self, stream):
        return stream.pending is not None

    def decode_stream(self, stream):
        value, stream.pending = stream.pending, None
        stream.result = self.keyword if value == 0.25 and stream.prefix else ''
        stream.prefix = value == 0.125

    def get_result(self, stream):
        return stream.result

    def reset_stream(self, stream):
        stream.prefix = False
        stream.result = ''


class Channel(Sink):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind


class FastStopRouteTests(unittest.TestCase):
    def bridge(self, kind, keyword='停一下'):
        with patch('fast_stop.keyword_spotter', return_value=KeywordBackend(keyword)):
            stop = FastStopDetector(gain=1)
        with patch('threading.Thread.start'):
            b = Bridge(None, None, fast_stop=stop)
        b.session.connection = Channel(kind)
        b.session.active = b.session.awake = True
        b.playback.playing = True
        b.save_status = lambda: None
        b.log = lambda *_args, **_fields: None
        b.capture.endpoint.near_speech = SimpleNamespace(
            feed=lambda *_: (False, False), reset=lambda: None
        )
        return b

    def feed(self, b, token, nanos):
        silence = np.zeros(FRAME)
        b.capture.endpoint.consume(
            CleanFrame(nanos, silence, silence, silence, np.full(FRAME, token))
        )

    def switch(self, b, kinds):
        original = b.session.connection
        for kind in kinds[1:]:
            target = original if kind == original.kind else Channel(kind)
            b.change_transport(target)
        # A later answer is unrelated to the command prefix from the old route.
        b.playback.playing = True
        return b.session.generation

    def transcripts(self, b):
        events = [json.loads(p.data) for p in b.session.connection.packets if p.kind == EVENT]
        return [e['text'] for e in events if e['event'] == 'transcript']

    def test_old_prefix_cannot_cancel_new_answer_after_handover(self):
        for kinds in (('usb', 'wifi'), ('wifi', 'usb'), ('usb', 'wifi', 'usb')):
            with self.subTest(kinds=kinds):
                b = self.bridge(kinds[0])
                self.feed(b, 0.125, 1_000_000_000)
                generation = self.switch(b, kinds)
                self.feed(b, 0.25, 2_000_000_000)
                self.assertTrue(b.playback.playing)
                self.assertEqual(b.session.generation, generation)
                self.assertEqual(self.transcripts(b), [])
                self.assertEqual(b.metrics.get('fast_stop_count', 0), 0)
                self.assertTrue(b.session.active)
                self.assertTrue(b.recognition.queue.empty())

    def test_fresh_command_still_stops_and_session_counters_survive(self):
        for kinds in (('usb', 'wifi'), ('wifi', 'usb'), ('usb', 'wifi', 'usb')):
            with self.subTest(kinds=kinds):
                b = self.bridge(kinds[0])
                self.feed(b, 0.125, 1_000_000_000)
                generation = self.switch(b, kinds)
                stop = b.capture.endpoint.fast_stop
                self.assertEqual(stop.samples, FRAME)
                self.assertEqual(stop.stream_samples, 0)
                self.feed(b, 0.125, 4_000_000_000)
                self.feed(b, 0.25, 4_010_000_000)
                self.assertFalse(b.playback.playing)
                self.assertEqual(b.session.generation, generation + 1)
                self.assertEqual(self.transcripts(b), ['停一下'])
                self.assertEqual(b.metrics['fast_stop_count'], 1)
                self.assertEqual(stop.samples, FRAME * 3)

    def test_reselecting_same_live_channel_does_not_cut_current_command(self):
        b = self.bridge('usb')
        self.feed(b, 0.125, 1_000_000_000)
        generation = b.session.generation
        b.change_transport(b.session.connection)
        self.feed(b, 0.25, 2_000_000_000)
        self.assertFalse(b.playback.playing)
        self.assertEqual(b.session.generation, generation + 1)
        self.assertEqual(self.transcripts(b), ['停一下'])

    def test_report_waits_for_full_stop_utterance_but_keeps_unambiguous_fast_stop(self):
        for keyword, report_current, expected in (
            ('停止', True, []),
            ('停止', False, ['停止']),
            ('停一下', True, ['停一下']),
        ):
            with self.subTest(keyword=keyword, report_current=report_current):
                b = self.bridge('usb', keyword)
                with patch.object(
                    b.chats.voice, 'has_current_snapshot', return_value=report_current
                ):
                    self.feed(b, 0.125, 2_000_000_000)
                    self.feed(b, 0.25, 2_010_000_000)
                self.assertEqual(self.transcripts(b), expected)
                self.assertEqual(b.playback.playing, not expected)


if __name__ == '__main__':
    unittest.main()

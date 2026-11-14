import json
import threading
import unittest
import numpy as np

from bridge import Bridge
from unittest.mock import Mock
from types import SimpleNamespace
from minimax_client import MiniMaxError
from operation_failure import OperationFailure
from protocol import receive, EVENT, SPEAKER
from test_runtime import Fragmented


class StreamPlaybackTests(unittest.TestCase):
    def bridge(self, on_packet=None):
        bridge = Bridge(None, None)
        bridge.session.active = True
        bridge.save_status = lambda: None

        class Sink:
            def __init__(self):
                self.packets = []
                self.callbacks = []

            def sendall(self, data):
                p = receive(Fragmented(data))
                self.packets.append(p)
                if on_packet:
                    # Device-to-host callbacks run on the reader thread, not
                    # recursively inside the host's locked socket write.
                    thread = threading.Thread(target=on_packet, args=(bridge, p))
                    self.callbacks.append(thread)
                    thread.start()
                elif p.kind == EVENT and json.loads(p.data).get('event') == 'play_end':
                    bridge.on_event({'event': 'play_done', 'generation': p.generation})

        bridge.session.connection = Sink()
        return bridge

    def test_streamed_pcm_preserves_frame_count_and_completion(self):
        b = self.bridge()
        pcm = np.arange(480, dtype=np.int16)
        self.assertTrue(b.play_stream(iter([pcm[:170], pcm[170:]]), 'test', b.session.generation))
        packets = b.session.connection.packets
        raw = b''.join(p.data for p in packets if p.kind == SPEAKER)
        self.assertEqual(raw, pcm.astype('<i2').tobytes())
        self.assertEqual(json.loads(packets[-1].data)['frames'], 480)

    def test_interrupt_closes_stream_and_sends_no_more_old_audio(self):
        class Chunks:
            closed = False

            def __iter__(self):
                return self

            def __next__(self):
                return np.ones(3200, dtype=np.int16)

            def close(self):
                self.closed = True

        def on_packet(b, p):
            if p.kind == SPEAKER:
                b.cancel('test_interrupt')

        b = self.bridge(on_packet)
        chunks = Chunks()
        self.assertFalse(b.play_stream(chunks, 'test', b.session.generation))
        self.assertTrue(chunks.closed)
        for thread in b.session.connection.callbacks:
            thread.join(1)
        self.assertEqual(len([p for p in b.session.connection.packets if p.kind == SPEAKER]), 1)
        self.assertFalse(
            any(
                p.kind == EVENT and json.loads(p.data).get('event') == 'play_end'
                for p in b.session.connection.packets
            )
        )

    def test_empty_tts_keeps_synthesis_stage_and_allows_next_request(self):
        b = self.bridge()
        generation = b.generation
        b.diagnostics.begin('text', generation)
        b.speech = SimpleNamespace(stream=lambda *_: iter([np.zeros(0, dtype=np.int16)]))
        with self.assertRaises(OperationFailure) as failure:
            b.speak('test', generation)
        self.assertEqual(
            (failure.exception.stage, failure.exception.kind), ('tts', 'provider_or_runtime')
        )
        self.assertEqual(
            b.diagnostics.failure_record(generation),
            {'stage': 'tts', 'kind': 'provider_or_runtime'},
        )
        self.assertFalse(b.playback.playing)
        self.assertEqual(b.session.connection.packets, [])
        b.speech.stream = lambda *_: iter([np.ones(480, dtype=np.int16)])
        next_generation = b.cancel('test_next_request')
        self.assertTrue(b.speak('next', next_generation))

    def test_empty_prerendered_and_live_audio_report_playback_stage(self):
        for source in ('prerendered', 'live'):
            with self.subTest(source=source):
                b = self.bridge()
                generation = b.generation
                b.diagnostics.begin('text', generation)
                with self.assertRaises(OperationFailure) as failure:
                    if source == 'prerendered':
                        b.play(np.zeros(0, dtype=np.int16), 'test', generation)
                    else:
                        b.playback.play_stream(iter(()), '', generation, live=Mock())
                self.assertEqual(failure.exception.stage, 'playback')
                self.assertEqual(b.diagnostics.failure_record(generation)['stage'], 'playback')
                self.assertFalse(b.playback.playing)
                self.assertEqual(b.session.connection.packets, [])

    def test_iterator_close_error_does_not_replace_primary_send_error(self):
        b = self.bridge()
        generation = b.generation
        b.diagnostics.begin('text', generation)
        timing = SimpleNamespace(rows=lambda: [], publish=None)
        closed = []

        def chunks():
            try:
                yield np.ones(3200, dtype=np.int16)
            finally:
                closed.append(True)
                raise RuntimeError('PRIVATE_STREAM_CLOSE_DETAIL')

        original = b.send

        def send(packet):
            if packet.kind == SPEAKER:
                raise BrokenPipeError('PRIVATE_DEVICE_ERROR')
            original(packet)

        b.send = send
        b.log = Mock()
        with self.assertRaises(OperationFailure) as failure:
            b.play_stream(chunks(), 'test', generation, timing=timing)
        self.assertEqual(
            (failure.exception.stage, failure.exception.kind), ('transport', 'transport_or_io')
        )
        self.assertEqual(closed, [True])
        self.assertIsNone(timing.publish)
        self.assertFalse(b.playback.playing)
        self.assertEqual(
            b.diagnostics.failure_record(generation),
            {'stage': 'transport', 'kind': 'transport_or_io'},
        )
        b.log.assert_any_call('playback_iterator_close_failed', error_type='RuntimeError')
        self.assertNotIn('PRIVATE', str(b.log.call_args_list))

    def test_cleanup_error_does_not_undo_confirmed_playback(self):
        class Chunks:
            def __iter__(self):
                return self

            def __next__(self):
                return next(self.pcm)

            def close(self):
                self.closed = True
                raise OSError('PRIVATE_CLOSE_DETAIL')

        b = self.bridge()
        generation = b.generation
        b.diagnostics.begin('text', generation)
        b.log = Mock()
        chunks = Chunks()
        chunks.pcm = iter([np.ones(480, dtype=np.int16)])
        chunks.closed = False
        self.assertTrue(b.play_stream(chunks, 'test', generation))
        self.assertTrue(chunks.closed)
        self.assertFalse(b.playback.playing)
        self.assertIsNone(b.diagnostics.failure_record(generation))
        b.log.assert_any_call('playback_iterator_close_failed', error_type='OSError')
        self.assertNotIn('PRIVATE', str(b.log.call_args_list))

    def test_cloud_failure_after_partial_audio_cancels_playback(self):
        def failing():
            yield np.zeros(4800, dtype=np.int16)
            raise MiniMaxError('Simulated network stream failure')

        b = self.bridge()
        generation = b.session.generation
        with self.assertRaises(OperationFailure) as failure:
            b.play_stream(failing(), 'test', generation)
        self.assertEqual(failure.exception.stage, 'tts')
        self.assertGreater(b.session.generation, generation)
        self.assertFalse(b.playback.playing)
        self.assertTrue(b.session.active)
        events = [json.loads(p.data) for p in b.session.connection.packets if p.kind == EVENT]
        self.assertTrue(any(e.get('event') == 'cancel' for e in events))
        self.assertTrue(any(e.get('phase') == 'error' for e in events))


if __name__ == '__main__':
    unittest.main()

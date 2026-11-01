import json
import threading
import unittest
import numpy as np

from bridge import Bridge
from minimax_client import MiniMaxError
from protocol import receive, EVENT, SPEAKER
from test_runtime import Fragmented


class StreamPlaybackTests(unittest.TestCase):
    def bridge(self, on_packet=None):
        bridge = Bridge(None, None)
        bridge.active = True
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

        bridge.connection = Sink()
        return bridge

    def test_streamed_pcm_preserves_frame_count_and_completion(self):
        b = self.bridge()
        pcm = np.arange(480, dtype=np.int16)
        self.assertTrue(b.play_stream(iter([pcm[:170], pcm[170:]]), 'test', b.generation))
        packets = b.connection.packets
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
        self.assertFalse(b.play_stream(chunks, 'test', b.generation))
        self.assertTrue(chunks.closed)
        for thread in b.connection.callbacks:
            thread.join(1)
        self.assertEqual(len([p for p in b.connection.packets if p.kind == SPEAKER]), 1)
        self.assertFalse(
            any(
                p.kind == EVENT and json.loads(p.data).get('event') == 'play_end'
                for p in b.connection.packets
            )
        )

    def test_cloud_failure_after_partial_audio_cancels_playback(self):
        def failing():
            yield np.zeros(4800, dtype=np.int16)
            raise MiniMaxError('Simulated network stream failure')

        b = self.bridge()
        generation = b.generation
        with self.assertRaises(MiniMaxError):
            b.play_stream(failing(), 'test', generation)
        self.assertGreater(b.generation, generation)
        self.assertFalse(b.playing)
        self.assertTrue(b.active)
        events = [json.loads(p.data) for p in b.connection.packets if p.kind == EVENT]
        self.assertTrue(any(e.get('event') == 'cancel' for e in events))
        self.assertTrue(any(e.get('phase') == 'error' for e in events))


if __name__ == '__main__':
    unittest.main()

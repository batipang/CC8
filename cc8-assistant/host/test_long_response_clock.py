"""Actual packet clock continuity and observed response blocks over long waits."""

import json
import unittest
from unittest.mock import patch
import numpy as np

from bridge import Bridge
from echo_pipeline import CleanFrame
from protocol import Packet, MIC, RENDER, EVENT, receive
from test_runtime import Fragmented
from test_playback_evidence import public_signal, scene
from test_response_output_window import Clock


class LongResponseClockTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.enterContext(patch('playback_evidence.threading.Timer', self.clock.timer))
        self.enterContext(patch('capture_pipeline.time.monotonic', lambda: self.clock.now))
        self.enterContext(patch('playback_controller.time.sleep', self.clock.advance))
        self.bridge = Bridge(None, None)
        self.bridge.session.active = True
        self.bridge.session.epoch = 7
        self.bridge.session.generation = 101
        self.bridge.save_status = lambda: None
        self.capture = self.bridge.capture
        self.capture.capture_lookahead_ns = 0
        self.capture.input_quality.reset(enabled=True)
        self.capture.echo.process = lambda n, raw: CleanFrame(n, raw, raw, raw, raw)
        self.capture.endpoint.consume = lambda frame: None
        self.sequence = 0
        self.nanos = 1_000_000_000
        bridge = self.bridge

        class Device:
            def sendall(self, data):
                packet = receive(Fragmented(data))
                if packet.kind == EVENT and json.loads(packet.data)['event'] == 'play_end':
                    bridge.on_event({'event': 'play_done', 'generation': packet.generation})

        self.bridge.session.connection = Device()
        self.addCleanup(self.capture.clear_response_probe)

    def microphone(self, pcm):
        self.bridge.on_packet(
            Packet(MIC, 7, self.sequence, 0, self.nanos, (pcm * 32768).astype('<i2').tobytes())
        )
        self.sequence += 1
        self.nanos += round(len(pcm) * 1e9 / 16000)

    def silence_until(self, end):
        while self.nanos < end:
            count = min(320, round((end - self.nanos) * 16000 / 1e9))
            self.assertGreater(count, 0)
            self.microphone(np.zeros(count))
        self.assertEqual(self.nanos, end)

    def prepare(self):
        self.microphone(np.zeros(320))
        token = self.capture.arm_response_probe(1)
        self.silence_until(4_400_000_000)
        self.capture.bind_response_probe('一加一等于几？', 101, self.nanos)
        return token

    def answer(self):
        self.silence_until(29_800_000_000)
        self.clock.advance(25.4)
        reference = public_signal()
        self.assertTrue(
            self.bridge.playback.play_stream(iter([(reference * 32768).astype('<i2')]), '二。', 101)
        )
        microphone = scene(reference)
        for frame in range(0, len(microphone), 320):
            self.microphone(microphone[frame : frame + 320])
            if 3200 <= frame < 19200:
                offset = frame - 3200
                self.bridge.on_packet(
                    Packet(
                        RENDER,
                        101,
                        offset // 320,
                        offset,
                        30_000_000_000 + round(offset * 1e9 / 16000),
                        (reference[offset : offset + 320] * 32768).astype('<i2').tobytes(),
                    )
                )

    def test_continuous_real_packet_clock_supports_observed_block_after_long_model_wait(self):
        token = self.prepare()
        self.answer()
        receipt = self.capture.read_response_probe(token)
        self.assertEqual(receipt['status'], 'observed')
        self.assertAlmostEqual(receipt['last_voiced_to_confirmed_output_block_ms'], 25720, delta=1)
        proof = receipt['interval_clock']
        self.assertEqual(proof['version'], 1)
        self.assertEqual(proof['discontinuities'], 0)
        self.assertGreaterEqual(proof['coverage_ms'], 25720)
        self.assertLessEqual(proof['coverage_ms'], 120002)
        self.assertEqual(self.capture.read_response_probe(token), {'status': 'unavailable'})
        self.assertFalse(self.capture.evidence.mic.size)

    def test_waiting_packet_gap_cannot_authorize_a_long_interval(self):
        token = self.prepare()
        self.sequence += 1
        self.answer()
        receipt = self.capture.read_response_probe(token)
        self.assertEqual(receipt['status'], 'unconfirmed')
        self.assertNotIn('last_voiced_to_confirmed_output_block_ms', receipt)
        self.assertNotIn('interval_clock', receipt)

    def test_gap_after_matching_still_revokes_the_long_clock_proof(self):
        token = self.prepare()
        self.answer()
        self.capture.note_read_gap(None, None)
        receipt = self.capture.read_response_probe(token)
        self.assertEqual(receipt['status'], 'unconfirmed')
        self.assertEqual(receipt['reason'], 'capture_clock_gap')
        self.assertNotIn('last_voiced_to_confirmed_output_block_ms', receipt)


if __name__ == '__main__':
    unittest.main()

"""Count only accepted microphone PCM, without affecting audio or retaining samples."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from bridge import Bridge
from dsp import FRAME
from protocol import MIC, MIC_PAIRED, RENDER, Packet


class InputQualityFlowTests(unittest.TestCase):
    def bridge(self, paired=False):
        with patch('threading.Thread.start'):
            bridge = Bridge(None, None)
        bridge.save_status = lambda: None
        bridge.log = lambda *args, **kwargs: None
        bridge.capture.reset_session()
        bridge.session.active = True
        bridge.session.epoch = 7
        bridge.session.capture_frontend = 'paired' if paired else 'mono'
        bridge.capture.paired_reference = paired
        bridge.capture.paired_peer_ready = paired
        bridge.capture.capture_lookahead_ns = 0
        bridge.capture.echo.process = Mock(return_value=None)
        return bridge

    def feed(self, bridge, samples, sequence=0, *, epoch=7, nanos=None, reference=None):
        samples = np.asarray(samples, dtype='<i2')
        if reference is None:
            kind, data = MIC, samples.tobytes()
        else:
            kind = MIC_PAIRED
            data = np.column_stack((samples, reference)).astype('<i2').tobytes()
        stamp = 1_000_000_000 + sequence * 10_000_000 if nanos is None else nanos
        bridge.on_packet(Packet(kind, epoch, sequence, 0, stamp, data))

    def quality(self, bridge):
        health = bridge.health()
        self.assertIn('input_quality', health, 'P0 needs pre-DSP input quality evidence')
        return health['input_quality']

    def test_missing_samples_are_unmeasured_instead_of_fabricated_silence(self):
        value = self.quality(self.bridge())
        self.assertEqual(value['status'], 'unmeasured')
        self.assertEqual(value['samples'], 0)
        self.assertIsNone(value['rms_dbfs'])
        self.assertIsNone(value['clipped_ratio'])
        self.assertIsNone(value['peak_int16'])
        self.assertIsNone(value['frontend'])

    def test_exact_signed_boundaries_are_counted_before_echo_processing(self):
        bridge = self.bridge()
        pcm = np.zeros(FRAME, dtype='<i2')
        pcm[:6] = [-32768, 32760, 32759, 32767, 0, -32760]
        self.feed(bridge, pcm)
        value = self.quality(bridge)
        self.assertEqual(value['samples'], FRAME)
        self.assertEqual(value['clipped_samples'], 4)
        self.assertEqual(value['zero_samples'], FRAME - 5)
        self.assertEqual(value['peak_int16'], 32768)
        self.assertEqual(value['max_consecutive_clipped_samples'], 2)
        self.assertAlmostEqual(value['clipped_ratio'], 4 / FRAME)
        expected = 10 * np.log10(np.mean(pcm.astype(float) ** 2) / 32768**2)
        self.assertAlmostEqual(value['rms_dbfs'], expected, places=2)
        np.testing.assert_array_equal(pcm[:6], [-32768, 32760, 32759, 32767, 0, -32760])
        self.assertEqual(value['scope'], 'received_mic_pcm_16khz_before_host_dsp')
        self.assertIs(value['device_adc_clipping_measured'], False)
        self.assertEqual(value['frontend'], 'mono')
        bridge.capture.echo.process.assert_called_once()
        np.testing.assert_array_equal(
            bridge.capture.echo.process.call_args.args[1], pcm.astype(float) / 32768
        )

    def test_paired_reference_extremes_never_count_as_microphone_clipping(self):
        bridge = self.bridge(paired=True)
        self.feed(bridge, np.zeros(FRAME), reference=np.full(FRAME, 32767))
        value = self.quality(bridge)
        self.assertEqual(
            (value['samples'], value['clipped_samples'], value['peak_int16']), (FRAME, 0, 0)
        )
        self.feed(bridge, np.full(FRAME, -32768), 1, reference=np.full(FRAME, -32768))
        value = self.quality(bridge)
        self.assertEqual((value['samples'], value['clipped_samples']), (2 * FRAME, FRAME))
        self.assertEqual(value['frontend'], 'paired')

    def test_consecutive_runs_cross_valid_packets_but_stop_at_all_gap_kinds(self):
        for gap in ('sequence', 'read', 'clock'):
            with self.subTest(gap=gap):
                bridge = self.bridge()
                self.feed(bridge, np.full(FRAME, 32767))
                self.feed(bridge, np.full(FRAME, -32768), 1)
                self.assertEqual(self.quality(bridge)['max_consecutive_clipped_samples'], 2 * FRAME)
                if gap == 'read':
                    bridge.capture.note_read_gap(7, 2)
                sequence = 3 if gap == 'sequence' else 2
                nanos = 1_030_000_000 if gap == 'clock' else None
                self.feed(bridge, np.full(FRAME, 32767), sequence, nanos=nanos)
                value = self.quality(bridge)
                self.assertEqual(value['samples'], 3 * FRAME)
                self.assertEqual(value['max_consecutive_clipped_samples'], 2 * FRAME)
                self.assertEqual(value['discontinuities'], 1)

    def test_run_combines_only_a_packet_tail_and_next_packet_prefix(self):
        bridge = self.bridge()
        first, second = np.zeros(FRAME), np.zeros(FRAME)
        first[-3:] = 32767
        second[:4] = -32768
        self.feed(bridge, first)
        self.feed(bridge, second, 1)
        value = self.quality(bridge)
        self.assertEqual(value['clipped_samples'], 7)
        self.assertEqual(value['max_consecutive_clipped_samples'], 7)

    def test_rejected_alignment_breaks_a_run_without_counting_invalid_audio(self):
        bridge = self.bridge()
        self.feed(bridge, np.full(FRAME, 32767))
        self.feed(bridge, np.full(FRAME - 1, 32767), 1, nanos=1_005_000_000)
        self.feed(bridge, np.full(FRAME, 32767), 2, nanos=1_010_000_000)
        value = self.quality(bridge)
        self.assertEqual(value['samples'], 2 * FRAME)
        self.assertEqual(value['max_consecutive_clipped_samples'], FRAME)
        self.assertEqual(value['discontinuities'], 1)

    def test_stale_epoch_and_render_never_change_microphone_quality(self):
        bridge = self.bridge()
        self.feed(bridge, np.full(FRAME, 32767), epoch=6)
        bridge.capture.echo.add_render = Mock()
        bridge.on_packet(
            Packet(
                RENDER,
                bridge.generation,
                0,
                0,
                1_000_000_000,
                np.full(FRAME, 32767, dtype='<i2').tobytes(),
            )
        )
        self.assertEqual(self.quality(bridge)['samples'], 0)
        self.feed(bridge, np.zeros(FRAME))
        self.assertEqual(self.quality(bridge)['clipped_samples'], 0)

    def test_stop_retains_only_final_numbers_and_restart_or_route_resets_them(self):
        bridge = self.bridge()
        self.feed(bridge, np.full(FRAME, 32767))
        bridge.session.active = False
        bridge.capture.close()
        self.feed(bridge, np.full(FRAME, 32767), 1)
        self.assertEqual(self.quality(bridge)['samples'], FRAME)
        bridge.capture.reset_session()
        bridge.session.active = True
        bridge.session.epoch = 8
        self.assertEqual(self.quality(bridge)['status'], 'unmeasured')
        self.feed(bridge, np.zeros(FRAME), epoch=8)
        self.assertEqual(self.quality(bridge)['samples'], FRAME)
        bridge.capture.reset_transport('wifi')
        self.assertEqual(self.quality(bridge)['status'], 'unmeasured')

    def test_reset_or_close_during_decode_cannot_publish_old_samples(self):
        for action in ('reset_session', 'close'):
            with self.subTest(action=action):
                bridge = self.bridge()
                self.feed(bridge, np.zeros(FRAME))
                original = np.frombuffer

                def interrupted(*args, **kwargs):
                    getattr(bridge.capture, action)()
                    return original(*args, **kwargs)

                with patch('capture_pipeline.np.frombuffer', side_effect=interrupted):
                    self.feed(bridge, np.full(FRAME, 32767), 1)
                value = self.quality(bridge)
                self.assertEqual(value['samples'], 0 if action == 'reset_session' else FRAME)
                self.assertEqual(value['clipped_samples'], 0)

    def test_health_snapshot_and_private_status_are_content_free_copies(self):
        bridge = self.bridge()
        self.feed(bridge, np.full(FRAME, 1234))
        value = self.quality(bridge)
        value['samples'] = -1
        self.assertEqual(self.quality(bridge)['samples'], FRAME)
        self.assertTrue(
            all(v is None or type(v) in (str, bool, int, float) for v in value.values())
        )
        with tempfile.TemporaryDirectory() as directory:
            bridge.reporting.hooks = replace(
                bridge.reporting.hooks, private_directory=lambda: Path(directory)
            )
            bridge.reporting.save_status()
            stored = json.loads((Path(directory) / 'status.json').read_text())
        self.assertEqual(stored['input_quality'], self.quality(bridge))

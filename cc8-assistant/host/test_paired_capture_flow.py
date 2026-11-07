"""Negotiation, packet ownership and AEC warm-up with the actual host pipeline."""

import threading
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import numpy as np
from bridge import Bridge
from protocol import Packet, MIC, MIC_PAIRED, validate_device_event, receive, decode_event
from echo_pipeline import EchoPipeline, EchoHooks, CleanFrame, MAX_CALIBRATION_FRAMES
from paired_capture import PairedReferenceFilter
from session_controller import SessionController
from test_runtime import Fragmented, Sink


def pair_event(epoch=7):
    return {
        'event': 'session_start',
        'epoch': epoch,
        'capture_frontend': 'paired',
        'sample_rate': 16000,
        'capture_sample_rate': 48000,
        'capture_channels': 4,
        'source': 7,
    }


class PairNegotiationTests(unittest.TestCase):
    def controller(self, preference='paired'):
        return SessionController(SimpleNamespace(), capture_frontend=preference)

    def test_both_preference_and_exact_capability_required(self):
        for preference in ('mono', 'paired'):
            for capability in (None, False, True, '1', 0, 1, 2):
                with self.subTest(preference=preference, capability=capability):
                    session = self.controller(preference)
                    peer = {} if capability is None else {'paired_capture_version': capability}
                    expected = (
                        'paired'
                        if preference == 'paired' and type(capability) is int and capability == 1
                        else 'mono'
                    )
                    self.assertEqual(session.negotiate_capture_frontend(peer), expected)
        with self.assertRaises(ValueError):
            self.controller('unbounded')

    def test_legacy_and_declared_fallback_do_not_enable_paired_data(self):
        session = self.controller()
        session.negotiate_capture_frontend({})
        self.assertTrue(session.accepts_capture_frontend({'epoch': 7}))
        self.assertFalse(session.accepts_capture_frontend(pair_event()))
        session.negotiate_capture_frontend({'paired_capture_version': 1})
        self.assertTrue(session.accepts_capture_frontend(pair_event()))
        self.assertTrue(
            session.accepts_capture_frontend(
                {
                    'epoch': 7,
                    'capture_frontend': 'mono',
                    'capture_fallback_reason': 'paired_format_unavailable',
                }
            )
        )
        for reason in ('credential-value', {}, True):
            self.assertFalse(
                session.accepts_capture_frontend(
                    {'epoch': 7, 'capture_frontend': 'mono', 'capture_fallback_reason': reason}
                )
            )

    def test_actual_paired_shape_is_bound_and_cannot_change_during_rebind(self):
        session = self.controller()
        session.negotiate_capture_frontend({'paired_capture_version': 1})
        for key, value in [
            ('sample_rate', 48000),
            ('capture_sample_rate', 16000),
            ('capture_channels', 2),
            ('source', 1),
            ('capture_channels', True),
            ('capture_fallback_reason', 'platform_api'),
        ]:
            event = {**pair_event(), key: value}
            self.assertFalse(session.accepts_capture_frontend(event))
        session.active = True
        session.epoch = 7
        session.capture_frontend = 'paired'
        self.assertFalse(session.accepts_capture_frontend({'epoch': 7, 'capture_frontend': 'mono'}))
        self.assertTrue(session.accepts_capture_frontend(pair_event()))
        self.assertFalse(session.accepts_capture_frontend({'epoch': 8, 'capture_frontend': 'mono'}))
        self.assertTrue(
            session.accepts_capture_frontend(
                {
                    'epoch': 8,
                    'capture_frontend': 'mono',
                    'capture_fallback_reason': 'paired_format_unavailable',
                }
            )
        )

    def test_device_event_validation_rejects_bad_types_before_any_capture(self):
        for key, value in [
            ('paired_capture_version', True),
            ('capture_sample_rate', True),
            ('capture_channels', -1),
            ('capture_frontend', []),
            ('capture_fallback_reason', {'api_key': 'must-not-log'}),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_device_event({**pair_event(), key: value})
        self.assertEqual(
            validate_device_event({'event': 'device_status', 'capture_frontend': 'none'})[
                'capture_frontend'
            ],
            'none',
        )

    def bridge(self):
        with patch('threading.Thread.start'):
            bridge = Bridge(None, None, capture_frontend='paired')
        bridge.save_status = lambda: None
        bridge.session.connection = Sink()
        bridge.session.active = True
        bridge.session.epoch = 7
        return bridge

    def test_invalid_peer_metadata_does_not_publish_format_or_ack_capabilities(self):
        bridge = self.bridge()
        peer = {
            'paired_capture_version': 1,
            'capture_reject_version': 1,
            'capture_start_ack_version': 1,
        }
        with patch.object(
            bridge.visual, 'update_peer', side_effect=ValueError('invalid peer fixture')
        ):
            with self.assertRaises(ValueError):
                bridge.session.ready(peer)
        self.assertEqual(bridge.session.capture_frontend_requested, 'mono')
        self.assertFalse(bridge.capture.paired_peer_ready)
        self.assertFalse(bridge.session.capture_start_ack)

    def test_real_event_route_preserves_paired_capture_and_never_restarts_after_stop(self):
        with patch('threading.Thread.start'), patch('bridge.WebRtcEchoCanceller'):
            bridge = Bridge(None, None, capture_frontend='paired')
            bridge.save_status = lambda: None
            usb, wifi = Sink(), Sink()
            usb.kind, wifi.kind = 'usb', 'wifi'
            ready = {
                'event': 'device_ready',
                'capture_reject_version': 1,
                'capture_start_ack_version': 1,
                'paired_capture_version': 1,
            }
            bridge.change_transport(usb)
            bridge.on_event({**ready, 'capture_active': False})
            hello = next(
                decode_event(packet.data)
                for packet in usb.packets
                if decode_event(packet.data)['event'] == 'hello'
            )
            self.assertEqual(hello['capture_frontend'], 'paired')
            self.assertFalse(bridge.session.active)
            claim = {
                **pair_event(),
                'capture_id': '1' * 32,
                'capture_start_token': hello['capture_start_token'],
            }
            bridge.on_event(claim)
            self.assertTrue(bridge.session.active)
            self.assertTrue(bridge.capture.paired_reference)
            self.assertEqual(bridge.health()['capture_frontend'], 'paired')
            bridge.change_transport(wifi)
            self.assertFalse(bridge.capture.paired_peer_ready)
            # A preserved recorder cannot submit any format before the new
            # route is registered; discard early PCM without killing that route.
            bridge.on_packet(Packet(MIC_PAIRED, 7, data=bytes(1280)))
            self.assertTrue(bridge.session.capture_route_pending)
            self.assertEqual(len(bridge.capture.pending_mic), 0)
            bridge.on_event({**ready, 'capture_active': True})
            rebound = {**claim, 'event': 'session_rebind', 'capture_active': True}
            bridge.on_event(rebound)
            self.assertTrue(bridge.session.active)
            self.assertTrue(bridge.capture.paired_reference)
            self.assertTrue(bridge.capture.paired_peer_ready)
            bridge.on_packet(Packet(MIC_PAIRED, 7, data=bytes(1280)))
            self.assertEqual(len(bridge.capture.pending_mic), 1)
            bridge.on_event({**claim, 'event': 'session_stop'})
            self.assertFalse(bridge.session.active)
            self.assertFalse(bridge.capture.paired_reference)
            self.assertEqual(bridge.health()['capture_frontend'], 'none')
            bridge.on_event(rebound)
            bridge.on_event(claim)
            bridge.on_packet(Packet(MIC_PAIRED, 7, data=bytes(1280)))
            self.assertFalse(bridge.session.active)
            self.assertFalse(bridge.capture.pending_mic)

    def test_unnegotiated_and_mixed_payloads_do_not_reach_dsp(self):
        bridge = self.bridge()
        bridge.capture.echo.process = Mock()
        with self.assertRaises(ValueError):
            bridge.on_packet(Packet(MIC_PAIRED, 7, data=bytes(640)))
        bridge.capture.paired_reference = True
        with self.assertRaises(ValueError):
            bridge.on_packet(Packet(MIC_PAIRED, 7, data=bytes(640)))
        bridge.capture.paired_peer_ready = True
        with self.assertRaises(ValueError):
            bridge.on_packet(Packet(MIC_PAIRED, 7, data=bytes(4)))
        with self.assertRaises(ValueError):
            bridge.on_packet(Packet(MIC, 7, data=bytes(320)))
        bridge.on_packet(Packet(MIC_PAIRED, 6, data=bytes(640)))
        bridge.capture.echo.process.assert_not_called()
        self.assertEqual(len(bridge.capture.pending_mic), 0)

    def test_logical_time_and_two_channels_survive_lookahead(self):
        bridge = self.bridge()
        bridge.capture.paired_reference = True
        bridge.capture.paired_peer_ready = True
        bridge.capture.endpoint.consume = Mock()
        calls = []

        def process(stamp, raw, reference):
            calls.append((stamp, raw.copy(), reference.copy()))
            return CleanFrame(stamp, raw, np.zeros(160), raw, raw, reference)

        bridge.capture.echo.process = process
        data = np.tile(np.array([[1000, -2000]], dtype='<i2'), (160, 1)).tobytes()
        for sequence in range(9):
            bridge.on_packet(
                Packet(
                    MIC_PAIRED,
                    7,
                    sequence,
                    sequence * 160,
                    1_000_000_000 + sequence * 10_000_000,
                    data,
                )
            )
        self.assertEqual(bridge.capture.last_received_mic_end, 1_090_000_000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 1_000_000_000)
        np.testing.assert_array_equal(calls[0][1], np.full(160, 1000 / 32768))
        np.testing.assert_array_equal(calls[0][2], np.full(160, -2000 / 32768))
        self.assertEqual(bridge.metrics['mic_frames'], 160)
        bridge.capture.reset_transport('wifi')
        self.assertTrue(bridge.capture.paired_reference)
        self.assertFalse(bridge.capture.paired_peer_ready)
        self.assertFalse(bridge.capture.pending_mic)
        with self.assertRaises(ValueError):
            bridge.on_packet(Packet(MIC_PAIRED, 7, 9, 1440, 1_090_000_000, data))
        bridge.capture.close()
        self.assertFalse(bridge.capture.paired_reference)

    def test_paired_framing_rejects_half_pairs_and_preserves_legacy(self):
        for count in (0, 1, 2, 3, 6, 6404):
            with self.subTest(count=count), self.assertRaises(ValueError):
                Packet(MIC_PAIRED, data=bytes(count)).encode()
        packet = Packet(MIC_PAIRED, 9, 11, 320, 1_000_000_000, bytes(1280))
        self.assertEqual(receive(Fragmented(packet.encode())), packet)
        legacy = Packet(MIC, 9, data=bytes(640))
        self.assertEqual(receive(Fragmented(legacy.encode())), legacy)


class Processor:
    def __init__(self, fail=False):
        self.calls = []
        self.closed = False
        self.fail = fail

    def process(self, mic, reference):
        if self.fail:
            raise RuntimeError('native fixture failed')
        self.calls.append((mic.copy(), reference.copy()))
        return mic.copy()

    def close(self):
        self.closed = True


class PairedEchoTests(unittest.TestCase):
    def echo(self):
        self.processors = []
        self.metrics = {}

        def factory(**options):
            processor = Processor()
            self.processors.append(processor)
            return processor

        hooks = EchoHooks(
            lock=threading.RLock(),
            active=lambda: True,
            valid=lambda generation: True,
            factory=factory,
            fast_stop_enabled=lambda: True,
            metric=lambda name, value: self.metrics.update({name: value}),
            log=lambda *args, **kwargs: None,
        )
        return EchoPipeline(hooks)

    def test_native_pair_uses_conditioned_reference_but_keeps_digital_gate_ownership(self):
        echo = self.echo()
        echo.open()
        echo.timeline = SimpleNamespace(at=lambda stamp, count: np.full(count, 0.2))
        raw = np.linspace(-0.1, 0.1, 160)
        reference = np.linspace(-0.002, 0.002, 160)
        expected = PairedReferenceFilter().process(raw, reference)
        frame = echo.process(1_000_000_000, raw, reference)
        for processor in self.processors:
            np.testing.assert_array_equal(processor.calls[0][0], expected[0])
            np.testing.assert_array_equal(processor.calls[0][1], expected[1])
        np.testing.assert_array_equal(frame.reference, np.full(160, 0.2))
        self.assertIs(frame.device_reference, reference)
        np.testing.assert_array_equal(frame.raw, raw)
        echo.record_calibration_frame(frame)
        self.assertEqual(len(echo.calibration_frames[0]), 3)
        reference.fill(0)
        self.assertTrue(np.any(echo.calibration_frames[0][2]))

    def test_calibration_preserves_digital_gate_alignment_without_shifting_paired_aec(self):
        echo = self.echo()
        echo.open()
        digital_stamps = []

        def timeline(stamp, count):
            digital_stamps.append(stamp)
            return np.full(count, 0.2)

        echo.timeline = SimpleNamespace(at=timeline)
        for index in range(110):
            echo.record_calibration_frame(
                echo.process(index * 10000000, np.full(160, 0.01), np.full(160, 0.001))
            )
        old = list(self.processors)

        def estimate(mic, render):
            np.testing.assert_array_equal(mic, np.full(110 * 160, 0.01))
            np.testing.assert_array_equal(render, np.full(110 * 160, 0.2))
            return 320, 0.8

        with patch('echo_pipeline.estimate_reference_shift', side_effect=estimate):
            self.assertTrue(echo.align_reference(1))
        self.assertTrue(all(processor.closed for processor in old))
        self.assertFalse(echo.calibration_frames)
        self.assertEqual(self.metrics['capture_reference_alignment'], 'paired_grid')
        self.assertEqual(self.metrics['reference_shift_scope'], 'digital_gate')
        self.assertEqual(self.metrics['reference_shift_samples'], 320)
        self.assertEqual(echo.reference_shift_ns, 20000000)
        self.assertEqual(len(self.processors[-1].calls), 110)
        expected = PairedReferenceFilter()
        for _ in range(110):
            expected.process(np.full(160, 0.01), np.full(160, 0.001))
        conditioned = expected.process(np.full(160, 0.02), np.full(160, 0.002))
        frame = echo.process(2000000000, np.full(160, 0.02), np.full(160, 0.002))
        self.assertEqual(digital_stamps[-1], 1980000000)
        np.testing.assert_array_equal(frame.reference, np.full(160, 0.2))
        np.testing.assert_array_equal(self.processors[-1].calls[-1][1], conditioned[1])

    def test_mixed_calibration_and_failed_rebuild_leave_working_pair(self):
        echo = self.echo()
        echo.open()
        working = echo.aec
        filter_owner = echo.paired_filter
        echo.calibration_frames = [(0, np.zeros(160))] * 99 + [(1, np.zeros(160), np.zeros(160))]
        with self.assertRaises(ValueError):
            echo.align_reference(1)
        self.assertIs(echo.aec, working)
        self.assertIs(echo.paired_filter, filter_owner)
        echo.calibration_frames = [(i, np.zeros(160), np.zeros(160)) for i in range(100)]
        failed = []

        def factory(**options):
            processor = Processor(fail=True)
            failed.append(processor)
            return processor

        echo.hooks = replace(echo.hooks, factory=factory)
        with self.assertRaises(RuntimeError):
            echo.align_reference(1)
        self.assertTrue(all(processor.closed for processor in failed))
        self.assertFalse(working.closed)
        self.assertIs(echo.aec, working)
        self.assertIs(echo.paired_filter, filter_owner)

    def test_transport_reset_and_close_do_not_retain_old_filter_state(self):
        echo = self.echo()
        echo.open()
        echo.process(1, np.ones(160), np.ones(160))
        echo.reset_transport()
        echo.process(2, np.zeros(160), np.zeros(160))
        for processor in self.processors:
            self.assertFalse(np.any(processor.calls[-1][0]))
            self.assertFalse(np.any(processor.calls[-1][1]))
        echo.close()
        self.assertIsNone(echo.process(3, np.zeros(160), np.zeros(160)))

    def test_long_pending_calibration_has_bounded_owned_audio(self):
        echo = self.echo()
        for index in range(1000):
            echo.record_calibration_frame(
                CleanFrame(
                    index, np.zeros(160), np.zeros(160), np.zeros(160), np.zeros(160), np.zeros(160)
                )
            )
        self.assertEqual(len(echo.calibration_frames), MAX_CALIBRATION_FRAMES)
        self.assertEqual(echo.calibration_frames[0][0], 1000 - MAX_CALIBRATION_FRAMES)


if __name__ == '__main__':
    unittest.main()

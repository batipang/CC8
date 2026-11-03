"""Public synthetic PCM through the actual observer and capture packet router."""

import json
import queue
import socket
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from announce import AnnouncementServer
from bridge import Bridge
from echo_pipeline import CleanFrame
from playback_evidence import PlaybackEvidence, MIC_LIMIT, PROBE_TEXT
from protocol import Packet, MIC, RENDER, EVENT, SPEAKER, receive
from test_runtime import Fragmented
from test_barge_in import Sink

RATE = 16000
START = 2_000_000_000


def public_signal():
    # Non-repeating deterministic broadband signal, not a user recording.
    rng = np.random.default_rng(608)
    noise = rng.normal(0, 0.12, 16000)
    return np.convolve(noise, [0.2, 0.6, 0.2], mode='same')


def scene(reference, delay=1920, gain=0.3, *, noise=0.001):
    rng = np.random.default_rng(702)
    mic = rng.normal(0, noise, 32000) + 0.04
    start = 3200 + delay
    mic[start : start + len(reference)] += gain * reference
    return mic


def feed(observer, reference, microphone, *, render_first=False):
    def render():
        for frame in range(0, len(reference), 320):
            observer.render(
                101, frame, START + round(frame * 1e9 / RATE), reference[frame : frame + 320]
            )

    if render_first:
        render()
    for frame in range(0, len(microphone), 320):
        stamp = START - 200_000_000 + round(frame * 1e9 / RATE)
        observer.microphone(7, stamp, microphone[frame : frame + 320])
        if not render_first and 3200 <= frame < 3200 + len(reference):
            offset = frame - 3200
            observer.render(
                101, offset, START + round(offset * 1e9 / RATE), reference[offset : offset + 320]
            )


class AcousticEvidenceTests(unittest.TestCase):
    def test_gain_dc_room_filter_and_packet_delivery_order(self):
        reference = public_signal()
        room = np.convolve(reference, [0.8, 0.1, 0.04], mode='full')[: len(reference)]
        for first in (True, False):
            for delay in (0, 1920, 9600):
                with self.subTest(render_first=first, delay=delay):
                    observer = PlaybackEvidence()
                    probe = observer.arm(101, 7)
                    feed(observer, reference, scene(room, delay, -0.4), render_first=first)
                    self.assertTrue(probe.done.is_set())
                    result = probe.result
                    self.assertEqual(result['status'], 'observed')
                    self.assertAlmostEqual(result['matched_window_delay_ms'], delay / 16, delta=1)
                    self.assertFalse(result['first_audible_onset_measured'])
                    self.assertFalse(result['human_dialogue_latency_measured'])
                    self.assertEqual(observer.mic.size + observer.reference.size, 0)
                    self.assertNotIn('pcm', json.dumps(result))

    def test_silence_noise_unrelated_and_periodic_are_not_success(self):
        ref = public_signal()
        tone = 0.15 * np.sin(np.arange(16000) * 2 * np.pi * 400 / RATE)
        cases = (
            (ref, np.zeros(32000), 'not_observed'),
            (ref, scene(np.random.default_rng(4).normal(0, 0.1, 16000)), 'not_observed'),
            (tone, scene(tone), 'ambiguous'),
            (np.zeros(24000), np.zeros(32000), 'insufficient_reference'),
        )
        for reference, microphone, expected in cases:
            with self.subTest(expected=expected):
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7)
                feed(observer, reference, microphone, render_first=True)
                self.assertEqual(probe.result['status'], expected)
                self.assertIsNone(probe.result.get('matched_window_delay_ms'))

    def test_stale_packets_do_not_complete_or_destroy_current_probe(self):
        observer = PlaybackEvidence()
        old = observer.arm(100, 6)
        observer.cancel()
        new = observer.arm(101, 7)
        observer.render(100, 0, START, np.ones(16000))
        observer.microphone(6, START, np.ones(16000))
        observer.cancel('expired', old)
        self.assertFalse(new.done.is_set())
        self.assertEqual(len(observer.mic) + len(observer.reference), 0)
        feed(observer, public_signal(), scene(public_signal()))
        self.assertEqual(new.result['status'], 'observed')
        observer.cancel()
        self.assertEqual(new.result['status'], 'observed')

    def test_deadline_frees_buffers_without_any_more_packets(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7, seconds=0.03)
        samples = np.ones(320)
        observer.microphone(7, START, samples)
        self.assertTrue(probe.done.wait(1))
        self.assertEqual(probe.result['status'], 'expired')
        self.assertEqual(observer.mic.size + observer.reference.size, 0)
        self.assertTrue(np.all(samples == 1))
        for seconds in (0, -1, True, float('nan'), float('inf'), 21):
            with self.assertRaises(ValueError):
                observer.arm(101, 7, seconds=seconds)

    def test_memory_bound_clock_gaps_and_missing_first_render(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        samples = np.full(1600, 0.1)
        for i in range(100):
            observer.microphone(7, START + i * 100_000_000, samples)
        self.assertEqual(len(observer.mic), MIC_LIMIT)
        observer.microphone(7, START + 11_000_000_000, samples)
        self.assertEqual(probe.result['status'], 'capture_clock_gap')
        probe = observer.arm(101, 7)
        observer.render(101, 320, START, samples)
        self.assertEqual(probe.result['status'], 'render_gap')
        probe = observer.arm(101, 7)
        observer.render(101, 0, START, samples)
        for i in range(1, 5):
            observer.render(101, i * 1600, START + i * 200_000_000, samples)
        self.assertFalse(probe.done.is_set())
        observer.cancel('expired')
        self.assertEqual(probe.result['status'], 'expired')
        self.assertEqual(probe.result['render_clock_resets'], 4)
        self.assertEqual(probe.result['max_render_clock_step_ms'], 100)

    def test_clock_refinement_or_real_gap_uses_only_a_new_contiguous_window(self):
        reference = public_signal()
        for actual_gap in (False, True):
            with self.subTest(actual_gap=actual_gap):
                observer = PlaybackEvidence()
                probe = observer.arm(101, 7)
                first_stamp = START if actual_gap else START + 300_000_000
                observer.render(101, 0, first_stamp, reference[:1600])
                shift = 200_000_000 if actual_gap else 0
                for frame in range(1600, len(reference), 320):
                    observer.render(
                        101,
                        frame,
                        START + round(frame * 1e9 / RATE) + shift,
                        reference[frame : frame + 320],
                    )
                if actual_gap:
                    mic = np.zeros(40000)
                    start = 3200 + 1920
                    mic[start : start + 1600] = reference[:1600] * 0.3
                    tail = start + 1600 + 3200
                    mic[tail : tail + len(reference) - 1600] = reference[1600:] * 0.3
                else:
                    mic = scene(reference)
                for frame in range(0, len(mic), 320):
                    observer.microphone(
                        7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
                    )
                self.assertEqual(probe.result['status'], 'observed')
                self.assertEqual(probe.result['render_clock_resets'], 1)
                self.assertAlmostEqual(probe.result['matched_window_delay_ms'], 120, delta=1)
                self.assertFalse(probe.result['first_audible_onset_measured'])

    def test_cannot_stitch_two_short_templates_across_a_clock_jump(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        ref = public_signal()[:3200]
        observer.render(101, 0, START, ref[:1600])
        observer.render(101, 1600, START + 300_000_000, ref[1600:])
        mic = np.zeros(32000)
        mic[5120:8320] = ref
        for frame in range(0, len(mic), 320):
            observer.microphone(
                7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
            )
        self.assertFalse(probe.done.is_set())
        self.assertEqual(len(observer.reference), 1600)
        observer.cancel('expired')
        self.assertEqual(probe.result['status'], 'expired')
        self.assertEqual(probe.result['render_clock_resets'], 1)

    def test_repeated_clock_refinements_can_still_produce_a_stable_later_window(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        ref = public_signal()
        for i in range(5):
            observer.render(101, i * 160, START + i * 100_000_000, ref[i * 160 : (i + 1) * 160])
        observer.render(101, 800, START + 500_000_000, ref[800:])
        mic = np.zeros(32000)
        mic[13120:28320] = ref[800:] * 0.3
        for frame in range(0, len(mic), 320):
            observer.microphone(
                7, START - 200_000_000 + round(frame * 1e9 / RATE), mic[frame : frame + 320]
            )
        self.assertEqual(probe.result['status'], 'observed')
        self.assertEqual(probe.result['render_clock_resets'], 5)
        self.assertEqual(probe.result['max_render_clock_step_ms'], 90)
        self.assertAlmostEqual(probe.result['matched_window_delay_ms'], 120, delta=1)

    def test_invalid_samples_and_missing_search_coverage_are_explicit(self):
        observer = PlaybackEvidence()
        for method, expected in (
            (observer.microphone, 'invalid_capture'),
            (observer.render, 'invalid_render'),
        ):
            probe = observer.arm(101, 7)
            if expected == 'invalid_capture':
                method(7, START, np.array([float('nan')]))
            else:
                method(101, 0, START, np.array([float('inf')]))
            self.assertEqual(probe.result['status'], expected)
        probe = observer.arm(101, 7)
        observer.render(101, 0, START, public_signal())
        observer.microphone(7, START + 200_000_000, np.ones(32000))
        self.assertEqual(probe.result['status'], 'insufficient_capture_coverage')

    def test_cancel_and_feed_race_cannot_retain_samples_or_complete_twice(self):
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        barrier = threading.Barrier(2)

        def producer():
            barrier.wait()
            feed(observer, public_signal(), scene(public_signal()))

        thread = threading.Thread(target=producer)
        thread.start()
        barrier.wait()
        observer.cancel()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(probe.done.is_set())
        self.assertIn(probe.result['status'], ('observed', 'cancelled'))
        self.assertEqual(len(observer.mic) + len(observer.reference), 0)


class ProbeIntegrationTests(unittest.TestCase):
    def bridge(self):
        bridge = Bridge(None, None)
        bridge.session.connection = Sink()
        bridge.save_status = lambda: None
        return bridge

    def test_owner_probe_cannot_open_mic_and_rejects_busy_capture(self):
        bridge = self.bridge()
        with self.assertRaises(RuntimeError):
            bridge.playback_probe()
        self.assertFalse(bridge.session.active)
        bridge.session.active = True
        for state in ('playing', 'calibrating'):
            owner = bridge.playback if state == 'playing' else bridge.calibration
            attr = 'playing' if state == 'playing' else 'active'
            setattr(owner, attr, True)
            with self.assertRaises(RuntimeError):
                bridge.playback_probe()
            setattr(owner, attr, False)
        self.assertIsNone(bridge.capture.evidence.current)

    def test_queue_failure_ends_trace_and_wipes_probe(self):
        bridge = self.bridge()
        bridge.session.active = True
        with patch.object(bridge.response, 'offer', side_effect=queue.Full):
            with self.assertRaisesRegex(RuntimeError, 'queue busy'):
                bridge.playback_probe()
        self.assertIsNone(bridge.capture.evidence.current)
        self.assertIsNone(bridge.announcements.generation)
        self.assertFalse(bridge.capture.measuring)
        self.assertEqual(bridge.diagnostics.report()['requests'][-1]['outcome'], 'failed')
        self.assertTrue(bridge.session.active)

    def test_server_deadline_and_invalid_shape_never_open_capture(self):
        from announce import Announcement

        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        job = Announcement(101, PROBE_TEXT)
        cancelled = []
        finished = []
        stub = SimpleNamespace(
            playback_probe=lambda: (job, probe),
            cancel_playback_probe=lambda job, probe: (
                cancelled.append(job.generation),
                observer.cancel(),
            ),
            finish_playback_probe=finished.append,
        )
        server = AnnouncementServer(stub)
        reader, writer = socket.socketpair()
        try:
            with patch('announce.time.monotonic', side_effect=(0, 23)):
                self.assertEqual(
                    server.playback_probe(reader, {'operation': 'playback_probe'})['status'],
                    'timeout',
                )
            self.assertEqual(cancelled, [101])
            self.assertEqual(finished, [probe])
            with self.assertRaises(ValueError):
                server.playback_probe(reader, {'operation': 'playback_probe', 'text': 'arbitrary'})
        finally:
            reader.close()
            writer.close()
            observer.cancel()

    def test_late_client_cleanup_cannot_cancel_or_unmute_a_successor_probe(self):
        bridge = self.bridge()
        bridge.session.active = True
        with patch.object(bridge.response, 'offer'):
            old_job, old_probe = bridge.playback_probe()
            bridge.cancel('new_request')
            new_job, new_probe = bridge.playback_probe()
            bridge.cancel_playback_probe(old_job, old_probe)
            bridge.finish_playback_probe(old_probe)
            self.assertTrue(bridge.valid(new_job.generation))
            self.assertIs(bridge.capture.probe, new_probe)
            self.assertFalse(new_probe.done.is_set())
            bridge.cancel_playback_probe(new_job, new_probe)
            self.assertTrue(new_probe.done.is_set())
            self.assertIsNone(bridge.capture.probe)

    def test_framed_packets_use_raw_microphone_and_ignore_old_generation(self):
        bridge = self.bridge()
        bridge.session.active = True
        bridge.session.epoch = 7
        probe = bridge.capture.evidence.arm(bridge.generation, 7)
        bridge.capture.echo.process = lambda stamp, raw: CleanFrame(
            stamp, raw, raw, raw * 0, raw * 0
        )
        bridge.capture.endpoint.consume = lambda frame: None
        reference = public_signal()
        mic = scene(reference)
        for frame in range(0, 32000, 320):
            bridge.on_packet(Packet(MIC, 6, frame // 320, frame, START, b'\0' * 640))
            bridge.on_packet(
                Packet(
                    MIC,
                    7,
                    frame // 320,
                    frame,
                    START - 200_000_000 + round(frame * 1e9 / RATE),
                    (mic[frame : frame + 320] * 32768).astype('<i2').tobytes(),
                )
            )
            if 3200 <= frame < 19200:
                offset = frame - 3200
                pcm = (reference[offset : offset + 320] * 32768).astype('<i2').tobytes()
                bridge.on_packet(Packet(RENDER, bridge.generation - 1, 0, offset, START, pcm))
                bridge.on_packet(
                    Packet(
                        RENDER,
                        bridge.generation,
                        0,
                        offset,
                        START + round(offset * 1e9 / RATE),
                        pcm,
                    )
                )
        self.assertEqual(probe.result['status'], 'observed')
        self.assertAlmostEqual(probe.result['matched_window_delay_ms'], 120, delta=1)
        for action in (
            lambda: bridge.cancel('test'),
            bridge.capture.close,
            lambda: bridge.capture.reset_transport('wifi'),
            bridge.capture.reset_session,
        ):
            probe = bridge.capture.evidence.arm(bridge.generation, 7)
            action()
            self.assertTrue(probe.done.is_set())
            self.assertNotEqual(probe.result['status'], 'observed')

    def test_real_owner_socket_waits_for_evidence_and_disconnect_cancels_only_owned_job(self):
        from announce import Announcement

        job = Announcement(101, PROBE_TEXT)
        observer = PlaybackEvidence()
        probe = observer.arm(101, 7)
        entered = threading.Event()
        cancelled = []

        def start():
            entered.set()
            return job, probe

        def cancel(owned, ticket):
            self.assertIs(owned, job)
            self.assertIs(ticket, probe)
            cancelled.append(True)
            observer.cancel()

        stub = SimpleNamespace(playback_probe=start, cancel_playback_probe=cancel)
        stub.finish_playback_probe = lambda probe: None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'probe.sock'
            server = AnnouncementServer(stub, path)
            server.start()
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(2)
                client.connect(str(path))
                client.sendall(b'{"operation":"playback_probe"}\n')
                self.assertTrue(entered.wait(1))
                feed(observer, public_signal(), scene(public_signal()))
                job.result = 'completed'
                job.done.set()
                result = json.loads(client.recv(4096))
                self.assertEqual(result['evidence']['status'], 'observed')
                client.close()
                self.assertFalse(cancelled)
                entered.clear()
                job.done.clear()
                probe = observer.arm(101, 7)
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(path))
                client.sendall(b'{"operation":"playback_probe"}\n')
                self.assertTrue(entered.wait(1))
                client.close()
                self.assertTrue(probe.done.wait(1))
                self.assertTrue(cancelled)
            finally:
                server.close()
                observer.cancel()

    def test_full_bridge_probe_rpc_with_actual_framing_dispatch_and_completion(self):
        reference = public_signal()

        class Speech:
            def stream(self, text, current):
                self_text.append(text)
                yield (reference * 32768).astype('<i2')

        self_text = []
        bridge = Bridge(Speech(), None)
        bridge.session.active = True
        bridge.session.epoch = 7
        bridge.save_status = lambda: None
        endpoint_frames = []
        bridge.capture.endpoint.consume = endpoint_frames.append
        bridge.capture.echo.process = lambda stamp, raw: CleanFrame(stamp, raw, raw, raw, raw)

        class AcousticLoopback:
            def sendall(self, data):
                packet = receive(Fragmented(data))
                if packet.kind == SPEAKER:
                    bridge.on_packet(
                        Packet(
                            RENDER,
                            packet.generation,
                            packet.sequence,
                            packet.frame,
                            START + round(packet.frame * 1e9 / RATE),
                            packet.data,
                        )
                    )
                if packet.kind == EVENT and json.loads(packet.data)['event'] == 'play_end':
                    microphone = scene(reference)
                    for frame in range(0, len(microphone), 320):
                        bridge.on_packet(
                            Packet(
                                MIC,
                                7,
                                frame // 320,
                                frame,
                                START - 200_000_000 + round(frame * 1e9 / RATE),
                                (microphone[frame : frame + 320] * 32768).astype('<i2').tobytes(),
                            )
                        )
                    bridge.on_event({'event': 'play_done', 'generation': packet.generation})

        bridge.session.connection = AcousticLoopback()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'probe.sock'
            server = AnnouncementServer(bridge, path)
            server.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(3)
                    client.connect(str(path))
                    client.sendall(b'{"operation":"playback_probe"}\n')
                    result = json.loads(client.recv(4096))
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['evidence']['status'], 'observed')
                self.assertEqual(self_text, [PROBE_TEXT])
                self.assertEqual(endpoint_frames, [])
                self.assertTrue(bridge.session.active)
                self.assertIsNone(bridge.capture.probe)
                self.assertIsNone(bridge.capture.evidence.current)
                self.assertFalse(bridge.capture.measuring)
            finally:
                server.close()
                bridge.cancel_playback_observation()

    def test_lookahead_measurement_tail_is_not_delivered_as_a_new_turn(self):
        bridge = self.bridge()
        bridge.session.active = True
        bridge.session.epoch = 7
        seen = []
        bridge.capture.endpoint.consume = seen.append
        bridge.capture.echo.process = lambda stamp, raw: CleanFrame(stamp, raw, raw, raw, raw)
        probe = bridge.capture.begin_probe(bridge.generation, 7)
        for sequence in range(3):
            stamp = START + sequence * 20_000_000
            bridge.on_packet(Packet(MIC, 7, sequence, sequence * 320, stamp, b'\0' * 640))
        bridge.capture.end_probe(probe)
        for sequence in range(3, 7):
            stamp = START + sequence * 20_000_000
            bridge.on_packet(Packet(MIC, 7, sequence, sequence * 320, stamp, b'\0' * 640))
        self.assertEqual(seen, [])
        for sequence in range(7, 10):
            stamp = START + sequence * 20_000_000
            bridge.on_packet(Packet(MIC, 7, sequence, sequence * 320, stamp, b'\0' * 640))
        self.assertTrue(seen)
        self.assertTrue(all(frame.nanos >= START + 60_000_000 for frame in seen))
        bridge.capture.reset_transport('usb')
        self.assertEqual(bridge.capture.discard_endpoint_before_ns, 0)


if __name__ == '__main__':
    unittest.main()

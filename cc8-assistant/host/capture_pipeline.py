"""Own bounded packet lookahead and dispatch 16-kHz/10-ms frames by device time."""

from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import time
from typing import Callable
import numpy as np
from diagnostics import TraceBook
from dsp import RATE, FRAME, level
from protocol import MIC, RENDER
from session_ports import EventSink, MetricSink
from echo_pipeline import EchoPipeline
from endpoint_pipeline import EndpointPipeline
from playback_evidence import PlaybackEvidence


@dataclass(frozen=True)
class CaptureHooks:
    generation: Callable[[], int]
    epoch: Callable[[], int]
    active: Callable[[], bool]
    calibrating: Callable[[], bool]
    diagnostics: Callable[[], TraceBook]
    metric: MetricSink
    add_metric: Callable[[str, int], None]
    log: EventSink
    save_status: Callable[[], None]


class CapturePipeline:
    def __init__(self, hooks: CaptureHooks, echo: EchoPipeline, endpoint: EndpointPipeline):
        self.hooks = hooks
        self.echo = echo
        self.endpoint = endpoint
        self.capture_lookahead_ns = 80_000_000
        self.pending_mic = deque()
        self.last_sequence = None
        self.last_status = 0
        self.evidence = PlaybackEvidence()
        self.probe = None
        self.measuring = False
        self.last_received_mic_end = 0
        self.discard_endpoint_before_ns = 0

    def begin_probe(self, generation, epoch):
        if self.probe is not None:
            raise RuntimeError('Playback measurement already active')
        # Publish suppression before arm/timer creation can yield to ingress.
        self.measuring = True
        try:
            self.endpoint.reset_voice_activity()
            probe = self.evidence.arm(generation, epoch)
        except Exception:
            self.measuring = False
            raise
        self.probe = probe
        return probe

    def end_probe(self, probe=None, status='cancelled'):
        if self.probe is not None and (probe is None or self.probe is probe):
            self.evidence.cancel(status, self.probe)
            self.probe.render_clock.cancel()
            # Lookahead still contains measurement audio. It must not become
            # a fresh conversation after the owner releases the test lease.
            # Do not mutate the ingress-owned deque from an IPC thread. A
            # device-clock watermark drains its old packets through AEC only.
            self.discard_endpoint_before_ns = self.last_received_mic_end
            self.measuring = False
            self.probe = None
            self.endpoint.reset_voice_activity()

    def begin_render_clock(self, generation, playback_id):
        probe = self.probe
        return bool(
            probe is not None
            and probe.generation == generation
            and probe.render_clock.begin(generation, playback_id)
        )

    def complete_render_clock(self, generation, playback_id, value, expected_frames):
        probe = self.probe
        if probe is not None and probe.generation == generation:
            probe.render_clock.accept(generation, playback_id, value, expected_frames)

    def reset_transport(self, kind):
        self.end_probe(status='transport_changed')
        self.evidence.cancel('transport_changed')
        self.last_received_mic_end = self.discard_endpoint_before_ns = 0
        self.capture_lookahead_ns = 120_000_000 if kind == 'wifi' else 80_000_000
        self.echo.reset_transport()
        self.pending_mic.clear()
        self.endpoint.reset_transport()
        self.last_sequence = None

    def reset_session(self):
        self.end_probe(status='session_changed')
        self.evidence.cancel('session_changed')
        self.last_received_mic_end = self.discard_endpoint_before_ns = 0
        self.echo.reset_session()
        self.endpoint.reset_session()
        self.last_sequence = None
        self.pending_mic.clear()

    def close(self):
        self.end_probe(status='capture_closed')
        self.evidence.cancel('capture_closed')
        self.endpoint.reset_voice_activity()
        self.echo.close()
        self.pending_mic.clear()
        self.echo.clear_calibration_frames()

    def process_microphone(self, packet):
        # Reference/capture use device timestamps, never network arrival time.
        measurement_packet = self.measuring or packet.nanos < self.discard_endpoint_before_ns
        if self.last_sequence is not None and packet.sequence != self.last_sequence + 1:
            self.evidence.cancel('capture_packet_gap')
            self.hooks.add_metric('sequence_gaps', 1)
            self.hooks.log('capture_packet_gap')
        self.last_sequence = packet.sequence
        mic = np.frombuffer(packet.data, dtype='<i2').astype(float) / 32768
        if len(mic) % FRAME:
            self.evidence.cancel('invalid_capture')
            self.hooks.log('unaligned_capture_frame', frames=len(mic))
            return
        self.evidence.microphone(packet.generation, packet.nanos, mic)
        self.hooks.add_metric('mic_frames', len(mic))
        for offset in range(0, len(mic), FRAME):
            stamp = packet.nanos + round(offset * 1e9 / RATE)
            frame = self.echo.process(stamp, mic[offset : offset + FRAME])
            if frame is None:
                return
            self.hooks.metric('input_dbfs', round(level(frame.raw), 1))
            self.hooks.metric('output_dbfs', round(level(frame.cleaned), 1))
            if self.hooks.calibrating():
                self.echo.record_calibration_frame(frame)
                continue
            if measurement_packet or self.measuring:
                # An explicit fixed-phrase measurement is not a conversation:
                # keep AEC running, but never turn its reflection into an ASR /
                # Dot tool request or a barge-in. Owner Stop still cancels it.
                continue
            self.endpoint.consume(frame)
        if time.monotonic() - self.last_status > 1:
            self.last_status = time.monotonic()
            self.hooks.save_status()

    def on_packet(self, p):
        if p.kind == RENDER:
            if p.generation != self.hooks.generation():
                return
            self.hooks.diagnostics().mark(p.generation, 'render_ack')
            pcm = np.frombuffer(p.data, dtype='<i2').astype(float) / 32768
            self.evidence.render(p.generation, p.frame, p.nanos, pcm)
            self.echo.add_render(p, pcm)
            self.hooks.add_metric('render_frames', len(pcm))
            self.hooks.metric('render_clock_discontinuities', self.echo.timeline.discontinuities)
            return
        if p.kind != MIC or not self.hooks.active() or p.generation != self.hooks.epoch():
            return
        self.last_received_mic_end = p.nanos + round(len(p.data) / 2 * 1e9 / RATE)
        if self.pending_mic and p.nanos <= self.pending_mic[-1].nanos:
            raise ValueError('Non-monotonic microphone timestamp')
        if len(self.pending_mic) >= 64:
            raise ValueError('Microphone lookahead exceeded')
        self.pending_mic.append(p)
        while self.pending_mic and p.nanos - self.pending_mic[0].nanos >= self.capture_lookahead_ns:
            self.process_microphone(self.pending_mic.popleft())

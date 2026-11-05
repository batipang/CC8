"""Own bounded packet lookahead and dispatch 16-kHz/10-ms frames by device time."""

from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import time
import uuid
from typing import Callable
import numpy as np
from diagnostics import TraceBook
from dsp import RATE, FRAME, level
from protocol import MIC, RENDER
from session_ports import EventSink, MetricSink
from echo_pipeline import EchoPipeline
from endpoint_pipeline import EndpointPipeline
from playback_evidence import PlaybackEvidence, Probe
from qa_speech import ALL_QUESTIONS, normalize_question


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


@dataclass
class ResponseAcoustic:
    token: str
    epoch: int
    question: str
    expires: float
    status: str = 'waiting'
    probe: Probe | None = None


class CapturePipeline:
    def __init__(self, hooks: CaptureHooks, echo: EchoPipeline, endpoint: EndpointPipeline):
        self.hooks = hooks
        self.echo = echo
        self.endpoint = endpoint
        self.capture_lookahead_ns = 80_000_000
        self.pending_mic = deque()
        self.last_sequence = None
        self.next_read_gap = None
        self.last_status = 0
        self.evidence = PlaybackEvidence()
        self.probe = None
        self.response_probe: ResponseAcoustic | None = None
        self.measuring = False
        self.last_received_mic_end = 0
        self.discard_endpoint_before_ns = 0

    def begin_probe(self, generation, epoch):
        if self.probe is not None or self.live_response_probe() is not None:
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

    def live_response_probe(self):
        owner = self.response_probe
        if owner is not None and (
            time.monotonic() >= owner.expires or owner.epoch != self.hooks.epoch()
        ):
            self.clear_response_probe()
            return None
        return owner

    def arm_response_probe(self, case):
        if type(case) is not int or not 1 <= case <= len(ALL_QUESTIONS):
            raise ValueError('Invalid public QA case')
        if (
            not self.hooks.active()
            or self.probe is not None
            or self.live_response_probe() is not None
        ):
            raise RuntimeError('Response acoustic test requires idle active capture')
        token = uuid.uuid4().hex
        self.response_probe = ResponseAcoustic(
            token,
            self.hooks.epoch(),
            normalize_question(ALL_QUESTIONS[case - 1]),
            time.monotonic() + 120,
        )
        return token

    def bind_response_probe(self, text, generation, input_end_ns):
        owner = self.live_response_probe()
        if owner is None or owner.status != 'waiting':
            return
        if normalize_question(text) != owner.question:
            owner.status = 'unmatched_input'
            return
        if type(input_end_ns) is not int or input_end_ns < 0:
            owner.status = 'input_clock_unavailable'
            return
        remaining = owner.expires - time.monotonic()
        if remaining <= 0:
            owner.status = 'expired'
            return
        try:
            owner.probe = self.evidence.arm(
                generation, owner.epoch, seconds=min(20, remaining), input_end_ns=input_end_ns
            )
        except RuntimeError:
            owner.status = 'probe_setup_failed'
            return
        owner.status = 'observing'

    def read_response_probe(self, token):
        owner = self.live_response_probe()
        if owner is None or token != owner.token:
            return {'status': 'unavailable'}
        probe = owner.probe
        if probe is None:
            if owner.status != 'waiting':
                self.clear_response_probe()
            return {'status': owner.status}
        if not probe.done.is_set():
            return {'status': owner.status}
        leading = probe.result['leading_acoustic_evidence']
        measured = leading.get('last_voiced_to_confirmed_output_block_ms')
        self.clear_response_probe()
        if measured is None:
            reason = probe.result['status']
            if reason == 'observed':
                reason = leading['status']
                if reason == 'observed':
                    reason = 'interval_out_of_bounds'
            return {'status': 'unconfirmed', 'generation': probe.generation, 'reason': reason}
        return {
            'status': 'observed',
            'generation': probe.generation,
            'last_voiced_to_confirmed_output_block_ms': measured,
        }

    def clear_response_probe(self):
        if self.response_probe is not None:
            probe = self.response_probe.probe
            self.response_probe = None
            if probe is not None:
                self.evidence.cancel(probe=probe)

    def end_probe(self, probe=None, status='cancelled'):
        if self.probe is not None and (probe is None or self.probe is probe):
            self.evidence.cancel(status, self.probe)
            self.probe.render_clock.cancel()
            # Lookahead still contains measurement audio. It must not become
            # a fresh conversation after the owner releases the test lease.
            # Do not mutate the ingress-owned deque from an IPC thread. A
            # device-clock watermark drains its old packets through AEC only.
            self.discard_buffered_endpoint_audio()
            self.measuring = False
            self.probe = None
            self.endpoint.reset_voice_activity()

    def discard_buffered_endpoint_audio(self):
        # Only the ingress thread drains lookahead. Preserve its AEC frames
        # while fencing audio already received at the session boundary.
        self.discard_endpoint_before_ns = self.last_received_mic_end

    def endpoint_audio_current(self, nanos):
        return not self.measuring and nanos >= self.discard_endpoint_before_ns

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
        self.clear_response_probe()
        self.end_probe(status='transport_changed')
        self.evidence.cancel('transport_changed')
        self.last_received_mic_end = self.discard_endpoint_before_ns = 0
        self.capture_lookahead_ns = 120_000_000 if kind == 'wifi' else 80_000_000
        self.echo.reset_transport()
        self.pending_mic.clear()
        self.endpoint.reset_transport()
        self.last_sequence = None
        self.next_read_gap = None

    def reset_session(self):
        self.clear_response_probe()
        self.end_probe(status='session_changed')
        self.evidence.cancel('session_changed')
        self.last_received_mic_end = self.discard_endpoint_before_ns = 0
        self.echo.reset_session()
        self.endpoint.reset_session()
        self.last_sequence = None
        self.next_read_gap = None
        self.pending_mic.clear()

    def close(self):
        self.clear_response_probe()
        self.end_probe(status='capture_closed')
        self.evidence.cancel('capture_closed')
        self.endpoint.reset_voice_activity()
        self.echo.close()
        self.pending_mic.clear()
        self.echo.clear_calibration_frames()
        self.next_read_gap = None

    def note_read_gap(self, epoch, sequence):
        if epoch is None:
            # Legacy device events had no epoch or following packet identity.
            self.evidence.cancel('capture_read_gap')
        elif self.hooks.active() and epoch == self.hooks.epoch():
            self.next_read_gap = sequence

    def process_microphone(self, packet, *, read_gap=False):
        # Reference/capture use device timestamps, never network arrival time.
        exclude_from_endpoint = not self.endpoint_audio_current(packet.nanos)
        sequence_gap = self.last_sequence is not None and packet.sequence != self.last_sequence + 1
        if sequence_gap:
            self.hooks.add_metric('sequence_gaps', 1)
            self.hooks.log('capture_packet_gap')
        self.last_sequence = packet.sequence
        if sequence_gap or read_gap:
            self.evidence.cancel('capture_read_gap' if read_gap else 'capture_packet_gap')
            self.endpoint.discard_capture_gap()
        mic = np.frombuffer(packet.data, dtype='<i2').astype(float) / 32768
        if len(mic) % FRAME:
            self.evidence.cancel('invalid_capture')
            if not (sequence_gap or read_gap):
                self.endpoint.discard_capture_gap()
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
            if exclude_from_endpoint or not self.endpoint_audio_current(stamp):
                # AEC still receives frames outside the current speech boundary.
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
        if self.pending_mic and p.nanos <= self.pending_mic[-1][0].nanos:
            raise ValueError('Non-monotonic microphone timestamp')
        if len(self.pending_mic) >= 64:
            raise ValueError('Microphone lookahead exceeded')
        self.last_received_mic_end = max(
            self.last_received_mic_end, p.nanos + round(len(p.data) / 2 * 1e9 / RATE)
        )
        read_gap = p.sequence == self.next_read_gap
        self.next_read_gap = None
        self.pending_mic.append((p, read_gap))
        while (
            self.pending_mic and p.nanos - self.pending_mic[0][0].nanos >= self.capture_lookahead_ns
        ):
            packet, read_gap = self.pending_mic.popleft()
            self.process_microphone(packet, read_gap=read_gap)

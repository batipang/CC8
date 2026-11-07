"""Own render reference, alignment samples and native echo processor lifetime."""

from __future__ import annotations
from contextlib import ExitStack
from dataclasses import dataclass
import threading
from typing import Callable, ContextManager, Protocol, TypeAlias
import numpy as np
from numpy.typing import NDArray
from dsp import RATE, FRAME, RenderTimeline, estimate_reference_shift
from session_ports import EventSink, MetricSink
from paired_capture import PairedReferenceFilter

MAX_CALIBRATION_FRAMES = 750

FloatFrame: TypeAlias = NDArray[np.float32] | NDArray[np.float64]


class EchoProcessor(Protocol):
    def process(self, mic: FloatFrame, reference: FloatFrame) -> FloatFrame: ...
    def close(self) -> None: ...


class EchoFactory(Protocol):
    def __call__(self, *, noise_suppression: bool = True) -> EchoProcessor: ...


@dataclass(frozen=True)
class EchoHooks:
    lock: ContextManager[object]
    active: Callable[[], bool]
    valid: Callable[[int], bool]
    factory: EchoFactory
    fast_stop_enabled: Callable[[], bool]
    metric: MetricSink
    log: EventSink


@dataclass(frozen=True)
class CleanFrame:
    nanos: int
    raw: FloatFrame
    reference: FloatFrame
    cleaned: FloatFrame
    stop_cleaned: FloatFrame
    device_reference: FloatFrame | None = None


class EchoPipeline:
    def __init__(self, hooks: EchoHooks):
        self.hooks = hooks
        self.dsp_lock = threading.RLock()
        self.timeline = RenderTimeline()
        self.aec = None
        self.stop_aec = None
        self.calibration_frames = []
        self.reference_shift_ns = 0
        self.paired_filter = PairedReferenceFilter()

    def reset_transport(self):
        self.timeline = RenderTimeline()
        with self.dsp_lock:
            self.paired_filter.clear()

    def reset_session(self):
        with self.dsp_lock:
            self.paired_filter.clear()
        self.timeline = RenderTimeline()
        self.calibration_frames = []
        self.reference_shift_ns = 0

    def open(self, frames=(), *, shift_ns=0):
        with self.dsp_lock, ExitStack() as pending:
            aec = self.hooks.factory()
            pending.callback(aec.close)
            stop_aec = None
            if self.hooks.fast_stop_enabled():
                stop_aec = self.hooks.factory(noise_suppression=False)
                pending.callback(stop_aec.close)
            # Keep the working pair and its alignment until both replacements
            # have been initialized and warmed with the same calibration data.
            paired_filter = PairedReferenceFilter()
            for item in frames:
                if len(item) not in (2, 3):
                    raise ValueError('Invalid calibration frame layout')
                stamp, pcm = item[:2]
                reference = self.timeline.at(stamp - shift_ns, FRAME)
                conditioned = pcm
                if len(item) == 3:
                    conditioned, reference = paired_filter.process(pcm, item[2])
                aec.process(conditioned, reference)
                if stop_aec:
                    stop_aec.process(conditioned, reference)
            self.close()
            self.aec, self.stop_aec = aec, stop_aec
            self.paired_filter = paired_filter
            self.reference_shift_ns = shift_ns
            pending.pop_all()

    def close(self):
        with self.dsp_lock:
            self.paired_filter.clear()
            if self.aec:
                self.aec.close()
                self.aec = None
            if self.stop_aec:
                self.stop_aec.close()
                self.stop_aec = None

    def clear_calibration_frames(self):
        self.calibration_frames.clear()

    def record_calibration_frame(self, frame):
        values = (frame.nanos, frame.raw.copy())
        if frame.device_reference is not None:
            values += (frame.device_reference.copy(),)
        self.calibration_frames.append(values)
        if len(self.calibration_frames) > MAX_CALIBRATION_FRAMES:
            del self.calibration_frames[:-MAX_CALIBRATION_FRAMES]

    def add_render(self, packet, pcm):
        self.timeline.add(packet.generation, packet.frame, packet.nanos, pcm)

    def cancel_pause(self):
        self.timeline.cancel_pause()

    def pause_at(self, nanos):
        self.timeline.pause_at(nanos)

    def resume_at(self, nanos):
        self.timeline.resume_at(nanos)

    def process(self, nanos, raw, device_reference=None):
        with self.dsp_lock:
            if not self.aec:
                return None
            reference = self.timeline.at(nanos - self.reference_shift_ns, FRAME)
            conditioned, cancellation_reference = raw, reference
            if device_reference is not None:
                conditioned, cancellation_reference = self.paired_filter.process(
                    raw, device_reference
                )
            cleaned = self.aec.process(conditioned, cancellation_reference)
            # Stop detection retains its existing AEC3 path without extra NS.
            stop_cleaned = (
                self.stop_aec.process(conditioned, cancellation_reference)
                if self.stop_aec
                else cleaned
            )
        return CleanFrame(nanos, raw, reference, cleaned, stop_cleaned, device_reference)

    def align_reference(self, generation=None):
        with self.hooks.lock:
            if not self.hooks.active() or (
                generation is not None and not self.hooks.valid(generation)
            ):
                return False
            frames = list(self.calibration_frames)
            if len(frames) < 100:
                return True
            paired = any(len(item) == 3 for item in frames)
            if any(len(item) not in (2, 3) for item in frames) or (
                paired and not all(len(item) == 3 for item in frames)
            ):
                raise ValueError('Capture reference layout changed during calibration')
            # The digital timeline still needs its original gate/evidence mapping.
            # This shift never changes the same-grid reference used by paired AEC.
            mic = np.concatenate([item[1] for item in frames])
            render = np.concatenate([self.timeline.at(item[0], FRAME) for item in frames])
            shift, correlation = estimate_reference_shift(mic, render)
            with self.dsp_lock:
                if not self.hooks.active():
                    return False
                self.open(frames, shift_ns=round(shift * 1e9 / RATE))
            self.hooks.metric(
                'capture_reference_alignment', 'paired_grid' if paired else 'render_timeline'
            )
            self.hooks.metric('reference_shift_scope', 'digital_gate' if paired else 'aec_and_gate')
            self.hooks.metric('reference_shift_samples', shift)
            self.hooks.metric('calibration_reference_correlation', round(correlation, 3))
            self.calibration_frames.clear()
            self.hooks.log(
                'reference_alignment_estimated',
                shift_samples=shift,
                correlation=round(correlation, 3),
            )
            return True

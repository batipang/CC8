"""Content-free signal counters for accepted wire PCM, before host DSP.

Paired input is already downsampled on Android. These counters cannot identify
ADC saturation, device AGC, clipping location, speech quality or acoustic SPL.
No sample, waveform, transcript or per-frame history is retained.
"""

from dataclasses import dataclass
import math
import threading

import numpy as np

from audio_signal import RATE
from protocol import MAX_PACKET
from response_clock import MAX_INTERVAL_MS

CLIP_THRESHOLD = 32760  # Same near-full-scale boundary as the Android diagnostic.
CONTINUITY_TOLERANCE_NS = 2_000_000


@dataclass(frozen=True)
class CaptureClockMark:
    owner: object
    samples: int
    discontinuities: int
    nanos: int


class InputQuality:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset(enabled=False)

    def reset(self, *, enabled=True):
        with self.lock:
            self.owner = object()
            self.enabled = enabled
            self.samples = self.clipped = self.zeros = self.peak = 0
            self.power = self.signed_sum = 0
            self.run = self.longest_run = self.discontinuities = 0
            self.next_nanos = self.frontend = None

    def token(self):
        with self.lock:
            return self.owner

    def clock_mark(self):
        with self.lock:
            if not self.enabled or not self.samples or self.next_nanos is None:
                return None
            return CaptureClockMark(self.owner, self.samples, self.discontinuities, self.next_nanos)

    def prove_interval(self, mark, input_end_ns, output_ns):
        with self.lock:
            if (
                not isinstance(mark, CaptureClockMark)
                or mark.owner is not self.owner
                or not self.enabled
                or mark.discontinuities != self.discontinuities
                or self.next_nanos is None
                or type(input_end_ns) is not int
                or type(output_ns) is not int
                or not mark.nanos <= input_end_ns <= output_ns <= self.next_nanos
            ):
                return None
            samples = self.samples - mark.samples
            duration_ns = round(samples * 1e9 / RATE)
            # Bound accumulated clock drift too, not just adjacent-packet gaps.
            if (
                samples <= 0
                or abs(self.next_nanos - mark.nanos - duration_ns) > CONTINUITY_TOLERANCE_NS
                or duration_ns > MAX_INTERVAL_MS * 1_000_000 + CONTINUITY_TOLERANCE_NS
            ):
                return None
            return {
                'version': 1,
                'coverage_ms': round(samples * 1000 / RATE, 3),
                'discontinuities': 0,
            }

    def close(self):
        with self.lock:
            # Keep only final scalar evidence available while capture is off.
            self.owner = object()
            self.enabled = False
            self.run = 0
            self.next_nanos = None

    def break_run(self, token):
        with self.lock:
            if token is self.owner and self.enabled:
                self.run = 0
                self.next_nanos = None
                self.discontinuities += 1

    def observe(self, data, token, *, paired, nanos, discontinuous=False):
        with self.lock:
            if token is not self.owner or not self.enabled:
                return
        if (
            type(data) is not bytes
            or not data
            or len(data) > MAX_PACKET
            or type(paired) is not bool
            or len(data) % (4 if paired else 2)
            or type(nanos) is not int
            or nanos < 0
            or type(discontinuous) is not bool
        ):
            raise ValueError('Invalid microphone quality extent')
        # Select the microphone column before counting. The paired reference
        # may contain full-scale values unrelated to the microphone signal.
        wire = np.frombuffer(data, dtype='<i2')[:: 2 if paired else 1]
        values = wire.astype(np.int64)
        clipped = None
        try:
            count = len(values)
            signed_sum = int(values.sum())
            power = int(values @ values)  # One bounded packet fits signed int64.
            np.abs(values, out=values)
            peak = int(values.max())
            zeros = int(np.count_nonzero(values == 0))
            clipped = values >= CLIP_THRESHOLD
            clip_count = int(np.count_nonzero(clipped))
            gaps = np.flatnonzero(~clipped)
            if len(gaps):
                leading = int(gaps[0])
                trailing = count - 1 - int(gaps[-1])
                longest = int(np.max(np.diff(np.r_[-1, gaps, count]) - 1))
            else:
                leading = trailing = longest = count
            frontend = 'paired' if paired else 'mono'
            with self.lock:
                # Decoding/reduction can yield while Stop or a route/session
                # reset revokes this owner. Never publish its late counters.
                if token is not self.owner or not self.enabled:
                    return
                if self.frontend not in (None, frontend):
                    raise ValueError('Microphone quality frontend changed without reset')
                clock_gap = (
                    self.next_nanos is not None
                    and abs(nanos - self.next_nanos) > CONTINUITY_TOLERANCE_NS
                )
                if discontinuous or clock_gap:
                    self.run = 0
                    self.discontinuities += 1
                self.longest_run = max(self.longest_run, longest, self.run + leading)
                self.run = self.run + count if leading == count else trailing
                self.samples += count
                self.clipped += clip_count
                self.zeros += zeros
                self.peak = max(self.peak, peak)
                self.power += power
                self.signed_sum += signed_sum
                self.frontend = frontend
                self.next_nanos = nanos + round(count * 1e9 / RATE)
        finally:
            values.fill(0)
            if clipped is not None:
                clipped.fill(False)

    def snapshot(self):
        with self.lock:
            n = self.samples
            return {
                'schema': 1,
                'status': 'observed' if n else 'unmeasured',
                'scope': 'received_mic_pcm_16khz_before_host_dsp',
                'sample_rate': RATE,
                'frontend': self.frontend,
                'device_adc_clipping_measured': False,
                'samples': n,
                'clip_threshold_int16': CLIP_THRESHOLD,
                'continuity_tolerance_ns': CONTINUITY_TOLERANCE_NS,
                'clipped_samples': self.clipped,
                'zero_samples': self.zeros,
                'clipped_ratio': round(self.clipped / n, 8) if n else None,
                'zero_ratio': round(self.zeros / n, 8) if n else None,
                'peak_int16': self.peak if n else None,
                'rms_dbfs': round(10 * math.log10(max(self.power / (n * 32768**2), 1e-12)), 3)
                if n
                else None,
                'dc_offset_int16': round(self.signed_sum / n, 3) if n else None,
                'max_consecutive_clipped_samples': self.longest_run,
                'discontinuities': self.discontinuities,
            }

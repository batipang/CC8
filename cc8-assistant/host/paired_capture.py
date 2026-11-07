"""Bounded paired PCM and causal reference conditioning; no device or file access."""

import math
import numpy as np
from audio_signal import FRAME

REFERENCE_GAIN = 16.0


def decode_pairs(data):
    """Own both normalized arrays; payload order is microphone, candidate reference."""
    if type(data) is not bytes or not data or len(data) > 6400 or len(data) % 4:
        raise ValueError('Invalid paired PCM extent')
    pairs = np.frombuffer(data, dtype='<i2').reshape(-1, 2)
    return pairs[:, 0].astype(np.float64) / 32768, pairs[:, 1].astype(np.float64) / 32768


class _HighPass:
    def __init__(self):
        # Second-order Butterworth, 80 Hz at 16 kHz. Both streams use exactly
        # these coefficients; no SciPy dependency or future samples at runtime.
        omega = 2 * math.pi * 80 / 16000
        cosine = math.cos(omega)
        alpha = math.sin(omega) / math.sqrt(2)
        denominator = 1 + alpha
        self.b0 = (1 + cosine) / (2 * denominator)
        self.b1 = -(1 + cosine) / denominator
        self.b2 = self.b0
        self.a1 = -2 * cosine / denominator
        self.a2 = (1 - alpha) / denominator
        self.clear()

    def clear(self):
        self.first = self.second = 0.0

    def process(self, values):
        out = np.empty(len(values), dtype=np.float64)
        first, second = self.first, self.second
        for index, value in enumerate(values):
            output = self.b0 * value + first
            first = self.b1 * value - self.a1 * output + second
            second = self.b2 * value - self.a2 * output
            out[index] = output
        self.first, self.second = first, second
        return out


class PairedReferenceFilter:
    """One capture-owned pair; malformed input cannot partially advance either stream."""

    def __init__(self):
        self.microphone = _HighPass()
        self.reference = _HighPass()

    def clear(self):
        self.microphone.clear()
        self.reference.clear()

    def process(self, microphone, reference):
        for samples in (microphone, reference):
            if (
                not isinstance(samples, np.ndarray)
                or samples.ndim != 1
                or len(samples) != FRAME
                or not np.issubdtype(samples.dtype, np.floating)
                or np.iscomplexobj(samples)
                or not np.all(np.isfinite(samples))
                or np.any(np.abs(samples) > 1)
            ):
                raise ValueError('Paired reference needs two finite 10 ms frames')
        return self.microphone.process(microphone), self.reference.process(
            reference
        ) * REFERENCE_GAIN

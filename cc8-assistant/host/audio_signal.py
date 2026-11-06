"""Shared host PCM geometry and normalized signal level."""

import numpy as np

RATE = 16000
FRAME = 160


def level(samples):
    return float(10 * np.log10(max(float(np.mean(samples * samples)), 1e-12)))

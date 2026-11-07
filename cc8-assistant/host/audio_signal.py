"""Shared host PCM geometry and normalized signal level."""

import numpy as np
from numpy.typing import NDArray

RATE = 16000
FRAME = 160
MAX_UTTERANCE_FRAMES = 1200


def level(samples: NDArray[np.float32] | NDArray[np.float64]) -> float:
    return float(10 * np.log10(max(float(np.mean(samples * samples)), 1e-12)))

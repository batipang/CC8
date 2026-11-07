"""Bounded local keyword recheck, without session state or cloud recognition."""

from dataclasses import dataclass
from typing import Callable, Protocol, TypeAlias
import numpy as np
from numpy.typing import NDArray
from audio_signal import FRAME, MAX_UTTERANCE_FRAMES

Samples: TypeAlias = NDArray[np.float32] | NDArray[np.float64]


@dataclass(frozen=True)
class WaitingUtterance:
    serial: int
    generation: int
    first_sample_ns: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (self.serial, self.generation, self.first_sample_ns)
        ):
            raise ValueError('Invalid waiting utterance ownership')


class KeywordRecheck(Protocol):
    def feed(self, pcm: NDArray[np.float32]) -> str: ...


def recheck_utterance(
    pcm: Samples,
    factory: Callable[[], KeywordRecheck],
    current: Callable[[], bool],
) -> bool | None:
    """Return a keyword decision, or None when unavailable/cancelled.

    The factory creates an independent stream on already loaded local weights.
    Session callbacks run outside native feed/engine locks. Own the temporary
    float32 copy so clearing it cannot alter audio later passed to ASR.
    """
    if not current():
        return None
    samples = np.array(pcm, dtype=np.float32, copy=True)
    try:
        if (
            samples.ndim != 1
            or not FRAME <= len(samples) <= MAX_UTTERANCE_FRAMES * FRAME
            or len(samples) % FRAME
            or not np.all(np.isfinite(samples))
            or not current()
        ):
            return None
        stream = factory()
        hit = False
        for offset in range(0, len(samples), FRAME):
            if not current():
                return None
            hit |= stream.feed(samples[offset : offset + FRAME]) == 'Bumblebee'
        return hit if current() else None
    finally:
        samples.fill(0)

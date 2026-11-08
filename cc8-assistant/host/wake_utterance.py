"""Bounded local keyword recheck, without session state or cloud recognition."""

from dataclasses import dataclass
import re
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
    keyword: str = 'Bumblebee'
    last_sample_ns: int | None = None
    candidate_token: str | None = None
    model_generation: int = 0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.serial,
                self.generation,
                self.first_sample_ns,
                self.model_generation,
            )
        ):
            raise ValueError('Invalid waiting utterance ownership')
        if self.keyword not in ('Bumblebee', '大黄蜂'):
            raise ValueError('Invalid waiting keyword')
        if self.last_sample_ns is not None and (
            type(self.last_sample_ns) is not int or self.last_sample_ns < self.first_sample_ns
        ):
            raise ValueError('Invalid waiting utterance end')
        if self.keyword == '大黄蜂' and (
            self.last_sample_ns is None
            or not isinstance(self.candidate_token, str)
            or re.fullmatch('[a-f0-9]{32}', self.candidate_token) is None
        ):
            raise ValueError('Chinese confirmation requires a completed interval')
        if self.keyword == 'Bumblebee' and self.candidate_token is not None:
            raise ValueError('Unexpected Chinese ownership')


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

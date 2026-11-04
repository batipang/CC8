"""Per-clause playout receipt admission; no callbacks while holding the lock."""

import threading
import numpy as np
from numpy.typing import NDArray


class PlayoutReceipt:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.identity: tuple[int, int] | None = None
        self.maximum = 0
        self.first_signal: int | None = None
        self.received = False

    def clear(self) -> None:
        with self.lock:
            self.identity = None
            self.maximum = 0
            self.first_signal = None
            self.received = False

    def begin(self, generation: int, playback_id: int) -> None:
        with self.lock:
            self.identity = (generation, playback_id)
            self.maximum = 0
            self.first_signal = None
            self.received = False

    def reserve(
        self, generation: int, playback_id: int, frame: int, pcm: NDArray[np.int16]
    ) -> None:
        # Reserve before sending: a real reader ACK can arrive before send returns.
        with self.lock:
            if self.identity != (generation, playback_id):
                return
            if frame != self.maximum:
                raise ValueError('Non-contiguous playout probe PCM')
            if self.first_signal is None:
                positions = np.flatnonzero(pcm)
                if positions.size:
                    self.first_signal = frame + int(positions[0])
            self.maximum += len(pcm)

    def accept(
        self,
        generation: int,
        playback_id: int,
        first_signal_frame: int,
        played_frames: int,
        accepted_frames: int,
    ) -> bool:
        with self.lock:
            if (
                self.identity != (generation, playback_id)
                or self.received
                or self.first_signal is None
                or first_signal_frame != self.first_signal
                or not self.first_signal < played_frames <= accepted_frames <= self.maximum
            ):
                return False
            self.received = True
            return True

    def expected_frames(self, generation: int, playback_id: int) -> int | None:
        with self.lock:
            return self.maximum if self.identity == (generation, playback_id) else None

    def observed(self, generation: int, playback_id: int) -> bool:
        with self.lock:
            return self.identity == (generation, playback_id) and self.received

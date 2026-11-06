"""Bounded local keyword streams; native calls share a lock, never call session ports."""

from __future__ import annotations
import threading
import time
from typing import Protocol
import numpy as np


class KeywordStream(Protocol):
    def accept_waveform(self, sample_rate: int, samples: np.ndarray) -> None: ...


class KeywordBackend(Protocol):
    def create_stream(self, keywords: str | None = None) -> KeywordStream: ...
    def is_ready(self, stream: KeywordStream) -> bool: ...
    def decode_stream(self, stream: KeywordStream) -> None: ...
    def get_result(self, stream: KeywordStream) -> str: ...
    def reset_stream(self, stream: KeywordStream) -> None: ...


def validated_gain(gain):
    if type(gain) not in (int, float) or not 1 <= gain <= 4:
        raise ValueError('Keyword gain must be between 1 and 4')
    return float(gain)


class StreamingKeywords:
    KEYWORDS = frozenset()

    def __init__(self, detector: KeywordBackend, gain=2.0, *, lock=None, keywords=None):
        self.gain = validated_gain(gain)
        self.detector = detector
        self.engine_lock = lock if lock is not None else threading.RLock()
        self.inline_keywords = keywords
        self.samples = 0
        self.compute_seconds = 0.0
        self.reset()

    def reset(self, *, clear_metrics=True):
        with self.engine_lock:
            self.stream = (
                self.detector.create_stream()
                if self.inline_keywords is None
                else self.detector.create_stream(self.inline_keywords)
            )
            if clear_metrics:
                self.samples = 0
                self.compute_seconds = 0.0
            self.stream_start_samples = self.samples

    @property
    def stream_samples(self):
        return self.samples - self.stream_start_samples

    def feed(self, pcm):
        pcm = np.asarray(pcm, dtype=np.float32)
        if pcm.ndim != 1 or len(pcm) > 6400 or not np.all(np.isfinite(pcm)):
            raise ValueError('Invalid keyword frame')
        started = time.perf_counter()
        with self.engine_lock:
            self.stream.accept_waveform(16000, np.clip(pcm * self.gain, -0.98, 0.98))
            self.samples += len(pcm)
            keyword = ''
            while self.detector.is_ready(self.stream):
                self.detector.decode_stream(self.stream)
                result = self.detector.get_result(self.stream)
                if result:
                    self.detector.reset_stream(self.stream)
                    if result in self.KEYWORDS:
                        keyword = result
            self.compute_seconds += time.perf_counter() - started
            return keyword

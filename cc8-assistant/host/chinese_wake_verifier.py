"""Opt-in Chinese wake verification over a bounded local input window."""

from collections import deque
import time

import numpy as np

from dsp import FRAME
from wake_verifier_process import MAX_SAMPLES


WINDOW_FRAMES = MAX_SAMPLES // FRAME
TARGET = '大黄蜂'


class ChineseWakeVerifier:
    def __init__(self, alternate, worker):
        self.alternate = alternate
        self.worker = worker
        self.primary_audio = deque()
        self.alternate_audio = deque()
        self.decode_seconds = 0.0

    def reset(self, *, clear_metrics=False):
        had_input = bool(self.alternate_audio)
        for audio in (self.primary_audio, self.alternate_audio):
            while audio:
                audio.popleft().fill(0)
        if had_input or clear_metrics:
            self.alternate.reset(clear_metrics=clear_metrics)
        if clear_metrics:
            self.decode_seconds = 0.0

    def _observe(self, audio, frame):
        if len(audio) == WINDOW_FRAMES:
            audio.popleft().fill(0)
        audio.append(frame.copy())

    def _classify(self, audio):
        pcm = np.concatenate(audio)
        started = time.perf_counter()
        try:
            return self.worker.classify(pcm)
        finally:
            self.decode_seconds += time.perf_counter() - started
            pcm.fill(0)

    def feed(self, primary_hit, primary, alternate):
        if (
            alternate is None
            or primary.shape != (FRAME,)
            or alternate.shape != (FRAME,)
            or not self.worker.ready
        ):
            self.reset()
            return primary_hit

        self._observe(self.primary_audio, primary)
        self._observe(self.alternate_audio, alternate)
        alternate_hit = self.alternate.feed(alternate)
        if primary_hit:
            target, confusable = self._classify(self.primary_audio)
            if target or not confusable:
                return primary_hit
        if alternate_hit == TARGET:
            target, confusable = self._classify(self.alternate_audio)
            if target and not confusable:
                return TARGET
        return ''

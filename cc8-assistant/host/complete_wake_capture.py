"""Separate bounded waiting capture; never changes ordinary speech/barge-in gates."""

from collections import deque
from dataclasses import dataclass
import numpy as np
from dsp import VoiceDetector, UTTERANCE_TOO_LONG, MAX_UTTERANCE_FRAMES
from capture_evidence import FRAME_NS, FRAME_JITTER_NS
from wake_utterance import WaitingUtterance


class ErasingVoiceDetector(VoiceDetector):
    def feed(self, pcm):
        if self.talking and len(self.audio) >= MAX_UTTERANCE_FRAMES:
            # The ordinary endpoint can omit quiet frames at its sample cap.
            # A whole-utterance authorization must never attest to that clipped
            # interval, even if only trailing silence exceeded the cap.
            self.reset()
            self.discarding = True
            return False, UTTERANCE_TOO_LONG, None
        if not self.talking and len(self.pre) == self.pre.maxlen:
            self.pre[0].fill(0)
        return super().feed(pcm)

    def reset(self):
        for frames in (self.pre, self.audio):
            for frame in frames:
                frame.fill(0)
        super().reset()


@dataclass(frozen=True)
class CompletedWake:
    ticket: WaitingUtterance
    audio: np.ndarray
    input_end_ns: int | None


class CompleteWakeCapture:
    def __init__(self):
        self.vad = ErasingVoiceDetector()
        self.stamps = deque(maxlen=VoiceDetector.PRE_ROLL_FRAMES)
        self.ticket = None
        self.last_stamp = self.last_voice = None

    def reset(self):
        self.vad.reset()
        self.stamps.clear()
        self.ticket = None
        self.last_stamp = self.last_voice = None

    def feed(self, pcm, voiced, stamp, ticket, take):
        if (
            self.last_stamp is not None
            and abs(stamp - self.last_stamp - FRAME_NS) > FRAME_JITTER_NS
        ):
            self.reset()
        self.last_stamp = stamp
        self.stamps.append(stamp)
        self.vad.external_voice = voiced
        began, audio, _ = self.vad.feed(pcm)
        if voiced:
            self.last_voice = stamp + FRAME_NS
        if began:
            count = len(self.vad.audio)
            self.ticket = ticket(self.stamps[-count]) if 0 < count <= len(self.stamps) else None
        if audio is UTTERANCE_TOO_LONG:
            self.ticket = None
            return None
        if audio is None:
            return None
        base, self.ticket = self.ticket, None
        owner = take(base, stamp) if base is not None else None
        if owner is None:
            audio.fill(0)
            return None
        return CompletedWake(owner, audio, self.last_voice)

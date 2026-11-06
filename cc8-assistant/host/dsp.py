"""Playback reference timing and local speech detection, with legacy DSP exports."""

from collections import deque
import numpy as np

from audio_signal import FRAME as FRAME, RATE as RATE, level as level
from echo_processors import (
    EchoCanceller as EchoCanceller,
    WebRtcEchoCanceller as WebRtcEchoCanceller,
)
from recognition_commands import STOP_COMMANDS as STOP_COMMANDS
from recognition_echo import resembles_playback as resembles_playback


class RenderTimeline:
    # 把设备播放帧映射到设备纳秒时钟，为 AEC 提供同一时刻的扬声器参考。
    # 暂停时暂存未播片段，恢复时整体平移，不能把静音间隙当作连续播放。
    def __init__(self):
        self.blocks = deque()
        self.anchors = {}
        self.discontinuities = 0
        self.paused_at = None
        self.held = []

    def pause_at(self, nanos):
        if self.paused_at is not None:
            return
        self.paused_at = nanos
        old = self.blocks
        self.blocks = deque()
        self.held = []
        for stamp, pcm in old:
            self._append(stamp, pcm)

    def resume_at(self, nanos):
        if self.paused_at is None:
            return
        gap = max(0, nanos - self.paused_at)
        self.blocks.extend((stamp + gap, pcm) for stamp, pcm in self.held)
        self.anchors = {key: value + gap for key, value in self.anchors.items()}
        self.paused_at = None
        self.held = []

    def cancel_pause(self):
        self.paused_at = None
        self.held = []

    def _append(self, stamp, pcm):
        if self.paused_at is None:
            self.blocks.append((stamp, pcm))
            return
        split = max(0, min(len(pcm), round((self.paused_at - stamp) * RATE / 1e9)))
        if split:
            self.blocks.append((stamp, pcm[:split]))
        if split < len(pcm):
            self.held.append((stamp + round(split * 1e9 / RATE), pcm[split:]))

    def add(self, generation, frame, nanos, pcm):
        # A single anchor per playback keeps packet scheduling jitter from
        # producing gaps in the playback reference. Timestamp refinements
        # are bounded; old reference remains for the acoustic tail on cancel.
        key = generation
        observed = nanos - frame * 1e9 / RATE
        anchor = self.anchors.get(key)
        if frame == 0 or anchor is None:
            anchor = observed
        elif abs(observed - anchor) > 40e6:
            # A cloud/USB stall can drain AudioTrack. The next samples then
            # really play later. Ignoring this jump invents continuous audio
            # during the gap and misaligns every subsequent AEC frame.
            anchor = observed
            self.discontinuities += 1
        else:
            anchor += 0.1 * (observed - anchor)
        self.anchors[key] = anchor
        self._append(int(anchor + frame * 1e9 / RATE), pcm.copy() * 0.35)
        while self.blocks and nanos - self.blocks[0][0] > 12e9:
            self.blocks.popleft()
        if len(self.anchors) > 64:
            self.anchors = {key: anchor}

    def at(self, nanos, count):
        out = np.zeros(count)
        for stamp, block in tuple(self.blocks):
            offset = round((stamp - nanos) * RATE / 1e9)
            left = max(0, offset)
            right = min(count, offset + len(block))
            if left < right:
                out[left:right] = block[left - offset : right - offset]
        return out


class ReferenceCapture:
    """Capture the render window corresponding to VAD's bounded utterance.

    This is playback PCM already held on the Mac, not another microphone or
    recording. Retaining it with the job avoids timeline expiry during ASR.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.pre = deque(maxlen=50)
        self.frames = []
        self.active = False

    def feed(self, reference, began, finished):
        if not self.active:
            self.pre.append(reference.copy())
            if began:
                self.frames = list(self.pre)
                self.pre.clear()
                self.active = True
        else:
            self.frames.append(reference.copy())
        if finished:
            output = np.concatenate(self.frames) if self.frames else np.zeros(0)
            self.reset()
            return output
        if len(self.frames) > 1200:
            self.reset()
            raise ValueError('Render capture exceeds VAD bound')
        return None


def estimate_reference_shift(mic, reference):
    """Estimate a causal filter lead-in from a user-visible silent calibration.

    Negative shifts need future reference blocks, supplied by the bridge's
    explicit 80 ms microphone lookahead. This is an alignment estimate, not
    a conversation latency measurement.
    """
    count = min(len(mic), len(reference), RATE * 5)
    if count < RATE or level(reference[:count]) < -70:
        return 0, 0.0
    y = mic[:count] - np.mean(mic[:count])
    x = reference[:count] - np.mean(reference[:count])
    size = 1 << (2 * count - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(y, size) * np.conj(np.fft.rfft(x, size)), size)
    yy = np.r_[0, np.cumsum(y * y)]
    xx = np.r_[0, np.cumsum(x * x)]
    lags = np.arange(-RATE // 2, RATE // 2 + 1)
    energy_y = np.where(
        lags >= 0, yy[count] - yy[np.maximum(lags, 0)], yy[count + np.minimum(lags, 0)]
    )
    energy_x = np.where(
        lags >= 0, xx[count - np.maximum(lags, 0)], xx[count] - xx[-np.minimum(lags, 0)]
    )
    denominator = np.sqrt(np.maximum(0, energy_y * energy_x))
    coefficients = np.divide(
        corr[lags % size], denominator, out=np.zeros_like(denominator), where=denominator > 0
    )
    best = int(np.argmax(np.abs(coefficients)))
    peak = float(abs(coefficients[best]))
    lag = int(lags[best])
    # Leave 20 ms of causal room response within the 250 ms adaptive filter.
    shift = lag - RATE // 50
    shift = max(-RATE * 60 // 1000, min(RATE * 400 // 1000, shift))
    return (shift, peak) if peak >= 0.15 else (0, peak)


class VoiceDetector:
    # 保留起音前缓冲，连续静音 80 帧收尾，单句最多 1200 帧；仅负责成句。
    # external_voice 可由近端门控提供，能量判定不等于说话人或专用唤醒词识别。
    """Energy VAD for prototype testing, not a semantic speech detector."""

    ONSET_FRAMES = 14

    def __init__(self):
        self.pre = deque(maxlen=50)
        self.audio = []
        self.hot = self.quiet = 0
        self.talking = False
        self.noise = -55.0
        self.external_voice = None

    def reset(self):
        self.pre.clear()
        self.audio.clear()
        self.hot = self.quiet = 0
        self.talking = False

    def feed(self, pcm):
        db = level(pcm)
        threshold = max(-40.0, min(-32.0, self.noise + 10))
        voiced = db > threshold if self.external_voice is None else self.external_voice
        started = False
        result = None
        if not self.talking:
            self.pre.append(pcm.copy())
            self.hot = self.hot + 1 if voiced else max(0, self.hot - 2)
            if not voiced:
                self.noise = 0.995 * self.noise + 0.005 * db
            if (
                self.hot >= self.ONSET_FRAMES
            ):  # 140 ms onset, with a 500 ms pre-roll; not end-to-end latency.
                self.talking = True
                self.audio = list(self.pre)
                self.pre.clear()
                self.quiet = 0
                started = True
        else:
            self.audio.append(pcm.copy())
            quiet = db < threshold - 3 if self.external_voice is None else not voiced
            self.quiet = self.quiet + 1 if quiet else 0
            if self.quiet >= 80 or len(self.audio) >= 1200:
                result = np.concatenate(self.audio)
                self.reset()
        return started, result, db


class NearSpeechDetector:
    # 比较原始收音、消回声残差与播放参考，用多帧投票降低播报尾音误打断。
    # dBFS 阈值越接近 0 越严格；声学证据不是声纹，不能辨别具体说话人。
    """Local WebRTC VAD plus residual energy/correlation; not speaker identity."""

    MIN_DBFS = -43.0
    MAX_DBFS = -34.0
    NOISE_MARGIN_DB = 12.0
    CONFIRM_WINDOW = 24
    CONFIRM_MIN_FRAMES = 20
    CONFIRM_VOTES = 16

    def __init__(self, vad=None):
        if vad is None:
            import webrtcvad

            vad = webrtcvad.Vad(2)
        self.vad = vad
        self.reset()

    def reset(self):
        self.votes = deque(maxlen=self.CONFIRM_WINDOW)
        self.raw = deque(maxlen=20)
        self.render = deque(maxlen=20)
        self.residual = deque(maxlen=20)
        self.noise = -58.0
        self.voiced = False
        self.credible = False
        self.correlation = 0.0
        self.residual_correlation = 0.0

    @staticmethod
    def coherence(signal, reference):
        # Search +/-50 ms as the room path and playback clock need not line up
        # at zero lag. Decimation keeps this inexpensive at 100 frames/second.
        a = signal[::4]
        b = reference[::4]
        n = len(a)
        if n < 80:
            return 0.0
        cross = np.correlate(a, b, 'full')
        lags = np.arange(-min(200, n // 3), min(200, n // 3) + 1)
        sa = np.r_[0.0, np.cumsum(a * a)]
        sb = np.r_[0.0, np.cumsum(b * b)]
        start_a = np.maximum(0, lags)
        end_a = n + np.minimum(0, lags)
        start_b = np.maximum(0, -lags)
        end_b = n - np.maximum(0, lags)
        energy = np.sqrt((sa[end_a] - sa[start_a]) * (sb[end_b] - sb[start_b]))
        return float(np.max(np.abs(cross[lags + n - 1]) / np.maximum(energy, 1e-9)))

    def feed(self, cleaned, mic, reference):
        if any(len(x) != FRAME for x in (cleaned, mic, reference)):
            raise ValueError('Near speech requires 10 ms frames')
        db = level(cleaned)
        threshold = max(self.MIN_DBFS, min(self.MAX_DBFS, self.noise + self.NOISE_MARGIN_DB))
        raw = np.rint(np.clip(cleaned * 1.5, -0.98, 0.98) * 32768).astype('<i2').tobytes()
        speech = bool(self.vad.is_speech(raw, RATE))
        if not speech:
            self.noise = 0.995 * self.noise + 0.005 * db
        self.raw.append(mic.copy())
        self.render.append(reference.copy())
        self.residual.append(cleaned.copy())
        rec = np.concatenate(self.raw)
        ref = np.concatenate(self.render)
        has_render = level(ref) > -60
        self.correlation = self.coherence(rec, ref) if has_render else 0.0
        self.residual_correlation = (
            self.coherence(np.concatenate(self.residual), ref) if has_render else 0.0
        )
        # Require the residual itself to match the speaker too: correlated raw
        # mic alone cannot distinguish an ordinary voice over louder playback.
        echo_only = (
            has_render
            and self.correlation > 0.78
            and self.residual_correlation > 0.60
            and db < level(rec) - 6
        )
        self.voiced = speech and db > threshold and not echo_only
        self.votes.append(self.voiced)
        self.credible = (
            len(self.votes) >= self.CONFIRM_MIN_FRAMES
            and sum(self.votes) >= self.CONFIRM_VOTES
            and self.voiced
        )
        return self.voiced, self.credible

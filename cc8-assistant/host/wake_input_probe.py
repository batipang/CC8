"""One short, opt-in in-memory observation of actual wake inputs."""

import hashlib
import threading
import uuid

import numpy as np

from dsp import FRAME
from wake_input_receipt import (
    CHINESE_FIXTURE_FRAMES,
    CHINESE_MAX_FRAMES,
    CHINESE_PCM_SHA256,
    FIXTURE_FRAMES,
    FIXTURE_PCM_SHA256,
    MAX_FRAMES,
    MIN_MATCH_FRAMES,
)

TTL_SECONDS = 7
DECIMATION = 8
# Frozen Daniel fixture. Fit the question after the pause, then independently
# look for the name near the implied start. These are source sample boundaries.
NAME_END = 10400
QUESTION_FIRST, QUESTION_LAST = 13600, 35600
# Fixed case 27 mixture has a 200 ms noise lead before the spoken name.
CHINESE_WINDOWS = (3200, 16000, 20800, 51200)
NAME_SEARCH = 30
RIVAL_RADIUS = 80


def _downsample(signal):
    count = len(signal) // DECIMATION
    return signal[: count * DECIMATION].reshape(count, DECIMATION).mean(axis=1)


def _scores(audio, reference):
    template = reference - reference.mean()
    size = 1 << (len(audio) + len(template) - 2).bit_length()
    covariance = np.fft.irfft(np.fft.rfft(audio, size) * np.fft.rfft(template[::-1], size), size)[
        len(template) - 1 : len(audio)
    ]
    sums = np.r_[0.0, audio.cumsum()]
    powers = np.r_[0.0, (audio**2).cumsum()]
    n = len(template)
    variance = powers[n:] - powers[:-n] - (sums[n:] - sums[:-n]) ** 2 / n
    norm = np.sqrt(np.maximum(variance * np.dot(template, template), 1e-18))
    return np.clip(np.abs(covariance) / norm, 0, 1)


def compare_stream(samples, reference, *, chinese=False):
    minimum = CHINESE_FIXTURE_FRAMES + 1280 if chinese else MIN_MATCH_FRAMES
    if len(samples) < minimum:
        return {'status': 'capture_incomplete'}
    audio, source = _downsample(samples), _downsample(reference)
    windows = CHINESE_WINDOWS if chinese else (0, NAME_END, QUESTION_FIRST, QUESTION_LAST)
    name_first, name_end, question_first, question_last = (n // DECIMATION for n in windows)
    question = source[question_first:question_last]
    scores = _scores(audio, question)
    peak = int(np.argmax(scores))
    confidence = float(scores[peak])
    rival = float(
        max(
            scores[: max(0, peak - RIVAL_RADIUS)].max(initial=0),
            scores[peak + RIVAL_RADIUS + 1 :].max(initial=0),
        )
    )
    result = {'question_ncc': round(confidence, 4), 'question_rival_ncc': round(rival, 4)}
    if confidence < 0.30 or confidence - rival < 0.075:
        return {**result, 'status': 'question_unconfirmed'}
    start = peak - question_first
    if start < NAME_SEARCH or start + len(source) > len(audio):
        return {**result, 'status': 'fixture_boundary_unconfirmed'}
    name_scores = _scores(
        audio[start + name_first - NAME_SEARCH : start + name_end + NAME_SEARCH],
        source[name_first:name_end],
    )
    name_peak = int(np.argmax(name_scores))
    name_score = float(name_scores[name_peak])
    name_start = (start + name_first + name_peak - NAME_SEARCH) * DECIMATION
    name_size = (name_end - name_first) * DECIMATION
    name_rms = float(np.sqrt(np.mean(samples[name_start : name_start + name_size] ** 2)))
    question_rms = float(
        np.sqrt(np.mean(samples[peak * DECIMATION : (peak + len(question)) * DECIMATION] ** 2))
    )
    halves = []
    for begin, end in ((0, len(question) // 2), (len(question) // 2, len(question))):
        halves.append(float(_scores(audio[peak + begin : peak + end], question[begin:end])[0]))
    status = (
        'matched'
        if min(halves) >= 0.20 and name_score >= 0.22 and min(name_rms, question_rms) >= 0.002
        else 'name_or_question_shape_unconfirmed'
    )
    return {
        **result,
        'status': status,
        'question_half_ncc': [round(part, 4) for part in halves],
        'name_ncc': round(name_score, 4),
        'name_rms': round(name_rms, 5),
        'question_rms': round(question_rms, 5),
        'name_relative_frames': (name_peak - NAME_SEARCH) * DECIMATION,
    }


class WakeInputProbe:
    def __init__(self, pcm, *, chinese=False, timer=threading.Timer):
        fixture = (
            (CHINESE_FIXTURE_FRAMES, CHINESE_PCM_SHA256)
            if chinese
            else (FIXTURE_FRAMES, FIXTURE_PCM_SHA256)
        )
        if len(pcm) != 2 * fixture[0] or hashlib.sha256(pcm).hexdigest() != fixture[1]:
            raise ValueError('Unexpected wake probe fixture')
        self.chinese = chinese
        self.limit = CHINESE_MAX_FRAMES if chinese else MAX_FRAMES
        self.token = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.reference = np.frombuffer(pcm, dtype='<i2').astype(float) / 32768
        self.primary, self.alternate = np.empty(self.limit), np.empty(self.limit)
        self.raw = np.empty(self.limit) if chinese else np.empty(0)
        self.frames = self.alternate_frames = 0
        self.raw_frames = 0
        self.stream_samples = None
        self.english_render_rejections = 0
        self.closed = False
        self.cancelled = threading.Event()
        self.timer = timer(10 if chinese else TTL_SECONDS, self.cancel)
        self.timer.daemon = True
        try:
            self.timer.start()
        except BaseException:
            self.cancel()
            raise

    def _close(self):
        self.closed = True
        self.timer.cancel()
        arrays = self.reference, self.primary, self.alternate, self.raw
        self.reference = self.primary = self.alternate = self.raw = np.empty(0)
        return arrays

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            for audio in self._close():
                audio.fill(0)

    def observe(
        self, primary, alternate, raw=None, *, stream_samples=None, english_render_rejected=False
    ):
        with self.lock:
            if self.closed or self.frames + FRAME > self.limit:
                return
            if primary.shape != (FRAME,) or any(
                stream is not None and stream.shape != (FRAME,) for stream in (alternate, raw)
            ):
                for audio in self._close():
                    audio.fill(0)
                return
            start = self.frames
            if not self.chinese:
                if start == 0 and type(stream_samples) is tuple and len(stream_samples) == 2:
                    self.stream_samples = stream_samples
                self.english_render_rejections += english_render_rejected
            self.primary[start : start + FRAME] = primary
            if alternate is not None:
                self.alternate[start : start + FRAME] = alternate
                self.alternate_frames += FRAME
            if self.chinese and raw is not None:
                self.raw[start : start + FRAME] = raw
                self.raw_frames += FRAME
            self.frames += FRAME

    def read(self):
        with self.lock:
            if self.closed:
                return {'status': 'unavailable'}
            arrays = self._close()
            reference, primary, alternate, raw = arrays
        try:
            primary = primary[: self.frames]
            alternate_view = alternate[: self.frames]
            primary_shape = compare_stream(primary, reference, chinese=self.chinese)
            alternate_shape = (
                compare_stream(alternate_view, reference, chinese=self.chinese)
                if self.alternate_frames == self.frames
                else {'status': 'input_gap'}
            )
            result = {
                'status': 'measured',
                'raw_audio_saved': False,
                'primary_frames': self.frames,
                'alternate_frames': self.alternate_frames,
                'primary': primary_shape,
                'alternate': alternate_shape,
            }
            streams = {'primary': primary, 'alternate': alternate_view}
            if self.stream_samples is not None:
                result['live_decoder'] = {
                    'primary_samples_before': self.stream_samples[0],
                    'alternate_samples_before': self.stream_samples[1],
                    'render_rejections': self.english_render_rejections,
                }
            if self.chinese:
                raw_view = raw[: self.frames]
                result.update(
                    language='zh',
                    raw_frames=self.raw_frames,
                    raw=compare_stream(raw_view, reference, chinese=True)
                    if self.raw_frames == self.frames
                    else {'status': 'input_gap'},
                )
                streams['raw'] = raw_view
            result['fresh_replay'] = {'status': 'not_run'}
            if all(result[key]['status'] == 'matched' for key in streams):
                from local_wake import replay_chinese_inputs, replay_english_inputs

                try:
                    result['fresh_replay'] = (
                        replay_chinese_inputs(streams, self.cancelled.is_set)
                        if self.chinese
                        else replay_english_inputs(primary, alternate_view, self.cancelled.is_set)
                    )
                except (ImportError, OSError, RuntimeError, ValueError):
                    result['fresh_replay'] = {'status': 'unavailable'}
            return result
        finally:
            for audio in arrays:
                audio.fill(0)

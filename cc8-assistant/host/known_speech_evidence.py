"""Bounded, opt-in corroboration of a public synthetic near speech window.

This observes existing capture only. Matching a window is not a measurement of
the first audible sample, speaker identity, or physical double talk. In
particular, overlapping digital render samples do not prove loudspeaker sound.
"""

import hashlib
import copy
import io
import os
from pathlib import Path
import re
import stat
import threading
import uuid
import wave

import numpy as np
from dsp import RATE
from mac_voice import _read_pcm, validate_prepared_speech
from playback_evidence import match_window, WINDOW, MIN_REFERENCE_RMS, MIN_CAPTURE_RMS
from known_speech_receipt import METHOD, STATUSES

MAX_FRAMES = RATE * 6
MAX_BYTES = 1024 * 1024


def load_fixture(root, directory, metadata):
    """Read only a single private fixture below the configured runtime root."""
    clean = validate_prepared_speech(metadata)
    if not isinstance(directory, str) or not re.fullmatch(
        r'cc8-public-speech-[a-z0-9_-]{6,64}', directory
    ):
        raise ValueError('Invalid public fixture directory')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(Path(root), flags)
    try:
        parent_fd = os.open(directory, flags, dir_fd=root_fd)
        try:
            for fd in (root_fd, parent_fd):
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError('Invalid public fixture ownership')
            fd = os.open(
                'source.wav', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
            )
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                    or not 0 < info.st_size <= MAX_BYTES
                ):
                    raise ValueError('Invalid public fixture file')
                artifact = source.read(MAX_BYTES + 1)
        finally:
            os.close(parent_fd)
    finally:
        os.close(root_fd)
    if len(artifact) > MAX_BYTES or hashlib.sha256(artifact).hexdigest() != clean['wav_sha256']:
        raise ValueError('Public fixture file mismatch')
    try:
        pcm = _read_pcm(io.BytesIO(artifact))
    except (EOFError, wave.Error):
        raise ValueError('Invalid public fixture audio') from None
    if len(pcm) != clean['frames'] * 2 or hashlib.sha256(pcm).hexdigest() != clean['pcm_sha256']:
        raise ValueError('Public fixture samples mismatch')
    return pcm


class KnownSpeechWindow:
    """One six-second raw capture window, independently of VAD and recognition."""

    def __init__(self, pcm, metadata, generation, epoch, *, near_only=False, timer=threading.Timer):
        clean = validate_prepared_speech(metadata)
        if (
            not isinstance(pcm, bytes)
            or len(pcm) != clean['frames'] * 2
            or hashlib.sha256(pcm).hexdigest() != clean['pcm_sha256']
            or any(type(n) is not int or n <= 0 for n in (generation, epoch))
        ):
            raise ValueError('Invalid known speech reference')
        reference = np.frombuffer(pcm, dtype='<i2').astype(float) / 32768
        offset = next(
            (
                i
                for i in range(0, len(reference) - WINDOW + 1, 320)
                if np.std(reference[i : i + 320]) >= MIN_REFERENCE_RMS
            ),
            None,
        )
        if offset is None:
            reference.fill(0)
            raise ValueError('Public speech window unavailable')
        self.lock = threading.RLock()
        self.token = uuid.uuid4().hex
        self.generation, self.epoch = generation, epoch
        self.near_only = near_only
        self.template = reference[offset : offset + WINDOW].copy()
        reference.fill(0)
        self.offset = offset
        self.input_digest = clean['pcm_sha256']
        self.mic = np.empty(MAX_FRAMES)
        self.render = np.empty(0 if near_only else MAX_FRAMES)
        self.mic_count = self.render_count = 0
        self.mic_start = self.render_start = None
        self.render_frame = None
        self.result = None
        self.cancelled = None
        self.analysing = False
        self.stimulus_boundary = None
        self.stimulus_delta = None
        self.preflight_pending = False
        self.timer = timer(8, lambda: self.cancel('expired'))
        self.timer.daemon = True
        try:
            self.timer.start()
        except BaseException:
            self.cancel('timer_unavailable')
            raise

    def cancel(self, reason='cancelled'):
        if reason not in STATUSES or reason in (
            'observed',
            'ambiguous',
            'not_observed',
            'reference_not_distinct',
            'capture_incomplete',
            'analysing',
        ):
            raise ValueError('Invalid known speech cancellation')
        with self.lock:
            self.cancelled = reason
            self.timer.cancel()
            for audio in (self.mic, self.render, self.template):
                audio.fill(0)
            self.mic = self.render = self.template = np.empty(0)

    def _receive(self, kind, nanos, pcm):
        if self.cancelled is not None or self.analysing or self.result is not None:
            return
        if (
            type(nanos) is not int
            or nanos <= 0
            or not isinstance(pcm, np.ndarray)
            or pcm.ndim != 1
            or not len(pcm)
            or not np.isfinite(pcm).all()
        ):
            self.cancel('invalid_audio')
            return
        count = getattr(self, kind + '_count')
        start = getattr(self, kind + '_start')
        if start is not None and abs(nanos - (start + round(count * 1e9 / RATE))) > 2_000_000:
            self.cancel(kind + '_discontinuous')
            return
        if count + len(pcm) > MAX_FRAMES:
            self.cancel(kind + '_limit_exceeded')
            return
        if start is None:
            setattr(self, kind + '_start', nanos)
        getattr(self, kind)[count : count + len(pcm)] = pcm
        setattr(self, kind + '_count', count + len(pcm))

    def microphone(self, epoch, nanos, pcm):
        with self.lock:
            if epoch == self.epoch:
                self._receive('mic', nanos, pcm)

    def reference(self, generation, frame, nanos, pcm):
        with self.lock:
            if self.near_only:
                if self.cancelled is None and self.result is None:
                    self.cancel('unexpected_render')
                return
            if generation != self.generation or self.cancelled or self.analysing or self.result:
                return
            if (
                type(frame) is not int
                or frame < 0
                or (self.render_frame is not None and frame != self.render_frame)
            ):
                self.cancel('render_discontinuous')
                return
            self._receive('render', nanos, pcm)
            if self.cancelled is None:
                self.render_frame = frame + len(pcm)

    def mark_stimulus(self, *, verify_far=False):
        """Freeze samples already received before the client may start playback.

        No fitting occurs under the audio lock. Repeated calls cannot move the
        causal boundary into later mixed speech or reuse a completed owner.
        """
        from causal_speech_model import MIN_PRE_FRAMES, analyse
        from known_speech_receipt import MIN_COMMON_FRAMES, estimated_overlap

        with self.lock:
            if (
                self.near_only
                or self.cancelled
                or self.analysing
                or self.result
                or self.stimulus_boundary is not None
            ):
                return {'status': 'unavailable'}
            if min(self.mic_count, self.render_count) < MIN_PRE_FRAMES:
                return {'status': 'insufficient_audio'}
            boundary = {
                'status': 'recorded',
                'pre_stimulus_mic_frames': self.mic_count,
                'pre_stimulus_render_frames': self.render_count,
            }
            if not verify_far:
                self.stimulus_boundary = (self.mic_count, self.render_count)
                return boundary
            delta = round((self.mic_start - self.render_start) * RATE / 1e9)
            overlap = estimated_overlap(self.mic_count, self.render_count, delta)
            coverage = {
                'estimated_mic_start_relative_to_render_frames': delta,
                'estimated_overlap_frames': overlap,
                'overlap_reference_rms': None,
            }
            if overlap < MIN_COMMON_FRAMES:
                return {
                    'status': 'awaiting_overlap',
                    'boundary': {**boundary, 'status': 'pending'},
                    'coverage': coverage,
                }
            mic = self.mic[: self.mic_count].copy()
            render = self.render[: self.render_count].copy()
            self.stimulus_boundary = (len(mic), len(render))
            self.stimulus_delta = delta
            self.preflight_pending = True

        try:
            left = max(0, delta)
            coverage['overlap_reference_rms'] = round(
                float(np.std(render[left : left + overlap])), 6
            )
            result = {
                'status': 'checked',
                'boundary': boundary,
                'coverage': coverage,
                'transfer': analyse(
                    mic, render, None, len(mic), len(render), estimated_delta=delta
                ),
            }
        finally:
            mic.fill(0)
            render.fill(0)
            with self.lock:
                self.preflight_pending = False
        with self.lock:
            return {'status': 'unavailable'} if self.cancelled else result

    def read(self):
        # Transfer buffers to this IPC caller; no FFT on the audio ingress path.
        with self.lock:
            if self.cancelled is not None:
                return self._receipt(self.cancelled)
            if self.result is not None:
                return copy.deepcopy(self.result)
            if self.analysing or self.preflight_pending:
                return self._receipt('analysing')
            self.analysing = True
            self.timer.cancel()
            mic, render, template = self.mic, self.render, self.template
            self.mic = self.render = self.template = np.empty(0)
        try:
            result = self._analyse(mic[: self.mic_count], render[: self.render_count], template)
            if self.stimulus_boundary is not None:
                from causal_speech_model import analyse

                result['far_transfer'] = analyse(
                    mic[: self.mic_count],
                    render[: self.render_count],
                    template,
                    *self.stimulus_boundary,
                    estimated_delta=self.stimulus_delta,
                )
        finally:
            for audio in (mic, render, template):
                audio.fill(0)
        with self.lock:
            self.result = self._receipt(self.cancelled) if self.cancelled else result
            return copy.deepcopy(self.result)

    def _receipt(self, status, **fields):
        return {
            'schema': 1,
            'method': METHOD,
            'status': status,
            'captured_frames': self.mic_count,
            'reference_frames': self.render_count,
            'template_offset_frames': self.offset,
            'template_frames': WINDOW,
            'input_pcm_sha256': self.input_digest,
            'raw_audio_saved': False,
            'first_audible_onset_measured': False,
            'physical_double_talk_confirmed': False,
            **({'near_only': True} if self.near_only else {}),
            **fields,
        }

    def _analyse(self, mic, render, template):
        if len(mic) < WINDOW + 320 or (not self.near_only and len(render) < WINDOW + 320):
            return self._receipt('capture_incomplete')
        peak, confidence, rival, status = match_window(mic[::2], template[::2])
        cross = 0.0
        if not self.near_only:
            _, cross, _, _ = match_window(render[::2], template[::2])
        fields = dict(
            confidence=round(confidence, 4),
            rival_confidence=round(rival, 4),
            render_similarity=round(cross, 4),
        )
        if round(cross, 4) >= 0.45:
            return self._receipt('reference_not_distinct', **fields)
        if status != 'observed':
            return self._receipt(status, **fields)
        # NCC ignores amplitude. An almost silent copy of the fixture must not
        # become acoustic confirmation solely because its shape is identical.
        if np.std(mic[peak : peak + WINDOW]) < MIN_CAPTURE_RMS:
            return self._receipt('not_observed', **fields)
        # Describe energetic digital reference over the matched capture window.
        # No acoustic far-end delay is assumed or subtracted here.
        overlap = 0
        if not self.near_only:
            start = self.mic_start + round(peak * 1e9 / RATE)
            render_offset = round((start - self.render_start) * RATE / 1e9)
            left, right = max(0, render_offset), min(len(render), render_offset + WINDOW)
            overlap = sum(
                min(160, right - i)
                for i in range(left, max(left, right), 160)
                if np.sqrt(np.mean(render[i : min(i + 160, right)] ** 2)) > 0.001
            )
        return self._receipt(
            'observed',
            match_start_offset_frames=peak,
            matched_window_digital_render_ms=round(overlap * 1000 / RATE, 3),
            **fields,
        )

"""Opt-in, bounded acoustic corroboration of one public playback probe.

Only already arriving MIC/RENDER packets are inspected. Device timestamps locate
a matching window; neither this correlation nor an AudioTrack ACK measures the
first audible sample or human dialogue latency. A separate leading-block check
requires independent shape matches in the already observed microphone samples.
Raw buffers never leave RAM.
"""

from dataclasses import dataclass, field
import math
import threading
import numpy as np
from dsp import RATE
from render_clock_report import RenderClockReceipt

PROBE_TEXT = '这是扬声器和麦克风的声学检查。请保持安静，检查结束后恢复正常。'
WINDOW = 3200  # 200 ms, long enough to reject most accidental short matches.
MIC_LIMIT = 48000
REFERENCE_LIMIT = 24000
MIN_DELAY = -1280  # Render-clock estimates can be slightly ahead of capture.
MAX_DELAY = 12800
LEADING_BLOCK = 320  # 20 ms at 16 kHz.
LEADING_LIMIT = 4000  # Search only the first 250 ms of the original reference.
RESPONSE_PREFIX_LIMIT = LEADING_LIMIT + WINDOW
PREFIX_AGREEMENT = 32  # 2 ms on the device capture clock.
PREFIX_SEARCH_RADIUS = 1280  # Inspect competing matches within 80 ms of the projected block.
MIN_REFERENCE_RMS = 0.01
MIN_CAPTURE_RMS = 0.002


def first_leading_block(reference):
    return next(
        (
            i
            for i in range(0, min(LEADING_LIMIT, len(reference) - LEADING_BLOCK + 1), LEADING_BLOCK)
            if np.std(reference[i : i + LEADING_BLOCK]) >= MIN_REFERENCE_RMS
        ),
        None,
    )


def block_correlation(expected, measured):
    expected = expected - expected.mean()
    measured = measured - measured.mean()
    if np.std(measured) < MIN_CAPTURE_RMS:
        return 0.0
    normalized = abs(np.dot(expected, measured)) / (
        np.linalg.norm(expected) * np.linalg.norm(measured)
    )
    return float(np.clip(normalized, 0, 1))


def match_window(audio, template):
    template = template - template.mean()
    # FFT convolution plus rolling variance gives gain/DC invariant NCC.
    size = 1 << (len(audio) + len(template) - 1).bit_length()
    covariance = np.fft.irfft(np.fft.rfft(audio, size) * np.fft.rfft(template[::-1], size), size)[
        len(template) - 1 : len(audio)
    ]
    sums = np.r_[0.0, np.cumsum(audio)]
    powers = np.r_[0.0, np.cumsum(audio * audio)]
    count = len(template)
    variance = powers[count:] - powers[:-count] - (sums[count:] - sums[:-count]) ** 2 / count
    energy = np.dot(template, template)
    scores = np.abs(covariance) / np.sqrt(np.maximum(variance * energy, 1e-18))
    scores = np.clip(scores, 0, 1)
    peak = int(np.argmax(scores))
    confidence = float(scores[peak])
    radius = round(0.03 * RATE / 2)
    scores[max(0, peak - radius) : peak + radius + 1] = 0
    rival = float(np.max(scores))
    status = (
        'observed'
        if confidence >= 0.55 and confidence - rival >= 0.12
        else 'ambiguous'
        if confidence >= 0.55
        else 'not_observed'
    )
    return peak * 2, confidence, rival, status


@dataclass(eq=False)
class Probe:
    generation: int
    epoch: int
    input_end_ns: int | None = None
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    render_clock: RenderClockReceipt = field(default_factory=RenderClockReceipt)


class PlaybackEvidence:
    def __init__(self):
        self.lock = threading.RLock()
        self.current = None
        self.timer = None
        self.mic = np.empty(0)
        self.reference = np.empty(0)
        self.response_prefix = np.empty(0)
        self.mic_start = None
        self.mic_end = None
        self.render_start = None
        self.render_end = None
        self.render_frame = None
        self.segment_frame = 0
        self.template_start = None
        self.template_offset = None
        self.clock_resets = 0
        self.max_clock_step_ms = 0

    def arm(self, generation, epoch, *, seconds=20, input_end_ns=None):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 20:
            raise ValueError('Invalid playback probe deadline')
        with self.lock:
            if self.current is not None:
                raise RuntimeError('Playback probe already active')
            probe = Probe(generation, epoch, input_end_ns=input_end_ns)
            self.current = probe
            self.clock_resets = 0
            self.max_clock_step_ms = 0
            self.timer = threading.Timer(seconds, lambda: self.cancel('expired', probe))
            self.timer.daemon = True
            try:
                self.timer.start()
            except RuntimeError:
                self._finish('timer_unavailable')
                raise
            return probe

    def cancel(self, status='cancelled', probe=None):
        with self.lock:
            if self.current is not None and (probe is None or probe is self.current):
                self._finish(status)

    def _finish(self, status, **evidence):
        probe = self.current
        if probe is None:
            return
        if self.timer is not None:
            self.timer.cancel()
        self.timer = None
        self.current = None
        self.mic.fill(0)
        self.reference.fill(0)
        self.response_prefix.fill(0)
        self.mic = np.empty(0)
        self.reference = np.empty(0)
        self.response_prefix = np.empty(0)
        self.mic_start = self.mic_end = None
        self.render_start = self.render_end = self.render_frame = None
        self.segment_frame = 0
        self.template_start = self.template_offset = None
        probe.result = {
            'schema': 2,
            'status': status,
            'generation': probe.generation,
            'capture_epoch': probe.epoch,
            'method': 'device_clock_normalized_window_correlation_v2',
            'render_clock_resets': self.clock_resets,
            'max_render_clock_step_ms': round(self.max_clock_step_ms, 3),
            'raw_audio_saved': False,
            'first_audible_onset_measured': False,
            'human_dialogue_latency_measured': False,
            'leading_acoustic_evidence': {'status': 'not_checked'},
            **evidence,
        }
        probe.done.set()

    def render(self, generation, frame, nanos, pcm):
        with self.lock:
            probe = self.current
            if probe is None or generation != probe.generation:
                return
            if not len(pcm) or not np.isfinite(pcm).all():
                self._finish('invalid_render')
                return
            if self.render_frame is None and frame != 0:
                self._finish('render_gap')
                return
            if self.render_frame is not None and frame != self.render_frame:
                self._finish('render_gap')
                return
            if probe.input_end_ns is not None and frame < RESPONSE_PREFIX_LIMIT:
                # Frame order is separate from timestamp estimates. This copy
                # may only be used if its waveform matches continuous capture.
                self.response_prefix = np.concatenate(
                    (self.response_prefix, pcm[: RESPONSE_PREFIX_LIMIT - frame])
                )
            if self.render_end is not None and abs(nanos - self.render_end) > 40_000_000:
                # AudioTrack can refine its clock or actually underrun. Neither
                # permits stitching old and new timestamps into one waveform.
                # Corroborate a fresh contiguous window after the discontinuity;
                # keep capture history, but discard the old reference template.
                self.clock_resets += 1
                self.max_clock_step_ms = max(
                    self.max_clock_step_ms, abs(nanos - self.render_end) / 1e6
                )
                self.reference.fill(0)
                self.reference = np.empty(0)
                self.template_start = self.template_offset = None
                self.render_start = nanos
                self.segment_frame = frame
            if self.render_start is None:
                self.render_start = nanos
            self.render_frame = frame + len(pcm)
            self.render_end = nanos + round(len(pcm) * 1e9 / RATE)
            keep = (
                REFERENCE_LIMIT
                if self.template_offset is None
                else LEADING_LIMIT + 2 * LEADING_BLOCK
            )
            if len(self.reference) < keep:
                self.reference = np.concatenate((self.reference, pcm[: keep - len(self.reference)]))
            if self.template_offset is None:
                for offset in range(0, len(self.reference) - WINDOW + 1, 160):
                    template = self.reference[offset : offset + WINDOW]
                    if np.std(template) >= MIN_REFERENCE_RMS:
                        self.template_offset = offset
                        self.template_start = self.render_start + round(offset * 1e9 / RATE)
                        break
                if self.template_offset is None and len(self.reference) == REFERENCE_LIMIT:
                    self._finish('insufficient_reference')
                    return
            self._analyze()

    def microphone(self, epoch, nanos, pcm):
        with self.lock:
            probe = self.current
            if probe is None or epoch != probe.epoch:
                return
            if not len(pcm) or not np.isfinite(pcm).all():
                self._finish('invalid_capture')
                return
            if self.mic_end is not None and abs(nanos - self.mic_end) > 2_000_000:
                self._finish('capture_clock_gap')
                return
            if self.mic_start is None:
                self.mic_start = nanos
            self.mic_end = nanos + round(len(pcm) * 1e9 / RATE)
            # Inputs belong to the capture pipeline. Own copies only; cleanup
            # must never zero the live AEC/recognition buffers.
            joined = np.concatenate((self.mic, pcm))
            dropped = max(0, len(joined) - MIC_LIMIT)
            self.mic = joined[dropped:].copy()
            self.mic_start += round(dropped * 1e9 / RATE)
            self._analyze()

    def _leading_evidence(self, aligned_origin, reference, offset):
        if offset is None:
            return {'status': 'reference_not_found'}
        capture_offset = aligned_origin + offset
        end = offset + 2 * LEADING_BLOCK
        if capture_offset < 0 or capture_offset + 2 * LEADING_BLOCK > len(self.mic):
            return {'status': 'capture_prefix_missing'}
        if end > len(reference):
            return {'status': 'reference_not_sustained'}

        correlations = []
        # Check both leading blocks at the long-window alignment. Finding an
        # unrelated short peak at another delay cannot create onset evidence.
        for start in (offset, offset + LEADING_BLOCK):
            expected = reference[start : start + LEADING_BLOCK][::2]
            if np.std(expected) < MIN_REFERENCE_RMS:
                return {'status': 'reference_not_sustained'}
            start += aligned_origin
            measured = self.mic[start : start + LEADING_BLOCK][::2]
            correlations.append(block_correlation(expected, measured))
        confidence = min(correlations)
        if confidence < 0.55:
            return {'status': 'leading_match_unconfirmed'}
        detected_nanos = self.mic_start + round(capture_offset * 1e9 / RATE)
        evidence = {
            'status': 'observed',
            'block_ms': 20,
            'consecutive_blocks': 2,
            'minimum_correlation': round(confidence, 4),
            'reference_block_start_ms': offset * 1000 // RATE,
        }
        if not self.clock_resets:
            evidence['capture_minus_render_origin_ms'] = round(
                (detected_nanos - self.render_start) / 1e6, 3
            )
        if self.current.input_end_ns is not None:
            elapsed = detected_nanos - self.current.input_end_ns
            if 0 <= elapsed <= 20_000_000_000:
                evidence['last_voiced_to_confirmed_output_block_ms'] = round(elapsed / 1e6, 3)
        return evidence

    def _response_leading_after_clock_jump(self, matched_capture_frame):
        if self.current.input_end_ns is None:
            return {'status': 'render_clock_discontinuous'}
        offset = first_leading_block(self.response_prefix)
        if offset is None or len(self.response_prefix) < offset + WINDOW:
            return {'status': 'response_prefix_reference_missing'}
        # An observed later segment gives an independent capture-time anchor.
        # If the first waveform is contiguous with it, both matches project
        # the same origin from their original render frame numbers.
        projected = matched_capture_frame - (self.segment_frame + self.template_offset)
        left = projected + offset - PREFIX_SEARCH_RADIUS
        right = projected + offset + PREFIX_SEARCH_RADIUS + WINDOW
        if left < 0 or right > len(self.mic):
            return {'status': 'capture_prefix_missing'}
        template = self.response_prefix[offset : offset + WINDOW][::2]
        if np.std(template) < MIN_REFERENCE_RMS:
            return {'status': 'response_prefix_reference_missing'}
        peak, confidence, rival, status = match_window(self.mic[left:right:2], template)
        origin = left + peak - offset
        if status != 'observed':
            return {'status': 'response_prefix_match_unconfirmed'}
        leading = self._verify_response_prefix(origin, offset)
        difference = origin - projected
        if abs(difference) > PREFIX_AGREEMENT:
            return {
                'status': 'response_prefix_alignment_mismatch',
                'anchor_diagnostic': {
                    'prefix_minus_projected_ms': round(difference * 1000 / RATE, 3),
                    'reference_anchor_spacing_ms': round(
                        (self.segment_frame + self.template_offset - offset) * 1000 / RATE, 3
                    ),
                    'prefix_correlation': round(confidence, 4),
                    'prefix_competing_correlation': round(rival, 4),
                    'prefix_block_status': leading['status'],
                },
            }
        if leading['status'] == 'observed':
            leading['alignment'] = 'matched_render_frames'
        return leading

    def _verify_response_prefix(self, origin, offset):
        leading = self._leading_evidence(origin, self.response_prefix, offset)
        if leading['status'] != 'observed':
            return leading
        # The aggregate can be high even when an audible block near the end
        # of the prefix is missing. Verify each remaining energetic block.
        for start in range(offset + 2 * LEADING_BLOCK, offset + WINDOW, LEADING_BLOCK):
            expected = self.response_prefix[start : start + LEADING_BLOCK][::2]
            capture = origin + start
            measured = self.mic[capture : capture + LEADING_BLOCK][::2]
            if (
                np.std(expected) >= MIN_REFERENCE_RMS
                and block_correlation(expected, measured) < 0.55
            ):
                return {'status': 'response_prefix_discontinuous'}
        return leading

    def _analyze(self):
        if self.template_start is None or self.mic_start is None:
            return
        left = round((self.template_start - self.mic_start) * RATE / 1e9) + MIN_DELAY
        right = left + MAX_DELAY - MIN_DELAY + WINDOW
        if right > len(self.mic):
            return
        if left < 0:
            self._finish('insufficient_capture_coverage')
            return
        template = self.reference[self.template_offset : self.template_offset + WINDOW][::2]
        peak, confidence, rival, status = match_window(self.mic[left:right:2], template)
        leading = {'status': 'not_checked'}
        if status == 'observed':
            if self.clock_resets:
                leading = self._response_leading_after_clock_jump(left + peak)
            else:
                leading = self._leading_evidence(
                    left + peak - self.template_offset,
                    self.reference,
                    first_leading_block(self.reference),
                )
        self._finish(
            status,
            leading_acoustic_evidence=leading,
            correlation=round(confidence, 4),
            competing_correlation=round(rival, 4),
            matching_window_ms=200,
            search_delay_ms=[-80, 800],
            matched_window_delay_ms=round((MIN_DELAY + peak) * 1000 / RATE, 3)
            if status == 'observed'
            else None,
        )

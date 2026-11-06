"""Fixed, content-free acoustic observations, never a speaker/intent classifier."""

from collections import deque
import math

COUNTS = frozenset(
    (
        'frames',
        'voiced_frames',
        'credible_frames',
        'render_frames',
        'pause_requested_frames',
        'voiced_after_pause_request_frames',
        'raw_correlated_frames',
        'residual_correlated_frames',
    )
)
TEMPORAL_COUNTS = frozenset(
    ('max_credible_run_frames', 'credible_with_render_frames', 'credible_without_render_frames')
)
ONSET_PREFIX_COUNTS = frozenset(
    (
        'onset_prefix_frames',
        'onset_prefix_credible_frames',
        'onset_prefix_render_frames',
        'onset_prefix_credible_with_render_frames',
    )
)
# VAD retains at most 50 pre-roll frames including the frame that begins the
# utterance. These separate counters cover only earlier continuous voiced
# frames; they never enlarge or replace the onset-to-end capture counters.
MAX_ONSET_PREFIX_FRAMES = 49
LEVELS = frozenset(('raw_peak_dbfs', 'cleaned_peak_dbfs', 'render_peak_dbfs'))
CORRELATIONS = frozenset(('max_raw_correlation', 'max_residual_correlation'))
FLAGS = frozenset(('echo_risk', 'during_playback', 'local_wake'))
VAD_TAIL = 'device_vad_tail_ms'
FIELDS = COUNTS | LEVELS | CORRELATIONS | FLAGS
FRAME_NS = 10_000_000
FRAME_JITTER_NS = 2_000_000
DECISIONS = frozenset(
    (
        'accepted',
        'empty',
        'echo_text',
        'echo_render',
        'weak_near',
        'wake_closed',
        'wake_only',
        'wake_sleep',
        'tool_sample_guard',
        'calibration',
        'stale',
        'error',
    )
)
TOOL_TRANSCRIPT_MISMATCHES = (
    'transcript_mismatch',
    'formatting_mismatch',
    'read_only_missing',
    'tool_instruction_mismatch',
    'arithmetic_mismatch',
    'arithmetic_form_mismatch',
)
TOOL_SAMPLE_REJECTION_REASONS = (
    'comparison_only',
    'not_open',
    'capture_changed',
    'generation_changed',
    'route_changed',
    'local_wake_missing',
    'capture_window',
) + TOOL_TRANSCRIPT_MISMATCHES
TOOL_ASR_MATCH_RESULTS = ('matched', 'empty') + TOOL_TRANSCRIPT_MISMATCHES
TOOL_ASR_LOCAL_RESULTS = TOOL_ASR_MATCH_RESULTS + ('error', 'timeout')


def valid_tool_asr_comparison(value, reason='comparison_only'):
    return (
        isinstance(value, dict)
        and (
            set(value) == {'primary', 'local'}
            or (
                set(value) == {'primary', 'local', 'local_engine'}
                and value['local_engine'] == 'sensevoice'
            )
        )
        and isinstance(value['primary'], str)
        and value['primary'] in TOOL_ASR_MATCH_RESULTS
        and (
            reason == 'comparison_only'
            or reason in TOOL_TRANSCRIPT_MISMATCHES
            and reason == value['primary']
        )
        and isinstance(value['local'], str)
        and value['local'] in TOOL_ASR_LOCAL_RESULTS
    )


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) - {VAD_TAIL} - TEMPORAL_COUNTS - ONSET_PREFIX_COUNTS != FIELDS
    ):
        raise ValueError('Invalid capture evidence fields')
    prefix = value.keys() & ONSET_PREFIX_COUNTS
    if prefix:
        validate_onset_prefix({k: value[k] for k in prefix})
    temporal = value.keys() & TEMPORAL_COUNTS
    if temporal and temporal != TEMPORAL_COUNTS:
        raise ValueError('Incomplete temporal capture evidence')
    for key in COUNTS | temporal:
        if type(value[key]) is not int or not 0 <= value[key] <= 2000:
            raise ValueError('Invalid capture frame count')
    frames = value['frames']
    if not frames or any(value[k] > frames for k in COUNTS):
        raise ValueError('Invalid capture frame coverage')
    if value['credible_frames'] > value['voiced_frames']:
        raise ValueError('Invalid credible frame count')
    if temporal and (
        value['credible_with_render_frames'] + value['credible_without_render_frames']
        != value['credible_frames']
        or value['credible_with_render_frames'] > value['render_frames']
        or value['credible_without_render_frames'] > frames - value['render_frames']
        or value['max_credible_run_frames'] > value['credible_frames']
        or bool(value['max_credible_run_frames']) != bool(value['credible_frames'])
    ):
        raise ValueError('Invalid temporal capture coverage')
    if value['voiced_after_pause_request_frames'] > min(
        value['voiced_frames'], value['pause_requested_frames']
    ):
        raise ValueError('Invalid post-pause frame count')
    if any(
        value[k] > value['render_frames']
        for k in ('raw_correlated_frames', 'residual_correlated_frames')
    ):
        raise ValueError('Invalid correlated frame count')
    for keys, lower, upper in ((LEVELS, -120, 60), (CORRELATIONS, 0, 1)):
        for key in keys:
            item = value[key]
            if (
                type(item) not in (int, float)
                or not lower <= item <= upper
                or not math.isfinite(item)
            ):
                raise ValueError('Invalid capture measurement')
    if any(type(value[k]) is not bool for k in FLAGS):
        raise ValueError('Invalid capture flags')
    if VAD_TAIL in value:
        tail = value[VAD_TAIL]
        max_tail = (frames - value['voiced_frames']) * (FRAME_NS + FRAME_JITTER_NS) / 1e6
        if (
            not value['voiced_frames']
            or type(tail) not in (int, float)
            or not 0 <= tail <= max_tail
        ):
            raise ValueError('Invalid device VAD tail timing')
    return dict(value)


def validate_onset_prefix(value):
    if (
        not isinstance(value, dict)
        or set(value) != ONSET_PREFIX_COUNTS
        or any(
            type(value[k]) is not int or not 0 <= value[k] <= MAX_ONSET_PREFIX_FRAMES
            for k in ONSET_PREFIX_COUNTS
        )
    ):
        raise ValueError('Invalid onset prefix counts')
    frames = value['onset_prefix_frames']
    credible = value['onset_prefix_credible_frames']
    render = value['onset_prefix_render_frames']
    overlap = value['onset_prefix_credible_with_render_frames']
    if (
        max(credible, render) > frames
        or overlap > min(credible, render)
        or overlap < credible + render - frames
    ):
        raise ValueError('Invalid onset prefix coverage')
    return dict(value)


class OnsetPrefix:
    """Hold at most 49 pairs of flags before VAD starts, never PCM or text."""

    def __init__(self):
        self.frames = deque(maxlen=MAX_ONSET_PREFIX_FRAMES)
        self.previous_ns = None

    def continuous(self, stamp):
        return (
            self.previous_ns is not None
            and abs(stamp - self.previous_ns - FRAME_NS) <= FRAME_JITTER_NS
        )

    def feed(self, frame, *, voiced, credible):
        from dsp import level

        if not voiced or not self.continuous(frame.nanos):
            self.frames.clear()
        self.previous_ns = frame.nanos
        if voiced:
            self.frames.append((bool(credible), level(frame.reference) > -60))

    def before(self, stamp, *, voiced):
        frames = self.frames if voiced and self.continuous(stamp) else ()
        return validate_onset_prefix(
            dict(
                onset_prefix_frames=len(frames),
                onset_prefix_credible_frames=sum(c for c, r in frames),
                onset_prefix_render_frames=sum(r for c, r in frames),
                onset_prefix_credible_with_render_frames=sum(c and r for c, r in frames),
            )
        )


class CaptureEvidence:
    """Aggregate onset-to-end frames only; no PCM/pre-roll retention or decisions."""

    def __init__(self, *, onset_prefix=None):
        from dsp import level

        self.level = level
        self.values = {key: 0 for key in COUNTS | CORRELATIONS | TEMPORAL_COUNTS}
        if onset_prefix is not None:
            self.values.update(validate_onset_prefix(onset_prefix))
        self.values.update({key: -120.0 for key in LEVELS})
        self.previous_ns = None
        self.last_voiced_ns = None
        self.contiguous = True
        self.credible_run = 0

    def feed(self, frame, near, *, voiced, credible, pause_requested):
        raw, cleaned, render = (self.level(x) for x in (frame.raw, frame.cleaned, frame.reference))
        correlations = (near.correlation, near.residual_correlation)
        if not all(math.isfinite(x) and -120 <= x <= 60 for x in (raw, cleaned, render)):
            raise ValueError('Invalid frame level observation')
        if not all(math.isfinite(x) and 0 <= x <= 1.000001 for x in correlations):
            raise ValueError('Invalid frame correlation observation')
        value = self.values
        stamp = frame.nanos
        if self.previous_ns is not None:
            contiguous = abs(stamp - self.previous_ns - FRAME_NS) <= FRAME_JITTER_NS
            self.contiguous &= contiguous
            if not contiguous:
                self.credible_run = 0
        self.previous_ns = stamp
        if voiced:
            self.last_voiced_ns = stamp
        value['frames'] += 1
        value['voiced_frames'] += int(voiced)
        value['credible_frames'] += int(credible)
        value['pause_requested_frames'] += int(pause_requested)
        value['voiced_after_pause_request_frames'] += int(pause_requested and voiced)
        rendering = render > -60
        value['render_frames'] += int(rendering)
        value['credible_with_render_frames'] += int(credible and rendering)
        value['credible_without_render_frames'] += int(credible and not rendering)
        self.credible_run = self.credible_run + 1 if credible else 0
        value['max_credible_run_frames'] = max(value['max_credible_run_frames'], self.credible_run)
        value['raw_correlated_frames'] += int(rendering and near.correlation > 0.78)
        value['residual_correlated_frames'] += int(rendering and near.residual_correlation > 0.60)
        for key, db in zip(
            ('raw_peak_dbfs', 'cleaned_peak_dbfs', 'render_peak_dbfs'), (raw, cleaned, render)
        ):
            value[key] = max(value[key], round(db, 1))
        for key, correlation in (
            ('max_raw_correlation', near.correlation),
            ('max_residual_correlation', near.residual_correlation),
        ):
            # A normalized correlation may exceed 1 by floating-point roundoff.
            value[key] = max(value[key], round(min(1.0, correlation), 4))

    def snapshot(self, *, echo_risk, during_playback, local_wake):
        evidence = {
            **self.values,
            'echo_risk': bool(echo_risk),
            'during_playback': bool(during_playback),
            'local_wake': bool(local_wake),
        }
        if self.contiguous and self.last_voiced_ns is not None:
            # Frames have the same duration, so subtracting their start stamps
            # also gives the interval between their ends on the capture clock.
            evidence[VAD_TAIL] = round((self.previous_ns - self.last_voiced_ns) / 1e6, 3)
        return validate(evidence)

    def last_voiced_end(self):
        if self.contiguous and self.last_voiced_ns is not None:
            return self.last_voiced_ns + FRAME_NS
        return None

"""Fixed, content-free acoustic observations, never a speaker/intent classifier."""

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
        'calibration',
        'stale',
        'error',
    )
)


def validate(value):
    if not isinstance(value, dict) or set(value) - {VAD_TAIL} != FIELDS:
        raise ValueError('Invalid capture evidence fields')
    for key in COUNTS:
        if type(value[key]) is not int or not 0 <= value[key] <= 2000:
            raise ValueError('Invalid capture frame count')
    frames = value['frames']
    if not frames or any(value[k] > frames for k in COUNTS):
        raise ValueError('Invalid capture frame coverage')
    if value['credible_frames'] > value['voiced_frames']:
        raise ValueError('Invalid credible frame count')
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


class CaptureEvidence:
    """Aggregate onset-to-end frames only; no PCM/pre-roll retention or decisions."""

    def __init__(self):
        from dsp import level

        self.level = level
        self.values = {key: 0 for key in COUNTS | CORRELATIONS}
        self.values.update({key: -120.0 for key in LEVELS})
        self.previous_ns = None
        self.last_voiced_ns = None
        self.contiguous = True

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
            self.contiguous &= abs(stamp - self.previous_ns - FRAME_NS) <= FRAME_JITTER_NS
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

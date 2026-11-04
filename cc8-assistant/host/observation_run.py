"""Whitelisted health curves; evidence of sampling, never proof of physical tests."""

from collections import Counter
from copy import deepcopy
import math
import re

GAUGES = {
    'rss_bytes': 2**50,
    'peak_rss_bytes': 2**50,
    'python_threads': 10**6,
    'open_fds': 10**6,
    'cpu_seconds': 10**12,
}
FLAGS = (
    'connected',
    'microphone_active',
    'playing',
    'calibrating',
    'recognition_worker',
    'response_worker',
    'model_ready',
)


def number(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError('Invalid health gauge')
    return value


def health_sample(payload):
    if not isinstance(payload, dict) or payload.get('ok') is not True:
        raise ValueError('Invalid health snapshot')
    result = {}
    for name in FLAGS:
        if type(payload.get(name)) is not bool:
            raise ValueError('Invalid health state')
        result[name] = payload[name]
    if payload.get('transport') not in ('usb', 'wifi', 'none'):
        raise ValueError('Invalid health transport')
    result['transport'] = payload['transport']
    result['uptime_seconds'] = number(payload.get('uptime_seconds'), 10**10)
    for name in ('mic_frames', 'render_frames'):
        value = payload.get(name)
        if type(value) is not int:
            raise ValueError('Invalid health counter')
        result[name] = number(value, 2**63 - 1)
    waiting = payload.get('waiting_for_wake')
    if waiting is not None and type(waiting) is not bool:
        raise ValueError('Invalid health wake state')
    result['waiting_for_wake'] = waiting
    instance = payload.get('runtime_instance')
    if instance is not None and (
        not isinstance(instance, str) or not re.fullmatch('[a-f0-9]{32}', instance)
    ):
        raise ValueError('Invalid runtime instance')
    resources = payload.get('resources', {})
    if not isinstance(resources, dict):
        raise ValueError('Invalid resource snapshot')
    for name, maximum in GAUGES.items():
        value = resources.get(name)
        result[name] = number(value, maximum) if value is not None else None
        if (
            name in ('rss_bytes', 'peak_rss_bytes', 'python_threads', 'open_fds')
            and value is not None
            and type(value) is not int
        ):
            raise ValueError('Invalid integer gauge')
    return instance, result


def trend(rows, field):
    values = [(row['elapsed_seconds'], row[field]) for row in rows if row.get(field) is not None]
    if not values:
        return {
            'samples': 0,
            'first': None,
            'last': None,
            'min': None,
            'max': None,
            'delta': None,
            'slope_per_hour': None,
        }
    xs, ys = zip(*values)
    xmean, ymean = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - xmean) ** 2 for x in xs)
    slope = (
        sum((x - xmean) * (y - ymean) for x, y in values) / denominator * 3600
        if denominator
        else None
    )
    return {
        'samples': len(values),
        'first': ys[0],
        'last': ys[-1],
        'min': min(ys),
        'max': max(ys),
        'delta': ys[-1] - ys[0],
        'slope_per_hour': round(slope, 3) if slope is not None else None,
    }


class ObservationRun:
    def __init__(self, interval, *, limit=6000):
        self.interval = interval
        self.limit = limit
        self.rows = []
        self.last_instance = None
        self.segment = 0
        self.last_success = None
        self.gaps = 0
        self.clock_discontinuities = 0
        self.uptime_discontinuities = 0
        # Callers supply both clocks relative to the start of this window.
        self.last_wall = 0
        self.last_elapsed = 0

    def _clock_breaks(self, elapsed, wall_elapsed):
        number(elapsed, 10**8)
        if (
            type(wall_elapsed) not in (int, float)
            or not math.isfinite(wall_elapsed)
            or abs(wall_elapsed) > 10**8
        ):
            raise ValueError('Invalid wall-clock sample')
        gap = elapsed - self.last_elapsed
        if gap < 0:
            raise ValueError('Non-monotonic observation')
        tolerance = max(5, 3 * self.interval)
        return gap > tolerance, abs(wall_elapsed - self.last_wall - gap) > tolerance

    def add(self, payload, elapsed, wall_elapsed, *, error=None):
        if len(self.rows) >= self.limit:
            raise ValueError('Observation capacity reached')
        gap, clock_break = self._clock_breaks(elapsed, wall_elapsed)
        instance, sample = (None, None) if error else health_sample(payload)
        if error not in (None, 'ipc_unavailable', 'invalid_health', 'backend_version_mismatch'):
            raise ValueError('Invalid observation error')
        self.gaps += gap
        self.clock_discontinuities += clock_break
        row = {
            'elapsed_seconds': round(elapsed, 3),
            'wall_elapsed_seconds': round(wall_elapsed, 3),
            'error': error,
        }
        if sample is not None:
            if instance is not None and instance != self.last_instance:
                self.segment += 1
                self.last_success = None
            if instance is None:
                # Older backends have no process-instance proof; never derive
                # CPU or cross-restart slopes by assuming they are one process.
                segment = None
            else:
                segment = self.segment
            if (
                self.last_success is not None
                and instance == self.last_instance
                and instance is not None
                and sample['uptime_seconds'] < self.last_success['uptime_seconds']
            ):
                self.uptime_discontinuities += 1
                self.segment += 1
                segment = self.segment
                self.last_success = None
            row.update(sample)
            row['segment'] = segment
            row['cpu_percent_of_one_core'] = None
            if self.last_success and segment is not None:
                before = self.last_success
                duration = elapsed - before['elapsed_seconds']
                if (
                    duration > 0
                    and before['cpu_seconds'] is not None
                    and sample['cpu_seconds'] is not None
                    and sample['cpu_seconds'] >= before['cpu_seconds']
                ):
                    row['cpu_percent_of_one_core'] = round(
                        (sample['cpu_seconds'] - before['cpu_seconds']) / duration * 100, 3
                    )
            self.last_success = row
            self.last_instance = instance
        self.rows.append(row)
        self.last_elapsed, self.last_wall = elapsed, wall_elapsed

    def report(self, *, end=None):
        # The final sleep can cross a deadline before another health poll runs.
        # Account for its coverage without adding a synthetic health sample.
        final_gap, final_clock_break = (
            self._clock_breaks(*end) if end is not None else (False, False)
        )
        rows = deepcopy(self.rows)
        successful = [row for row in rows if row['error'] is None]
        grouped = {}
        for row in successful:
            if row['segment'] is not None:
                grouped.setdefault(row['segment'], []).append(row)
        segments = sorted(grouped)
        missing = sum(
            any(row[name] is None for name in GAUGES) or row['segment'] is None
            for row in successful
        )
        return {
            'schema': 1,
            'contains_content': False,
            'device_capture_started': False,
            'physical_handover_asserted': False,
            'standby_wake_asserted': False,
            'long_run_acceptance_asserted': False,
            'polls': len(rows),
            'successful_polls': len(successful),
            'errors': dict(Counter(row['error'] for row in rows if row['error'])),
            'resource_coverage_incomplete_polls': missing,
            'observed_runtime_segments': len(segments),
            'unhealthy_polls': sum(
                not row['recognition_worker']
                or not row['response_worker']
                or not row['model_ready']
                for row in successful
            ),
            'disconnected_polls': sum(not row['connected'] for row in successful),
            'poll_gaps': self.gaps + final_gap,
            'wall_clock_discontinuities': self.clock_discontinuities + final_clock_break,
            'uptime_discontinuities': self.uptime_discontinuities,
            'segments': [
                {
                    'segment': segment,
                    'gauges': {field: trend(grouped[segment], field) for field in GAUGES},
                }
                for segment in segments
            ],
            'samples': rows,
        }

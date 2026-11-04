"""Audit one saved 72-hour observation without querying or changing the runtime."""

import argparse
import json
from pathlib import Path
import re

from observation_run import FLAGS, GAUGES, number, trend
from private_output import read_private_json

DURATION = 72 * 3600
MAX_BYTES = 8 * 1024**2
# Uptime is sampled during a bounded 5 s IPC query. The observer timestamps
# its receipt afterwards; allow another 0.2 s for timestamp rounding.
MAX_UPTIME_OFFSET_SPAN = 5.2
NO_CLAIMS = (
    'contains_content',
    'device_capture_started',
    'physical_handover_asserted',
    'standby_wake_asserted',
    'long_run_acceptance_asserted',
)


def require(condition):
    if not condition:
        raise ValueError('Invalid observation report')


def load_report(path):
    return read_private_json(path, MAX_BYTES)


def audit(report, backend):
    require(type(report) is dict and type(report.get('schema')) is int and report['schema'] == 1)
    require(all(report.get(flag) is False for flag in NO_CLAIMS))
    window, rows = report.get('observation'), report.get('samples')
    require(type(window) is dict and type(rows) is list and len(rows) <= 6000)
    interval = number(window.get('interval_seconds'), 300)
    require(interval >= 0.05)
    end = number(window.get('elapsed_seconds'), 10**8)
    wall_end = window.get('wall_elapsed_seconds')
    require(type(wall_end) in (int, float) and -(10**8) <= wall_end <= 10**8)
    reasons = []
    if window.get('state') != 'completed':
        reasons.append('window_not_completed')
    if window.get('expected_backend_version') != backend:
        reasons.append('backend_version_not_bound')
    if window.get('requested_seconds') != DURATION or min(end, wall_end) < DURATION:
        reasons.append('window_shorter_than_72_hours')
    if interval > 60:
        reasons.append('poll_interval_over_one_minute')

    counts = dict.fromkeys(
        (
            'poll_gaps',
            'wall_clock_discontinuities',
            'uptime_discontinuities',
            'unhealthy_polls',
            'disconnected_polls',
            'resource_coverage_incomplete_polls',
        ),
        0,
    )
    previous_elapsed = previous_wall = 0
    tolerance = max(5, 3 * interval)
    segments, successful, errors = set(), [], {}
    last_uptime = {}
    uptime_offsets = {}

    def check_time(elapsed, wall):
        nonlocal previous_elapsed, previous_wall
        require(previous_elapsed <= elapsed <= end)
        gap = elapsed - previous_elapsed
        # Each stored timestamp is rounded to milliseconds. Treat a value
        # near the limit conservatively rather than rounding a gap away.
        counts['poll_gaps'] += gap >= tolerance - 0.001
        counts['wall_clock_discontinuities'] += abs(wall - previous_wall - gap) >= tolerance - 0.002
        previous_elapsed, previous_wall = elapsed, wall

    for row in rows:
        require(type(row) is dict and 'error' in row)
        elapsed = number(row.get('elapsed_seconds'), 10**8)
        wall = row.get('wall_elapsed_seconds')
        require(type(wall) in (int, float) and -(10**8) <= wall <= 10**8)
        check_time(elapsed, wall)
        error = row['error']
        if error is not None:
            require(error in ('ipc_unavailable', 'invalid_health', 'backend_version_mismatch'))
            errors[error] = errors.get(error, 0) + 1
            continue
        require(all(type(row.get(flag)) is bool for flag in FLAGS))
        segment = row.get('segment')
        require(segment is None or type(segment) is int and 1 <= segment <= len(rows))
        uptime = number(row.get('uptime_seconds'), 10**10)
        if segment is not None:
            segments.add(segment)
            counts['uptime_discontinuities'] += uptime < last_uptime.get(segment, 0)
            last_uptime[segment] = uptime
            offset = uptime - elapsed
            low, high = uptime_offsets.get(segment, (offset, offset))
            uptime_offsets[segment] = min(low, offset), max(high, offset)
        counts['unhealthy_polls'] += not all(
            row[flag] for flag in ('recognition_worker', 'response_worker', 'model_ready')
        )
        counts['disconnected_polls'] += not row['connected'] or row.get('transport') not in (
            'usb',
            'wifi',
        )
        for name, maximum in GAUGES.items():
            value = row.get(name)
            if value is not None:
                number(value, maximum)
                require(name == 'cpu_seconds' or type(value) is int)
        counts['resource_coverage_incomplete_polls'] += segment is None or any(
            row.get(name) is None for name in GAUGES
        )
        successful.append(row)

    check_time(end, wall_end)
    if not successful or segments != {1} or report.get('observed_runtime_segments') != 1:
        reasons.append('single_runtime_not_confirmed')
    if errors or report.get('errors') != {}:
        reasons.append('poll_errors')
    if report.get('polls') != len(rows) or report.get('successful_polls') != len(successful):
        reasons.append('poll_totals_inconsistent')
    reasons.extend(name for name, count in counts.items() if count or report.get(name) != 0)
    uptime_span = max((high - low for low, high in uptime_offsets.values()), default=0)
    if uptime_span > MAX_UPTIME_OFFSET_SPAN:
        reasons.append('uptime_clock_mismatch')

    # Retain usable trends only for a single process with complete gauge coverage.
    gauges = (
        {field: trend(successful, field) for field in GAUGES}
        if segments == {1}
        and not counts['resource_coverage_incomplete_polls']
        and uptime_span <= MAX_UPTIME_OFFSET_SPAN
        else {}
    )
    return {
        'scope': 'recorded_72h_backend_window',
        'window_verified': not reasons,
        'full_p0_acceptance': False,
        'polls': len(rows),
        'successful_polls': len(successful),
        'elapsed_seconds': end,
        'uptime_offset_span_seconds': round(uptime_span, 3),
        'errors': errors,
        'counts': counts,
        'reasons': reasons,
        'gauges': gauges,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--expect-backend', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8})){2}', args.expect_backend):
        parser.error('Expected a canonical backend version')
    try:
        result = audit(load_report(args.report), args.expect_backend)
    except (OSError, ValueError, OverflowError, RecursionError):
        print(json.dumps({'window_verified': False, 'reasons': ['invalid_private_report']}))
        return 2
    print(json.dumps(result, allow_nan=False))
    return 0 if result['window_verified'] else 2

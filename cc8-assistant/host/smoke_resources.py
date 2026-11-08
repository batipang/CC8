"""Bounded, content-free bridge gauges from health reads a check already performs.

These are measurements of the bridge process, excluding model/tool children.
Missing values and sampling gaps never become a memory-leak acceptance claim.
"""

from copy import deepcopy
import math

from observation_run import GAUGES, trend

INTERVAL = 60
CAPACITY = 256
MAX_SECONDS = 86400
SCOPE = 'host_bridge_process_only'
INTEGER_GAUGES = frozenset(GAUGES) - {'cpu_seconds'}


def valid_number(value, maximum):
    return type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value)


def gauge_row(payload, elapsed):
    resources = payload.get('resources')
    malformed = resources is not None and not isinstance(resources, dict)
    values = resources if isinstance(resources, dict) else {}
    row = {'elapsed_seconds': round(elapsed, 3), 'missing_gauges': [], 'invalid_gauges': []}
    for name, maximum in GAUGES.items():
        value = values.get(name)
        invalid = malformed or (
            value is not None
            and (
                not valid_number(value, maximum)
                or (name in INTEGER_GAUGES and type(value) is not int)
            )
        )
        row[name] = None if invalid else value
        if invalid:
            row['invalid_gauges'].append(name)
        elif value is None:
            row['missing_gauges'].append(name)
    return row


class ResourceEvidence:
    def __init__(self):
        self.rows = []
        self.latest = None
        self.next_due = 0
        self.dropped = 0

    def observe(self, payload, elapsed):
        row = gauge_row(payload, elapsed)
        self.latest = row
        if elapsed >= self.next_due:
            self.next_due = elapsed + INTERVAL
            # Reserve one slot for the last actual read, including cleanup.
            if len(self.rows) < CAPACITY - 1:
                self.rows.append(row)
            else:
                self.dropped += 1

    def report(self):
        rows = deepcopy(self.rows)
        if self.latest is not None:
            if rows and rows[-1]['elapsed_seconds'] == self.latest['elapsed_seconds']:
                rows[-1] = deepcopy(self.latest)
            else:
                rows.append(deepcopy(self.latest))
        return {
            'schema': 1,
            'scope': SCOPE,
            'interval_seconds': INTERVAL,
            'capacity': CAPACITY,
            'dropped_interval_samples': self.dropped,
            'samples': rows,
        }


def audit(evidence, elapsed):
    """Validate saved rows and recompute summaries; never trust saved totals."""
    if (
        not isinstance(evidence, dict)
        or set(evidence)
        != {
            'schema',
            'scope',
            'interval_seconds',
            'capacity',
            'dropped_interval_samples',
            'samples',
        }
        or type(evidence['schema']) is not int
        or evidence['schema'] != 1
        or evidence['scope'] != SCOPE
        or type(evidence['interval_seconds']) is not int
        or evidence['interval_seconds'] != INTERVAL
        or type(evidence['capacity']) is not int
        or evidence['capacity'] != CAPACITY
        or type(evidence['dropped_interval_samples']) is not int
        or not 0 <= evidence['dropped_interval_samples'] <= MAX_SECONDS // INTERVAL
        or not valid_number(elapsed, MAX_SECONDS)
        or not isinstance(evidence['samples'], list)
        or len(evidence['samples']) > CAPACITY
    ):
        raise ValueError('Invalid bridge resource evidence')
    rows = evidence['samples']
    if evidence['dropped_interval_samples'] and len(rows) != CAPACITY:
        raise ValueError('Invalid resource capacity evidence')
    previous = -1
    for index, row in enumerate(rows):
        if (
            not isinstance(row, dict)
            or set(row) != {'elapsed_seconds', 'missing_gauges', 'invalid_gauges', *GAUGES}
            or not valid_number(row['elapsed_seconds'], elapsed)
            or row['elapsed_seconds'] <= previous
            or (
                index
                and index < len(rows) - 1
                and row['elapsed_seconds'] - previous < INTERVAL - 0.001
            )
            or any(
                not isinstance(row[key], list)
                or row[key] != [name for name in GAUGES if name in row[key]]
                for key in ('missing_gauges', 'invalid_gauges')
            )
            or set(row['missing_gauges']) & set(row['invalid_gauges'])
        ):
            raise ValueError('Invalid bridge resource sample')
        previous = row['elapsed_seconds']
        for name, maximum in GAUGES.items():
            absent = name in row['missing_gauges'] or name in row['invalid_gauges']
            value = row[name]
            if (absent and value is not None) or (
                not absent
                and (
                    not valid_number(value, maximum)
                    or (name in INTEGER_GAUGES and type(value) is not int)
                )
            ):
                raise ValueError('Invalid bridge resource gauge')
    invalid = sum(bool(row['invalid_gauges']) for row in rows)
    missing = sum(bool(row['missing_gauges']) for row in rows)
    measured = sum(row[name] is not None for row in rows for name in GAUGES)
    trends = {name: trend(rows, name) for name in GAUGES}
    regressions = {}
    for name in ('cpu_seconds', 'peak_rss_bytes'):
        values = [row[name] for row in rows if row[name] is not None]
        regressions[name] = sum(b < a for a, b in zip(values, values[1:]))
        if regressions[name]:
            trends[name]['delta'] = trends[name]['slope_per_hour'] = None
    times = [0, *(row['elapsed_seconds'] for row in rows), elapsed]
    return {
        'scope': SCOPE,
        'status': 'unavailable'
        if not measured
        else 'partial'
        if invalid or missing or any(regressions.values()) or evidence['dropped_interval_samples']
        else 'observed',
        'samples': len(rows),
        'invalid_samples': invalid,
        'missing_samples': missing,
        'dropped_interval_samples': evidence['dropped_interval_samples'],
        'sample_span_seconds': round(times[-2] - times[1], 3) if rows else 0,
        'largest_gap_seconds': round(max(b - a for a, b in zip(times, times[1:])), 3),
        'cumulative_discontinuities': regressions,
        'gauges': trends,
        'long_term_stability_asserted': False,
    }

"""Accumulate validated content-free receipts beyond the recent IPC window."""

from collections import Counter
from copy import deepcopy
import math
import re
from diagnostics import ORIGINS, SCENARIOS, OUTCOMES, STAGES, summarize_requests
from operation_failure import STAGES as ERROR_STAGES

ERRORS = frozenset(
    (
        'rate_limit',
        'quota',
        'credential_or_permission',
        'approval_declined',
        'timeout',
        'transport_or_io',
        'provider_or_runtime',
    )
)
ROW_FIELDS = frozenset(
    (
        'request_id',
        'origin',
        'scenario',
        'generation',
        'stages',
        'outcome',
        'error',
        'active_stage',
        'turn_id',
        'task_outcome',
    )
)


class RunCapacityError(ValueError):
    pass


def enum(value, choices):
    return isinstance(value, str) and value in choices


def receipt(value):
    """Rebuild only known scalar/enum fields; never forward arbitrary dictionaries."""
    if (
        not isinstance(value, dict)
        or set(value) - ROW_FIELDS
        or not {'request_id', 'origin', 'scenario', 'generation', 'stages', 'outcome', 'error'}
        <= set(value)
    ):
        raise ValueError('Invalid diagnostic receipt fields')
    ident = value.get('request_id')
    if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident):
        raise ValueError('Invalid diagnostic request ID')
    if not enum(value.get('origin'), ORIGINS) or not enum(value.get('scenario'), SCENARIOS):
        raise ValueError('Invalid diagnostic category')
    generation = value.get('generation')
    if generation is not None and (type(generation) is not int or not 0 <= generation < 2**32):
        raise ValueError('Invalid diagnostic generation')
    outcome = value.get('outcome')
    if outcome is not None and not enum(outcome, OUTCOMES):
        raise ValueError('Invalid diagnostic outcome')
    stages = value.get('stages')
    if not isinstance(stages, dict) or set(stages) - STAGES:
        raise ValueError('Invalid diagnostic stages')
    clean_stages = {}
    for name, elapsed in stages.items():
        if (
            type(elapsed) not in (int, float)
            or not 0 <= elapsed <= 604800000
            or not math.isfinite(elapsed)
        ):
            raise ValueError('Invalid diagnostic elapsed time')
        clean_stages[name] = elapsed
    error = value.get('error')
    if error is not None:
        if not isinstance(error, dict) or set(error) != {'stage', 'kind'}:
            raise ValueError('Invalid diagnostic error')
        if not enum(error['stage'], ERROR_STAGES) or not enum(error['kind'], ERRORS):
            raise ValueError('Invalid diagnostic error category')
        error = dict(error)
    result = dict(
        request_id=ident,
        origin=value['origin'],
        scenario=value['scenario'],
        generation=generation,
        stages=clean_stages,
        outcome=outcome,
        error=error,
    )
    if 'active_stage' in value:
        if not enum(value['active_stage'], ERROR_STAGES):
            raise ValueError('Invalid diagnostic active stage')
        result['active_stage'] = value['active_stage']
    if 'turn_id' in value:
        turn = value['turn_id']
        if not isinstance(turn, str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}', turn):
            raise ValueError('Invalid diagnostic turn ID')
        result['turn_id'] = turn
    if 'task_outcome' in value:
        if not enum(value['task_outcome'], OUTCOMES - {'rejected'}):
            raise ValueError('Invalid diagnostic task outcome')
        result['task_outcome'] = value['task_outcome']
    return result


def snapshot(payload):
    if (
        not isinstance(payload, dict)
        or type(payload.get('schema')) is not int
        or payload['schema'] != 1
    ):
        raise ValueError('Unsupported diagnostic schema')
    if payload.get('clock') != 'mac_monotonic' or payload.get('contains_content') is not False:
        raise ValueError('Unsupported diagnostic clock/content contract')
    if payload.get('render_ack_is_audible_onset') is not False:
        raise ValueError('Unsupported diagnostic playback contract')
    if payload.get('playout_ack_is_audible_onset', False) is not False:
        raise ValueError('Unsupported diagnostic acoustic contract')
    rows = payload.get('requests')
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError('Invalid diagnostic window')
    clean = [receipt(row) for row in rows]
    if len({row['request_id'] for row in clean}) != len(clean):
        raise ValueError('Duplicate diagnostic request ID')
    return clean


class DiagnosticRun:
    def __init__(self, limit=10000):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('Invalid diagnostic run capacity')
        self.limit = limit
        self.rows = {}
        self.baseline = None
        self.previous = set()
        self.discontinuities = 0
        self.polls = 0

    def ingest(self, payload):
        rows = snapshot(payload)
        ids = {row['request_id'] for row in rows}
        baseline = ids if self.baseline is None else self.baseline
        # Could be restart, overflow or a gap. Never claim complete coverage.
        discontinuity = bool(self.previous and not self.previous.intersection(ids))
        # Prepare a complete update first; bad/conflicting snapshots commit nothing.
        updates = {}
        for row in rows:
            ident = row['request_id']
            if ident in baseline:
                continue
            old = self.rows.get(ident)
            if old:
                if old['origin'] != row['origin']:
                    raise ValueError('Conflicting diagnostic origin')
                for field in ('outcome', 'task_outcome', 'error'):
                    if old.get(field) is not None and row.get(field) != old[field]:
                        raise ValueError('Conflicting diagnostic result')
                for name, elapsed in old['stages'].items():
                    if row['stages'].get(name) != elapsed:
                        raise ValueError('Conflicting diagnostic stage')
            updates[ident] = row
        if len(self.rows.keys() | updates.keys()) > self.limit:
            raise RunCapacityError('Diagnostic run capacity reached')
        self.baseline = baseline
        self.discontinuities += int(discontinuity)
        self.rows.update(updates)
        self.previous = ids
        self.polls += 1

    def report(self):
        rows = deepcopy(list(self.rows.values()))
        return {
            'schema': 1,
            'clock': 'mac_monotonic',
            'contains_content': False,
            'render_ack_is_audible_onset': False,
            'playout_ack_is_audible_onset': False,
            'coverage': {
                'polls': self.polls,
                'baseline_excluded': len(self.baseline or ()),
                'window_discontinuities': self.discontinuities,
                'complete_capture_asserted': False,
                'capacity': self.limit,
            },
            'summary': summarize_requests(rows),
            'task_outcomes': dict(
                Counter(row['task_outcome'] for row in rows if 'task_outcome' in row)
            ),
            'groups': {
                origin: summarize_requests([row for row in rows if row['origin'] == origin])
                for origin in sorted(ORIGINS)
            },
            'scenarios': {
                kind: summarize_requests([row for row in rows if row['scenario'] == kind])
                for kind in sorted(SCENARIOS)
            },
            'requests': rows,
        }

"""Accumulate validated content-free receipts beyond the recent IPC window."""

from collections import Counter
from copy import deepcopy
import math
import re
from diagnostics import (
    ORIGINS,
    SCENARIOS,
    OUTCOMES,
    STAGES,
    DOT_CONTEXT_FIELDS,
    MAX_CONFIRMED_PLAYBACK_CLAUSES,
    MAX_DEVICE_PAUSE_MS,
    percentiles,
    summarize_requests,
    summarize_dot_contexts,
)
from capture_evidence import validate as validate_capture, DECISIONS
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
        'dot_rpc_reused',
        'dot_context_reused',
        'capture',
        'gate',
        'confirmed_playback_clauses',
        'device_pause_from_trigger_ms',
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
    if 'confirmed_playback_clauses' in value:
        count = value['confirmed_playback_clauses']
        if (
            type(count) is not int
            or not 1 <= count <= MAX_CONFIRMED_PLAYBACK_CLAUSES
            or not {'playout_ack', 'playback_done'} <= clean_stages.keys()
        ):
            raise ValueError('Invalid confirmed playback clause count')
        result['confirmed_playback_clauses'] = count
    if 'device_pause_from_trigger_ms' in value:
        elapsed = value['device_pause_from_trigger_ms']
        if (
            value['origin'] != 'voice'
            or type(elapsed) not in (int, float)
            or not 0 <= elapsed <= MAX_DEVICE_PAUSE_MS
            or not {'pause_request', 'pause_ack'} <= clean_stages.keys()
        ):
            raise ValueError('Invalid device pause timing')
        result['device_pause_from_trigger_ms'] = elapsed
    if 'turn_id' in value:
        turn = value['turn_id']
        if not isinstance(turn, str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}', turn):
            raise ValueError('Invalid diagnostic turn ID')
        result['turn_id'] = turn
    if 'task_outcome' in value:
        if not enum(value['task_outcome'], OUTCOMES - {'rejected'}):
            raise ValueError('Invalid diagnostic task outcome')
        result['task_outcome'] = value['task_outcome']
    if 'capture' in value:
        if value['origin'] != 'voice':
            raise ValueError('Capture evidence requires voice origin')
        result['capture'] = validate_capture(value['capture'])
    if 'gate' in value:
        if value['origin'] != 'voice' or not enum(value['gate'], DECISIONS):
            raise ValueError('Invalid gate receipt')
        result['gate'] = value['gate']
    for stage, field in DOT_CONTEXT_FIELDS.items():
        if stage in clean_stages or field in value:
            if stage not in clean_stages or type(value.get(field)) is not bool:
                raise ValueError('Invalid Dot context receipt')
            result[field] = value[field]
    return result


def snapshot(payload, *, limit=128):
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
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError('Invalid diagnostic window')
    clean = [receipt(row) for row in rows]
    if len({row['request_id'] for row in clean}) != len(clean):
        raise ValueError('Duplicate diagnostic request ID')
    return clean


def voice_playout_coverage(rows):
    """Separate confirmed completed voice responses from partial stage timing."""
    groups = {
        kind: {'outcomes': Counter(), 'elapsed': [], 'untimed_confirmed': 0}
        for kind in sorted(SCENARIOS)
    }
    for row in rows:
        if row['origin'] != 'voice':
            continue
        group = groups[row['scenario']]
        outcome = row['outcome'] or 'pending'
        if outcome == 'completed':
            if (
                row.get('gate') != 'accepted'
                or row['generation'] is None
                or row.get('task_outcome') != 'completed'
                or row['error'] is not None
                or 'brain_start' not in row['stages']
            ):
                outcome = 'completed_without_response_receipt'
            elif not row.get('confirmed_playback_clauses'):
                outcome = 'completed_without_playout_confirmation'
            else:
                outcome = 'completed_with_confirmed_playout'
                stages = row['stages']
                end, ack = stages.get('utterance_end'), stages['playout_ack']
                if end is not None and end <= ack <= stages['playback_done']:
                    group['elapsed'].append(ack - end)
                else:
                    group['untimed_confirmed'] += 1
        group['outcomes'][outcome] += 1

    result = {}
    for kind, group in groups.items():
        outcomes = group['outcomes']
        observed = sum(outcomes.values())
        terminal = observed - outcomes['pending'] - outcomes['rejected']
        result[kind] = {
            'observed_voice_requests': observed,
            'terminal_for_rate': terminal,
            'outcomes': dict(outcomes),
            'confirmed_response_rate': (
                outcomes['completed_with_confirmed_playout'] / terminal if terminal else None
            ),
            'utterance_to_first_playout_ack_ms': percentiles(group['elapsed']),
            'confirmed_without_valid_timing': group['untimed_confirmed'],
        }
    return result


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
                for field in (
                    'outcome',
                    'task_outcome',
                    'error',
                    'dot_rpc_reused',
                    'dot_context_reused',
                    'capture',
                    'gate',
                    'device_pause_from_trigger_ms',
                ):
                    if old.get(field) is not None and row.get(field) != old[field]:
                        raise ValueError('Conflicting diagnostic result')
                for name, elapsed in old['stages'].items():
                    if row['stages'].get(name) != elapsed:
                        raise ValueError('Conflicting diagnostic stage')
                old_count = old.get('confirmed_playback_clauses', 0)
                count = row.get('confirmed_playback_clauses', 0)
                if count < old_count or old['outcome'] is not None and count != old_count:
                    raise ValueError('Conflicting confirmed playback clause count')
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
            'dot_contexts': summarize_dot_contexts(rows),
            'voice_playout_coverage': voice_playout_coverage(rows),
            'requests': rows,
        }

"""Bounded acoustic-test admission while a local, unadmitted VAD candidate settles.

This observer never cancels work, changes capture or submits a request. Candidate
IDs stay in RAM; exported evidence contains only fixed states, counts and time.
"""

from math import isfinite

SETTLE_SECONDS = 2.5
POLL_SECONDS = 0.25
MAX_OBSERVATIONS = 12
STATES = frozenset(
    (
        'waiting',
        'settled',
        'owner_activity',
        'unknown_pending',
        'unsupported',
        'capture_changed',
        'evidence_lost',
        'timed_out',
    )
)
CANDIDATE_STAGES = frozenset(('voice_detected', 'utterance_end'))


class PendingWorkFailure(RuntimeError):
    """Only a fixed, content-free reason may cross the automation boundary."""


def _candidate(row):
    stages = row.get('stages')
    return (
        row.get('origin') == 'voice'
        and row.get('generation') is None
        and row.get('gate') in (None, 'wake_closed')
        and all(
            row.get(k) is None
            for k in ('asr_input_samples', 'active_stage', 'turn_id', 'task_outcome', 'error')
        )
        and isinstance(stages, dict)
        and bool(stages)
        and set(stages) <= CANDIDATE_STAGES
        and all(type(n) in (int, float) and 0 <= n <= 1e9 and isfinite(n) for n in stages.values())
    )


def _counts(rows):
    counts = dict(candidate=0, request=0, unknown=0)
    for row in rows:
        if row.get('outcome') is not None:
            continue
        key = (
            'candidate'
            if _candidate(row)
            else 'request'
            if row.get('origin') in ('voice', 'text', 'vision', 'announcement')
            and (type(row.get('generation')) is int or bool(row.get('stages')))
            else 'unknown'
        )
        counts[key] += 1
    return counts


def _lease(health):
    numbers = tuple(
        health.get(k)
        for k in ('capture_generation', 'local_wake_count', 'voice_asr_attempts', 'render_frames')
    )
    instance = health.get('runtime_instance')
    if not (
        isinstance(instance, str)
        and 0 < len(instance) <= 128
        and all(type(n) is int and 0 <= n < 2**53 for n in numbers)
        and all(
            health.get(k) is True
            for k in ('connected', 'microphone_active', 'waiting_for_wake', 'local_wake_ready')
        )
        and health.get('transport') == 'usb'
        and health.get('playing') is health.get('calibrating') is False
    ):
        return None
    return (instance, *numbers)


def same_capture(before, after):
    lease = _lease(before)
    return lease is not None and _lease(after) == lease


def settle_pending_voice(rows, health, *, read_rows, read_health, clock, sleep, snapshot):
    """Wait only for never-admitted candidates to end explicitly wake-closed.

    Older peers without capture-generation evidence keep the previous refusal.
    The time budget is checked after RPCs too: slow reads may exceed the budget,
    but cannot authorize playback. RPCs retain their own existing deadlines.
    """
    if all(row.get('outcome') is not None for row in rows):
        return rows, health
    began = clock()
    observations = []
    original = {row['request_id'] for row in rows if row.get('outcome') is not None}
    watched = set()
    lease = _lease(health)

    def publish(state, counts):
        elapsed = round(max(0.0, clock() - began), 4)
        observations.append(dict(seconds=elapsed, **counts))
        snapshot(
            dict(
                schema=1,
                state=state,
                elapsed_seconds=elapsed,
                observations=[dict(row) for row in observations],
            )
        )

    def fail(state, counts):
        publish(state, counts)
        raise PendingWorkFailure('owner_request_pending')

    for index in range(MAX_OBSERVATIONS):
        counts = _counts(rows)
        current = {row['request_id']: row for row in rows}
        watched.update(current.keys() - original)
        if not watched <= current.keys():
            fail('evidence_lost', counts)
        observed = [current[key] for key in watched]
        if any(not _candidate(row) for row in observed):
            fail(
                'owner_activity'
                if any(
                    row.get('origin') in ('voice', 'text', 'vision', 'announcement')
                    for row in observed
                    if not _candidate(row)
                )
                else 'unknown_pending',
                counts,
            )
        if any(row.get('outcome') not in (None, 'rejected') for row in observed):
            fail('owner_activity', counts)
        if any(
            row.get('outcome') == 'rejected' and row.get('gate') != 'wake_closed'
            for row in observed
        ):
            fail('unknown_pending', counts)
        if lease is None:
            fail('unsupported', counts)
        if _lease(health) != lease:
            fail('capture_changed', counts)
        if clock() - began > SETTLE_SECONDS:
            fail('timed_out', counts)
        if not any(counts.values()):
            publish('settled', counts)
            return rows, health
        if index == MAX_OBSERVATIONS - 1 or clock() - began >= SETTLE_SECONDS:
            fail('timed_out', counts)
        publish('waiting', counts)
        sleep(min(POLL_SECONDS, max(0, SETTLE_SECONDS - (clock() - began))))
        rows = read_rows()
        health = read_health()
    raise AssertionError('Unreachable bounded probe observer')


def valid_evidence(value, *, passed=False):
    """Legacy missing evidence remains unknown; an unsettled wait cannot pass."""
    if value is None:
        return True
    if not (
        isinstance(value, dict)
        and set(value) == {'schema', 'state', 'elapsed_seconds', 'observations'}
        and type(value['schema']) is int
        and value['schema'] == 1
        and isinstance(value['state'], str)
        and value['state'] in STATES
        and (not passed or value['state'] == 'settled')
        and type(value['elapsed_seconds']) in (int, float)
        and 0 <= value['elapsed_seconds'] <= 86400
        and isfinite(value['elapsed_seconds'])
        and isinstance(value['observations'], list)
        and 1 <= len(value['observations']) <= MAX_OBSERVATIONS
    ):
        return False
    previous = 0
    for row in value['observations']:
        if not (
            isinstance(row, dict)
            and set(row) == {'seconds', 'candidate', 'request', 'unknown'}
            and type(row['seconds']) in (int, float)
            and previous <= row['seconds'] <= value['elapsed_seconds']
            and isfinite(row['seconds'])
            and all(
                type(row[k]) is int and 0 <= row[k] <= 128
                for k in ('candidate', 'request', 'unknown')
            )
            and sum(row[k] for k in ('candidate', 'request', 'unknown')) <= 128
        ):
            return False
        previous = row['seconds']
    last = value['observations'][-1]
    return previous == value['elapsed_seconds'] and (
        value['state'] != 'settled'
        or (
            value['elapsed_seconds'] <= SETTLE_SECONDS
            and len(value['observations']) >= 2
            and value['observations'][0]['candidate'] > 0
            and all(row['request'] == row['unknown'] == 0 for row in value['observations'])
            and all(last[k] == 0 for k in ('candidate', 'request', 'unknown'))
        )
    )

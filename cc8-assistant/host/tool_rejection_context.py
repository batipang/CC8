"""Bounded, content-free proof of one ASR rejection plus closed pre-ASR candidates."""

from capture_evidence import TOOL_TRANSCRIPT_MISMATCHES
from diagnostics import ASR_INPUT_SAMPLES, valid_asr_input_samples

FIELDS = {
    'origin',
    'gate',
    'outcome',
    'generation',
    'turn_id',
    'task_outcome',
    'error',
    ASR_INPUT_SAMPLES,
    'tool_sample_rejection',
    'stages',
}
CLOSED_STAGES = {'voice_detected', 'utterance_end'}
GUARD_STAGES = {'utterance_end', 'wake_detected', 'asr_start', 'asr_end'}
MAX_CLOSED = 8
STAGE_COUNTS = (
    'wake_detected',
    'asr_start',
    'asr_end',
    'brain_start',
    'playout_ack',
    'playback_done',
    'pause_request',
    'pause_ack',
    'resume_ack',
)


def _candidate(value, *, guarded):
    if (
        not isinstance(value, dict)
        or set(value) != FIELDS
        or value['origin'] != 'voice'
        or value['outcome'] != 'rejected'
        or value['gate'] != ('tool_sample_guard' if guarded else 'wake_closed')
        or any(value[k] is not None for k in ('generation', 'turn_id', 'task_outcome', 'error'))
    ):
        return None
    stages = value['stages']
    if not isinstance(stages, dict) or not all(
        type(x) in (int, float) and 0 <= x <= 604800000 for x in stages.values()
    ):
        return None
    if guarded:
        if (
            not GUARD_STAGES <= set(stages) <= GUARD_STAGES | {'voice_detected'}
            or stages['asr_start'] > stages['asr_end']
            or not valid_asr_input_samples(value[ASR_INPUT_SAMPLES])
            or value['tool_sample_rejection'] not in TOOL_TRANSCRIPT_MISMATCHES
        ):
            return None
    elif (
        set(stages) != CLOSED_STAGES
        or stages['voice_detected'] > stages['utterance_end']
        or value[ASR_INPUT_SAMPLES] is not None
        or value['tool_sample_rejection'] is not None
    ):
        return None
    return dict(value, stages=dict(stages))


def validate_context(value):
    if (
        not isinstance(value, dict)
        or set(value) != {'version', 'guarded', 'closed_candidates'}
        or type(value['version']) is not int
        or value['version'] != 1
        or not isinstance(value['closed_candidates'], list)
        or not 1 <= len(value['closed_candidates']) <= MAX_CLOSED
    ):
        return None
    guarded = _candidate(value['guarded'], guarded=True)
    closed = [_candidate(r, guarded=False) for r in value['closed_candidates']]
    if guarded is None or any(r is None for r in closed):
        return None
    return dict(version=1, guarded=guarded, closed_candidates=closed)


def summarize_context(rows):
    if (
        not isinstance(rows, list)
        or not 2 <= len(rows) <= MAX_CLOSED + 1
        or any(not isinstance(r, dict) for r in rows)
    ):
        return None
    guards = [r for r in rows if r.get('gate') == 'tool_sample_guard']
    if len(guards) != 1:
        return None
    # Project only declared scalars and stage names; no IDs or transcript fields.
    guarded = guards[0]
    return validate_context(
        dict(
            version=1,
            guarded={k: guarded.get(k) for k in FIELDS},
            closed_candidates=[{k: r.get(k) for k in FIELDS} for r in rows if r is not guarded],
        )
    )

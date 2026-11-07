"""Pure aggregation of copied diagnostic rows, without trace or resource ownership.

TraceBook snapshots its bounded records under its own lock before calling these
functions. They inspect the snapshot and never retain or modify requests.
"""

from collections import Counter
import math
from capture_evidence import VAD_TAIL

VAD_ONSET_PAUSE = 'device_pause_from_vad_onset_ms'

INTERVALS = {
    'wake_to_asr_ms': ('wake_detected', 'asr_start'),
    'asr_ms': ('asr_start', 'asr_end'),
    'render_asr_ms': ('render_asr_start', 'render_asr_end'),
    'utterance_to_asr_ms': ('utterance_end', 'asr_end'),
    'brain_first_clause_ms': ('brain_start', 'first_clause'),
    'dot_rpc_prepare_ms': ('dot_rpc_start', 'dot_rpc_ready'),
    'dot_context_ms': ('dot_rpc_ready', 'dot_context_ready'),
    'dot_preparation_ms': ('brain_start', 'dot_turn_start'),
    'dot_turn_ack_ms': ('dot_turn_start', 'dot_turn_ack'),
    'dot_first_delta_ms': ('dot_turn_start', 'dot_first_delta'),
    'dot_delta_to_clause_ms': ('dot_first_delta', 'first_clause'),
    'dot_turn_complete_ms': ('dot_turn_start', 'dot_receipt'),
    'tts_first_pcm_ms': ('tts_start', 'first_pcm'),
    'utterance_to_render_ack_ms': ('utterance_end', 'render_ack'),
    'utterance_to_playout_ack_ms': ('utterance_end', 'playout_ack'),
    'pcm_to_playout_ack_ms': ('first_pcm', 'playout_ack'),
    'pause_roundtrip_ms': ('pause_request', 'pause_ack'),
}


def percentiles(values):
    values = sorted(values)

    def at(q):
        p = (len(values) - 1) * q
        left = math.floor(p)
        right = math.ceil(p)
        return round(values[left] + (values[right] - values[left]) * (p - left), 1)

    return {
        'samples': len(values),
        'p50': at(0.5) if values else None,
        'p95': at(0.95) if values else None,
    }


def summarize_requests(samples):
    counts = dict(Counter(r['outcome'] or 'pending' for r in samples))
    terminal = sum(counts.get(k, 0) for k in ('completed', 'failed', 'cancelled'))
    latencies = {
        name: percentiles(
            [
                r['stages'][b] - r['stages'][a]
                for r in samples
                if a in r['stages'] and b in r['stages'] and r['stages'][b] >= r['stages'][a]
            ]
        )
        for name, (a, b) in INTERVALS.items()
    }
    for metric in ('device_pause_from_trigger_ms', VAD_ONSET_PAUSE):
        latencies[metric] = percentiles(
            [row[metric] for row in samples if row.get('gate') == 'accepted' and metric in row]
        )
    latencies[VAD_TAIL] = percentiles(
        [
            row['capture'][VAD_TAIL]
            for row in samples
            if row.get('gate') == 'accepted' and VAD_TAIL in row.get('capture', {})
        ]
    )
    return {
        'samples': len(samples),
        'outcomes': counts,
        'success_rate': counts.get('completed', 0) / terminal if terminal else None,
        'latencies': latencies,
    }


def summarize_dot_contexts(samples):
    # Missing flags include older backends and non-Dot requests; never infer cold.
    return {
        name: {
            label: summarize_requests([r for r in samples if r.get(field) is value])
            for label, value in (('new', False), ('reused', True), ('unreported', None))
        }
        for name, field in (('rpc', 'dot_rpc_reused'), ('thread', 'dot_context_reused'))
    }

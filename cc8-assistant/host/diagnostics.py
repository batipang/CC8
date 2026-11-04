"""Bounded pipeline receipts with Mac stages and optional device elapsed times."""

from collections import Counter, deque
from copy import deepcopy
import math
import re
import threading
import time
import uuid
from capture_evidence import validate as validate_capture, DECISIONS, VAD_TAIL
from operation_failure import OperationFailure, ClassifiedFailure, STAGES as ERROR_STAGES

DOT_STAGES = frozenset(
    (
        'dot_rpc_start',
        'dot_rpc_ready',
        'dot_context_ready',
        'dot_turn_start',
        'dot_turn_ack',
        'dot_first_delta',
        'dot_receipt',
    )
)
DOT_CONTEXT_FIELDS = {
    'dot_rpc_ready': 'dot_rpc_reused',
    'dot_context_ready': 'dot_context_reused',
}
ROUTE_COMPLETION_STAGES = {'web': 'web_route_completed', 'tool': 'tool_route_completed'}
STAGES = (
    frozenset(
        (
            'voice_detected',
            'wake_detected',
            'utterance_end',
            'asr_start',
            'asr_end',
            'brain_start',
            'first_clause',
            'tts_start',
            'first_pcm',
            'render_ack',
            'playout_ack',
            'playback_done',
            'confirmed_playout_ack',
            'confirmed_playback_done',
            'task_result',
            'pause_request',
            'pause_ack',
            'resume_ack',
        )
    )
    | DOT_STAGES
    | frozenset(ROUTE_COMPLETION_STAGES.values())
)
ORIGINS = frozenset(('voice', 'text', 'vision', 'announcement'))
SCENARIOS = frozenset(('qa', 'web', 'tool', 'vision', 'announcement'))
OUTCOMES = frozenset(('completed', 'failed', 'cancelled', 'rejected'))
INTERVALS = {
    'wake_to_asr_ms': ('wake_detected', 'asr_start'),
    'asr_ms': ('asr_start', 'asr_end'),
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
MAX_DEVICE_PAUSE_MS = 60_000


def error_kind(error):
    """Never copy exception messages, URLs, provider bodies or credentials."""
    if isinstance(error, OperationFailure):
        return error.kind
    status = getattr(error, 'http_status', None)
    if status == 429:
        return 'rate_limit'
    if status == 402:
        return 'quota'
    if status in (401, 403):
        return 'credential_or_permission'
    if isinstance(error, ClassifiedFailure):
        return error.kind
    if type(error).__name__ == 'DotApprovalRequired':
        return 'approval_declined'
    if isinstance(error, TimeoutError) or type(error).__name__ in (
        'Timeout',
        'ReadTimeout',
        'ConnectTimeout',
    ):
        return 'timeout'
    if isinstance(error, (ConnectionError, EOFError, OSError)):
        return 'transport_or_io'
    return 'provider_or_runtime'


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
    latencies['device_pause_from_trigger_ms'] = percentiles(
        [
            row['device_pause_from_trigger_ms']
            for row in samples
            if row.get('gate') == 'accepted' and 'device_pause_from_trigger_ms' in row
        ]
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


MAX_CONFIRMED_PLAYBACK_CLAUSES = 65535


class TraceBook:
    # 白名单内只记录阶段及单端耗时，不接收用户文本、音频、图片或任意 provider 字典。
    def __init__(self, limit=128, clock=time.monotonic):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError('Invalid diagnostic capacity')
        self.clock = clock
        self.limit = limit
        self.lock = threading.RLock()
        self.rows = deque()
        self.generations = {}

    def begin(self, origin, generation=None):
        if origin not in ORIGINS:
            raise ValueError('Unknown diagnostic origin')
        with self.lock:
            row = {
                'request_id': uuid.uuid4().hex,
                'origin': origin,
                'scenario': origin if origin in ('vision', 'announcement') else 'qa',
                'generation': None,
                'started': self.clock(),
                'stages': {},
                'outcome': None,
                'error': None,
            }
            self.rows.append(row)
            while len(self.rows) > self.limit:
                old = self.rows.popleft()
                if self.generations.get(old['generation']) == old['request_id']:
                    self.generations.pop(old['generation'], None)
            if generation is not None:
                self.bind(row['request_id'], generation)
            return row['request_id']

    def _row(self, key):
        ident = self.generations.get(key) if type(key) is int else key
        return next((r for r in reversed(self.rows) if r['request_id'] == ident), None)

    def bind(self, ident, generation):
        if type(generation) is not int or generation < 0:
            raise ValueError('Invalid generation')
        with self.lock:
            row = self._row(ident)
            if row is not None:
                old = row['generation']
                if self.generations.get(old) == ident:
                    self.generations.pop(old, None)
                row['generation'] = generation
                self.generations[generation] = ident

    def capture(self, key, value):
        clean = validate_capture(value)
        self._voice_detail(key, 'capture', clean)

    def capture_record(self, key):
        with self.lock:
            row = self._row(key)
            return deepcopy(row.get('capture')) if row else None

    def gate(self, key, value):
        if not isinstance(value, str) or value not in DECISIONS:
            raise ValueError('Invalid voice gate decision')
        self._voice_detail(key, 'gate', value)

    def _voice_detail(self, key, name, value):
        # These observations may finish after cancellation/terminal receipt.
        # They cannot change its outcome, generation or an earlier observation.
        with self.lock:
            row = self._row(key)
            if row is None:
                return
            if row['origin'] != 'voice':
                raise ValueError('Voice evidence requires a voice request')
            if name in row and row[name] != value:
                raise ValueError('Conflicting voice evidence')
            row[name] = value

    def task_result(self, key, outcome):
        if outcome not in OUTCOMES - {'rejected'}:
            raise ValueError('Invalid task result')
        with self.lock:
            row = self._row(key)
            if row is not None and 'task_outcome' not in row:
                row['task_outcome'] = outcome
                self.mark(key, 'task_result')

    def scenario(self, key, value):
        if value not in SCENARIOS:
            raise ValueError('Invalid diagnostic scenario')
        with self.lock:
            row = self._row(key)
            if row is not None:
                row['scenario'] = value

    def ensure(self, generation, origin):
        with self.lock:
            row = self._row(generation)
            return row['request_id'] if row else self.begin(origin, generation)

    def mark(self, key, stage):
        """Keep the first stage timestamp, returning this observation's elapsed time."""
        if stage not in STAGES:
            raise ValueError('Unknown diagnostic stage')
        with self.lock:
            row = self._row(key)
            if row is not None:
                phase = {
                    'asr_start': 'asr',
                    'brain_start': 'brain',
                    'tts_start': 'tts',
                    'first_pcm': 'playback',
                }.get(stage)
                if phase:
                    row['active_stage'] = phase
                elapsed = round((self.clock() - row['started']) * 1000, 1)
                row['stages'].setdefault(stage, elapsed)
                return elapsed

    def pause_ack(self, key, trigger_nanos, applied_nanos):
        with self.lock:
            row = self._row(key)
            if row is None or 'pause_ack' in row['stages']:
                return
            self.mark(key, 'pause_ack')
            # The frame estimate and AudioTrack.pause() return use the same device clock.
            if (
                row['origin'] == 'voice'
                and 'pause_request' in row['stages']
                and type(trigger_nanos) is int
                and type(applied_nanos) is int
                and 0 < trigger_nanos <= applied_nanos < 2**64
                and applied_nanos - trigger_nanos <= MAX_DEVICE_PAUSE_MS * 1_000_000
            ):
                row['device_pause_from_trigger_ms'] = round(
                    (applied_nanos - trigger_nanos) / 1_000_000, 1
                )

    def dot_stage(self, key, stage, observed_at, reused=None):
        """Admit only local Mac timestamps, including buffered pre-ACK events.

        The source is the owned RPC reader, not a provider/device timestamp.
        Commit flag and timestamp together; cancellation admission is the caller's
        session guard. A late callback must not rewrite a previously seen mode.
        """
        if not isinstance(stage, str) or stage not in DOT_STAGES:
            raise ValueError('Unknown Dot diagnostic stage')
        field = DOT_CONTEXT_FIELDS.get(stage)
        if (field and type(reused) is not bool) or (not field and reused is not None):
            raise ValueError('Invalid Dot context mode')
        with self.lock:
            row = self._row(key)
            if row is None:
                return
            if (
                type(observed_at) not in (int, float)
                or not row['started'] <= observed_at <= self.clock()
                or not math.isfinite(observed_at)
            ):
                raise ValueError('Invalid Dot local observation time')
            if field and field in row and row[field] is not reused:
                raise ValueError('Conflicting Dot context mode')
            row['stages'].setdefault(stage, round((observed_at - row['started']) * 1000, 1))
            if field:
                row[field] = reused

    def playback_done(self, generation, *, confirmed_ack_ms):
        with self.lock:
            completed_ms = self.mark(generation, 'playback_done')
            row = self._row(generation)
            if confirmed_ack_ms is not None and row is not None and row['outcome'] is None:
                row['confirmed_playback_clauses'] = min(
                    row.get('confirmed_playback_clauses', 0) + 1, MAX_CONFIRMED_PLAYBACK_CLAUSES
                )
                # Keep both timestamps from the first matched clause. The
                # request-wide first progress and completion can differ.
                row['stages'].setdefault('confirmed_playout_ack', confirmed_ack_ms)
                row['stages'].setdefault('confirmed_playback_done', completed_ms)

    def finish(self, key, outcome):
        if outcome not in OUTCOMES:
            raise ValueError('Unknown diagnostic outcome')
        with self.lock:
            row = self._row(key)
            if row is not None and row['outcome'] is None:
                row['outcome'] = outcome

    def link_turn(self, key, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', ident):
            return
        with self.lock:
            row = self._row(key)
            if row is not None:
                row['turn_id'] = ident

    def current_stage(self, key, *, default='asr'):
        with self.lock:
            row = self._row(key)
            return row.get('active_stage', default) if row else default

    def failure_record(self, key):
        with self.lock:
            row = self._row(key)
            return deepcopy(row.get('error')) if row else None

    def failure(self, key, stage, error):
        if isinstance(error, OperationFailure):
            stage = error.stage
        if stage not in ERROR_STAGES:
            stage = 'runtime'
        with self.lock:
            row = self._row(key)
            if row is not None and row['error'] is None:
                row['error'] = {'stage': stage, 'kind': error_kind(error)}
                self.finish(key, 'failed')

    def report(self):
        with self.lock:
            rows = deepcopy(list(self.rows))
        for row in rows:
            row.pop('started')

        return {
            'schema': 1,
            'clock': 'mac_monotonic',
            'capacity': self.limit,
            'window': 'recent_requests_only',
            'contains_content': False,
            'render_ack_is_audible_onset': False,
            'playout_ack_is_audible_onset': False,
            'groups': {
                o: summarize_requests([r for r in rows if r['origin'] == o])
                for o in sorted(ORIGINS)
            },
            'scenarios': {
                s: summarize_requests([r for r in rows if r['scenario'] == s])
                for s in sorted(SCENARIOS)
            },
            'dot_contexts': summarize_dot_contexts(rows),
            'requests': rows,
        }

"""Bounded, content-free pipeline receipts measured on the Mac monotonic clock."""

from collections import Counter, deque
from copy import deepcopy
import math
import re
import threading
import time
import uuid

STAGES = frozenset(
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
        'playback_done',
        'task_result',
        'pause_request',
        'pause_ack',
        'resume_ack',
    )
)
ORIGINS = frozenset(('voice', 'text', 'vision', 'announcement'))
SCENARIOS = frozenset(('qa', 'web', 'tool', 'vision', 'announcement'))
OUTCOMES = frozenset(('completed', 'failed', 'cancelled', 'rejected'))
INTERVALS = {
    'wake_to_asr_ms': ('wake_detected', 'asr_start'),
    'asr_ms': ('asr_start', 'asr_end'),
    'utterance_to_asr_ms': ('utterance_end', 'asr_end'),
    'brain_first_clause_ms': ('brain_start', 'first_clause'),
    'tts_first_pcm_ms': ('tts_start', 'first_pcm'),
    'utterance_to_render_ack_ms': ('utterance_end', 'render_ack'),
    'pause_roundtrip_ms': ('pause_request', 'pause_ack'),
}


def error_kind(error):
    """Never copy exception messages, URLs, provider bodies or credentials."""
    status = getattr(error, 'http_status', None)
    if status == 429:
        return 'rate_limit'
    if status == 402:
        return 'quota'
    if status in (401, 403):
        return 'credential_or_permission'
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
    return {
        'samples': len(samples),
        'outcomes': counts,
        'success_rate': counts.get('completed', 0) / terminal if terminal else None,
        'latencies': {
            name: percentiles(
                [
                    r['stages'][b] - r['stages'][a]
                    for r in samples
                    if a in r['stages'] and b in r['stages'] and r['stages'][b] >= r['stages'][a]
                ]
            )
            for name, (a, b) in INTERVALS.items()
        },
    }


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
                row['stages'].setdefault(stage, round((self.clock() - row['started']) * 1000, 1))

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

    def current_stage(self, key):
        with self.lock:
            row = self._row(key)
            return row.get('active_stage', 'asr') if row else 'asr'

    def failure(self, key, stage, error):
        if stage not in ('asr', 'brain', 'tts', 'playback', 'transport'):
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
            'groups': {
                o: summarize_requests([r for r in rows if r['origin'] == o])
                for o in sorted(ORIGINS)
            },
            'scenarios': {
                s: summarize_requests([r for r in rows if r['scenario'] == s])
                for s in sorted(SCENARIOS)
            },
            'requests': rows,
        }

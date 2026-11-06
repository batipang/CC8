"""Explicit, bounded CC8 hardware checks; default invocation only prints a plan."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
import uuid

from private_output import PrivateOutput, private_directory
from release_info import load_release
from mac_voice import (
    PreparedSpeech,
    SpeechFixtureCleanupError,
    resolve_voice,
    validate_prepared_speech,
    __file__ as voice_source,
)
from capture_evidence import (
    validate as validate_capture,
    ONSET_PREFIX_COUNTS,
    TOOL_SAMPLE_REJECTION_REASONS,
    TOOL_TRANSCRIPT_MISMATCHES,
    valid_tool_asr_comparison,
)
from known_speech_receipt import (
    MIN_COMMON_FRAMES,
    validate as validate_known_speech,
    validate_far_preflight,
    valid_request as known_speech_request,
)
from diagnostics import (
    ASR_INPUT_SAMPLES,
    ASR_STOP_STAGE,
    DOT_STAGES,
    INTERVALS,
    MAX_CONFIRMED_PLAYBACK_CLAUSES,
    MAX_DEVICE_PAUSE_MS,
    ROUTE_COMPLETION_STAGES,
    VAD_ONSET_PAUSE,
    percentiles,
    valid_asr_input_samples,
)
from diagnostic_run import receipt
from wake_input_receipt import valid_request as wake_input_request
from wake_input_receipt import summarize as summarize_wake_input
from soak_background import Background, KINDS as BACKGROUND_KINDS, valid_background
from qa_speech import QUESTIONS as QA_LATENCY_QUESTIONS
from qa_speech import WAKE as ACOUSTIC_WAKE
from qa_speech import (
    ALL_QUESTIONS,
    MATH_SCENARIOS,
    SCENARIO_DIGESTS,
    SCENARIO_QUESTIONS,
    TOOL_COMPARE_ENGINES,
    tool_sample_request,
    math_answer_status,
    normalize_question,
)

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = 'com.happyhome.cc8assistant/.MainActivity'
TEST_TEXT = (
    '这是大黄蜂自动测试。现在检查语音播放、字幕高亮和播放完成回执。'
    '这段固定文字只用于设备检查，测试结束后会恢复麦克风关闭状态。'
)
MULTI_CLAUSE_TEXT = (
    '这是本机自动化的多段播报测试，用于检查同一回合内的播放回执，测试结束后将保持麦克风关闭。' * 3
)
BARGE_ANNOUNCEMENT = (
    '窗外的雨渐渐停了，小路上还有一层薄薄的水。小松鼠带着一篮松果走过石桥，'
    '看见河面映出了云朵和树梢。它把篮子放在大石头上，坐下来听水声，等朋友们一起回家。'
)
BARGE_QUERY = '换个问题，三加五等于几？'
STOP_QUERY = '停一下'
ASR_STOP_QUERY = '大黄蜂，别说了。'
ENGLISH_QUESTION = 'What is three plus five?'
ENGLISH_WAKE_MIN_BACKEND = '0.14.67'
ENGLISH_WAKE_PHASES = (
    ('awake', frozenset(('capture', 'awake_dialogue'))),
    ('standby', frozenset(('capture', 'standby', 'acoustic'))),
)
ACOUSTIC_QUESTION = QA_LATENCY_QUESTIONS[0]
WAKE_SELF_TEXT = {
    'zh': '大黄蜂正在播报。现在只检查设备自己的声音。',
    'en': 'Bumblebee is speaking. This is a playback check.',
}
WAKE_SELF_TIMEOUT = 30
ECHO_SOAK_SECONDS = 600
ECHO_SOAK_CAPTURE_MS = 900000
LEGACY_ECHO_SOAK_SECONDS = 900
LEGACY_ECHO_SOAK_CAPTURE_MS = 1320000
ECHO_SOAK_QUIET_SECONDS = 2
ECHO_SOAK_MIN_BACKEND = '0.14.63'
ECHO_SOAK_COUNTERS = ('barge_in_pauses', 'barge_in_resumed', 'fast_stop_count')
ECHO_SOAK_CHECK = 'awake_self_playback_soak'
ECHO_SOAK_SCOPE = 'synthetic_self_playback_soak'
MULTI_CLAUSE_CHECK = 'multi_clause_announcement_and_playout'
EXPECTED_CLAUSES = 2
CHECKS = (
    'preflight',
    'maintenance_open_keeps_microphone_off',
    'announcement_and_playout',
    'logical_wifi_handover',
    'explicit_open_starts_capture',
    'home_stops_capture',
    'maintenance_reopen_keeps_microphone_off',
    'real_idle_black_screen',
    'synthetic_acoustic_wake_and_response',
    'speaker_microphone_acoustic_evidence',
)
CAPTURE_CLOCK_CHECK = 'recording_clock_provenance'
RENDER_CLOCK_CHECK = 'playback_clock_provenance'
LEADING_ACOUSTIC_CHECK = 'leading_playback_acoustic_evidence'
AWAKE_DIALOGUE_CHECK = 'synthetic_awake_dialogue'
BARGE_PRIME_CHECK = 'synthetic_awake_prerequisite_for_interruption'
BARGE_CHECK = 'synthetic_acoustic_interruption'
STOP_CHECK = 'synthetic_acoustic_stop'
ASR_STOP_CHECK = 'synthetic_acoustic_asr_stop'
ACTIVE_HANDOVER_CHECK = 'active_capture_logical_handover'
ALL_ROUTE_LOSS_CHECK = 'capture_stops_during_all_route_loss'
ALL_ROUTE_LOSS_REQUEST = {'operation': 'transport_all_test'}
WAKE_SELF_CHECK = 'self_playback_does_not_wake'
POST_DIALOGUE_SECONDS = 20
STANDBY_SECONDS = 900
PAIRED_PLAYOUT_MIN_BACKEND = '0.14.50'
RESPONSE_ACOUSTIC_MIN_BACKEND = '0.14.57'
EXTENDED_VOICE_MIN_BACKEND = '0.14.60'
ASR_STOP_MIN_BACKEND = '0.14.66'
ROUTED_LATENCY_MIN_BACKEND = '0.14.62'
TOOL_SAMPLE_MIN_BACKEND = '0.15.4'
TOOL_COMPARE_MIN_BACKEND = '0.15.11'
TOOL_COMPARISON_CHECK = 'fixed_tool_asr_comparison'
RENDER_OVERLAP_MIN_BACKEND = '0.14.83'
STOP_RECEIPT_MIN_APK_CODE = 69
STOP_VISUAL_MIN_APK_CODE = 71
SMOKE_OPTIONS = (
    'capture',
    'standby',
    'acoustic',
    'awake_dialogue',
    'synthetic_barge',
    'synthetic_stop',
    'wake_self_playback',
    'active_handover',
    'all_route_loss',
    'multi_clause',
    'playback_evidence',
    'clock_evidence',
    'capture_clock_evidence',
    'leading_acoustic_evidence',
)
P0_DEVICE_PHASES = (
    (
        'transport_and_self_wake',
        ('capture', 'multi_clause', 'active_handover', 'wake_self_playback', 'all_route_loss'),
    ),
    (
        'interruption_and_playback',
        (
            'capture',
            'synthetic_barge',
            'capture_clock_evidence',
            'playback_evidence',
            'clock_evidence',
            'leading_acoustic_evidence',
        ),
    ),
    ('awake_dialogue', ('capture', 'awake_dialogue')),
    ('standby_1', ('capture', 'standby', 'acoustic')),
    ('standby_2', ('capture', 'standby', 'acoustic')),
    ('standby_3', ('capture', 'standby', 'acoustic')),
)
QA_LATENCY_PHASES = tuple(
    (f'qa_{index:02}', ('capture', 'awake_dialogue', 'latency_sample'))
    for index in range(1, len(QA_LATENCY_QUESTIONS) + 1)
)
ECHO_SOAK_PHASES = tuple(
    (f'self_playback_{index}', ('capture', 'awake_dialogue', 'echo_soak')) for index in (1, 2, 3)
)
INTERRUPTION_PHASES = tuple(
    (f'{kind}_{index:02}', ('capture', f'synthetic_{kind}'))
    for index in range(1, 16)
    for kind in ('barge', 'stop')
)
EXTENDED_VOICE_PHASES = (
    *P0_DEVICE_PHASES[:2],
    ('synthetic_stop', ('capture', 'synthetic_stop')),
    ('awake_dialogue', ('capture', 'awake_dialogue', 'response_acoustic_evidence')),
    *P0_DEVICE_PHASES[3:],
)
QA_STAGE_METRICS = ('utterance_to_asr_ms', 'brain_first_clause_ms', 'tts_first_pcm_ms')
QA_DOT_METRICS = (
    'dot_rpc_prepare_ms',
    'dot_context_ms',
    'dot_preparation_ms',
    'dot_turn_ack_ms',
    'dot_first_delta_ms',
    'dot_delta_to_clause_ms',
)
QA_ENDPOINT_METRIC = 'device_vad_tail_ms'
QA_OPTIONAL_METRICS = (*QA_DOT_METRICS, QA_ENDPOINT_METRIC)
QA_PLAYOUT_METRIC = 'utterance_to_confirmed_playout_ack_ms'
ACOUSTIC_RESPONSE_METRIC = 'last_voiced_to_confirmed_output_block_ms'
BARGE_DEVICE_METRIC = 'device_pause_from_trigger_ms'
FAST_STOP_DEVICE_METRIC = 'detecting_frame_to_device_stop_ms'
MAX_FAST_STOP_DEVICE_MS = 20000
STOP_VISUAL_METRIC = 'device_stop_to_visuals_cleared_ms'
MAX_STOP_VISUAL_MS = 2000
MAX_PHASE_ELAPSED_MS = 86_400_000


class CheckFailure(RuntimeError):
    pass


class EvidenceFailure(CheckFailure):
    """An observation is incomplete after its operational prerequisites passed."""


class RuntimeChanged(CheckFailure):
    """The test can no longer act on its original backend instance."""


def require_evidence(condition, reason):
    if not condition:
        raise EvidenceFailure(reason)


def require(condition, reason):
    if not condition:
        raise CheckFailure(reason)


def observed_response_interval(value):
    if not isinstance(value, dict) or value.get('status') != 'observed':
        return None
    elapsed = value.get(ACOUSTIC_RESPONSE_METRIC)
    return elapsed if type(elapsed) in (int, float) and 0 <= elapsed <= 20000 else None


RESPONSE_PROBE_STATES = frozenset(
    (
        'unavailable',
        'waiting',
        'observing',
        'expired',
        'unmatched_input',
        'input_clock_unavailable',
        'probe_setup_failed',
    )
)
RESPONSE_PROBE_REASONS = frozenset(
    (
        'expired',
        'cancelled',
        'invalid_render',
        'render_gap',
        'insufficient_reference',
        'invalid_capture',
        'capture_clock_gap',
        'capture_read_gap',
        'capture_packet_gap',
        'insufficient_capture_coverage',
        'ambiguous',
        'not_observed',
        'render_clock_discontinuous',
        'reference_not_found',
        'capture_prefix_missing',
        'reference_not_sustained',
        'leading_match_unconfirmed',
        'response_prefix_reference_missing',
        'response_prefix_match_unconfirmed',
        'response_prefix_alignment_mismatch',
        'response_prefix_discontinuous',
        'interval_out_of_bounds',
    )
)

ACOUSTIC_COVERAGE_REASONS = frozenset(
    (
        'ambiguous',
        'not_observed',
        'insufficient_reference',
        'insufficient_capture_coverage',
        'render_clock_discontinuous',
        'reference_not_found',
        'reference_not_sustained',
        'capture_prefix_missing',
        'leading_match_unconfirmed',
        'response_prefix_reference_missing',
        'response_prefix_match_unconfirmed',
        'response_prefix_alignment_mismatch',
        'response_prefix_discontinuous',
    )
)


def acoustic_coverage_reason(value):
    if (
        isinstance(value, dict)
        and value.get('status') == 'unconfirmed'
        and ACOUSTIC_RESPONSE_METRIC not in value
    ):
        reason = value.get('reason')
        if isinstance(reason, str) and reason in ACOUSTIC_COVERAGE_REASONS:
            return reason
    return None


def summarize_response_anchor(value):
    limits = {
        'prefix_minus_projected_ms': (-80, 80),
        'reference_anchor_spacing_ms': (-240, 20000),
        'prefix_correlation': (0.55, 1),
        'prefix_competing_correlation': (0, 1),
    }
    if not isinstance(value, dict) or set(value) != limits.keys() | {'prefix_block_status'}:
        return None
    if any(
        type(value[key]) not in (int, float) or not lower <= value[key] <= upper
        for key, (lower, upper) in limits.items()
    ):
        return None
    if (
        abs(value['prefix_minus_projected_ms']) <= 2
        or value['prefix_competing_correlation'] > value['prefix_correlation']
        or value['prefix_block_status']
        not in (
            'observed',
            'capture_prefix_missing',
            'reference_not_sustained',
            'leading_match_unconfirmed',
            'response_prefix_discontinuous',
        )
    ):
        return None
    return dict(value)


def summarize_response_acoustic(value, generation):
    if not isinstance(value, dict):
        return {'status': 'unconfirmed', 'reason': 'invalid_receipt'}
    status = value.get('status')
    if type(status) is str and status in RESPONSE_PROBE_STATES:
        return {'status': 'unconfirmed', 'reason': status}
    if status not in ('observed', 'unconfirmed'):
        return {'status': 'unconfirmed', 'reason': 'invalid_receipt'}
    actual = value.get('generation')
    if type(actual) is not int or actual != generation:
        return {'status': 'unconfirmed', 'reason': 'generation_mismatch'}
    if status == 'unconfirmed':
        reason = value.get('reason')
        timing = {
            'status': 'unconfirmed',
            'reason': reason
            if type(reason) is str and reason in RESPONSE_PROBE_REASONS
            else 'unknown',
        }
        if reason == 'response_prefix_alignment_mismatch':
            diagnostic = summarize_response_anchor(value.get('anchor_diagnostic'))
            if diagnostic is not None:
                timing['anchor_diagnostic'] = diagnostic
        if reason == 'response_prefix_discontinuous':
            room_status = value.get('room_fit_status')
            if room_status in (
                'reference_coverage_missing',
                'capture_coverage_missing',
                'model_unconfirmed',
                'prefix_capture_below_floor',
                'prefix_gain_out_of_range',
                'prefix_shape_mismatch',
            ):
                timing['room_fit_status'] = room_status
        return timing
    elapsed = observed_response_interval(value)
    if elapsed is None:
        return {'status': 'unconfirmed', 'reason': 'invalid_interval'}
    timing = {'status': 'observed', ACOUSTIC_RESPONSE_METRIC: elapsed}
    if 'alignment' in value:
        if value['alignment'] not in (
            'matched_render_frames',
            'matched_render_frames_room_fit',
            'matched_render_frames_room_alignment',
        ):
            return {'status': 'unconfirmed', 'reason': 'invalid_receipt'}
        timing['alignment'] = value['alignment']
    return timing


CAPTURE_CLOCK_INVALID_REASONS = tuple(
    'invalid_' + name
    for name in (
        'frame_range',
        'capture_range',
        'frame_distance',
        'origin',
        'timestamp',
        'future_timestamp',
        'stale_timestamp',
        'offset',
    )
)


def summarize_capture_clock(value):
    """Keep only bounded numeric evidence from the current device capture window."""
    if value is None:
        return {'status': 'missing'}
    counters = ('samples', 'available', 'unavailable', 'invalid', 'duplicate', 'discontinuities')
    offsets = ('min_offset_ns', 'max_offset_ns')
    if not isinstance(value, dict) or any(
        type(value.get(k)) is not int or not 0 <= value[k] <= 6100 for k in counters
    ):
        return {'status': 'malformed'}
    if value['samples'] != sum(value[k] for k in counters[1:]):
        return {'status': 'malformed'}
    fields = counters + offsets if value['available'] else counters
    if set(value) & set(CAPTURE_CLOCK_INVALID_REASONS):
        if (
            any(
                type(value.get(k)) is not int or not 0 <= value[k] <= 6100
                for k in CAPTURE_CLOCK_INVALID_REASONS
            )
            or sum(value[k] for k in CAPTURE_CLOCK_INVALID_REASONS) != value['invalid']
        ):
            return {'status': 'malformed'}
        fields += CAPTURE_CLOCK_INVALID_REASONS
    if set(value) != set(fields):
        return {'status': 'malformed'}
    if value['available'] and (
        any(type(value[k]) is not int for k in offsets)
        or not -2000000000 <= value[offsets[0]] <= value[offsets[1]] <= 2000000000
    ):
        return {'status': 'malformed'}
    observed = value['available'] >= 2 and not (value['invalid'] or value['discontinuities'])
    return {'status': 'observed' if observed else 'unconfirmed', **{k: value[k] for k in fields}}


def valid_leading_evidence(value, delay_ms):
    if not isinstance(value, dict) or value.get('status') != 'observed':
        return False
    offset = value.get('reference_block_start_ms')
    arrival = value.get('capture_minus_render_origin_ms')
    correlation = value.get('minimum_correlation')
    return (
        type(offset) is int
        and 0 <= offset <= 240
        and offset % 20 == 0
        and type(arrival) in (int, float)
        and -81 <= arrival <= 1041
        and type(delay_ms) in (int, float)
        and -80 <= delay_ms <= 800
        and abs(arrival - offset - delay_ms) <= 0.1
        and type(correlation) in (int, float)
        and 0.55 <= correlation <= 1
        and type(value.get('block_ms')) is int
        and value['block_ms'] == 20
        and type(value.get('consecutive_blocks')) is int
        and value['consecutive_blocks'] == 2
    )


REJECTED_VOICE_GATES = (
    'empty',
    'echo_text',
    'echo_render',
    'weak_near',
    'wake_closed',
    'tool_sample_guard',
)


def classify_voice_records(rows):
    """A VAD trace exists before admission; never equate it with a dialogue."""
    groups = {name: [] for name in ('admitted', 'rejected', 'pending', 'invalid')}
    for row in rows:
        stages = row.get('stages', {})
        require(isinstance(stages, dict), 'diagnostics_unavailable')
        gate, outcome, generation = row.get('gate'), row.get('outcome'), row.get('generation')
        if (
            gate == 'accepted'
            or type(generation) is int
            or 'brain_start' in stages
            or row.get('task_outcome') is not None
        ):
            category = 'admitted'
        elif generation is not None or row.get('error') is not None:
            category = 'invalid'
        elif outcome is None and gate in (None, *REJECTED_VOICE_GATES):
            category = 'pending'
        elif outcome == 'rejected' and gate in REJECTED_VOICE_GATES:
            category = 'rejected'
        else:
            category = 'invalid'
        groups[category].append(row)
    return groups


def summarize_rejection_capture(row):
    if 'capture' not in row:
        return {'status': 'missing'}
    try:
        return {'status': 'observed', **validate_capture(row['capture'])}
    except ValueError:
        return {'status': 'malformed'}


def response_playout_timing(row):
    if row is None:
        return {'status': 'missing'}
    stages = row['stages']
    if not {'confirmed_playout_ack', 'confirmed_playback_done'}.intersection(stages):
        return {'status': 'missing'}
    try:
        clean = receipt(row)
    except ValueError:
        return {'status': 'malformed'}
    stages = clean['stages']
    end = stages.get('utterance_end')
    ack = stages['confirmed_playout_ack']
    if end is None:
        return {'status': 'missing'}
    if end > ack or clean.get('gate') != 'accepted' or clean['generation'] is None:
        return {'status': 'malformed'}
    return {
        'status': 'observed',
        'utterance_end_ms': end,
        'confirmed_playout_ack_ms': ack,
        'confirmed_playback_done_ms': stages['confirmed_playback_done'],
        'utterance_to_confirmed_playout_ack_ms': round(ack - end, 1),
    }


def qa_stage_latencies(row, scenario='qa'):
    try:
        clean = receipt(row)
    except ValueError:
        return {}
    if clean['scenario'] != scenario:
        return {}
    stages = clean['stages']
    result = {
        name: round(stages[end] - stages[start], 1)
        for name in (*QA_STAGE_METRICS, *QA_DOT_METRICS)
        for start, end in (INTERVALS[name],)
        if start in stages and end in stages and stages[end] >= stages[start]
    }
    capture = clean.get('capture', {})
    if QA_ENDPOINT_METRIC in capture:
        result[QA_ENDPOINT_METRIC] = capture[QA_ENDPOINT_METRIC]
    return result


def objects(text, marker):
    # dumpsys also contains captions and OEM details. Return only the requested
    # JSON object in memory; the report builder never copies these objects.
    matches = re.findall(re.escape(marker) + r'(\{[^\n]*\})', text)
    require(len(matches) == 1, 'device_status_missing_or_ambiguous')
    value = json.loads(matches[0])
    require(isinstance(value, dict), 'invalid_device_status')
    return value


def matching_acoustic_task(workspace, generation, question):
    tasks = workspace.get('tasks')
    if (
        workspace.get('ok') is not True
        or type(generation) is not int
        or not 0 <= generation < 2**32
        or not isinstance(tasks, list)
    ):
        return None
    matches = [
        task
        for task in tasks
        if isinstance(task, dict)
        and type(task.get('generation')) is int
        and task['generation'] == generation
    ]
    if len(matches) != 1 or matches[0].get('status') != 'completed':
        return None
    title = matches[0].get('title')
    if not isinstance(title, str) or len(title) > 80:
        return None
    if question == ENGLISH_QUESTION:
        matched = re.fullmatch(
            r"(?:bumble[ -]*bee[, :]+)?(?:what is|what's) (?:three|3) *(?:plus|\+) *(?:five|5)",
            ' '.join(title.lower().split()).rstrip('?.!'),
        )
    else:
        matched = normalize_question(title) == normalize_question(question)
    return matches[0] if matched else None


def english_wake_counts(health):
    counts = tuple(health.get(key) for key in ('local_wake_count', 'local_wake_english_count'))
    require(
        all(type(value) is int and value >= 0 for value in counts) and counts[0] >= counts[1],
        'english_wake_counters_unavailable',
    )
    return counts


def english_wake_input_coverage(before, after):
    """Summarize decoder input between playback boundary snapshots."""
    if before is None and after is None:
        return {'status': 'unavailable'}
    for value in (before, after):
        if not isinstance(value, dict) or value.keys() != {'primary', 'alternate'}:
            return {'status': 'invalid'}
        primary, alternate = value['primary'], value['alternate']
        if type(primary) is not int or primary < 0:
            return {'status': 'invalid'}
        if alternate is not None and (type(alternate) is not int or not 0 <= alternate <= primary):
            return {'status': 'invalid'}
    primary = after['primary'] - before['primary']
    if (before['alternate'] is None) != (after['alternate'] is None):
        return {'status': 'invalid'}
    alternate = after['alternate'] - before['alternate'] if after['alternate'] is not None else None
    if primary < 0 or alternate is not None and not 0 <= alternate <= primary:
        return {'status': 'invalid'}
    return {'status': 'observed', 'primary_samples': primary, 'alternate_samples': alternate}


def rpc(path, request, timeout=5):
    # Workspace status is read-only. The test client cannot create tasks via IPC.
    allowed = (
        {'operation': 'health'},
        {'operation': 'diagnostics'},
        {'operation': 'workspace_status'},
        {'operation': 'transport_status'},
    )
    require(
        request in allowed
        or request == {'operation': 'transport_test', 'seconds': 12}
        or request == ALL_ROUTE_LOSS_REQUEST
        or request == {'operation': 'playback_probe'}
        or known_speech_request(request)
        or wake_input_request(request)
        or tool_sample_request(request)
        or (
            request.get('operation') == 'response_probe_arm'
            and set(request) == {'operation', 'case'}
            and type(request['case']) is int
            and 1 <= request['case'] <= len(ALL_QUESTIONS)
        )
        or (
            request.get('operation') == 'response_probe_result'
            and set(request) == {'operation', 'token'}
            and isinstance(request['token'], str)
            and re.fullmatch('[a-f0-9]{32}', request['token']) is not None
        )
        or request == {'text': TEST_TEXT}
        or request == {'text': MULTI_CLAUSE_TEXT}
        or request == {'text': BARGE_ANNOUNCEMENT}
        or request in ({'text': text} for text in WAKE_SELF_TEXT.values()),
        'unsupported_test_operation',
    )
    info = path.lstat()
    require(
        stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        'unsafe_runtime_socket',
    )
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:

        def arm():
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'ipc_deadline')
            conn.settimeout(remaining)

        arm()
        conn.connect(str(path))
        arm()
        conn.sendall((json.dumps(request) + '\n').encode())
        data = bytearray()
        while b'\n' not in data:
            arm()
            block = conn.recv(4096)
            require(bool(block), 'incomplete_ipc_response')
            data.extend(block)
            require(len(data) <= 1024 * 1024, 'oversized_ipc_response')
    result = json.loads(data)
    require(isinstance(result, dict), 'invalid_ipc_response')
    return result


def interruption_input(value, *, stop=False, asr_stop=False):
    require(not asr_stop or stop, 'invalid_prepared_interruption_input')
    kind = 'asr_stop' if asr_stop else 'local_stop' if stop else 'barge'
    require(isinstance(value, dict), 'invalid_prepared_interruption_input')
    require(
        value.get('kind') == kind and value.get('voice_family') == 'Tingting',
        'invalid_prepared_interruption_input',
    )
    try:
        clean = validate_prepared_speech(
            {key: item for key, item in value.items() if key not in ('kind', 'voice_family')}
        )
    except ValueError:
        raise CheckFailure('invalid_prepared_interruption_input') from None
    return {**clean, 'kind': kind, 'voice_family': 'Tingting'}


class Device:
    def __init__(self, *, fixture_directory=None):
        sys.path.insert(0, str(ROOT / 'device-access'))
        import cc8_install

        self.access = cc8_install
        self.transport = self.access.target()
        self.access.probe(self.transport)
        self.fixture_directory = fixture_directory

    def shell(self, command):
        # Recheck the physical target before each operation, never choose a new
        # USB target after a cable/device replacement during this test.
        require(self.access.target() == self.transport, 'usb_target_changed')
        return self.access.shell(self.transport, command, timeout=15)

    def open(self, capture=False, *, capture_limit_ms=180000):
        if capture:
            require(
                type(capture_limit_ms) is int and 1000 <= capture_limit_ms <= 1500000,
                'invalid_test_capture_limit',
            )
            # An older APK ignores unknown extras. Refuse before opening it,
            # rather than silently allowing a supposedly bounded recording.
            device, _ = self.status()
            require(
                device.get('test_capture_limit_supported') is True,
                'device_test_capture_limit_unsupported',
            )
            require(device.get('microphone_active') is False, 'device_microphone_already_active')
        self.shell(
            'input keyevent 224; am start -W --activity-clear-top -n '
            + COMPONENT
            + ' --ez auto_start_voice '
            + ('true' if capture else 'false')
            + (' --el test_capture_limit_ms ' + str(capture_limit_ms) if capture else '')
        )

    def home(self):
        self.shell('input keyevent 3')

    def status(self):
        text = self.shell('dumpsys activity ' + COMPONENT)
        return objects(text, 'CC8_ASSISTANT_STATUS '), objects(text, 'HUD_UI_STATUS ')

    def prepare_speech_probe(self, *, english=False):
        return resolve_voice('Daniel' if english else 'Tingting')

    def prepare_interruption_probe(self, *, stop=False, asr_stop=False):
        require(not asr_stop or stop, 'unknown_synthetic_probe')
        self.close_speech_probe()
        phrase = ASR_STOP_QUERY if asr_stop else STOP_QUERY if stop else BARGE_QUERY
        try:
            directory = getattr(self, 'fixture_directory', None)
            prepared = PreparedSpeech(
                'Tingting',
                phrase,
                directory=private_directory(directory) if directory is not None else None,
            )
        except SpeechFixtureCleanupError as error:
            self._prepared_interruption = error.fixture
            raise
        self._prepared_interruption = prepared
        self._prepared_kind = 'asr_stop' if asr_stop else 'local_stop' if stop else 'barge'
        return interruption_input(
            {**prepared.metadata, 'kind': self._prepared_kind, 'voice_family': 'Tingting'},
            stop=stop,
            asr_stop=asr_stop,
        )

    def close_speech_probe(self):
        prepared = getattr(self, '_prepared_interruption', None)
        if prepared is not None:
            prepared.close()
            self._prepared_interruption = None

    def interruption_fixture(self):
        prepared = getattr(self, '_prepared_interruption', None)
        require(prepared is not None, 'synthetic_interruption_not_prepared')
        return {'directory': prepared.directory_name, 'metadata': prepared.metadata}

    def speak_probe(
        self,
        *,
        interruption=False,
        stop=False,
        asr_stop=False,
        english=False,
        question=ACOUSTIC_QUESTION,
        expected_input=None,
    ):
        require(
            question == ENGLISH_QUESTION if english else question in ALL_QUESTIONS,
            'unknown_synthetic_probe',
        )
        require(
            not (interruption and stop)
            and not (english and (interruption or stop))
            and (not asr_stop or stop),
            'unknown_synthetic_probe',
        )
        if interruption or stop:
            prepared = getattr(self, '_prepared_interruption', None)
            kind = 'asr_stop' if asr_stop else 'local_stop' if stop else 'barge'
            require(
                prepared is not None and getattr(self, '_prepared_kind', None) == kind,
                'synthetic_interruption_not_prepared',
            )
            if expected_input is not None:
                current = interruption_input(
                    {**prepared.metadata, 'kind': kind, 'voice_family': 'Tingting'},
                    stop=stop,
                    asr_stop=asr_stop,
                )
                require(
                    current == interruption_input(expected_input, stop=stop, asr_stop=asr_stop),
                    'synthetic_interruption_receipt_changed',
                )
            prepared.play()
            return
        require(expected_input is None, 'unknown_synthetic_probe')
        voice = self.prepare_speech_probe(english=True) if english else self.prepare_speech_probe()
        phrase = ACOUSTIC_WAKE + '，' + question
        if english:
            phrase = 'Bumblebee, ' + question.lower()
        subprocess.run(
            [
                '/usr/bin/say',
                '-v',
                voice,
                *(['-r', '155'] if english else []),
                phrase,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=20,
        )

    def version(self):
        text = self.shell('dumpsys package com.happyhome.cc8assistant')
        name = re.search(r'\bversionName=([^\s]+)', text)
        code = re.search(r'\bversionCode=(\d+)', text)
        require(name is not None and code is not None, 'device_package_version_missing')
        return name.group(1), int(code.group(1))


class SmokeRun:
    def __init__(
        self,
        device,
        request,
        *,
        capture=False,
        standby=False,
        acoustic=False,
        awake_dialogue=False,
        english_wake=False,
        english_fixture=None,
        wake_input_shape=False,
        latency_sample=False,
        tool_local_compare=False,
        tool_compare_engine='whisper',
        latency_scenario='qa',
        qa_question=ACOUSTIC_QUESTION,
        response_acoustic_evidence=False,
        response_acoustic_coverage=False,
        require_render_overlap=False,
        known_speech_evidence=False,
        known_speech_room_model=False,
        synthetic_barge=False,
        synthetic_stop=False,
        stop_route='local',
        wake_self_playback=False,
        echo_soak=False,
        background=None,
        active_handover=False,
        all_route_loss=False,
        multi_clause=False,
        playback_evidence=False,
        clock_evidence=False,
        capture_clock_evidence=False,
        leading_acoustic_evidence=False,
        clock=time.monotonic,
        sleep=time.sleep,
        checkpoint=lambda value: None,
        expected_apk=None,
    ):
        self.device, self.request = device, request
        self.capture, self.standby, self.acoustic = capture, standby, acoustic
        self.awake_dialogue = awake_dialogue
        self.english_wake = english_wake
        self.english_fixture = english_fixture
        self.wake_input_shape = wake_input_shape
        self.english_fixture_played = False
        self.latency_sample = latency_sample
        self.latency_scenario = latency_scenario
        if tool_local_compare and not (capture and latency_sample and latency_scenario == 'tool'):
            raise ValueError('Local comparison requires a bounded tool sample')
        if tool_local_compare and (
            not awake_dialogue or qa_question not in SCENARIO_QUESTIONS['tool']
        ):
            raise ValueError('Local comparison requires a fixed awake tool question')
        if tool_compare_engine not in TOOL_COMPARE_ENGINES or (
            tool_compare_engine != 'whisper' and not tool_local_compare
        ):
            raise ValueError('Local comparison engine requires an explicit comparison')
        self.tool_local_compare = tool_local_compare
        self.tool_compare_engine = tool_compare_engine
        self.tool_sample_token = None
        self.qa_question = ENGLISH_QUESTION if english_wake else qa_question
        self.response_acoustic_evidence = response_acoustic_evidence
        self.response_acoustic_coverage = response_acoustic_coverage
        self.require_render_overlap = require_render_overlap
        self.known_speech_required = known_speech_evidence
        self.known_speech_room_model = known_speech_room_model
        self.known_speech_evidence = None
        self.synthetic_barge = synthetic_barge
        self.synthetic_stop = synthetic_stop
        self.asr_stop = synthetic_stop and stop_route == 'asr'
        self.interruption_enabled = synthetic_barge or synthetic_stop
        self.wake_self_playback = wake_self_playback
        self.echo_soak = echo_soak
        self.background = background
        self.echo_soak_evidence = None
        self.active_handover = active_handover
        self.active_handover_evidence = None
        self.all_route_loss = all_route_loss
        self.all_route_loss_evidence = None
        self.wake_self_evidence = None
        self.barge_evidence = None
        self.multi_clause = multi_clause
        self.multi_clause_evidence = None
        self.playback_evidence = playback_evidence
        self.clock_evidence = clock_evidence
        self.capture_clock_required = capture_clock_evidence
        self.leading_acoustic_evidence = leading_acoustic_evidence
        self.capture_clock_evidence = None
        self.capture_start_failure = None
        self.playback_probe_evidence = None
        self.clock, self.sleep, self.checkpoint = clock, sleep, checkpoint
        self.started = clock()
        self.checks = []
        self.touched = False
        self.cleanup_verified = False
        self.cleanup_reason = None
        self.initial_instance = None
        self.runtime_change_reason = None
        self.source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.speech_source_sha256 = hashlib.sha256(Path(voice_source).read_bytes()).hexdigest()
        self.expected_apk = expected_apk
        self.acoustic_evidence = None
        self.standby_evidence = None
        self.prepared_input = None
        self.prepared_input_cleaned = None
        self.preparation_attempted = False

    def health(self):
        if self.runtime_change_reason is not None:
            raise RuntimeChanged(self.runtime_change_reason)
        try:
            value = self.request({'operation': 'health'})
            require(value.get('ok') is True, 'health_unavailable')
            instance = value.get('runtime_instance')
            require(isinstance(instance, str) and bool(instance), 'runtime_instance_missing')
            if self.initial_instance is not None and instance != self.initial_instance:
                raise RuntimeChanged('runtime_restarted_during_test')
        except RuntimeChanged as error:
            self.runtime_change_reason = str(error)
            raise
        return value

    def wait(self, condition, seconds, reason):
        deadline = self.clock() + seconds
        while True:
            value = condition()
            if value:
                return value
            require(self.clock() < deadline, reason)
            self.sleep(min(0.5, max(0, deadline - self.clock())))

    def report(self, state):
        return {
            'schema': 1,
            'automation_source_sha256': self.source_sha256,
            'speech_probe_source_sha256': self.speech_source_sha256,
            'expected_apk_version': self.expected_apk[0] if self.expected_apk else None,
            'expected_apk_code': self.expected_apk[1] if self.expected_apk else None,
            'state': state,
            'elapsed_seconds': round(self.clock() - self.started, 3),
            'checks': list(self.checks),
            'cleanup_microphone_off_verified': self.cleanup_verified,
            'cleanup_reason': (self.runtime_change_reason or self.cleanup_reason)
            if state == 'cleanup_unconfirmed'
            else None,
            'capture_tests_enabled': self.capture,
            'standby_enabled': self.standby,
            'synthetic_acoustic_probe_enabled': (
                self.acoustic or self.awake_dialogue or self.interruption_enabled
            ),
            'awake_dialogue_enabled': self.awake_dialogue,
            **(
                {
                    'scope': TOOL_COMPARISON_CHECK,
                    'tool_case': SCENARIO_QUESTIONS['tool'].index(self.qa_question) + 1,
                    'tool_compare_engine': self.tool_compare_engine,
                }
                if self.tool_local_compare
                else {}
            ),
            **(
                {'english_fixture_played': self.english_fixture_played}
                if self.english_fixture is not None
                else {}
            ),
            'require_render_overlap': self.require_render_overlap,
            'known_speech_evidence_required': self.known_speech_required,
            'known_speech_room_model_enabled': self.known_speech_room_model,
            'known_speech_evidence': dict(self.known_speech_evidence)
            if self.known_speech_evidence is not None
            else None,
            'multi_clause_evidence': self.multi_clause_evidence,
            'prepared_interruption_input': (
                dict(self.prepared_input) if self.prepared_input is not None else None
            ),
            'prepared_interruption_input_cleaned': self.prepared_input_cleaned,
            'raw_audio_saved': False,
            'contains_conversation_content': False,
            'physical_usb_unplug_tested': False,
            'human_accuracy_asserted': False,
            'acoustic_latency_asserted': False,
            'full_p0_acceptance': False,
            'acoustic_evidence': self.acoustic_evidence,
            'barge_evidence': self.barge_evidence,
            'wake_self_evidence': self.wake_self_evidence,
            'echo_soak_evidence': self.echo_soak_evidence,
            'active_handover_evidence': self.active_handover_evidence,
            'all_route_loss_evidence': self.all_route_loss_evidence,
            'standby_evidence': self.standby_evidence,
            'playback_probe_evidence': self.playback_probe_evidence,
            'capture_clock_evidence': self.capture_clock_evidence,
            'capture_start_failure': self.capture_start_failure,
        }

    def check(self, name, action, *, continue_if_unconfirmed=False):
        began = self.clock()
        row = {'name': name, 'state': 'running'}
        self.checks.append(row)
        self.checkpoint(self.report('running'))
        try:
            result = action()
            row['state'] = 'passed'
            return result
        except CheckFailure as error:
            row.update(state='failed', reason=str(error))
            if not (continue_if_unconfirmed and isinstance(error, EvidenceFailure)):
                raise
        except KeyboardInterrupt:
            row['state'] = 'interrupted'
            raise
        except Exception:
            # Provider errors, dumpsys output and arbitrary exception messages
            # never enter the report or terminal.
            row.update(state='failed', reason='test_operation_failed')
            raise CheckFailure('test_operation_failed') from None
        finally:
            row['elapsed_seconds'] = round(self.clock() - began, 3)
            self.checkpoint(self.report('running'))

    def preflight(self):
        if self.english_wake:
            if self.english_fixture is None:
                self.device.prepare_speech_probe(english=True)
        elif self.acoustic or self.awake_dialogue or self.interruption_enabled:
            self.device.prepare_speech_probe()
        h = self.health()
        if self.english_wake:
            english_wake_counts(h)
        if self.multi_clause or self.wake_self_playback or self.echo_soak:
            require(h.get('clause_receipts_supported') is True, 'clause_receipts_unsupported')
        if self.wake_self_playback or self.active_handover or self.all_route_loss or self.echo_soak:
            self.audio_counters(h)
        if self.echo_soak:
            self.echo_soak_counters(h)
        require(
            not self.playback_evidence or h.get('playback_evidence_supported') is True,
            'backend_playback_evidence_unsupported',
        )
        require(
            not self.known_speech_required or h.get('known_speech_evidence_supported') is True,
            'backend_known_speech_evidence_unsupported',
        )
        require(
            not self.known_speech_room_model or h.get('far_reference_overlap_supported') is True,
            'backend_far_reference_overlap_unsupported',
        )
        require(
            not self.leading_acoustic_evidence
            or h.get('leading_acoustic_evidence_supported') is True,
            'backend_leading_acoustic_evidence_unsupported',
        )
        require(
            not self.response_acoustic_evidence
            or h.get('response_acoustic_evidence_supported') is True,
            'backend_response_acoustic_evidence_unsupported',
        )
        require(
            not self.clock_evidence or h.get('render_clock_evidence_supported') is True,
            'device_render_clock_evidence_unsupported',
        )
        require(h.get('connected') is True and h.get('transport') == 'usb', 'usb_not_ready')
        require(h.get('microphone_active') is False, 'owner_microphone_already_active')
        require(h.get('playing') is False and h.get('calibrating') is False, 'owner_audio_busy')
        self.initial_instance = h['runtime_instance']
        if self.expected_apk is not None:
            require(self.device.version() == self.expected_apk, 'device_source_version_mismatch')
        device, ui = self.device.status()
        require(
            not self.capture or device.get('test_capture_limit_supported') is True,
            'device_test_capture_limit_unsupported',
        )
        require(
            not (self.active_handover or self.all_route_loss)
            or device.get('test_capture_epoch_supported') is True,
            'device_test_capture_epoch_unsupported',
        )
        require(
            not self.synthetic_barge
            or device.get('test_interruption_transcript_supported') is True,
            'device_interruption_transcript_unsupported',
        )
        if self.synthetic_stop or (
            self.synthetic_barge
            and self.expected_apk is not None
            and self.expected_apk[1] >= STOP_RECEIPT_MIN_APK_CODE
        ):
            require(
                device.get('test_playback_stop_supported') is True,
                'device_playback_stop_evidence_unsupported',
            )
        if self.synthetic_stop:
            if self.asr_stop or (
                self.expected_apk is not None and self.expected_apk[1] >= STOP_VISUAL_MIN_APK_CODE
            ):
                require(
                    device.get('test_stop_visual_timing_supported') is True,
                    'device_stop_visual_timing_unsupported',
                )
            require(
                h.get('fast_stop_ready') is True
                and h.get('fast_stop_timing_supported') is True
                and type(h.get('fast_stop_count')) is int,
                'backend_fast_stop_evidence_unsupported',
            )
            require(
                not self.asr_stop or h.get('asr_engine') == 'minimax',
                'minimax_stop_asr_not_configured',
            )
        require(
            not self.capture_clock_required
            or device.get('capture_clock_evidence_supported') is True,
            'device_capture_clock_evidence_unsupported',
        )
        require(device.get('foreground') is True, 'assistant_not_foreground')
        require(device.get('microphone_active') is False, 'device_microphone_already_active')
        require(
            device.get('vision_active') is False and ui.get('camera_open') is False, 'camera_busy'
        )
        require(ui.get('wake_checked') is True or not self.standby, 'standby_requires_wake_mode')
        if self.awake_dialogue or self.interruption_enabled:
            require(ui.get('wake_checked') is True, 'awake_dialogue_requires_wake_mode')
        if self.wake_self_playback:
            require(ui.get('wake_checked') is True, 'wake_self_playback_requires_wake_mode')
        if self.active_handover:
            require(ui.get('wake_checked') is True, 'active_handover_requires_wake_mode')
        if self.all_route_loss:
            require(ui.get('wake_checked') is True, 'all_route_loss_requires_wake_mode')
        if (
            self.awake_dialogue
            or self.interruption_enabled
            or self.multi_clause
            or self.wake_self_playback
            or self.active_handover
            or self.all_route_loss
        ):
            require(
                all(row.get('outcome') is not None for row in self.diagnostic_requests()),
                'owner_request_pending',
            )
        t = self.request({'operation': 'transport_status'})
        require(
            not self.all_route_loss or t.get('all_routes_test_suppressed') is False,
            'all_route_loss_test_unavailable',
        )
        require(
            t.get('wifi_ready') is True and t.get('usb_ready') is True, 'standby_route_not_ready'
        )
        if self.interruption_enabled:
            self.preparation_attempted = True
            self.prepared_input = interruption_input(
                self.device.prepare_interruption_probe(
                    stop=self.synthetic_stop, asr_stop=self.asr_stop
                ),
                stop=self.synthetic_stop,
                asr_stop=self.asr_stop,
            )
            # Synthesis happens with capture off and may take time. Do not
            # launch maintenance UI over a request or capture started meanwhile.
            h = self.health()
            require(
                h.get('connected') is True
                and h.get('transport') == 'usb'
                and h.get('microphone_active') is False
                and h.get('playing') is False
                and h.get('calibrating') is False,
                'owner_state_changed_during_synthesis',
            )
            device, ui = self.device.status()
            require(
                device.get('foreground') is True
                and device.get('microphone_active') is False
                and device.get('vision_active') is False
                and ui.get('camera_open') is False
                and ui.get('wake_checked') is True
                and all(row.get('outcome') is not None for row in self.diagnostic_requests()),
                'owner_state_changed_during_synthesis',
            )
            require(
                self.expected_apk is None or self.device.version() == self.expected_apk,
                'device_source_version_mismatch',
            )
            transport = self.request({'operation': 'transport_status'})
            require(
                transport.get('usb_ready') is True and transport.get('wifi_ready') is True,
                'owner_state_changed_during_synthesis',
            )

    def inactive(self):
        h = self.health()
        return h.get('connected') is True and h.get('microphone_active') is False

    def maintenance_open(self):
        self.touched = True
        self.device.open(False)
        self.wait(self.inactive, 20, 'maintenance_capture_state_invalid')
        self.sleep(2)
        health = self.health()
        require(health.get('connected') is True, 'maintenance_connection_lost')
        require(health.get('microphone_active') is False, 'maintenance_open_started_capture')
        d, ui = self.device.status()
        require(
            d.get('foreground') is True and d.get('microphone_active') is False,
            'maintenance_device_state_invalid',
        )
        require(ui.get('camera_open') is False, 'unexpected_camera_open')

    def playback(self):
        before = self.diagnostic_requests()
        ids = {r['request_id'] for r in before}
        text = MULTI_CLAUSE_TEXT if self.multi_clause else TEST_TEXT
        frames = None
        if self.multi_clause:
            require(
                all(row.get('outcome') is not None for row in before),
                'owner_request_pending',
            )
            health = self.health()
            require(
                health.get('playing') is False and health.get('calibrating') is False,
                'owner_audio_busy',
            )
            frames = health.get('mic_frames')
            require(type(frames) is int and frames >= 0, 'microphone_frame_count_missing')
            self.multi_clause_evidence = {'expected_clauses': EXPECTED_CLAUSES}

        def check_capture(health):
            if frames is not None:
                require(
                    health.get('connected') is True and health.get('transport') == 'usb',
                    'multi_clause_usb_changed',
                )
                require(
                    health.get('microphone_active') is False and health.get('mic_frames') == frames,
                    'multi_clause_capture_changed',
                )

        if self.multi_clause:
            check_capture(health)
        observed = 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.request, {'text': text})
            while not result.done():
                health = self.health()
                check_capture(health)
                if health.get('playing') is True:
                    d, ui = self.device.status()
                    require(d.get('microphone_active') is False, 'playback_started_capture')
                    require(ui.get('caption_progress_overlap') is False, 'caption_progress_overlap')
                    require(ui.get('halo_status_overlap') is False, 'halo_status_overlap')
                    require(ui.get('caption_line_visible') is True, 'caption_line_clipped')
                    require(ui.get('camera_open') is False, 'unexpected_camera_open')
                    observed += 1
                self.sleep(0.25)
            reply = result.result()
            require(reply.get('status') == 'completed', 'announcement_not_completed')
        if self.multi_clause:
            check_capture(self.health())
            require(reply.get('characters') == len(text), 'announcement_size_mismatch')
        rows = [
            r
            for r in self.diagnostic_requests()
            if r['request_id'] not in ids and r.get('origin') == 'announcement'
        ]
        require(len(rows) == 1, 'announcement_receipt_missing_or_ambiguous')
        row = rows[0]
        require(row.get('outcome') == 'completed', 'announcement_receipt_not_completed')
        require(
            {'playout_ack', 'playback_done'} <= set(row.get('stages', {})),
            'playout_receipt_missing',
        )
        if self.multi_clause:
            count = row.get('confirmed_playback_clauses')
            self.multi_clause_evidence['confirmed_clauses'] = (
                count
                if type(count) is int and 0 < count <= MAX_CONFIRMED_PLAYBACK_CLAUSES
                else None
            )
            require(
                type(count) is int and count == EXPECTED_CLAUSES,
                'multi_clause_receipts_not_confirmed',
            )
        require(observed > 0, 'no_playing_ui_sample')

    def handover(self):
        require(self.inactive(), 'handover_requires_microphone_off')
        require(
            self.request({'operation': 'transport_test', 'seconds': 12}).get('ok') is True,
            'route_suppression_rejected',
        )

        def route(kind):
            h = self.health()
            require(h.get('microphone_active') is False, 'handover_started_capture')
            return h.get('connected') is True and h.get('transport') == kind

        self.wait(lambda: route('wifi'), 10, 'wifi_route_not_selected')
        self.wait(lambda: route('usb'), 25, 'usb_priority_not_restored')
        d, _ = self.device.status()
        require(
            d.get('microphone_active') is False and d.get('transport') == 'usb',
            'device_handover_state_invalid',
        )

    def active_capture_handover(self):
        def capture_health():
            health = self.health()
            require(
                health.get('connected') is True
                and health.get('microphone_active') is True
                and health.get('calibrating') is False
                and health.get('playing') is False
                and health.get('waiting_for_wake') is True,
                'active_handover_capture_changed',
            )
            return health

        def native_capture(kind):
            before = self.clock()
            d = self.capture_status()
            after = self.clock()
            epoch = d.get('test_capture_epoch')
            remaining = d['test_capture_remaining_ms'] / 1000
            require(d.get('transport') == kind, 'active_handover_device_route_invalid')
            require(type(epoch) is int and 0 < epoch < 2**31, 'active_handover_epoch_missing')
            return epoch, before + remaining, after + remaining, remaining

        def switches(kind):
            status = self.request({'operation': 'transport_status'})
            count = status.get('transport_switches')
            require(
                status.get('transport') == kind
                and status.get('wifi_ready') is True
                and status.get('usb_test_suppressed') is (kind == 'wifi')
                and (kind != 'usb' or status.get('usb_ready') is True)
                and type(count) is int
                and count >= 0,
                'active_handover_transport_receipt_invalid',
            )
            return count

        baseline = capture_health()
        require(baseline.get('transport') == 'usb', 'active_handover_requires_usb')
        counters = self.audio_counters(baseline)
        first_epoch, first_lo, first_hi, budget = native_capture('usb')
        require(budget >= 45, 'active_handover_capture_budget_insufficient')
        base_switches = switches('usb')
        rows = self.diagnostic_requests()
        require(all(row.get('outcome') is not None for row in rows), 'owner_request_pending')
        before = {row['request_id'] for row in rows}
        evidence = {'routes': [], 'no_new_requests': False}
        self.active_handover_evidence = evidence
        self.checkpoint(self.report('running'))
        require(
            self.request({'operation': 'transport_test', 'seconds': 12}).get('ok') is True,
            'route_suppression_rejected',
        )

        last_frames = counters['mic_frames']

        def newer_frames(kind, minimum):
            nonlocal last_frames
            health = capture_health()
            current = self.audio_counters(health)
            frames = current['mic_frames']
            require(frames >= last_frames, 'active_handover_mic_counter_regressed')
            last_frames = frames
            require(
                all(
                    current[key] == counters[key]
                    for key in ('render_frames', 'voice_asr_attempts', 'local_wake_count')
                ),
                'active_handover_unexpected_audio_activity',
            )
            return frames if health.get('transport') == kind and frames > minimum else None

        for step, (kind, timeout) in enumerate((('wifi', 8), ('usb', 25)), start=1):
            first = self.wait(
                lambda: newer_frames(kind, -1), timeout, 'active_handover_route_not_selected'
            )
            progressed = self.wait(
                lambda: newer_frames(kind, first), 2, 'active_handover_no_new_mic_frames'
            )
            epoch, low, high, remaining = native_capture(kind)
            require(epoch == first_epoch, 'active_handover_capture_restarted')
            require(
                remaining < budget and low <= first_hi + 0.25 and high >= first_lo - 0.25,
                'active_handover_capture_deadline_changed',
            )
            budget = remaining
            count = switches(kind) - base_switches
            require(count == step, 'active_handover_unexpected_switch_count')
            evidence['routes'].append(
                dict(transport=kind, mic_frames_advanced=progressed - first, switches=count)
            )
            self.checkpoint(self.report('running'))
        new = [row for row in self.diagnostic_requests() if row['request_id'] not in before]
        require(
            all(
                row.get('origin') == 'voice'
                and row.get('outcome') == 'rejected'
                and row.get('gate') == 'wake_closed'
                and row.get('generation') is None
                and 'asr_start' not in row.get('stages', {})
                for row in new
            ),
            'active_handover_unexpected_request',
        )
        evidence['no_new_requests'] = True

    def all_routes_loss_probe(self):
        self.start_capture()
        baseline = self.health()
        counters = self.audio_counters(baseline)
        d, ui = self.device.status()
        self.capture_status((d, ui))
        require(
            baseline.get('connected') is True
            and baseline.get('transport') == d.get('transport') == 'usb'
            and baseline.get('microphone_active') is True
            and baseline.get('calibrating') is False
            and baseline.get('playing') is False
            and baseline.get('waiting_for_wake') is True
            and counters['mic_frames'] > 0
            and type(d.get('test_capture_epoch')) is int
            and d['test_capture_remaining_ms'] >= 45000,
            'all_route_loss_capture_not_ready',
        )
        attempts = ui.get('auto_voice_start_attempts')
        require(type(attempts) is int and attempts > 0, 'all_route_loss_ui_state_unavailable')
        routes = self.request({'operation': 'transport_status'})
        require(
            routes.get('usb_ready') is True
            and routes.get('wifi_ready') is True
            and routes.get('usb_test_suppressed') is False
            and routes.get('all_routes_test_suppressed') is False,
            'all_route_loss_routes_not_ready',
        )
        rows = self.diagnostic_requests()
        require(all(row.get('outcome') is not None for row in rows), 'owner_request_pending')
        ids = {row['request_id'] for row in rows}
        evidence = {
            'both_routes_down': False,
            'capture_stopped': False,
            'usb_priority_restored': False,
            'post_reconnect_mic_off_seconds': 0,
        }
        self.all_route_loss_evidence = evidence
        self.checkpoint(self.report('running'))
        reply = self.request(ALL_ROUTE_LOSS_REQUEST)
        require(
            reply.get('ok') is True and reply.get('seconds') == 10,
            'all_route_loss_suppression_rejected',
        )

        def device_stopped(d, ui):
            return (
                d.get('microphone_active') is False
                and d.get('test_capture_remaining_ms') == 0
                and 'test_capture_epoch' not in d
                and d.get('foreground') is True
                and d.get('vision_active') is False
                and ui.get('camera_open') is False
                and ui.get('auto_voice_pending') is False
                and ui.get('auto_voice_start_attempts') == attempts
            )

        def disconnected():
            health = self.health()
            status = self.request({'operation': 'transport_status'})
            require(status.get('all_routes_test_suppressed') is True, 'all_route_loss_window_ended')
            if (
                health.get('connected') is not False
                or health.get('transport') != 'none'
                or health.get('microphone_active') is not False
                or status.get('usb_ready') is not False
                or status.get('wifi_ready') is not False
            ):
                return None
            device, ui_state = self.device.status()
            if (
                device.get('connected') is False
                and device.get('transport') == 'none'
                and device_stopped(device, ui_state)
            ):
                require(
                    self.request({'operation': 'transport_status'}).get(
                        'all_routes_test_suppressed'
                    )
                    is True,
                    'all_route_loss_window_ended',
                )
                return self.audio_counters(health), device.get('mic_frames')
            return None

        stopped, native_frames = self.wait(disconnected, 7, 'all_route_loss_stop_unconfirmed')
        require(
            type(native_frames) is int and native_frames > 0, 'all_route_loss_native_count_missing'
        )
        require(
            all(
                stopped[key] == counters[key]
                for key in ('render_frames', 'voice_asr_attempts', 'local_wake_count')
            ),
            'all_route_loss_unexpected_audio',
        )
        evidence.update(both_routes_down=True, capture_stopped=True)
        self.checkpoint(self.report('running'))

        def reconnect():
            health = self.health()
            require(
                health.get('microphone_active') is False
                and health.get('calibrating') is False
                and health.get('playing') is False
                and self.audio_counters(health) == stopped,
                'all_route_loss_reconnect_started_audio',
            )
            status = self.request({'operation': 'transport_status'})
            return (
                health.get('connected') is True
                and health.get('transport') == status.get('transport') == 'usb'
                and status.get('usb_ready') is True
                and status.get('wifi_ready') is True
                and status.get('all_routes_test_suppressed') is False
            )

        self.wait(reconnect, 35, 'all_route_loss_reconnect_not_confirmed')
        quiet = self.clock()
        while self.clock() - quiet < 5:
            self.sleep(min(0.5, 5 - (self.clock() - quiet)))
            require(reconnect(), 'all_route_loss_restored_route_changed')
            evidence['post_reconnect_mic_off_seconds'] = round(self.clock() - quiet, 3)
        device, ui_state = self.device.status()
        require(
            device.get('connected') is True
            and device.get('transport') == 'usb'
            and device_stopped(device, ui_state)
            and device.get('mic_frames') == native_frames,
            'all_route_loss_device_reopened_capture',
        )
        require(
            all(row['request_id'] in ids for row in self.diagnostic_requests()),
            'all_route_loss_unexpected_request',
        )
        evidence['usb_priority_restored'] = True

    @property
    def capture_limit_ms(self):
        if self.echo_soak:
            return ECHO_SOAK_CAPTURE_MS
        if self.standby:
            return 1200000
        return 300000 if self.interruption_enabled else 180000

    def start_capture(self):
        limit = self.capture_limit_ms
        if self.latency_sample and self.latency_scenario == 'tool':
            case = SCENARIO_QUESTIONS['tool'].index(self.qa_question) + 1
            operation = 'tool_sample_compare_arm' if self.tool_local_compare else 'tool_sample_arm'
            request = {'operation': operation, 'case': case}
            if self.tool_compare_engine != 'whisper':
                request['engine'] = self.tool_compare_engine
            armed = self.request(request)
            token = armed.get('token')
            require(
                armed.get('status') == 'armed'
                and tool_sample_request({'operation': 'tool_sample_begin', 'token': token}),
                'tool_sample_guard_not_armed',
            )
            self.tool_sample_token = token
        self.device.open(True, capture_limit_ms=limit)
        try:
            self.wait(
                lambda: self.health().get('microphone_active') is True, 15, 'capture_not_started'
            )
            self.wait(
                lambda: self.health().get('calibrating') is False, 25, 'calibration_not_finished'
            )
            h = self.health()
            require(
                h.get('microphone_active') is True and h.get('local_wake_ready') is True,
                'wake_capture_not_ready',
            )
            return self.capture_status()
        except RuntimeChanged:
            raise
        except (CheckFailure, OSError, ValueError, subprocess.SubprocessError):
            self.record_capture_start_failure()
            raise

    def record_capture_start_failure(self):
        # Read both sides before Home or a maintenance open can change the
        # evidence. Missing evidence cannot overwrite the original failure.
        evidence = dict.fromkeys(
            ('host_microphone_active', 'device_microphone_active', 'device_phase')
        )
        self.capture_start_failure = evidence
        try:
            host = self.health().get('microphone_active')
            if type(host) is bool:
                evidence['host_microphone_active'] = host
        except RuntimeChanged:
            return
        except (CheckFailure, OSError, ValueError, subprocess.SubprocessError):
            pass
        try:
            device, _ = self.device.status()
        except (CheckFailure, OSError, ValueError, subprocess.SubprocessError):
            return
        active, phase = device.get('microphone_active'), device.get('phase')
        if type(active) is bool:
            evidence['device_microphone_active'] = active
        if phase in ('calibrating', 'error', 'idle', 'listening', 'speaking', 'thinking'):
            evidence['device_phase'] = phase

    def capture_status(self, status=None):
        limit = self.capture_limit_ms
        d, ui = self.device.status() if status is None else status
        require(
            type(d.get('test_capture_limit_ms')) is int
            and d['test_capture_limit_ms'] == limit
            and type(d.get('test_capture_remaining_ms')) is int
            and 0 < d['test_capture_remaining_ms'] <= limit,
            'device_test_capture_limit_not_active',
        )
        require(
            d.get('foreground') is True
            and d.get('microphone_active') is True
            and d.get('vision_active') is False
            and ui.get('camera_open') is False,
            'device_capture_state_invalid',
        )
        return d

    def capture_clock_probe(self, device_status):
        self.capture_clock_evidence = summarize_capture_clock(
            device_status.get('capture_clock_evidence')
        )
        require_evidence(
            self.capture_clock_evidence['status'] == 'observed',
            'capture_clock_provenance_unconfirmed',
        )

    def require_idle_probe_capture(self):
        h = self.health()
        require(
            h.get('connected') is True
            and h.get('transport') == 'usb'
            and h.get('microphone_active') is True
            and h.get('playing') is False
            and h.get('calibrating') is False,
            'playback_probe_requires_idle_capture',
        )
        return self.capture_status()

    def playback_probe(self):
        self.require_idle_probe_capture()
        result = self.request({'operation': 'playback_probe'})
        evidence = result.get('evidence')
        if isinstance(evidence, dict):
            self.playback_probe_evidence = evidence
        # Only a completed stimulus with a still healthy capture can leave
        # optional evidence unconfirmed and allow follow-up checks.
        require(result.get('status') == 'completed', 'playback_probe_not_completed')
        self.require_idle_probe_capture()
        require_evidence(isinstance(evidence, dict), 'playback_probe_evidence_missing')
        require_evidence(
            evidence.get('status') == 'observed', 'playback_not_acoustically_corroborated'
        )

    def render_clock_probe(self):
        evidence = self.playback_probe_evidence or {}
        clock = evidence.get('render_clock_evidence')
        require_evidence(
            isinstance(clock, dict) and clock.get('status') == 'observed',
            'render_clock_provenance_unconfirmed',
        )

    def leading_acoustic_probe(self):
        evidence = self.playback_probe_evidence or {}
        require_evidence(
            evidence.get('status') == 'observed'
            and type(evidence.get('render_clock_resets')) is int
            and evidence['render_clock_resets'] == 0
            and valid_leading_evidence(
                evidence.get('leading_acoustic_evidence'),
                evidence.get('matched_window_delay_ms'),
            ),
            'leading_acoustic_evidence_unconfirmed',
        )

    def home(self):
        self.device.home()

        def stopped():
            host = self.health()
            if host.get('microphone_active') is not False:
                return None
            device, _ = self.device.status()
            if device.get('microphone_active') is not False:
                return None
            frames = host.get('mic_frames'), device.get('mic_frames')
            require(
                all(type(count) is int and count >= 0 for count in frames),
                'home_microphone_frame_count_missing',
            )
            return frames

        before = self.wait(stopped, 15, 'home_did_not_stop_capture')
        self.sleep(2)
        after = stopped()
        require(after is not None, 'home_restarted_capture')
        require(before == after, 'home_microphone_frames_increasing')

    def idle(self):
        began = self.clock()
        self.start_capture()
        deadline = self.clock() + 970
        evidence = {
            'last_awake_seconds': None,
            'black_screen_poll_started_seconds': None,
            'first_black_screen_seconds': None,
        }
        self.standby_evidence = evidence
        while True:
            poll_started = self.clock()
            h = self.health()
            require(
                h.get('microphone_active') is True and h.get('connected') is True,
                'idle_capture_or_connection_lost',
            )
            d, ui = self.device.status()
            observed = self.clock()
            require(observed <= deadline, 'idle_black_screen_timeout')
            require(ui.get('camera_open') is False, 'unexpected_camera_open')
            require(d.get('foreground') is True, 'idle_left_foreground')
            self.capture_status((d, ui))
            sleeping = ui.get('screen_sleeping')
            require(type(sleeping) is bool, 'idle_screen_state_unavailable')
            if sleeping:
                evidence['black_screen_poll_started_seconds'] = round(poll_started - began, 3)
                evidence['first_black_screen_seconds'] = round(observed - began, 3)
                require(
                    poll_started - began >= STANDBY_SECONDS,
                    'idle_black_screen_too_early',
                )
                require(evidence['last_awake_seconds'] is not None, 'idle_awake_state_unobserved')
                require(
                    h.get('waiting_for_wake') is True and h.get('local_wake_ready') is True,
                    'idle_local_wake_not_ready',
                )
                return
            evidence['last_awake_seconds'] = round(observed - began, 3)
            require(observed < deadline, 'idle_black_screen_timeout')
            self.checkpoint(self.report('running'))
            self.sleep(min(5, deadline - observed))

    def diagnostic_requests(self):
        rows = self.request({'operation': 'diagnostics'}).get('requests')
        require(
            isinstance(rows, list)
            and all(isinstance(r, dict) and isinstance(r.get('request_id'), str) for r in rows),
            'diagnostics_unavailable',
        )
        require(len({r['request_id'] for r in rows}) == len(rows), 'duplicate_diagnostic_id')
        return rows

    def acoustic_probe(self, *, awake=False, quiet_seconds=POST_DIALOGUE_SECONDS):
        # A computer speaker is a synthetic source with unknown room geometry.
        # This can verify the live chain, never a human recall rate or dB level.
        h = self.health()
        require(
            h.get('connected') is True
            and h.get('transport') == 'usb'
            and h.get('microphone_active') is True
            and h.get('playing') is False
            and h.get('calibrating') is False
            and h.get('waiting_for_wake') is True
            and h.get('local_wake_ready') is True,
            'acoustic_probe_requires_idle_capture',
        )
        rows = self.diagnostic_requests()
        ids = {r['request_id'] for r in rows}
        require(all(r.get('outcome') is not None for r in rows), 'owner_request_pending')
        d, ui = self.device.status()
        require(ui.get('screen_sleeping') is (not awake), 'acoustic_probe_screen_state_invalid')
        d = self.capture_status((d, ui))
        # Reserve space for the speech command and post-playback observation.
        reserve = quiet_seconds + 5
        available = d['test_capture_remaining_ms'] / 1000
        require(available > 20 + reserve, 'acoustic_probe_capture_budget_insufficient')
        response_deadline = self.clock() + available - reserve
        probe_token = None
        if self.response_acoustic_evidence:
            case = ALL_QUESTIONS.index(self.qa_question) + 1
            armed = self.request({'operation': 'response_probe_arm', 'case': case})
            probe_token = armed.get('token')
            require(
                armed.get('status') == 'armed'
                and isinstance(probe_token, str)
                and re.fullmatch('[a-f0-9]{32}', probe_token) is not None,
                'response_acoustic_probe_not_armed',
            )
        mic_before = h.get('mic_frames')
        wake_before = english_wake_counts(h) if self.english_wake else None
        input_before = (
            self.health().get('english_wake_input_samples') if self.english_wake else None
        )
        input_token = None
        if self.wake_input_shape:
            armed = self.request(
                {
                    'operation': 'wake_input_arm',
                    'directory': self.english_fixture.directory_name,
                    'metadata': self.english_fixture.metadata,
                }
            )
            input_token = armed.get('token')
            require(
                armed.get('status') == 'armed'
                and isinstance(input_token, str)
                and re.fullmatch('[a-f0-9]{32}', input_token) is not None,
                'wake_input_probe_not_armed',
            )
        if self.latency_sample and self.latency_scenario == 'tool':
            require(
                self.tool_sample_token is not None
                and self.request(
                    {'operation': 'tool_sample_begin', 'token': self.tool_sample_token}
                )
                == {'status': 'ready'},
                'tool_sample_guard_not_ready',
            )
        if self.english_fixture is not None:
            self.english_fixture.play()
            self.english_fixture_played = True
        elif self.english_wake:
            self.device.speak_probe(question=self.qa_question, english=True)
        else:
            self.device.speak_probe(question=self.qa_question)
        input_coverage = (
            english_wake_input_coverage(
                input_before, self.health().get('english_wake_input_samples')
            )
            if self.english_wake
            else None
        )
        input_shape = (
            summarize_wake_input(
                self.request({'operation': 'wake_input_result', 'token': input_token})
            )
            if input_token is not None
            else None
        )

        admitted_id = None
        recognized_stimulus_matches = None
        math_answer = None

        def received():
            nonlocal admitted_id
            health = self.health()
            require(
                health.get('connected') is True and health.get('microphone_active') is True,
                'acoustic_capture_or_connection_lost',
            )
            require(health.get('transport') == 'usb', 'acoustic_probe_usb_changed')
            rows = [
                r
                for r in self.diagnostic_requests()
                if r['request_id'] not in ids and r.get('origin') == 'voice'
            ]
            groups = classify_voice_records(rows)
            admitted, rejected = groups['admitted'], groups['rejected']
            tool_rejections = {}
            for row in rejected:
                if row['gate'] == 'tool_sample_guard':
                    reason = row.get('tool_sample_rejection')
                    if reason not in TOOL_SAMPLE_REJECTION_REASONS:
                        reason = 'unreported'
                    tool_rejections[reason] = tool_rejections.get(reason, 0) + 1
            mic_now = health.get('mic_frames')
            wake_rejection = (
                rejected[0]
                if len(rows) == 1 and rejected and rejected[0]['gate'] == 'wake_closed'
                else None
            )
            completed = next(
                (
                    r
                    for r in admitted
                    if r.get('outcome') == 'completed'
                    and r.get('task_outcome') == 'completed'
                    and {'wake_detected', 'asr_end', 'brain_start', 'playout_ack', 'playback_done'}
                    <= set(r.get('stages', {}))
                ),
                None,
            )
            route = completed.get('scenario') if completed is not None else None
            tool_latency = self.latency_sample and self.latency_scenario == 'tool'
            # Keep only fixed counts, categories and timing. A completed task
            # is not a played answer.
            self.acoustic_evidence = {
                'new_mic_frames': (
                    mic_now - mic_before
                    if type(mic_now) is int
                    and type(mic_before) is int
                    and 0 <= mic_before <= mic_now
                    else None
                ),
                'new_voice_requests': len(rows),
                'admitted_voice_requests': len(admitted),
                'rejected_voice_candidates': len(rejected),
                'rejected_voice_gates': {
                    gate: sum(row['gate'] == gate for row in rejected)
                    for gate in REJECTED_VOICE_GATES
                },
                **({'tool_sample_rejections': tool_rejections} if tool_rejections else {}),
                'sole_wake_rejection_capture': (
                    summarize_rejection_capture(wake_rejection)
                    if wake_rejection is not None
                    else None
                ),
                'pending_voice_candidates': len(groups['pending']),
                'unclassified_terminal_candidates': len(groups['invalid']),
                'rejected_pause_count': sum('pause_ack' in r.get('stages', {}) for r in rejected),
                'unresumed_rejected_pauses': sum(
                    'pause_ack' in r.get('stages', {}) and 'resume_ack' not in r['stages']
                    for r in rejected
                ),
                'completed_requests': sum(r.get('outcome') == 'completed' for r in rows),
                'cancelled_requests': sum(r.get('outcome') == 'cancelled' for r in rows),
                'completed_tasks': sum(r.get('task_outcome') == 'completed' for r in rows),
                'recognized_stimulus_matches': recognized_stimulus_matches,
                **({'tool_sample_guarded': True} if tool_latency else {}),
                **({'math_answer_status': math_answer} if math_answer is not None else {}),
                'response_playout_timing': response_playout_timing(completed),
                **(
                    {
                        'qa_stage_latencies_ms': qa_stage_latencies(
                            completed, self.latency_scenario
                        ),
                        'latency_scenario': route if route in tuple(SCENARIO_QUESTIONS) else None,
                        'latency_route_completed': ROUTE_COMPLETION_STAGES.get(
                            self.latency_scenario
                        )
                        in completed['stages'],
                    }
                    if self.latency_sample and completed is not None
                    else {}
                ),
                'stage_counts': {
                    stage: sum(stage in r.get('stages', {}) for r in rows)
                    for stage in (
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
                },
                'post_playback_quiet_seconds': (self.acoustic_evidence or {}).get(
                    'post_playback_quiet_seconds', 0
                ),
            }
            if tool_latency and not self.tool_local_compare and len(rows) == 1 and rejected:
                row = rejected[0]
                stages = row.get('stages', {})
                started, ended = stages.get('asr_start'), stages.get('asr_end')
                if (
                    row['gate'] == 'tool_sample_guard'
                    and row.get('tool_sample_rejection') in TOOL_TRANSCRIPT_MISMATCHES
                    and type(started) in (int, float)
                    and type(ended) in (int, float)
                    and 0 <= started <= ended <= 604800000
                    and DOT_STAGES.isdisjoint(stages)
                    and 'task_result' not in stages
                    and row.get('turn_id') is None
                    and row.get('task_outcome') is None
                ):
                    samples = row.get(ASR_INPUT_SAMPLES)
                    self.acoustic_evidence['sole_tool_rejection_input'] = {
                        ASR_INPUT_SAMPLES: samples if valid_asr_input_samples(samples) else None,
                        'capture': summarize_rejection_capture(row),
                    }
            if wake_before is not None:
                local, english = english_wake_counts(health)
                wake_delta = local - wake_before[0]
                english_delta = english - wake_before[1]
                self.acoustic_evidence.update(
                    wake_language='en',
                    local_wake_detections=wake_delta,
                    english_wake_detections=english_delta,
                    english_wake_input_coverage=input_coverage,
                )
                if input_shape is not None:
                    self.acoustic_evidence['english_wake_input_shape'] = input_shape
                require(
                    0 <= english_delta <= wake_delta <= 1,
                    'english_wake_counter_delta_invalid',
                )
            self.checkpoint(self.report('running'))
            if self.tool_local_compare:
                require(
                    not admitted
                    and len(rows) <= 1
                    and not self.acoustic_evidence['stage_counts']['brain_start']
                    and not self.acoustic_evidence['completed_tasks']
                    and not groups['invalid'],
                    'tool_comparison_unexpected_voice_activity',
                )
                if not rejected:
                    return None
                row = rejected[0]
                require(row['gate'] != 'wake_closed', 'tool_comparison_local_wake_not_confirmed')
                comparison = row.get('tool_asr_comparison')
                samples = row.get(ASR_INPUT_SAMPLES)
                require(
                    tool_rejections == {'comparison_only': 1}
                    and row.get('outcome') == 'rejected'
                    and row.get('task_outcome') is None
                    and row.get('turn_id') is None
                    and row.get('error') is None
                    and {'wake_detected', 'asr_start', 'asr_end'} <= set(row.get('stages', {}))
                    and valid_tool_asr_comparison(comparison)
                    and comparison.get('local_engine', 'whisper') == self.tool_compare_engine
                    and valid_asr_input_samples(samples),
                    'tool_comparison_receipt_invalid',
                )
                self.acoustic_evidence['tool_asr_comparison'] = dict(comparison)
                self.acoustic_evidence['same_input_samples'] = samples
                self.checkpoint(self.report('running'))
                require(
                    comparison['local'] not in ('error', 'timeout'), 'tool_comparison_decode_failed'
                )
                return comparison
            if tool_latency:
                require(
                    not any(row['gate'] == 'tool_sample_guard' for row in rejected),
                    'tool_sample_transcript_not_confirmed',
                )
            require(len(admitted) <= 1, 'unexpected_extra_voice_request')
            if admitted:
                current_id = admitted[0]['request_id']
                require(
                    admitted_id is None or current_id == admitted_id,
                    'unexpected_extra_voice_request',
                )
                admitted_id = current_id
            require(not groups['invalid'], 'synthetic_voice_candidate_not_rejected')
            require(
                all(
                    r.get('outcome') in (None, 'completed') and r.get('error') is None
                    for r in admitted
                ),
                'synthetic_voice_request_not_completed',
            )
            if wake_before is not None and self.acoustic_evidence['english_wake_detections'] != 1:
                require(completed is None, 'synthetic_english_wake_not_confirmed')
                return None
            return completed

        response_seconds = min(120, response_deadline - self.clock())
        require(response_seconds > 0, 'acoustic_probe_capture_budget_insufficient')
        completed = self.wait(received, response_seconds, 'synthetic_acoustic_chain_not_confirmed')
        if self.tool_local_compare:
            return
        matched_task = matching_acoustic_task(
            self.request({'operation': 'workspace_status'}),
            completed.get('generation'),
            self.qa_question,
        )
        self.health()
        recognized_stimulus_matches = matched_task is not None
        self.acoustic_evidence['recognized_stimulus_matches'] = recognized_stimulus_matches
        if (
            matched_task is not None
            and self.latency_sample
            and self.latency_scenario in MATH_SCENARIOS
        ):
            math_answer = math_answer_status(self.qa_question, matched_task.get('result'))
            self.acoustic_evidence['math_answer_status'] = math_answer
        self.checkpoint(self.report('running'))
        require(recognized_stimulus_matches, 'synthetic_stimulus_not_confirmed')
        # An immediate cleanup could hide a post-answer echo turn. Keep one
        # bounded observation window. An extra admitted dialogue fails, while
        # a transient candidate must resolve and a rejected pause must resume.
        # Counts remain visible; success never implies pause-free playback.
        quiet_started = self.clock()
        while self.clock() - quiet_started < quiet_seconds:
            self.sleep(min(0.5, quiet_seconds - (self.clock() - quiet_started)))
            require(received(), 'synthetic_acoustic_chain_not_confirmed')
            self.acoustic_evidence['post_playback_quiet_seconds'] = round(
                self.clock() - quiet_started, 3
            )
            self.checkpoint(self.report('running'))
        require(
            self.acoustic_evidence['pending_voice_candidates'] == 0,
            'unresolved_voice_candidate',
        )
        require(
            self.acoustic_evidence['unresumed_rejected_pauses'] == 0,
            'rejected_pause_not_resumed',
        )
        if probe_token is not None:
            observed = self.request({'operation': 'response_probe_result', 'token': probe_token})
            self.health()
            timing = summarize_response_acoustic(observed, completed.get('generation'))
            self.acoustic_evidence['response_acoustic_timing'] = timing
            require_evidence(
                timing['status'] == 'observed'
                or self.response_acoustic_coverage
                and self.latency_sample
                and acoustic_coverage_reason(timing) is not None,
                'response_acoustic_timing_unconfirmed',
            )
        d, ui = self.device.status()
        require(
            ui.get('screen_sleeping') is False and d.get('foreground') is True,
            'awake_dialogue_left_foreground' if awake else 'acoustic_probe_did_not_wake_foreground',
        )
        self.capture_status((d, ui))

    @staticmethod
    def audio_counters(health):
        keys = ('local_wake_count', 'voice_asr_attempts', 'mic_frames', 'render_frames')
        require(
            all(type(health.get(key)) is int and health[key] >= 0 for key in keys),
            'audio_counters_unavailable',
        )
        return {key: health[key] for key in keys}

    def wake_self_probe(self):
        def capture_health():
            health = self.health()
            require(
                health.get('connected') is True
                and health.get('transport') == 'usb'
                and health.get('microphone_active') is True
                and health.get('calibrating') is False
                and health.get('local_wake_ready') is True,
                'wake_self_playback_capture_lost',
            )
            return health

        def verify_counters(before, evidence):
            current = capture_health()
            after = self.audio_counters(current)
            changes = {key: after[key] - value for key, value in before.items()}
            require(all(delta >= 0 for delta in changes.values()), 'wake_self_counter_regressed')
            evidence.update(changes)
            self.checkpoint(self.report('running'))
            require(not changes['local_wake_count'], 'wake_self_playback_woke')
            require(not changes['voice_asr_attempts'], 'wake_self_playback_reached_asr')
            require(
                current.get('waiting_for_wake') is True and current.get('playing') is False,
                'wake_self_playback_not_waiting',
            )

        self.wake_self_evidence = []
        for language, text in WAKE_SELF_TEXT.items():
            health = capture_health()
            require(
                health.get('playing') is False and health.get('waiting_for_wake') is True,
                'wake_self_playback_not_waiting',
            )
            status = self.capture_status()
            require(
                status['test_capture_remaining_ms']
                >= (WAKE_SELF_TIMEOUT + POST_DIALOGUE_SECONDS + 5) * 1000,
                'wake_self_playback_capture_budget_insufficient',
            )
            rows = self.diagnostic_requests()
            require(all(row.get('outcome') is not None for row in rows), 'owner_request_pending')
            ids = {row['request_id'] for row in rows}
            before = self.audio_counters(health)
            evidence = {'language': language, 'playback_completed': False, 'quiet_seconds': 0}
            self.wake_self_evidence.append(evidence)

            self.checkpoint(self.report('running'))
            reply = self.request({'text': text})
            # Retain counter changes even if the wake or a spurious turn cut playback short.
            verify_counters(before, evidence)
            require(
                reply.get('status') == 'completed' and reply.get('characters') == len(text),
                'wake_self_playback_not_completed',
            )
            require(
                evidence['mic_frames'] > 0 and evidence['render_frames'] > 0,
                'wake_self_playback_audio_not_observed',
            )
            quiet_started = self.clock()
            while self.clock() - quiet_started < POST_DIALOGUE_SECONDS:
                self.sleep(min(0.5, POST_DIALOGUE_SECONDS - (self.clock() - quiet_started)))
                evidence['quiet_seconds'] = round(self.clock() - quiet_started, 3)
                verify_counters(before, evidence)

            new = [row for row in self.diagnostic_requests() if row['request_id'] not in ids]
            announcements = [row for row in new if row.get('origin') == 'announcement']
            require(
                len(announcements) == 1
                and announcements[0].get('outcome') == 'completed'
                and type(announcements[0].get('confirmed_playback_clauses')) is int
                and announcements[0]['confirmed_playback_clauses'] == 1
                and {'playout_ack', 'playback_done'} <= announcements[0].get('stages', {}).keys(),
                'wake_self_playback_receipt_missing',
            )
            evidence['playback_completed'] = True
            voices = [row for row in new if row.get('origin') == 'voice']
            rejected = classify_voice_records(voices)['rejected']
            require(
                len(rejected) == len(new) - 1
                and all(
                    not {'wake_detected', 'asr_start', 'pause_request'}
                    & row.get('stages', {}).keys()
                    for row in rejected
                ),
                'wake_self_playback_unexpected_request',
            )
            self.capture_status()

    @staticmethod
    def echo_soak_counters(health):
        require(
            all(type(health.get(key)) is int and health[key] >= 0 for key in ECHO_SOAK_COUNTERS),
            'echo_soak_counters_unavailable',
        )
        return {key: health[key] for key in ECHO_SOAK_COUNTERS}

    def self_playback_soak(self):
        context = self.background.play() if self.background else nullcontext(lambda: True)
        with context as background_active:
            self.observe_self_playback(background_active)

    def observe_self_playback(self, background_active):
        def snapshot():
            health = self.health()
            require(
                health.get('connected') is True
                and health.get('transport') == 'usb'
                and health.get('microphone_active') is True
                and health.get('calibrating') is False
                and health.get('waiting_for_wake') is False
                and health.get('playing') is False,
                'echo_soak_capture_or_awake_lost',
            )
            return {**self.audio_counters(health), **self.echo_soak_counters(health)}

        def delta(after, before):
            changes = {key: after[key] - value for key, value in before.items()}
            require(all(value >= 0 for value in changes.values()), 'echo_soak_counter_regressed')
            return changes

        require(background_active(), 'echo_soak_background_ended')
        baseline = snapshot()
        rows = self.diagnostic_requests()
        require(
            rows and all(row.get('outcome') is not None for row in rows),
            'owner_request_pending',
        )
        began = self.clock()
        self.echo_soak_evidence = evidence = {
            'elapsed_seconds': 0,
            'completed_announcements': 0,
            'pause_requests': 0,
            'device_pauses': 0,
            'device_resumes': 0,
            'render_samples': 0,
            'admitted_voice_requests': 0,
        }
        if self.background:
            evidence['background'] = self.background.receipt
            evidence['background_observed_seconds'] = 0
        while self.clock() - began < ECHO_SOAK_SECONDS:
            require(background_active(), 'echo_soak_background_ended')
            device = self.capture_status()
            require(
                device['test_capture_remaining_ms'] >= 135000,
                'echo_soak_capture_budget_insufficient',
            )
            before = snapshot()
            ids = {row['request_id'] for row in rows}
            last = rows[-1]['request_id']
            reply = self.request({'text': BARGE_ANNOUNCEMENT})
            self.sleep(ECHO_SOAK_QUIET_SECONDS)
            after = snapshot()
            changes = delta(after, before)
            totals = delta(after, baseline)
            rows = self.diagnostic_requests()
            require(last in {row['request_id'] for row in rows}, 'echo_soak_receipt_window_lost')
            new = [row for row in rows if row['request_id'] not in ids]
            announcements = [row for row in new if row.get('origin') == 'announcement']
            voices = [row for row in new if row.get('origin') == 'voice']
            groups = classify_voice_records(voices)
            paused = [row for row in voices if 'pause_ack' in row.get('stages', {})]
            resumed = sum('resume_ack' in row['stages'] for row in paused)
            evidence['device_pauses'] += len(paused)
            evidence['device_resumes'] += resumed
            evidence['admitted_voice_requests'] += len(groups['admitted'])
            require(background_active(), 'echo_soak_background_ended')
            evidence.update(
                elapsed_seconds=round(self.clock() - began, 3),
                pause_requests=totals['barge_in_pauses'],
                render_samples=totals['render_frames'],
            )
            if self.background:
                evidence['background_observed_seconds'] = evidence['elapsed_seconds']
            self.checkpoint(self.report('running'))
            require(
                len(new) == len(announcements) + len(voices)
                and len(groups['rejected']) == len(voices)
                and not totals['local_wake_count']
                and not totals['fast_stop_count'],
                'echo_soak_unexpected_voice',
            )
            require(
                totals['barge_in_pauses'] <= 1
                and totals['barge_in_pauses'] == totals['barge_in_resumed']
                and changes['barge_in_pauses'] == len(paused) == resumed,
                'echo_soak_false_pause_limit_or_resume_missing',
            )
            require(
                reply.get('status') == 'completed'
                and reply.get('characters') == len(BARGE_ANNOUNCEMENT)
                and len(announcements) == 1
                and announcements[0].get('outcome') == 'completed'
                and type(announcements[0].get('confirmed_playback_clauses')) is int
                and announcements[0]['confirmed_playback_clauses'] == 1
                and {'confirmed_playout_ack', 'confirmed_playback_done'}
                <= announcements[0]['stages'].keys()
                and changes['mic_frames'] > 0
                and changes['render_frames'] > 0,
                'echo_soak_playback_unconfirmed',
            )
            evidence['completed_announcements'] += 1

    def interruption_probe(self):
        def active_health():
            health = self.health()
            require(
                health.get('connected') is True
                and health.get('transport') == 'usb'
                and health.get('microphone_active') is True
                and health.get('calibrating') is False,
                'interruption_capture_or_usb_lost',
            )
            return health

        health = active_health()
        require(
            health.get('playing') is False and health.get('waiting_for_wake') is False,
            'interruption_requires_awake_idle_capture',
        )
        device = self.capture_status()
        stop_supported = (
            self.expected_apk is not None and self.expected_apk[1] >= STOP_RECEIPT_MIN_APK_CODE
        ) or device.get('test_playback_stop_supported') is True
        available = device['test_capture_remaining_ms'] / 1000
        require(available >= 120, 'interruption_capture_budget_insufficient')
        deadline = self.clock() + available
        before = self.diagnostic_requests()
        require(all(row.get('outcome') is not None for row in before), 'owner_request_pending')
        baseline = {row['request_id'] for row in before}
        seen = set()
        announcement_generation = None
        evidence = {
            'announcement_started': False,
            'stimulus_played': False,
            'announcement_cancelled': False,
            'replacement_completed': False,
            'device_pause_from_trigger_ms': None,
            VAD_ONSET_PAUSE: None,
            'transcript_matches_stimulus': None,
            'admitted_voice_requests': 0,
            'rejected_pause_requests': 0,
            'pending_voice_candidates': 0,
            'post_playback_quiet_seconds': 0,
        }
        self.barge_evidence = evidence
        stop_only = self.synthetic_stop
        local_stop = stop_only and not self.asr_stop
        if stop_only:
            require(stop_supported, 'device_playback_stop_evidence_unsupported')
            before_stop = health.get('fast_stop_count')
            before_asr = health.get('voice_asr_attempts')
            require(
                health.get('fast_stop_ready') is True
                and health.get('fast_stop_timing_supported') is True
                and all(type(count) is int and count >= 0 for count in (before_stop, before_asr)),
                'backend_fast_stop_evidence_unsupported',
            )

        def observe():
            health = active_health()
            rows = [r for r in self.diagnostic_requests() if r['request_id'] not in baseline]
            ids = {r['request_id'] for r in rows}
            require(seen <= ids, 'interruption_receipt_window_lost')
            seen.update(ids)
            announcements = [r for r in rows if r.get('origin') == 'announcement']
            voices = [r for r in rows if r.get('origin') == 'voice']
            # The stop detector can discard the VAD trace for the same
            # phrase. Accept one such trace only before ASR or admission.
            partial = [
                row
                for row in voices
                if local_stop
                and evidence['stimulus_played']
                and row.get('gate') == 'stale'
                and row.get('outcome') == 'cancelled'
                and all(row.get(key) is None for key in ('generation', 'error', 'task_outcome'))
                and isinstance(row.get('stages'), dict)
                and not {'asr_start', 'asr_end', 'brain_start'} & row.get('stages', {}).keys()
            ]
            groups = classify_voice_records([row for row in voices if row not in partial])
            admitted = groups['admitted']
            announcement = announcements[0] if len(announcements) == 1 else {}
            replacement = admitted[0] if len(admitted) == 1 else {}
            try:
                captured = validate_capture(replacement.get('capture'))
            except ValueError:
                captured = {}
            if not stop_only:
                evidence['replacement_capture'] = captured or None
                samples = replacement.get(ASR_INPUT_SAMPLES)
                evidence[ASR_INPUT_SAMPLES] = samples if valid_asr_input_samples(samples) else None
            if self.require_render_overlap:
                evidence['credible_render_overlap_frames'] = captured.get(
                    'credible_with_render_frames'
                )
                # Pre-VAD observations explain coverage; they do not satisfy
                # the existing onset-to-end strict interruption requirement.
                evidence['onset_prefix'] = (
                    {key: captured[key] for key in ONSET_PREFIX_COUNTS}
                    if ONSET_PREFIX_COUNTS <= captured.keys()
                    else None
                )
            elapsed = replacement.get('device_pause_from_trigger_ms')
            onset = replacement.get(VAD_ONSET_PAUSE)
            old_generation = announcement.get('generation')
            new_generation = replacement.get('generation')
            evidence.update(
                announcement_cancelled=announcement.get('outcome') == 'cancelled',
                replacement_completed=(
                    replacement.get('gate') == 'accepted'
                    and type(old_generation) is int
                    and type(new_generation) is int
                    and old_generation < new_generation
                    and replacement.get('outcome') == 'completed'
                    and replacement.get('task_outcome') == 'completed'
                    and {
                        'pause_request',
                        'pause_ack',
                        'asr_end',
                        'brain_start',
                        'playout_ack',
                        'playback_done',
                    }
                    <= replacement.get('stages', {}).keys()
                ),
                device_pause_from_trigger_ms=(
                    elapsed
                    if type(elapsed) in (int, float) and 0 <= elapsed <= MAX_DEVICE_PAUSE_MS
                    else None
                ),
                admitted_voice_requests=len(admitted),
                rejected_pause_requests=sum(
                    'pause_request' in r.get('stages', {}) for r in groups['rejected']
                ),
                pending_voice_candidates=len(groups['pending']),
            )
            trigger = evidence['device_pause_from_trigger_ms']
            evidence[VAD_ONSET_PAUSE] = (
                onset
                if trigger is not None
                and type(onset) in (int, float)
                and trigger <= onset <= MAX_DEVICE_PAUSE_MS
                else None
            )
            self.checkpoint(self.report('running'))
            require(
                len(announcements) <= 1 and len(rows) == len(announcements) + len(voices),
                'interruption_unexpected_request',
            )
            require(not groups['invalid'] and len(admitted) <= 1, 'interruption_unexpected_voice')
            require(not evidence['rejected_pause_requests'], 'interruption_rejected_voice_paused')
            if stop_only:
                current_stop = health.get('fast_stop_count')
                require(
                    health.get('fast_stop_ready') is True
                    and type(current_stop) is int
                    and 0
                    <= current_stop - before_stop
                    <= int(local_stop and evidence['stimulus_played']),
                    'fast_stop_count_unexpected',
                )
                evidence['fast_stop_detections'] = current_stop - before_stop
                current_asr = health.get('voice_asr_attempts')
                if type(current_asr) is int:
                    evidence['stop_cloud_asr_attempts'] = current_asr - before_asr
                if local_stop:
                    require(
                        type(current_asr) is int and current_asr == before_asr,
                        'fast_stop_reached_cloud_asr',
                    )
                    require(not admitted and len(partial) <= 1, 'fast_stop_unexpected_voice_turn')
                else:
                    require(
                        health.get('asr_engine') == 'minimax'
                        and type(current_asr) is int
                        and 0 <= current_asr - before_asr <= 2 * int(evidence['stimulus_played']),
                        'asr_stop_unexpected_asr_count',
                    )
                    require(
                        not any('asr_start' in row.get('stages', {}) for row in groups['rejected']),
                        'asr_stop_unexpected_voice',
                    )
                    stages = replacement.get('stages', {})
                    require(
                        'brain_start' not in stages and replacement.get('task_outcome') is None,
                        'asr_stop_reached_dot',
                    )
                    times = [stages.get(key) for key in ('asr_start', 'asr_end', ASR_STOP_STAGE)]
                    evidence['asr_stop_completed'] = (
                        replacement.get('gate') == 'accepted'
                        and replacement.get('outcome') == 'completed'
                        and replacement.get('error') is None
                        and type(old_generation) is int
                        and type(new_generation) is int
                        and new_generation == old_generation + 1
                        and all(type(value) in (int, float) and isfinite(value) for value in times)
                        and 0 <= times[0] <= times[1] <= times[2]
                    )
            return health, announcement, replacement

        def started():
            nonlocal announcement_generation
            health, announcement, replacement = observe()
            require(not replacement, 'interruption_voice_before_stimulus')
            require(announcement.get('outcome') is None, 'interruption_announcement_ended_early')
            if (
                health.get('playing') is not True
                or health.get('waiting_for_wake') is not False
                or 'playout_ack' not in announcement.get('stages', {})
                or evidence['pending_voice_candidates']
            ):
                return False
            d, ui = self.device.status()
            self.capture_status((d, ui))
            ready = (
                ui.get('screen_sleeping') is False
                and d.get('announcement_active') is True
                and d.get('phase') == 'speaking'
                and d.get('playback_paused') is False
            )
            if ready:
                announcement_generation = announcement.get('generation')
                require(
                    type(announcement_generation) is int and announcement_generation > 0,
                    'interruption_announcement_generation_missing',
                )
            return ready

        def completed():
            health, announcement, replacement = observe()
            require(
                announcement.get('outcome') in (None, 'cancelled'),
                'interruption_announcement_not_cancelled',
            )
            if local_stop:
                return (
                    evidence['announcement_cancelled']
                    and evidence['fast_stop_detections'] == 1
                    and health.get('playing') is False
                )
            if self.asr_stop:
                return (
                    replacement
                    if evidence['announcement_cancelled']
                    and evidence['asr_stop_completed']
                    and evidence['stop_cloud_asr_attempts'] in (1, 2)
                    and health.get('playing') is False
                    else None
                )
            require(
                replacement.get('outcome') in (None, 'completed') and not replacement.get('error'),
                'interruption_replacement_failed',
            )
            if evidence['announcement_cancelled'] and evidence['replacement_completed']:
                timing = response_playout_timing(replacement)
                evidence['response_playout_timing'] = timing
                require(
                    timing['status'] == 'observed', 'interruption_replacement_playout_unconfirmed'
                )
                return replacement
            return None

        with ThreadPoolExecutor(max_workers=1) as pool:
            reply = pool.submit(self.request, {'text': BARGE_ANNOUNCEMENT})
            self.wait(started, 25, 'interruption_playout_not_observed')
            evidence['announcement_started'] = True
            known_token = None
            if self.known_speech_required:
                armed = self.request(
                    {
                        'operation': 'known_speech_arm',
                        'generation': announcement_generation,
                        **self.device.interruption_fixture(),
                    }
                )
                require(
                    isinstance(armed, dict)
                    and set(armed) == {'status', 'token'}
                    and armed['status'] == 'armed'
                    and isinstance(armed['token'], str)
                    and re.fullmatch('[a-f0-9]{32}', armed['token']),
                    'known_speech_arm_unconfirmed',
                )
                known_token = armed['token']
                # Freeze already-received far-only samples before permitting
                # external speech. Never fit using the mixed test segment.
                if self.known_speech_room_model:
                    self.sleep(1.0)

                    def preflight_reference():
                        receipt = self.request(
                            {
                                'operation': 'known_speech_stimulus',
                                'token': known_token,
                                'verify_far': True,
                            }
                        )
                        if receipt == {'status': 'insufficient_audio'}:
                            return None
                        try:
                            checked = validate_far_preflight(receipt)
                        except ValueError:
                            raise CheckFailure('known_speech_boundary_unconfirmed') from None
                        evidence['far_reference_preflight'] = checked
                        return checked if checked['status'] == 'checked' else None

                    preflight = self.wait(
                        preflight_reference, 1.5, 'known_speech_common_overlap_unconfirmed'
                    )
                    require(
                        preflight['coverage']['estimated_overlap_frames'] >= MIN_COMMON_FRAMES,
                        'known_speech_common_overlap_unconfirmed',
                    )
                    require(
                        preflight['transfer']['status'] == 'validated',
                        'known_speech_far_reference_unconfirmed',
                    )
                    boundary = preflight['boundary']
                    original_generation = announcement_generation
                    require(
                        started() and announcement_generation == original_generation,
                        'known_speech_far_playback_changed',
                    )
                else:
                    self.sleep(0.25)
            prepared_options = (
                {'expected_input': self.prepared_input} if self.prepared_input is not None else {}
            )
            if self.asr_stop:
                self.device.speak_probe(stop=True, asr_stop=True, **prepared_options)
            elif stop_only:
                self.device.speak_probe(stop=True, **prepared_options)
            else:
                self.device.speak_probe(interruption=True, **prepared_options)
            evidence['stimulus_played'] = True
            if known_token is not None:
                value = self.request({'operation': 'known_speech_result', 'token': known_token})
                try:
                    clean = validate_known_speech(value)
                except ValueError:
                    raise CheckFailure('known_speech_evidence_invalid') from None
                require(
                    clean['input_pcm_sha256'] == self.prepared_input['pcm_sha256']
                    and clean['template_offset_frames'] + clean['template_frames']
                    <= self.prepared_input['frames'],
                    'known_speech_input_mismatch',
                )
                self.known_speech_evidence = clean
                if self.known_speech_room_model:
                    transfer = clean.get('far_transfer')
                    require(
                        isinstance(transfer, dict)
                        and all(
                            transfer[k] == boundary[k]
                            for k in ('pre_stimulus_mic_frames', 'pre_stimulus_render_frames')
                        ),
                        'known_speech_boundary_mismatch',
                    )
            self.checkpoint(self.report('running'))
            remaining = min(
                12 if local_stop else 30 if self.asr_stop else 75,
                deadline - self.clock() - POST_DIALOGUE_SECONDS - 5,
            )
            require(remaining > 0, 'interruption_capture_budget_insufficient')
            failure = (
                'asr_stop_not_confirmed'
                if self.asr_stop
                else 'fast_stop_not_confirmed'
                if local_stop
                else 'interruption_replacement_not_confirmed'
            )
            replacement = self.wait(completed, remaining, failure)
            require(
                reply.result(timeout=5).get('status') == 'interrupted',
                'interruption_announcement_receipt_invalid',
            )
        require(
            stop_only or evidence['device_pause_from_trigger_ms'] is not None,
            'interruption_pause_timing_missing',
        )
        quiet_started = self.clock()
        stop_asr_attempts = evidence.get('stop_cloud_asr_attempts')
        while self.clock() - quiet_started < POST_DIALOGUE_SECONDS:
            self.sleep(min(0.5, POST_DIALOGUE_SECONDS - (self.clock() - quiet_started)))
            require(completed(), failure)
            require(
                not self.asr_stop or evidence.get('stop_cloud_asr_attempts') == stop_asr_attempts,
                'asr_stop_unexpected_asr_count',
            )
            evidence['post_playback_quiet_seconds'] = round(self.clock() - quiet_started, 3)
        require(not evidence['pending_voice_candidates'], 'interruption_unresolved_voice')
        d, ui = self.device.status()
        self.capture_status((d, ui))
        if stop_supported:
            cancel_generation = (
                announcement_generation + 1 if stop_only else replacement['generation']
            )
            stop = d.get('test_playback_stop')
            valid_stop = (
                isinstance(stop, dict)
                and set(stop) == {'generation', 'playback_id', 'cancel_generation', 'nanos'}
                and all(type(value) is int and value > 0 for value in stop.values())
                and stop['generation'] == announcement_generation
                and stop['cancel_generation'] == cancel_generation
            )
            evidence['device_stop_applied'] = valid_stop
        if not stop_only:
            transcript = d.get('test_interruption_transcript')
            require(
                isinstance(transcript, dict)
                and set(transcript) == {'generation', 'matched'}
                and type(transcript['generation']) is int
                and transcript['generation'] == replacement['generation']
                and type(transcript['matched']) is bool,
                'interruption_transcript_receipt_missing',
            )
            evidence['transcript_matches_stimulus'] = transcript['matched']
            require(transcript['matched'], 'interruption_transcript_mismatch')
        if stop_supported:
            require_evidence(valid_stop, 'interruption_device_stop_not_confirmed')
            if local_stop:
                timing = active_health().get('fast_stop_observation')
                require_evidence(
                    isinstance(timing, dict)
                    and set(timing) == {'cancel_generation', 'detecting_frame_nanos'}
                    and type(timing['cancel_generation']) is int
                    and timing['cancel_generation'] == cancel_generation
                    and type(timing['detecting_frame_nanos']) is int
                    and 0 < timing['detecting_frame_nanos'] <= stop['nanos'],
                    'fast_stop_device_timing_unconfirmed',
                )
                elapsed_ns = stop['nanos'] - timing['detecting_frame_nanos']
                require_evidence(
                    elapsed_ns <= MAX_FAST_STOP_DEVICE_MS * 1000000,
                    'fast_stop_device_timing_out_of_range',
                )
                evidence[FAST_STOP_DEVICE_METRIC] = round(elapsed_ns / 1e6, 3)
            if stop_only and (
                self.asr_stop
                or d.get('test_stop_visual_timing_supported') is True
                or (
                    self.expected_apk is not None
                    and self.expected_apk[1] >= STOP_VISUAL_MIN_APK_CODE
                )
            ):
                visuals = d.get('test_stop_visual_timing')
                require_evidence(
                    isinstance(visuals, dict)
                    and set(visuals) == {'cancel_generation', 'nanos'}
                    and type(visuals['cancel_generation']) is int
                    and visuals['cancel_generation'] == cancel_generation
                    and type(visuals['nanos']) is int
                    and 0 <= visuals['nanos'] - stop['nanos'] <= MAX_STOP_VISUAL_MS * 1000000,
                    'asr_stop_visual_timing_unconfirmed'
                    if self.asr_stop
                    else 'fast_stop_visual_timing_unconfirmed',
                )
                evidence[STOP_VISUAL_METRIC] = round((visuals['nanos'] - stop['nanos']) / 1e6, 3)
        require(
            ui.get('screen_sleeping') is False
            and d.get('playback_paused') is False
            and (not stop_only or d.get('announcement_active') is False),
            'interruption_device_not_ready_after_response',
        )
        if self.require_render_overlap:
            overlap = evidence.get('credible_render_overlap_frames')
            require_evidence(
                type(overlap) is int and 0 < overlap <= 2000,
                'interruption_render_overlap_unconfirmed',
            )
        if self.known_speech_required:
            require_evidence(
                isinstance(self.known_speech_evidence, dict)
                and self.known_speech_evidence.get('status') == 'observed',
                'known_speech_window_unconfirmed',
            )

    def cleanup(self):
        if not self.touched:
            return
        # Keep a fixed phase code if a step fails. Exception bodies from the
        # device/IPC can contain private output and must not reach the report.
        self.cleanup_reason = 'cleanup_runtime_unconfirmed'
        try:
            self.health()
        except RuntimeChanged:
            raise
        except (OSError, ValueError, CheckFailure):
            # A failed health query alone must not prevent emergency Home
            # from stopping this test's bounded device capture.
            pass
        # Home always stops AudioRecord and disarms automatic startup. A fresh
        # maintenance open then restores the original foreground, mic-off state.
        self.cleanup_reason = 'cleanup_home_failed'
        self.device.home()
        self.cleanup_reason = 'cleanup_capture_stop_unconfirmed'
        self.wait(
            lambda: self.health().get('microphone_active') is False,
            15,
            'cleanup_capture_not_stopped',
        )
        self.cleanup_reason = 'cleanup_maintenance_open_failed'
        self.device.open(False)
        self.cleanup_reason = 'cleanup_connection_unconfirmed'
        self.wait(self.inactive, 25, 'cleanup_connection_not_restored')
        self.cleanup_reason = 'cleanup_foreground_unconfirmed'
        self.sleep(2)
        d, ui = self.device.status()
        require(
            d.get('foreground') is True
            and d.get('microphone_active') is False
            and ui.get('camera_open') is False
            and self.inactive(),
            'cleanup_not_confirmed',
        )
        if self.tool_sample_token is not None:
            self.cleanup_reason = 'cleanup_tool_sample_guard_unconfirmed'
            require(
                self.request({'operation': 'tool_sample_release', 'token': self.tool_sample_token})
                == {'status': 'released'},
                'cleanup_tool_sample_guard_not_released',
            )
            self.tool_sample_token = None
        self.cleanup_verified = True
        self.cleanup_reason = None

    def run(self):
        state = 'failed'
        try:
            self.check(CHECKS[0], self.preflight)
            self.check(CHECKS[1], self.maintenance_open)
            if not self.latency_sample:
                self.check(MULTI_CLAUSE_CHECK if self.multi_clause else CHECKS[2], self.playback)
                self.check(CHECKS[3], self.handover)
            if self.capture:
                device_status = self.check(CHECKS[4], self.start_capture)
                if self.capture_clock_required:
                    self.check(
                        CAPTURE_CLOCK_CHECK,
                        lambda: self.capture_clock_probe(device_status),
                        continue_if_unconfirmed=True,
                    )
                if self.playback_evidence:
                    self.check(CHECKS[9], self.playback_probe, continue_if_unconfirmed=True)
                if self.clock_evidence:
                    self.check(
                        RENDER_CLOCK_CHECK, self.render_clock_probe, continue_if_unconfirmed=True
                    )
                if self.leading_acoustic_evidence:
                    self.check(
                        LEADING_ACOUSTIC_CHECK,
                        self.leading_acoustic_probe,
                        continue_if_unconfirmed=True,
                    )
                if self.active_handover:
                    self.check(ACTIVE_HANDOVER_CHECK, self.active_capture_handover)
                if self.wake_self_playback:
                    self.check(WAKE_SELF_CHECK, self.wake_self_probe)
                if self.awake_dialogue or self.interruption_enabled:
                    quiet_seconds = 2 if self.interruption_enabled else POST_DIALOGUE_SECONDS
                    if self.tool_local_compare:
                        awake_check = TOOL_COMPARISON_CHECK
                    else:
                        awake_check = (
                            BARGE_PRIME_CHECK if self.interruption_enabled else AWAKE_DIALOGUE_CHECK
                        )
                    self.check(
                        awake_check,
                        lambda: self.acoustic_probe(awake=True, quiet_seconds=quiet_seconds),
                    )
                if self.echo_soak:
                    self.check(ECHO_SOAK_CHECK, self.self_playback_soak)
                if self.interruption_enabled:
                    self.check(
                        ASR_STOP_CHECK
                        if self.asr_stop
                        else STOP_CHECK
                        if self.synthetic_stop
                        else BARGE_CHECK,
                        self.interruption_probe,
                    )
                self.check(CHECKS[5], self.home)
                self.check(CHECKS[6], self.maintenance_open)
                if self.all_route_loss:
                    self.check(ALL_ROUTE_LOSS_CHECK, self.all_routes_loss_probe)
            if self.standby:
                self.check(CHECKS[7], self.idle)
            if self.acoustic:
                self.check(CHECKS[8], self.acoustic_probe)
            if all(row['state'] == 'passed' for row in self.checks):
                state = 'passed'
        except CheckFailure:
            pass
        except KeyboardInterrupt:
            state = 'interrupted'
        finally:
            try:
                self.cleanup()
            except Exception:
                state = 'cleanup_unconfirmed'
                if self.cleanup_reason is None:
                    self.cleanup_reason = 'cleanup_operation_failed'
            finally:
                if self.preparation_attempted:
                    self.prepared_input_cleaned = False
                    try:
                        self.device.close_speech_probe()
                        self.prepared_input_cleaned = True
                    except Exception:
                        state = 'cleanup_unconfirmed'
                        if self.cleanup_reason is None:
                            self.cleanup_reason = 'cleanup_fixture_not_removed'
            self.checkpoint(self.report(state))
        return self.report(state)


def planned_checks(options, *, stop_route='local', tool_comparison=False):
    if tool_comparison:
        return [*CHECKS[:2], CHECKS[4], TOOL_COMPARISON_CHECK, *CHECKS[5:7]]
    if 'latency_sample' in options:
        return [*CHECKS[:2], CHECKS[4], AWAKE_DIALOGUE_CHECK, *CHECKS[5:7]]
    interruption = {'synthetic_barge', 'synthetic_stop'} & options
    return (
        [*CHECKS[:2], MULTI_CLAUSE_CHECK if 'multi_clause' in options else CHECKS[2], CHECKS[3]]
        + (
            [CHECKS[4]]
            + ([CAPTURE_CLOCK_CHECK] if 'capture_clock_evidence' in options else [])
            + ([CHECKS[9]] if 'playback_evidence' in options else [])
            + ([RENDER_CLOCK_CHECK] if 'clock_evidence' in options else [])
            + ([LEADING_ACOUSTIC_CHECK] if 'leading_acoustic_evidence' in options else [])
            + ([ACTIVE_HANDOVER_CHECK] if 'active_handover' in options else [])
            + ([WAKE_SELF_CHECK] if 'wake_self_playback' in options else [])
            + (
                [BARGE_PRIME_CHECK if interruption else AWAKE_DIALOGUE_CHECK]
                if interruption or 'awake_dialogue' in options
                else []
            )
            + ([ECHO_SOAK_CHECK] if 'echo_soak' in options else [])
            + (
                [ASR_STOP_CHECK if stop_route == 'asr' else STOP_CHECK]
                if 'synthetic_stop' in options
                else [BARGE_CHECK]
                if interruption
                else []
            )
            + list(CHECKS[5:7])
            + ([ALL_ROUTE_LOSS_CHECK] if 'all_route_loss' in options else [])
            if 'capture' in options
            else []
        )
        + ([CHECKS[7]] if 'standby' in options else [])
        + ([CHECKS[8]] if 'acoustic' in options else [])
    )


def awake_english_gate_miss(result):
    evidence = result.get('acoustic_evidence') or {}
    last = result['checks'][-1]
    return (
        result['state'] == 'failed'
        and last['name'] == AWAKE_DIALOGUE_CHECK
        and last.get('reason') == 'synthetic_acoustic_chain_not_confirmed'
        and evidence.get('new_voice_requests') == 1
        and evidence.get('rejected_voice_gates', {}).get('wake_closed') == 1
        and evidence.get('english_wake_detections') == 0
        and evidence.get('admitted_voice_requests') == 0
        and evidence.get('stage_counts', {}).get('asr_start') == 0
    )


def run_english_wake_pair(make_run, checkpoint, *, fixture_directory=None, awake_only=False):
    """Own one fixed WAV across the awake control and real standby check."""
    cases = ENGLISH_WAKE_PHASES[:1] if awake_only else ENGLISH_WAKE_PHASES
    phases = [dict(name=name, state='not_run') for name, _ in cases]
    report = {
        'schema': 1,
        'scope': 'english_wake_awake' if awake_only else 'english_wake_pair',
        'state': 'running',
        'phases': phases,
        'prepared_english_input': None,
        'prepared_english_input_cleaned': None,
        'full_p0_acceptance': False,
        'raw_audio_saved': False,
        'contains_conversation_content': False,
    }
    checkpoint(report)
    fixture = None
    state = 'failed'
    try:
        options = {'directory': fixture_directory} if fixture_directory is not None else {}
        fixture = PreparedSpeech(
            'Daniel', 'Bumblebee, ' + ENGLISH_QUESTION.lower(), rate=155, **options
        )
        report['prepared_english_input'] = validate_prepared_speech(fixture.metadata)
        report['prepared_english_input_cleaned'] = False
        if fixture_directory is not None and not wake_input_request(
            {
                'operation': 'wake_input_arm',
                'directory': fixture.directory_name,
                'metadata': report['prepared_english_input'],
            }
        ):
            report['reason'] = 'wake_fixture_identity_unconfirmed'
            return report
        for phase, (_, options) in zip(phases, cases, strict=True):

            def update(value, *, phase=phase):
                phase.update(state=value['state'], report=value)
                checkpoint(report)

            result = make_run(options, update, english_fixture=fixture).run()
            update(result)
            if result['state'] == 'interrupted':
                state = 'interrupted'
                break
            if not (
                result['cleanup_microphone_off_verified'] is True
                and result.get('english_fixture_played') is True
            ):
                break
            # Continue an awake miss only if the local gate blocked the sole
            # candidate before ASR. The pair still retains the awake failure.
            if result['state'] != 'passed' and not (
                phase['name'] == 'awake' and awake_english_gate_miss(result)
            ):
                break
        else:
            if all(phase['state'] == 'passed' for phase in phases):
                state = 'passed'
    except SpeechFixtureCleanupError as error:
        fixture = error.fixture
        report['reason'] = 'fixture_preparation_failed'
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        report['reason'] = 'pair_operation_failed'
    except KeyboardInterrupt:
        state = 'interrupted'
    finally:
        if fixture is not None:
            try:
                fixture.close()
                report['prepared_english_input_cleaned'] = True
            except Exception:
                report['prepared_english_input_cleaned'] = False
                report['reason'] = 'cleanup_fixture_not_removed'
                state = 'cleanup_unconfirmed'
        report['state'] = state
        checkpoint(report)
    return report


def matched_playout(evidence):
    timing = evidence.get('response_playout_timing')
    if not isinstance(timing, dict) or timing.get('status') != 'observed':
        return False
    start = timing.get('utterance_end_ms')
    ack = timing.get('confirmed_playout_ack_ms')
    done = timing.get('confirmed_playback_done_ms')
    elapsed = timing.get(QA_PLAYOUT_METRIC)
    return (
        all(
            type(value) in (int, float) and 0 <= value <= MAX_PHASE_ELAPSED_MS
            for value in (start, ack, done, elapsed)
        )
        and start <= ack <= done
        and elapsed == round(ack - start, 1)
    )


def device_suite_phases(
    *, qa_latency, extended_voice, interruption=False, echo_soak=False, legacy_echo_soak=False
):
    if echo_soak:
        return ECHO_SOAK_PHASES[:2] if legacy_echo_soak else ECHO_SOAK_PHASES
    if interruption:
        return INTERRUPTION_PHASES
    if qa_latency:
        return QA_LATENCY_PHASES
    return EXTENDED_VOICE_PHASES if extended_voice else P0_DEVICE_PHASES


def run_device_suite(
    make_run,
    save,
    *,
    qa_latency=False,
    response_acoustic=False,
    response_acoustic_coverage=False,
    require_render_overlap=False,
    extended_voice=False,
    latency_scenario='qa',
    interruption=False,
    echo_soak=False,
    background=None,
    legacy_echo_soak=False,
):
    if require_render_overlap and not interruption:
        raise ValueError('Render overlap requires an interruption suite')
    if response_acoustic_coverage and (
        not (qa_latency and response_acoustic) or extended_voice or interruption or echo_soak
    ):
        raise ValueError('Acoustic coverage requires a QA acoustic suite')
    planned = device_suite_phases(
        qa_latency=qa_latency,
        extended_voice=extended_voice,
        interruption=interruption,
        echo_soak=echo_soak,
        legacy_echo_soak=legacy_echo_soak,
    )
    scope = 'synthetic_qa_latency' if qa_latency else 'fixed_device_suite'
    if interruption:
        scope = 'synthetic_interruption_latency'
    if echo_soak:
        scope = ECHO_SOAK_SCOPE
    phases = [
        dict(name=name, state='not_run', expected_checks=planned_checks(set(options)))
        for name, options in planned
    ]
    suite = dict(
        schema=1,
        scope=scope,
        state='running',
        phases=phases,
        full_p0_acceptance=False,
        physical_usb_unplug_tested=False,
        human_accuracy_asserted=False,
    )
    if extended_voice:
        suite['extended_voice_evidence'] = True
    if qa_latency:
        suite['audible_onset_measured'] = False
        suite['qa_corpus_sha256'] = SCENARIO_DIGESTS[latency_scenario]
        if latency_scenario != 'qa':
            suite['latency_scenario'] = latency_scenario
        suite['latencies_ms'] = {
            name: percentiles([])
            for name in (*QA_STAGE_METRICS, *QA_OPTIONAL_METRICS, QA_PLAYOUT_METRIC)
        }
        if response_acoustic:
            suite['response_acoustic_evidence'] = True
            suite['latencies_ms'][ACOUSTIC_RESPONSE_METRIC] = percentiles([])
        if response_acoustic_coverage:
            suite['response_acoustic_coverage'] = True
            suite['response_acoustic_unconfirmed'] = {}
        if latency_scenario in MATH_SCENARIOS:
            suite['math_answers'] = dict.fromkeys(
                ('matched', 'mismatch', 'unconfirmed', 'unreported'), 0
            )
    if require_render_overlap:
        suite['require_render_overlap'] = True
    interruption_samples = {}
    if interruption:
        interruption_samples = {
            name: [] for name in (BARGE_DEVICE_METRIC, FAST_STOP_DEVICE_METRIC, STOP_VISUAL_METRIC)
        }
        suite['latencies_ms'] = {
            name: percentiles(values) for name, values in interruption_samples.items()
        }
    if echo_soak:
        echo_seconds = LEGACY_ECHO_SOAK_SECONDS if legacy_echo_soak else ECHO_SOAK_SECONDS
        echo_limit = LEGACY_ECHO_SOAK_CAPTURE_MS if legacy_echo_soak else ECHO_SOAK_CAPTURE_MS
        if not legacy_echo_soak:
            suite['self_playback_seconds_per_phase'] = echo_seconds
        if background is not None:
            suite['background'] = background
        suite['echo_soak_totals'] = dict.fromkeys(
            (
                'elapsed_seconds',
                'completed_announcements',
                'pause_requests',
                'device_pauses',
                'device_resumes',
                'render_samples',
            ),
            0,
        )
    samples = []
    save(suite)
    source = None
    for index, (phase, (_, options)) in enumerate(zip(phases, planned)):
        phase['state'] = 'running'

        def checkpoint(report, phase=phase):
            phase['report'] = report
            save(suite)

        save(suite)
        run = (
            make_run(
                set(options), checkpoint, qa_question=SCENARIO_QUESTIONS[latency_scenario][index]
            )
            if qa_latency
            else make_run(set(options), checkpoint)
        )
        if source is None:
            source = (run.source_sha256, run.speech_source_sha256)
        if (run.source_sha256, run.speech_source_sha256) != source:
            phase.update(state='failed', reason='suite_source_changed')
        else:
            result = run.run()
            phase['report'] = result
            phase['state'] = result['state']
            if phase['state'] == 'passed' and (
                result['cleanup_microphone_off_verified'] is not True
                or [(row['name'], row['state']) for row in result['checks']]
                != [(name, 'passed') for name in phase['expected_checks']]
            ):
                phase.update(state='failed', reason='suite_phase_evidence_incomplete')
            if phase['state'] == 'passed' and 'synthetic_stop' in options:
                stop = result.get('barge_evidence')
                apk = result.get('expected_apk_code')
                visuals_required = interruption or (
                    type(apk) is int and apk >= STOP_VISUAL_MIN_APK_CODE
                )
                if not (
                    isinstance(stop, dict)
                    and stop.get('device_stop_applied') is True
                    and stop.get('announcement_cancelled') is True
                    and stop.get('stimulus_played') is True
                    and type(stop.get('fast_stop_detections')) is int
                    and stop['fast_stop_detections'] == 1
                    and type(stop.get('stop_cloud_asr_attempts')) is int
                    and stop['stop_cloud_asr_attempts'] == 0
                    and type(stop.get(FAST_STOP_DEVICE_METRIC)) in (int, float)
                    and 0 <= stop[FAST_STOP_DEVICE_METRIC] <= MAX_FAST_STOP_DEVICE_MS
                    and (
                        not visuals_required
                        or type(stop.get(STOP_VISUAL_METRIC)) in (int, float)
                        and 0 <= stop[STOP_VISUAL_METRIC] <= MAX_STOP_VISUAL_MS
                    )
                    and type(stop.get('admitted_voice_requests')) is int
                    and stop['admitted_voice_requests'] == 0
                    and type(stop.get('post_playback_quiet_seconds')) in (int, float)
                    and isfinite(stop['post_playback_quiet_seconds'])
                    and stop['post_playback_quiet_seconds'] >= POST_DIALOGUE_SECONDS
                ):
                    phase.update(state='failed', reason='suite_synthetic_stop_evidence_incomplete')
            if phase['state'] == 'passed' and interruption:
                evidence = result.get('barge_evidence')
                if 'synthetic_barge' in options:
                    if not (
                        isinstance(evidence, dict)
                        and (
                            not require_render_overlap
                            or result.get('require_render_overlap') is True
                            and type(evidence.get('credible_render_overlap_frames')) is int
                            and 0 < evidence['credible_render_overlap_frames'] <= 2000
                        )
                        and all(
                            evidence.get(key) is True
                            for key in (
                                'device_stop_applied',
                                'announcement_cancelled',
                                'stimulus_played',
                                'replacement_completed',
                                'transcript_matches_stimulus',
                            )
                        )
                        and type(evidence.get(BARGE_DEVICE_METRIC)) in (int, float)
                        and 0 <= evidence[BARGE_DEVICE_METRIC] <= MAX_DEVICE_PAUSE_MS
                        and (
                            evidence.get(VAD_ONSET_PAUSE) is None
                            or type(evidence[VAD_ONSET_PAUSE]) in (int, float)
                            and evidence[BARGE_DEVICE_METRIC]
                            <= evidence[VAD_ONSET_PAUSE]
                            <= MAX_DEVICE_PAUSE_MS
                        )
                        and type(evidence.get('admitted_voice_requests')) is int
                        and evidence['admitted_voice_requests'] == 1
                        and type(evidence.get('post_playback_quiet_seconds')) in (int, float)
                        and POST_DIALOGUE_SECONDS
                        <= evidence['post_playback_quiet_seconds']
                        <= 86400
                        and matched_playout(evidence)
                    ):
                        phase.update(
                            state='failed', reason='suite_synthetic_barge_evidence_incomplete'
                        )
                    else:
                        interruption_samples[BARGE_DEVICE_METRIC].append(
                            evidence[BARGE_DEVICE_METRIC]
                        )
                        if evidence.get(VAD_ONSET_PAUSE) is not None:
                            interruption_samples.setdefault(VAD_ONSET_PAUSE, []).append(
                                evidence[VAD_ONSET_PAUSE]
                            )
                else:
                    for name in (FAST_STOP_DEVICE_METRIC, STOP_VISUAL_METRIC):
                        interruption_samples[name].append(evidence[name])
                suite['latencies_ms'] = {
                    name: percentiles(values) for name, values in interruption_samples.items()
                }
            if phase['state'] == 'passed' and (
                'awake_dialogue' in options or 'acoustic' in options
            ):
                evidence = result.get('acoustic_evidence')
                evidence = evidence if isinstance(evidence, dict) else {}
                quiet = evidence.get('post_playback_quiet_seconds')
                timing = evidence.get('response_playout_timing')
                if 'awake_dialogue' in options and (
                    type(quiet) not in (int, float)
                    or not isfinite(quiet)
                    or quiet < POST_DIALOGUE_SECONDS
                ):
                    phase.update(state='failed', reason='suite_awake_dialogue_quiet_incomplete')
                elif not isinstance(timing, dict) or timing.get('status') != 'observed':
                    phase.update(state='failed', reason='suite_response_playout_timing_incomplete')
                elif response_acoustic_coverage and (
                    not matched_playout(evidence)
                    or any(
                        type(evidence.get(name)) is not int or evidence[name] != expected
                        for name, expected in (
                            ('admitted_voice_requests', 1),
                            ('pending_voice_candidates', 0),
                            ('unresumed_rejected_pauses', 0),
                        )
                    )
                ):
                    phase.update(
                        state='failed', reason='suite_acoustic_coverage_dialogue_incomplete'
                    )
                elif evidence.get('recognized_stimulus_matches') is not True:
                    phase.update(state='failed', reason='suite_synthetic_stimulus_not_confirmed')
                elif (
                    'response_acoustic_evidence' in options
                    and observed_response_interval(evidence.get('response_acoustic_timing')) is None
                ):
                    phase.update(state='failed', reason='suite_awake_response_acoustic_incomplete')
                elif qa_latency and evidence.get('latency_scenario', 'qa') != latency_scenario:
                    phase.update(state='failed', reason='suite_latency_scenario_unconfirmed')
                elif (
                    qa_latency
                    and latency_scenario in ROUTE_COMPLETION_STAGES
                    and evidence.get('latency_route_completed') is not True
                ):
                    phase.update(state='failed', reason='suite_latency_route_incomplete')
                elif qa_latency:
                    stages = evidence.get('qa_stage_latencies_ms')
                    acoustic_timing = evidence.get('response_acoustic_timing')
                    interval = observed_response_interval(acoustic_timing)
                    missing_reason = (
                        acoustic_coverage_reason(acoustic_timing)
                        if response_acoustic_coverage
                        else None
                    )
                    math_answers = suite.get('math_answers')
                    math_answer = evidence.get('math_answer_status', 'unreported')
                    if (
                        not isinstance(stages, dict)
                        or set(stages).difference(QA_OPTIONAL_METRICS) != set(QA_STAGE_METRICS)
                        or any(
                            type(value) not in (int, float) or not 0 <= value <= 604800000
                            for value in (*stages.values(), timing.get(QA_PLAYOUT_METRIC))
                        )
                    ):
                        phase.update(state='failed', reason='suite_qa_stage_timing_incomplete')
                    elif response_acoustic and interval is None and missing_reason is None:
                        phase.update(state='failed', reason='suite_qa_acoustic_timing_incomplete')
                    elif math_answers is not None and (
                        not isinstance(math_answer, str) or math_answer not in math_answers
                    ):
                        phase.update(state='failed', reason='suite_math_answer_evidence_invalid')
                    else:
                        if math_answers is not None:
                            math_answers[math_answer] += 1
                        sample = {**stages, QA_PLAYOUT_METRIC: timing[QA_PLAYOUT_METRIC]}
                        if response_acoustic and interval is not None:
                            sample[ACOUSTIC_RESPONSE_METRIC] = interval
                        if missing_reason is not None:
                            unconfirmed = suite['response_acoustic_unconfirmed']
                            unconfirmed[missing_reason] = unconfirmed.get(missing_reason, 0) + 1
                        samples.append(sample)
                        suite['latencies_ms'] = {
                            name: percentiles(
                                [sample[name] for sample in samples if name in sample]
                            )
                            for name in suite['latencies_ms']
                        }
        if phase['state'] == 'passed' and echo_soak:
            evidence = result.get('echo_soak_evidence')
            totals = suite['echo_soak_totals']
            phase_elapsed = result.get('elapsed_seconds')
            if not (
                isinstance(evidence, dict)
                and type(evidence.get('elapsed_seconds')) in (int, float)
                and echo_seconds <= evidence['elapsed_seconds'] <= echo_limit / 1000
                and type(phase_elapsed) in (int, float)
                and isfinite(phase_elapsed)
                and evidence['elapsed_seconds'] <= phase_elapsed
                and all(
                    type(evidence.get(key)) is int and evidence[key] >= 0
                    for key in totals
                    if key != 'elapsed_seconds'
                )
                and evidence['completed_announcements'] > 0
                and evidence['render_samples'] > 0
                and evidence['pause_requests']
                == evidence['device_pauses']
                == evidence['device_resumes']
                and type(evidence.get('admitted_voice_requests')) is int
                and evidence['admitted_voice_requests'] == 0
                and evidence.get('background') == background
                and (
                    background is None
                    or evidence.get('background_observed_seconds') == evidence['elapsed_seconds']
                )
            ):
                phase.update(state='failed', reason='suite_echo_soak_evidence_incomplete')
            else:
                for key in totals:
                    totals[key] += evidence[key]
                if totals['pause_requests'] > 1:
                    phase.update(state='failed', reason='suite_echo_soak_false_pause_limit')
        if phase['state'] != 'passed':
            suite['state'] = phase['state']
            save(suite)
            return suite
        save(suite)
    suite['state'] = (
        'acoustic_incomplete' if suite.get('response_acoustic_unconfirmed') else 'passed'
    )
    save(suite)
    return suite


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument(
        '--execute', action='store_true', help='Run visible hardware checks and fixed MiniMax TTS'
    )
    parser.add_argument(
        '--capture',
        action='store_true',
        help='Explicitly enable bounded foreground mic lifecycle tests',
    )
    batches = parser.add_mutually_exclusive_group()
    batches.add_argument(
        '--p0-device-suite',
        action='store_true',
        help='Run fixed device checks and three separate standby cycles with one runtime lock',
    )
    batches.add_argument(
        '--qa-latency-suite',
        action='store_true',
        help='Sample 30 fixed synthetic QA responses with matched playback receipts',
    )
    batches.add_argument(
        '--interruption-suite',
        action='store_true',
        help='Measure 15 synthetic question interruptions and 15 local stop commands',
    )
    batches.add_argument(
        '--self-playback-soak',
        action='store_true',
        help='Observe two timed awake self-playback windows without external speech',
    )
    parser.add_argument('--background-scenario', choices=BACKGROUND_KINDS)
    parser.add_argument('--background-wav', type=Path)
    parser.add_argument('--background-sha256')
    parser.add_argument('--tool-asr-comparison', choices=TOOL_COMPARE_ENGINES)
    parser.add_argument('--tool-case', type=int, metavar='1..30')
    parser.add_argument(
        '--known-speech-evidence',
        action='store_true',
        help='Corroborate one fixed near speech window during a separate synthetic barge run',
    )
    parser.add_argument(
        '--known-speech-room-model',
        action='store_true',
        help='Add pre-stimulus held-out transfer diagnostics; does not relax the raw match gate',
    )
    parser.add_argument(
        '--latency-scenario',
        choices=tuple(SCENARIO_QUESTIONS),
        default='qa',
        help='Select simple questions, public web lookup, or read-only local tool prompts',
    )
    parser.add_argument(
        '--extended-voice-evidence',
        action='store_true',
        help='Add local stop and room-confirmed awake response to the fixed P0 device suite',
    )
    parser.add_argument(
        '--standby',
        action='store_true',
        help='Also wait for a real 15-minute foreground idle screen',
    )
    parser.add_argument(
        '--acoustic',
        action='store_true',
        help='Play one public synthetic query on Mac speakers after standby',
    )
    parser.add_argument(
        '--awake-dialogue',
        action='store_true',
        help='Play one public synthetic query while the screen is already awake',
    )
    parser.add_argument(
        '--english-wake',
        action='store_true',
        help='Use one fixed English wake and question stimulus in an individual speech check',
    )
    parser.add_argument(
        '--english-wake-pair',
        action='store_true',
        help='Compare awake and real standby using one prepared English WAV',
    )
    parser.add_argument(
        '--wake-input-shape',
        action='store_true',
        help='Corroborate the fixed English fixture at the actual model inputs',
    )
    parser.add_argument(
        '--synthetic-barge',
        action='store_true',
        help='Use one fixed Mac speech stimulus to interrupt a verified ongoing CC8 announcement',
    )
    parser.add_argument(
        '--require-render-overlap',
        action='store_true',
        help='Require energetic render reference in credible frames of a synthetic interruption',
    )
    parser.add_argument(
        '--synthetic-stop',
        action='store_true',
        help='Verify a fixed local stop word and device stop without a new voice request',
    )
    parser.add_argument(
        '--stop-route',
        choices=('local', 'asr'),
        default='local',
        help='With --synthetic-stop, verify the local keyword or the MiniMax utterance route',
    )
    parser.add_argument(
        '--wake-self-playback',
        action='store_true',
        help='Check that fixed Chinese and English CC8 announcements cannot wake the local detector',
    )
    parser.add_argument(
        '--active-handover',
        action='store_true',
        help='Verify that the same bounded recording continues on Wi-Fi and USB',
    )
    parser.add_argument(
        '--all-route-loss',
        action='store_true',
        help='Temporarily close both assistant channels and verify recording stops and stays off',
    )
    parser.add_argument(
        '--multi-clause',
        action='store_true',
        help='Verify separate device receipts for both clauses of a fixed announcement',
    )
    parser.add_argument(
        '--playback-evidence',
        action='store_true',
        help='Corroborate a fixed speaker phrase using already active bounded capture',
    )
    parser.add_argument(
        '--clock-evidence',
        action='store_true',
        help='Also require opt-in presentation-clock provenance from a supporting APK',
    )
    parser.add_argument(
        '--capture-clock-evidence',
        action='store_true',
        help='Require Android recording-clock evidence from the bounded capture window',
    )
    parser.add_argument(
        '--leading-acoustic-evidence',
        action='store_true',
        help='Require corroboration of the leading blocks of the fixed speaker phrase',
    )
    parser.add_argument(
        '--response-acoustic-evidence',
        action='store_true',
        help='Corroborate fixed awake answers in an individual run or the QA latency suite',
    )
    parser.add_argument(
        '--collect-acoustic-coverage',
        action='store_true',
        help='Continue a QA acoustic suite after a bounded acoustic-only miss',
    )
    args = parser.parse_args(argv)
    options = {key for key in SMOKE_OPTIONS if getattr(args, key)}
    batch = (
        args.p0_device_suite
        or args.qa_latency_suite
        or args.interruption_suite
        or args.self_playback_soak
    )
    tool_comparison = args.tool_asr_comparison is not None
    if tool_comparison:
        if (
            not tool_sample_request({'operation': 'tool_sample_arm', 'case': args.tool_case})
            or options != {'capture', 'awake_dialogue'}
            or batch
            or args.english_wake
            or args.english_wake_pair
            or args.response_acoustic_evidence
        ):
            parser.error(
                '--tool-asr-comparison requires --tool-case and --capture --awake-dialogue'
            )
    elif args.tool_case is not None:
        parser.error('--tool-case requires --tool-asr-comparison')
    asr_stop = args.stop_route == 'asr'
    if args.known_speech_room_model and not args.known_speech_evidence:
        parser.error('--known-speech-room-model requires --known-speech-evidence')
    if args.known_speech_evidence and (
        batch or not args.synthetic_barge or not args.capture or args.synthetic_stop
    ):
        parser.error('--known-speech-evidence requires a separate --capture --synthetic-barge run')
    if asr_stop and not args.synthetic_stop:
        parser.error('--stop-route asr requires --synthetic-stop')
    if args.english_wake and (
        batch
        or not (args.awake_dialogue or args.acoustic)
        or args.response_acoustic_evidence
        or args.synthetic_stop
        or args.synthetic_barge
    ):
        parser.error(
            '--english-wake requires a separate --awake-dialogue or --standby --acoustic run'
        )
    if args.english_wake_pair:
        if batch or options != {'capture'} or args.english_wake or args.response_acoustic_evidence:
            parser.error('--english-wake-pair requires --capture and no individual check flags')
        args.english_wake = True
    if args.wake_input_shape and not (
        args.english_wake_pair or (args.english_wake and options == {'capture', 'awake_dialogue'})
    ):
        parser.error('--wake-input-shape requires the English wake pair or the awake English check')
    prepared_english = args.english_wake_pair or args.wake_input_shape
    bound_runtime = (
        batch or asr_stop or args.english_wake or args.known_speech_evidence or tool_comparison
    )
    background_receipt = None
    if any((args.background_scenario, args.background_wav, args.background_sha256)):
        background_receipt = {'kind': args.background_scenario, 'sha256': args.background_sha256}
        if (
            not args.self_playback_soak
            or args.background_wav is None
            or not valid_background(background_receipt)
        ):
            parser.error(
                'Background playback requires --self-playback-soak and all background flags'
            )
    if args.extended_voice_evidence and not args.p0_device_suite:
        parser.error('--extended-voice-evidence requires --p0-device-suite')
    if args.latency_scenario != 'qa' and not args.qa_latency_suite:
        parser.error('--latency-scenario requires --qa-latency-suite')
    if args.response_acoustic_evidence and not (
        args.qa_latency_suite or (not batch and args.awake_dialogue)
    ):
        parser.error('--response-acoustic-evidence requires --awake-dialogue or --qa-latency-suite')
    if args.collect_acoustic_coverage and not (
        args.qa_latency_suite and args.response_acoustic_evidence
    ):
        parser.error(
            '--collect-acoustic-coverage requires --qa-latency-suite and --response-acoustic-evidence'
        )
    if batch and options != {'capture'}:
        parser.error('Device batches require --capture and accept no individual check flags')
    if args.standby and not args.capture or args.acoustic and not args.standby:
        parser.error('--standby requires --capture; --acoustic requires --standby')
    if args.awake_dialogue and (not args.capture or args.standby):
        parser.error('--awake-dialogue requires --capture and a separate run from --standby')
    if args.synthetic_barge and (not args.capture or args.standby):
        parser.error('--synthetic-barge requires --capture and a separate run from --standby')
    if args.require_render_overlap and not (args.synthetic_barge or args.interruption_suite):
        parser.error('--require-render-overlap requires --synthetic-barge or --interruption-suite')
    if args.synthetic_stop and (
        not args.capture or args.standby or args.synthetic_barge or args.awake_dialogue
    ):
        parser.error('--synthetic-stop requires --capture and a separate speech check')
    if args.wake_self_playback and (
        not args.capture
        or args.standby
        or args.awake_dialogue
        or args.synthetic_barge
        or args.synthetic_stop
    ):
        parser.error(
            '--wake-self-playback requires --capture and a separate run from speech or standby probes'
        )
    if args.active_handover and not args.capture:
        parser.error('--active-handover requires --capture')
    if args.all_route_loss and (not args.capture or args.standby):
        parser.error('--all-route-loss requires --capture and a separate run from --standby')
    if args.playback_evidence and not args.capture:
        parser.error('--playback-evidence requires --capture')
    if args.clock_evidence and not args.playback_evidence:
        parser.error('--clock-evidence requires --playback-evidence')
    if args.capture_clock_evidence and not args.capture:
        parser.error('--capture-clock-evidence requires --capture')
    if args.leading_acoustic_evidence and not args.playback_evidence:
        parser.error('--leading-acoustic-evidence requires --playback-evidence')
    minimum_batch_backend = PAIRED_PLAYOUT_MIN_BACKEND
    if args.require_render_overlap:
        minimum_batch_backend = RENDER_OVERLAP_MIN_BACKEND
    elif args.self_playback_soak:
        minimum_batch_backend = ECHO_SOAK_MIN_BACKEND
    elif args.latency_scenario == 'tool':
        minimum_batch_backend = TOOL_SAMPLE_MIN_BACKEND
    elif args.latency_scenario == 'web':
        minimum_batch_backend = ROUTED_LATENCY_MIN_BACKEND
    elif args.extended_voice_evidence or args.interruption_suite:
        minimum_batch_backend = EXTENDED_VOICE_MIN_BACKEND
    elif args.response_acoustic_evidence:
        minimum_batch_backend = RESPONSE_ACOUSTIC_MIN_BACKEND
    if not args.execute:
        if prepared_english:
            cases = ENGLISH_WAKE_PHASES if args.english_wake_pair else ENGLISH_WAKE_PHASES[:1]
            plan = {
                'phases': [dict(name=name, checks=planned_checks(flags)) for name, flags in cases],
                'execute': False,
                'capture': True,
                'wake_language': 'en',
                'shared_prepared_input': True,
                'full_p0_acceptance': False,
                'minimum_backend_version': ENGLISH_WAKE_MIN_BACKEND,
            }
            if args.wake_input_shape:
                plan.update(wake_input_shape=True, minimum_backend_version='0.15.1')
        elif batch:
            plan = {
                'phases': [
                    dict(name=name, checks=planned_checks(set(flags)))
                    for name, flags in device_suite_phases(
                        qa_latency=args.qa_latency_suite,
                        extended_voice=args.extended_voice_evidence,
                        interruption=args.interruption_suite,
                        echo_soak=args.self_playback_soak,
                    )
                ],
                'execute': False,
                'capture': True,
                'full_p0_acceptance': False,
                'minimum_backend_version': minimum_batch_backend,
            }
            if args.qa_latency_suite:
                plan['qa_corpus_sha256'] = SCENARIO_DIGESTS[args.latency_scenario]
                if args.latency_scenario != 'qa':
                    plan['latency_scenario'] = args.latency_scenario
                if args.response_acoustic_evidence:
                    plan['response_acoustic_evidence'] = True
                if args.collect_acoustic_coverage:
                    plan['response_acoustic_coverage'] = True
            if args.self_playback_soak:
                plan['self_playback_seconds_per_phase'] = ECHO_SOAK_SECONDS
                if background_receipt is not None:
                    plan['background'] = background_receipt
            if args.interruption_suite:
                plan['minimum_apk_code'] = STOP_VISUAL_MIN_APK_CODE
            if args.extended_voice_evidence:
                plan['extended_voice_evidence'] = True
                plan['minimum_apk_code'] = STOP_RECEIPT_MIN_APK_CODE
        else:
            plan = {
                'checks': planned_checks(
                    options, stop_route=args.stop_route, tool_comparison=tool_comparison
                ),
                'execute': False,
                **{
                    key: getattr(args, key)
                    for key in SMOKE_OPTIONS
                    if key not in ('standby', 'acoustic')
                },
                'full_p0_acceptance': False,
                'response_acoustic_evidence': args.response_acoustic_evidence,
            }
            if asr_stop:
                plan.update(
                    stop_route='asr',
                    minimum_backend_version=ASR_STOP_MIN_BACKEND,
                    minimum_apk_code=STOP_VISUAL_MIN_APK_CODE,
                )
            if args.english_wake:
                plan.update(wake_language='en', minimum_backend_version=ENGLISH_WAKE_MIN_BACKEND)
        if args.require_render_overlap:
            plan['require_render_overlap'] = True
            plan['minimum_backend_version'] = RENDER_OVERLAP_MIN_BACKEND
        if args.known_speech_evidence:
            plan['known_speech_evidence'] = True
            plan['minimum_backend_version'] = '0.14.87'
        if args.known_speech_room_model:
            plan['known_speech_room_model'] = True
            plan['minimum_backend_version'] = '0.14.90'
        if tool_comparison:
            plan.update(
                scope=TOOL_COMPARISON_CHECK,
                tool_case=args.tool_case,
                tool_compare_engine=args.tool_asr_comparison,
                minimum_backend_version=TOOL_COMPARE_MIN_BACKEND,
                dot_dispatch_expected=False,
            )
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    try:
        background = None
        if background_receipt is not None:
            require(sys.platform == 'darwin', 'echo_soak_background_requires_macos')
            background = Background.load(
                args.background_wav, args.background_scenario, args.background_sha256
            )
        runtime = args.runtime_root
        require(
            runtime.is_absolute() and runtime.resolve() == runtime,
            'runtime_root_must_be_real_absolute',
        )
        socket_path = runtime / 'device-access/private/voice-runtime/announce.sock'
        # Check the intended live workspace before any Android/UI operation.
        health = rpc(socket_path, {'operation': 'health'})
        manifest = load_release(runtime)
        require(
            health.get('bridge_version') == manifest.backend_version
            and type(health.get('protocol_version')) is int
            and health['protocol_version'] == manifest.protocol_version,
            'runtime_source_version_mismatch',
        )
        instance = health.get('runtime_instance')
        if args.known_speech_evidence:
            require(
                health.get('known_speech_evidence_supported') is True,
                'backend_known_speech_evidence_unsupported',
            )
        if args.known_speech_room_model:
            require(
                health.get('far_reference_overlap_supported') is True,
                'backend_far_reference_overlap_unsupported',
            )
        if args.require_render_overlap:
            require(
                tuple(map(int, manifest.backend_version.split('.')))
                >= tuple(map(int, RENDER_OVERLAP_MIN_BACKEND.split('.'))),
                'runtime_render_overlap_evidence_unsupported',
            )
        if args.english_wake:
            require(
                tuple(map(int, manifest.backend_version.split('.')))
                >= tuple(map(int, ENGLISH_WAKE_MIN_BACKEND.split('.'))),
                'runtime_english_wake_evidence_unsupported',
            )
            english_wake_counts(health)
        if tool_comparison or args.qa_latency_suite and args.latency_scenario == 'tool':
            require(
                health.get('tool_sample_guard_supported') is True,
                'backend_tool_sample_guard_unsupported',
            )
        if tool_comparison:
            require(
                tuple(map(int, manifest.backend_version.split('.')))
                >= tuple(map(int, TOOL_COMPARE_MIN_BACKEND.split('.')))
                and health.get('asr_engine') in ('minimax', *TOOL_COMPARE_ENGINES)
                and health['asr_engine'] != args.tool_asr_comparison,
                'runtime_tool_comparison_unsupported',
            )
        if args.wake_input_shape:
            require(
                health.get('wake_input_probe_supported') is True, 'wake_input_probe_unsupported'
            )
        if asr_stop:
            require(
                tuple(map(int, manifest.backend_version.split('.')))
                >= tuple(map(int, ASR_STOP_MIN_BACKEND.split('.')))
                and manifest.assistant.version_code >= STOP_VISUAL_MIN_APK_CODE
                and health.get('asr_engine') == 'minimax',
                'runtime_asr_stop_evidence_unsupported',
            )
        if batch:
            if tuple(map(int, manifest.backend_version.split('.'))) < tuple(
                map(int, minimum_batch_backend.split('.'))
            ):
                parser.exit(2, f'固定设备验收批次需要后台 {minimum_batch_backend} 或更新版本。\n')
        if bound_runtime:
            require(
                isinstance(instance, str)
                and bool(instance)
                and health.get('release_id') == manifest.release_id,
                'suite_runtime_identity_unconfirmed',
            )
        if (args.response_acoustic_evidence or args.extended_voice_evidence) and health.get(
            'response_acoustic_evidence_supported'
        ) is not True:
            parser.exit(2, '当前后台未声明支持问答声学计时。\n')
        minimum_stop_apk = (
            STOP_VISUAL_MIN_APK_CODE if args.interruption_suite else STOP_RECEIPT_MIN_APK_CODE
        )
        if (args.extended_voice_evidence or args.interruption_suite) and (
            manifest.assistant.version_code < minimum_stop_apk
            or health.get('fast_stop_ready') is not True
            or health.get('fast_stop_timing_supported') is not True
            or type(health.get('fast_stop_count')) is not int
        ):
            parser.exit(2, '当前版本组合不支持扩展语音验收。\n')

        def request(payload):
            timeout = 5
            if payload.get('text') in WAKE_SELF_TEXT.values():
                timeout = WAKE_SELF_TIMEOUT
            elif 'text' in payload:
                timeout = 125
            elif payload == {'operation': 'playback_probe'}:
                timeout = 25
            result = rpc(socket_path, payload, timeout)
            if bound_runtime and payload == {'operation': 'health'}:
                if not (
                    result.get('runtime_instance') == instance
                    and result.get('release_id') == manifest.release_id
                    and result.get('bridge_version') == manifest.backend_version
                    and type(result.get('protocol_version')) is int
                    and result['protocol_version'] == manifest.protocol_version
                    and (not tool_comparison or result.get('asr_engine') == health['asr_engine'])
                ):
                    raise RuntimeChanged('suite_runtime_changed')
            return result

        folder = private_directory(
            private_directory(ROOT / 'device-access/private') / 'device-checks'
        )
        # All callers targeting this runtime share one lock, even when their
        # command sources/report directories are in different checkouts.
        lock = private_directory(socket_path.parent) / 'automation-test.lock'
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'r+') as handle:
            info = os.fstat(handle.fileno())
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and not info.st_mode & 0o077
                and info.st_nlink == 1,
                'unsafe_test_lock',
            )
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if bound_runtime:
                request({'operation': 'health'})
            name = (
                ('device-suite-' if batch else 'device-')
                + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
                + uuid.uuid4().hex[:8]
                + '.json'
            )
            with PrivateOutput(folder / name) as output:

                def save(value):
                    output.write(
                        {
                            **value,
                            'backend_version': health.get('bridge_version'),
                            'protocol_version': health.get('protocol_version'),
                            **(
                                {'tool_primary_engine': health['asr_engine']}
                                if tool_comparison
                                else {}
                            ),
                            **({'release_id': manifest.release_id} if bound_runtime else {}),
                        }
                    )

                device = (
                    Device(fixture_directory=socket_path.parent / 'public-speech-fixtures')
                    if args.known_speech_evidence
                    else Device()
                )
                if args.extended_voice_evidence or args.interruption_suite:
                    device_status, _ = device.status()
                    require(
                        device_status.get('test_playback_stop_supported') is True,
                        'device_playback_stop_evidence_unsupported',
                    )
                    if args.interruption_suite:
                        require(
                            device_status.get('test_stop_visual_timing_supported') is True,
                            'device_stop_visual_timing_unsupported',
                        )

                def make_run(
                    enabled, checkpoint, *, qa_question=ACOUSTIC_QUESTION, english_fixture=None
                ):
                    return SmokeRun(
                        device,
                        request,
                        checkpoint=checkpoint,
                        qa_question=qa_question,
                        latency_scenario='tool' if tool_comparison else args.latency_scenario,
                        tool_local_compare=tool_comparison,
                        tool_compare_engine=args.tool_asr_comparison or 'whisper',
                        background=background,
                        stop_route=args.stop_route,
                        english_wake=args.english_wake,
                        english_fixture=english_fixture,
                        wake_input_shape=args.wake_input_shape,
                        response_acoustic_evidence=(
                            args.response_acoustic_evidence
                            or 'response_acoustic_evidence' in enabled
                        ),
                        response_acoustic_coverage=args.collect_acoustic_coverage,
                        known_speech_evidence=args.known_speech_evidence,
                        known_speech_room_model=args.known_speech_room_model,
                        require_render_overlap=(
                            args.require_render_overlap and 'synthetic_barge' in enabled
                        ),
                        expected_apk=(manifest.assistant.version, manifest.assistant.version_code),
                        **dict.fromkeys(enabled - {'response_acoustic_evidence'}, True),
                    )

                print('自动设备检查已开始；结果仅保存状态，不留录音、图像或对话正文。', flush=True)
                keep_awake = None
                try:
                    if (
                        args.standby or batch or args.english_wake_pair
                    ) and sys.platform == 'darwin':
                        keep_awake = subprocess.Popen(
                            ['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    if prepared_english:
                        fixture_directory = (
                            private_directory(socket_path.parent / 'public-speech-fixtures')
                            if args.wake_input_shape
                            else None
                        )
                        result = run_english_wake_pair(
                            make_run,
                            save,
                            fixture_directory=fixture_directory,
                            awake_only=not args.english_wake_pair,
                        )
                    elif batch:
                        result = run_device_suite(
                            make_run,
                            save,
                            qa_latency=args.qa_latency_suite,
                            response_acoustic=args.response_acoustic_evidence,
                            response_acoustic_coverage=args.collect_acoustic_coverage,
                            require_render_overlap=args.require_render_overlap,
                            extended_voice=args.extended_voice_evidence,
                            latency_scenario=args.latency_scenario,
                            interruption=args.interruption_suite,
                            echo_soak=args.self_playback_soak,
                            background=background_receipt,
                        )
                    elif tool_comparison:
                        result = make_run(
                            options | {'latency_sample'},
                            save,
                            qa_question=SCENARIO_QUESTIONS['tool'][args.tool_case - 1],
                        ).run()
                    else:
                        result = make_run(options, save).run()
                finally:
                    if keep_awake is not None and keep_awake.poll() is None:
                        keep_awake.terminate()
                        keep_awake.wait(timeout=5)
                if batch or prepared_english:
                    summary = {
                        'state': result['state'],
                        'phases': [
                            {key: phase[key] for key in ('name', 'state')}
                            for phase in result['phases']
                        ],
                    }
                    if args.self_playback_soak:
                        summary['echo_soak_totals'] = result['echo_soak_totals']
                        if background_receipt is not None:
                            summary['background'] = background_receipt
                    if args.qa_latency_suite or args.interruption_suite:
                        summary['latencies_ms'] = result['latencies_ms']
                        if 'math_answers' in result:
                            summary['math_answers'] = result['math_answers']
                        if 'response_acoustic_unconfirmed' in result:
                            summary['response_acoustic_unconfirmed'] = result[
                                'response_acoustic_unconfirmed'
                            ]
                        if args.latency_scenario != 'qa':
                            summary['latency_scenario'] = args.latency_scenario
                else:
                    summary = {
                        key: result[key]
                        for key in ('state', 'checks', 'cleanup_microphone_off_verified')
                    }
                    if tool_comparison:
                        summary['tool_asr_comparison'] = (
                            result.get('acoustic_evidence') or {}
                        ).get('tool_asr_comparison')
                print(json.dumps(summary, ensure_ascii=False))
                print(output.path)
                return 0 if result['state'] == 'passed' else 2
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        parser.exit(2, '自动设备检查未启动或未完成；未自动重复设备动作。\n')


if __name__ == '__main__':
    raise SystemExit(main())

"""Explicit, bounded CC8 hardware checks; default invocation only prints a plan."""

import argparse
from concurrent.futures import ThreadPoolExecutor
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
import unicodedata
import uuid

from private_output import PrivateOutput, private_directory
from release_info import load_release
from mac_voice import resolve_voice, __file__ as voice_source
from diagnostics import INTERVALS, MAX_CONFIRMED_PLAYBACK_CLAUSES, MAX_DEVICE_PAUSE_MS, percentiles
from diagnostic_run import receipt

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
ACOUSTIC_WAKE = '大黄蜂'
ACOUSTIC_QUESTION = '一加一等于几？'
QA_LATENCY_QUESTIONS = tuple(
    f'{left}加{right}等于几？' for left in '一二三四五六' for right in '一二三四五'
)
QA_CORPUS_SHA256 = hashlib.sha256('\n'.join(QA_LATENCY_QUESTIONS).encode()).hexdigest()
QA_DIGITS = str.maketrans('一二三四五六', '123456')
WAKE_SELF_TEXT = {
    'zh': '大黄蜂正在播报。现在只检查设备自己的声音。',
    'en': 'Bumblebee is speaking. This is a playback check.',
}
WAKE_SELF_TIMEOUT = 30
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
ACTIVE_HANDOVER_CHECK = 'active_capture_logical_handover'
ALL_ROUTE_LOSS_CHECK = 'capture_stops_during_all_route_loss'
ALL_ROUTE_LOSS_REQUEST = {'operation': 'transport_all_test'}
WAKE_SELF_CHECK = 'self_playback_does_not_wake'
POST_DIALOGUE_SECONDS = 20
STANDBY_SECONDS = 900
PAIRED_PLAYOUT_MIN_BACKEND = '0.14.50'
SMOKE_OPTIONS = (
    'capture',
    'standby',
    'acoustic',
    'awake_dialogue',
    'synthetic_barge',
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
QA_STAGE_METRICS = ('utterance_to_asr_ms', 'brain_first_clause_ms', 'tts_first_pcm_ms')
QA_ENDPOINT_METRIC = 'device_vad_tail_ms'
QA_PLAYOUT_METRIC = 'utterance_to_confirmed_playout_ack_ms'


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


def classify_voice_records(rows):
    """A VAD trace exists before admission; never equate it with a dialogue."""
    rejected_gates = ('empty', 'echo_text', 'echo_render', 'weak_near', 'wake_closed')
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
        elif outcome is None and gate in (None, *rejected_gates):
            category = 'pending'
        elif outcome == 'rejected' and gate in rejected_gates:
            category = 'rejected'
        else:
            category = 'invalid'
        groups[category].append(row)
    return groups


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


def qa_stage_latencies(row):
    try:
        clean = receipt(row)
    except ValueError:
        return {}
    if clean['scenario'] != 'qa':
        return {}
    stages = clean['stages']
    result = {
        name: round(stages[end] - stages[start], 1)
        for name in QA_STAGE_METRICS
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


def normalize_acoustic_question(text):
    text = ''.join(unicodedata.normalize('NFKC', text).split()).rstrip('。?!.,，')
    if text.startswith(ACOUSTIC_WAKE):
        text = text[len(ACOUSTIC_WAKE) :].lstrip(',，:：')
    return text.translate(QA_DIGITS).replace('+', '加').replace('=', '等于')


def matches_acoustic_question(workspace, generation, question=ACOUSTIC_QUESTION):
    tasks = workspace.get('tasks')
    if (
        workspace.get('ok') is not True
        or type(generation) is not int
        or not 0 <= generation < 2**32
        or not isinstance(tasks, list)
    ):
        return False
    matches = [
        task
        for task in tasks
        if isinstance(task, dict)
        and type(task.get('generation')) is int
        and task['generation'] == generation
    ]
    if len(matches) != 1 or matches[0].get('status') != 'completed':
        return False
    title = matches[0].get('title')
    if not isinstance(title, str) or len(title) > 80:
        return False
    return normalize_acoustic_question(title) == normalize_acoustic_question(question)


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


class Device:
    def __init__(self):
        sys.path.insert(0, str(ROOT / 'device-access'))
        import cc8_install

        self.access = cc8_install
        self.transport = self.access.target()
        self.access.probe(self.transport)

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

    def prepare_speech_probe(self):
        return resolve_voice('Tingting')

    def speak_probe(self, *, interruption=False, question=ACOUSTIC_QUESTION):
        require(question in QA_LATENCY_QUESTIONS, 'unknown_synthetic_probe')
        voice = self.prepare_speech_probe()
        subprocess.run(
            [
                '/usr/bin/say',
                '-v',
                voice,
                BARGE_QUERY if interruption else ACOUSTIC_WAKE + '，' + question,
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
        latency_sample=False,
        qa_question=ACOUSTIC_QUESTION,
        synthetic_barge=False,
        wake_self_playback=False,
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
        self.latency_sample = latency_sample
        self.qa_question = qa_question
        self.synthetic_barge = synthetic_barge
        self.wake_self_playback = wake_self_playback
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
        self.initial_instance = None
        self.runtime_change_reason = None
        self.source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.speech_source_sha256 = hashlib.sha256(Path(voice_source).read_bytes()).hexdigest()
        self.expected_apk = expected_apk
        self.acoustic_evidence = None
        self.standby_evidence = None

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
            'cleanup_reason': self.runtime_change_reason
            if state == 'cleanup_unconfirmed'
            else None,
            'capture_tests_enabled': self.capture,
            'standby_enabled': self.standby,
            'synthetic_acoustic_probe_enabled': (
                self.acoustic or self.awake_dialogue or self.synthetic_barge
            ),
            'awake_dialogue_enabled': self.awake_dialogue,
            'multi_clause_evidence': self.multi_clause_evidence,
            'raw_audio_saved': False,
            'contains_conversation_content': False,
            'physical_usb_unplug_tested': False,
            'human_accuracy_asserted': False,
            'acoustic_latency_asserted': False,
            'full_p0_acceptance': False,
            'acoustic_evidence': self.acoustic_evidence,
            'barge_evidence': self.barge_evidence,
            'wake_self_evidence': self.wake_self_evidence,
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
        if self.acoustic or self.awake_dialogue or self.synthetic_barge:
            self.device.prepare_speech_probe()
        h = self.health()
        if self.multi_clause or self.wake_self_playback:
            require(h.get('clause_receipts_supported') is True, 'clause_receipts_unsupported')
        if self.wake_self_playback or self.active_handover or self.all_route_loss:
            self.audio_counters(h)
        require(
            not self.playback_evidence or h.get('playback_evidence_supported') is True,
            'backend_playback_evidence_unsupported',
        )
        require(
            not self.leading_acoustic_evidence
            or h.get('leading_acoustic_evidence_supported') is True,
            'backend_leading_acoustic_evidence_unsupported',
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
        if self.awake_dialogue or self.synthetic_barge:
            require(ui.get('wake_checked') is True, 'awake_dialogue_requires_wake_mode')
        if self.wake_self_playback:
            require(ui.get('wake_checked') is True, 'wake_self_playback_requires_wake_mode')
        if self.active_handover:
            require(ui.get('wake_checked') is True, 'active_handover_requires_wake_mode')
        if self.all_route_loss:
            require(ui.get('wake_checked') is True, 'all_route_loss_requires_wake_mode')
        if (
            self.awake_dialogue
            or self.synthetic_barge
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
        if self.standby:
            return 1200000
        return 300000 if self.synthetic_barge else 180000

    def start_capture(self):
        limit = self.capture_limit_ms
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
        self.device.speak_probe(question=self.qa_question)

        admitted_id = None
        recognized_stimulus_matches = None

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
            # Keep only fixed counts and timing, never request IDs, transcripts,
            # provider text or audio. A completed task is not a played answer.
            self.acoustic_evidence = {
                'new_voice_requests': len(rows),
                'admitted_voice_requests': len(admitted),
                'rejected_voice_candidates': len(rejected),
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
                'response_playout_timing': response_playout_timing(completed),
                **(
                    {'qa_stage_latencies_ms': qa_stage_latencies(completed)}
                    if self.latency_sample and completed is not None
                    else {}
                ),
                'stage_counts': {
                    stage: sum(stage in r.get('stages', {}) for r in rows)
                    for stage in (
                        'wake_detected',
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
            self.checkpoint(self.report('running'))
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
            return completed

        response_seconds = min(120, response_deadline - self.clock())
        require(response_seconds > 0, 'acoustic_probe_capture_budget_insufficient')
        completed = self.wait(received, response_seconds, 'synthetic_acoustic_chain_not_confirmed')
        matched = matches_acoustic_question(
            self.request({'operation': 'workspace_status'}),
            completed.get('generation'),
            self.qa_question,
        )
        self.health()
        recognized_stimulus_matches = matched
        self.acoustic_evidence['recognized_stimulus_matches'] = recognized_stimulus_matches
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
        available = device['test_capture_remaining_ms'] / 1000
        require(available >= 120, 'interruption_capture_budget_insufficient')
        deadline = self.clock() + available
        before = self.diagnostic_requests()
        require(all(row.get('outcome') is not None for row in before), 'owner_request_pending')
        baseline = {row['request_id'] for row in before}
        seen = set()
        evidence = {
            'announcement_started': False,
            'stimulus_played': False,
            'announcement_cancelled': False,
            'replacement_completed': False,
            'device_pause_from_trigger_ms': None,
            'transcript_matches_stimulus': None,
            'admitted_voice_requests': 0,
            'rejected_pause_requests': 0,
            'pending_voice_candidates': 0,
            'post_playback_quiet_seconds': 0,
        }
        self.barge_evidence = evidence

        def observe():
            health = active_health()
            rows = [r for r in self.diagnostic_requests() if r['request_id'] not in baseline]
            ids = {r['request_id'] for r in rows}
            require(seen <= ids, 'interruption_receipt_window_lost')
            seen.update(ids)
            announcements = [r for r in rows if r.get('origin') == 'announcement']
            voices = [r for r in rows if r.get('origin') == 'voice']
            groups = classify_voice_records(voices)
            admitted = groups['admitted']
            announcement = announcements[0] if len(announcements) == 1 else {}
            replacement = admitted[0] if len(admitted) == 1 else {}
            elapsed = replacement.get('device_pause_from_trigger_ms')
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
            self.checkpoint(self.report('running'))
            require(
                len(announcements) <= 1 and len(rows) == len(announcements) + len(voices),
                'interruption_unexpected_request',
            )
            require(not groups['invalid'] and len(admitted) <= 1, 'interruption_unexpected_voice')
            require(not evidence['rejected_pause_requests'], 'interruption_rejected_voice_paused')
            return health, announcement, replacement

        def started():
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
            return (
                ui.get('screen_sleeping') is False
                and d.get('announcement_active') is True
                and d.get('phase') == 'speaking'
                and d.get('playback_paused') is False
            )

        def completed():
            _, announcement, replacement = observe()
            require(
                announcement.get('outcome') in (None, 'cancelled'),
                'interruption_announcement_not_cancelled',
            )
            require(
                replacement.get('outcome') in (None, 'completed') and not replacement.get('error'),
                'interruption_replacement_failed',
            )
            if evidence['announcement_cancelled'] and evidence['replacement_completed']:
                return replacement
            return None

        with ThreadPoolExecutor(max_workers=1) as pool:
            reply = pool.submit(self.request, {'text': BARGE_ANNOUNCEMENT})
            self.wait(started, 25, 'interruption_playout_not_observed')
            evidence['announcement_started'] = True
            self.device.speak_probe(interruption=True)
            evidence['stimulus_played'] = True
            self.checkpoint(self.report('running'))
            remaining = min(75, deadline - self.clock() - POST_DIALOGUE_SECONDS - 5)
            require(remaining > 0, 'interruption_capture_budget_insufficient')
            replacement = self.wait(completed, remaining, 'interruption_replacement_not_confirmed')
            require(
                reply.result(timeout=5).get('status') == 'interrupted',
                'interruption_announcement_receipt_invalid',
            )
        require(
            evidence['device_pause_from_trigger_ms'] is not None,
            'interruption_pause_timing_missing',
        )
        quiet_started = self.clock()
        while self.clock() - quiet_started < POST_DIALOGUE_SECONDS:
            self.sleep(min(0.5, POST_DIALOGUE_SECONDS - (self.clock() - quiet_started)))
            require(completed(), 'interruption_replacement_not_confirmed')
            evidence['post_playback_quiet_seconds'] = round(self.clock() - quiet_started, 3)
        require(not evidence['pending_voice_candidates'], 'interruption_unresolved_voice')
        d, ui = self.device.status()
        self.capture_status((d, ui))
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
        require(
            ui.get('screen_sleeping') is False and d.get('playback_paused') is False,
            'interruption_device_not_ready_after_response',
        )

    def cleanup(self):
        if not self.touched:
            return
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
        self.device.home()
        self.wait(
            lambda: self.health().get('microphone_active') is False,
            15,
            'cleanup_capture_not_stopped',
        )
        self.device.open(False)
        self.wait(self.inactive, 25, 'cleanup_connection_not_restored')
        self.sleep(2)
        d, ui = self.device.status()
        require(
            d.get('foreground') is True
            and d.get('microphone_active') is False
            and ui.get('camera_open') is False
            and self.inactive(),
            'cleanup_not_confirmed',
        )
        self.cleanup_verified = True

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
                if self.awake_dialogue or self.synthetic_barge:
                    quiet_seconds = 2 if self.synthetic_barge else POST_DIALOGUE_SECONDS
                    self.check(
                        BARGE_PRIME_CHECK if self.synthetic_barge else AWAKE_DIALOGUE_CHECK,
                        lambda: self.acoustic_probe(awake=True, quiet_seconds=quiet_seconds),
                    )
                if self.synthetic_barge:
                    self.check(BARGE_CHECK, self.interruption_probe)
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
            self.checkpoint(self.report(state))
        return self.report(state)


def planned_checks(options):
    if 'latency_sample' in options:
        return [*CHECKS[:2], CHECKS[4], AWAKE_DIALOGUE_CHECK, *CHECKS[5:7]]
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
                [BARGE_PRIME_CHECK if 'synthetic_barge' in options else AWAKE_DIALOGUE_CHECK]
                if {'awake_dialogue', 'synthetic_barge'} & options
                else []
            )
            + ([BARGE_CHECK] if 'synthetic_barge' in options else [])
            + list(CHECKS[5:7])
            + ([ALL_ROUTE_LOSS_CHECK] if 'all_route_loss' in options else [])
            if 'capture' in options
            else []
        )
        + ([CHECKS[7]] if 'standby' in options else [])
        + ([CHECKS[8]] if 'acoustic' in options else [])
    )


def run_device_suite(make_run, save, *, qa_latency=False):
    planned = QA_LATENCY_PHASES if qa_latency else P0_DEVICE_PHASES
    phases = [
        dict(name=name, state='not_run', expected_checks=planned_checks(set(options)))
        for name, options in planned
    ]
    suite = dict(
        schema=1,
        scope='synthetic_qa_latency' if qa_latency else 'fixed_device_suite',
        state='running',
        phases=phases,
        full_p0_acceptance=False,
        physical_usb_unplug_tested=False,
        human_accuracy_asserted=False,
    )
    if qa_latency:
        suite['audible_onset_measured'] = False
        suite['qa_corpus_sha256'] = QA_CORPUS_SHA256
        suite['latencies_ms'] = {
            name: percentiles([])
            for name in (*QA_STAGE_METRICS, QA_ENDPOINT_METRIC, QA_PLAYOUT_METRIC)
        }
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
            make_run(set(options), checkpoint, qa_question=QA_LATENCY_QUESTIONS[index])
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
                elif evidence.get('recognized_stimulus_matches') is not True:
                    phase.update(state='failed', reason='suite_synthetic_stimulus_not_confirmed')
                elif qa_latency:
                    stages = evidence.get('qa_stage_latencies_ms')
                    if (
                        not isinstance(stages, dict)
                        or set(stages) - {QA_ENDPOINT_METRIC} != set(QA_STAGE_METRICS)
                        or any(
                            type(value) not in (int, float) or not 0 <= value <= 604800000
                            for value in (*stages.values(), timing.get(QA_PLAYOUT_METRIC))
                        )
                    ):
                        phase.update(state='failed', reason='suite_qa_stage_timing_incomplete')
                    else:
                        samples.append({**stages, QA_PLAYOUT_METRIC: timing[QA_PLAYOUT_METRIC]})
                        suite['latencies_ms'] = {
                            name: percentiles(
                                [sample[name] for sample in samples if name in sample]
                            )
                            for name in suite['latencies_ms']
                        }
        if phase['state'] != 'passed':
            suite['state'] = phase['state']
            save(suite)
            return suite
        save(suite)
    suite['state'] = 'passed'
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
        '--synthetic-barge',
        action='store_true',
        help='Use one fixed Mac speech stimulus to interrupt a verified ongoing CC8 announcement',
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
    args = parser.parse_args(argv)
    options = {key for key in SMOKE_OPTIONS if getattr(args, key)}
    batch = args.p0_device_suite or args.qa_latency_suite
    if batch and options != {'capture'}:
        parser.error('Device batches require --capture and accept no individual check flags')
    if args.standby and not args.capture or args.acoustic and not args.standby:
        parser.error('--standby requires --capture; --acoustic requires --standby')
    if args.awake_dialogue and (not args.capture or args.standby):
        parser.error('--awake-dialogue requires --capture and a separate run from --standby')
    if args.synthetic_barge and (not args.capture or args.standby):
        parser.error('--synthetic-barge requires --capture and a separate run from --standby')
    if args.wake_self_playback and (
        not args.capture or args.standby or args.awake_dialogue or args.synthetic_barge
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
    if not args.execute:
        if batch:
            plan = {
                'phases': [
                    dict(name=name, checks=planned_checks(set(flags)))
                    for name, flags in (
                        QA_LATENCY_PHASES if args.qa_latency_suite else P0_DEVICE_PHASES
                    )
                ],
                'execute': False,
                'capture': True,
                'full_p0_acceptance': False,
                'minimum_backend_version': PAIRED_PLAYOUT_MIN_BACKEND,
            }
            if args.qa_latency_suite:
                plan['qa_corpus_sha256'] = QA_CORPUS_SHA256
        else:
            plan = {
                'checks': planned_checks(options),
                'execute': False,
                **{
                    key: getattr(args, key)
                    for key in SMOKE_OPTIONS
                    if key not in ('standby', 'acoustic')
                },
                'full_p0_acceptance': False,
            }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    try:
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
        if batch:
            if tuple(map(int, manifest.backend_version.split('.'))) < tuple(
                map(int, PAIRED_PLAYOUT_MIN_BACKEND.split('.'))
            ):
                parser.exit(
                    2, f'固定设备验收批次需要后台 {PAIRED_PLAYOUT_MIN_BACKEND} 或更新版本。\n'
                )
            require(
                isinstance(instance, str)
                and bool(instance)
                and health.get('release_id') == manifest.release_id,
                'suite_runtime_identity_unconfirmed',
            )

        def request(payload):
            timeout = 5
            if payload.get('text') in WAKE_SELF_TEXT.values():
                timeout = WAKE_SELF_TIMEOUT
            elif 'text' in payload:
                timeout = 125
            elif payload == {'operation': 'playback_probe'}:
                timeout = 25
            result = rpc(socket_path, payload, timeout)
            if batch and payload == {'operation': 'health'}:
                if not (
                    result.get('runtime_instance') == instance
                    and result.get('release_id') == manifest.release_id
                    and result.get('bridge_version') == manifest.backend_version
                    and type(result.get('protocol_version')) is int
                    and result['protocol_version'] == manifest.protocol_version
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
            if batch:
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
                            **({'release_id': manifest.release_id} if batch else {}),
                        }
                    )

                device = Device()

                def make_run(enabled, checkpoint, *, qa_question=ACOUSTIC_QUESTION):
                    return SmokeRun(
                        device,
                        request,
                        checkpoint=checkpoint,
                        qa_question=qa_question,
                        expected_apk=(manifest.assistant.version, manifest.assistant.version_code),
                        **dict.fromkeys(enabled, True),
                    )

                print('自动设备检查已开始；结果仅保存状态，不留录音、图像或对话正文。', flush=True)
                keep_awake = None
                try:
                    if (args.standby or batch) and sys.platform == 'darwin':
                        keep_awake = subprocess.Popen(
                            ['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    if batch:
                        result = run_device_suite(make_run, save, qa_latency=args.qa_latency_suite)
                    else:
                        result = make_run(options, save).run()
                finally:
                    if keep_awake is not None and keep_awake.poll() is None:
                        keep_awake.terminate()
                        keep_awake.wait(timeout=5)
                if batch:
                    summary = {
                        'state': result['state'],
                        'phases': [
                            {key: phase[key] for key in ('name', 'state')}
                            for phase in result['phases']
                        ],
                    }
                    if args.qa_latency_suite:
                        summary['latencies_ms'] = result['latencies_ms']
                else:
                    summary = {
                        key: result[key]
                        for key in ('state', 'checks', 'cleanup_microphone_off_verified')
                    }
                print(json.dumps(summary, ensure_ascii=False))
                print(output.path)
                return 0 if result['state'] == 'passed' else 2
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        parser.exit(2, '自动设备检查未启动或未完成；未自动重复设备动作。\n')


if __name__ == '__main__':
    raise SystemExit(main())

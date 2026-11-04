"""Explicit, bounded CC8 hardware checks; default invocation only prints a plan."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import hashlib
import json
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
from mac_voice import resolve_voice, __file__ as voice_source

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = 'com.happyhome.cc8assistant/.MainActivity'
TEST_TEXT = (
    '这是大黄蜂自动测试。现在检查语音播放、字幕高亮和播放完成回执。'
    '这段固定文字只用于设备检查，测试结束后会恢复麦克风关闭状态。'
)
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


class CheckFailure(RuntimeError):
    pass


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


def objects(text, marker):
    # dumpsys also contains captions and OEM details. Return only the requested
    # JSON object in memory; the report builder never copies these objects.
    matches = re.findall(re.escape(marker) + r'(\{[^\n]*\})', text)
    require(len(matches) == 1, 'device_status_missing_or_ambiguous')
    value = json.loads(matches[0])
    require(isinstance(value, dict), 'invalid_device_status')
    return value


def rpc(path, request, timeout=5):
    # This test client deliberately has no camera, task or arbitrary tool route.
    allowed = (
        {'operation': 'health'},
        {'operation': 'diagnostics'},
        {'operation': 'transport_status'},
    )
    require(
        request in allowed
        or request == {'operation': 'transport_test', 'seconds': 12}
        or request == {'operation': 'playback_probe'}
        or request == {'text': TEST_TEXT},
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

    def speak_probe(self):
        voice = self.prepare_speech_probe()
        subprocess.run(
            ['/usr/bin/say', '-v', voice, '大黄蜂，一加一等于几？'],
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
        self.playback_evidence = playback_evidence
        self.clock_evidence = clock_evidence
        self.capture_clock_required = capture_clock_evidence
        self.leading_acoustic_evidence = leading_acoustic_evidence
        self.capture_clock_evidence = None
        self.playback_probe_evidence = None
        self.clock, self.sleep, self.checkpoint = clock, sleep, checkpoint
        self.started = clock()
        self.checks = []
        self.touched = False
        self.cleanup_verified = False
        self.initial_instance = None
        self.source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.speech_source_sha256 = hashlib.sha256(Path(voice_source).read_bytes()).hexdigest()
        self.expected_apk = expected_apk
        self.acoustic_evidence = None

    def health(self):
        value = self.request({'operation': 'health'})
        require(value.get('ok') is True, 'health_unavailable')
        instance = value.get('runtime_instance')
        require(isinstance(instance, str) and bool(instance), 'runtime_instance_missing')
        if self.initial_instance is not None:
            require(instance == self.initial_instance, 'runtime_restarted_during_test')
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
            'capture_tests_enabled': self.capture,
            'standby_enabled': self.standby,
            'synthetic_acoustic_probe_enabled': self.acoustic,
            'raw_audio_saved': False,
            'contains_conversation_content': False,
            'physical_usb_unplug_tested': False,
            'human_accuracy_asserted': False,
            'acoustic_latency_asserted': False,
            'full_p0_acceptance': False,
            'acoustic_evidence': self.acoustic_evidence,
            'playback_probe_evidence': self.playback_probe_evidence,
            'capture_clock_evidence': self.capture_clock_evidence,
        }

    def check(self, name, action):
        began = self.clock()
        row = {'name': name, 'state': 'running'}
        self.checks.append(row)
        self.checkpoint(self.report('running'))
        try:
            action()
            row['state'] = 'passed'
        except CheckFailure as error:
            row.update(state='failed', reason=str(error))
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
        if self.acoustic:
            self.device.prepare_speech_probe()
        h = self.health()
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
        t = self.request({'operation': 'transport_status'})
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
        before = self.request({'operation': 'diagnostics'})
        ids = {r.get('request_id') for r in before.get('requests', [])}
        observed = 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.request, {'text': TEST_TEXT})
            while not result.done():
                if self.health().get('playing') is True:
                    d, ui = self.device.status()
                    require(d.get('microphone_active') is False, 'playback_started_capture')
                    require(ui.get('caption_progress_overlap') is False, 'caption_progress_overlap')
                    require(ui.get('halo_status_overlap') is False, 'halo_status_overlap')
                    require(ui.get('caption_line_visible') is True, 'caption_line_clipped')
                    require(ui.get('camera_open') is False, 'unexpected_camera_open')
                    observed += 1
                self.sleep(0.25)
            require(result.result().get('status') == 'completed', 'announcement_not_completed')
        after = self.request({'operation': 'diagnostics'})
        rows = [
            r
            for r in after.get('requests', [])
            if r.get('request_id') not in ids and r.get('origin') == 'announcement'
        ]
        require(len(rows) == 1, 'announcement_receipt_missing_or_ambiguous')
        row = rows[0]
        require(row.get('outcome') == 'completed', 'announcement_receipt_not_completed')
        require(
            {'playout_ack', 'playback_done'} <= set(row.get('stages', {})),
            'playout_receipt_missing',
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

    def start_capture(self):
        limit = 1200000 if self.standby else 180000
        self.device.open(True, capture_limit_ms=limit)
        self.wait(lambda: self.health().get('microphone_active') is True, 15, 'capture_not_started')
        self.wait(lambda: self.health().get('calibrating') is False, 25, 'calibration_not_finished')
        h = self.health()
        require(
            h.get('microphone_active') is True and h.get('local_wake_ready') is True,
            'wake_capture_not_ready',
        )
        d, ui = self.device.status()
        require(
            type(d.get('test_capture_limit_ms')) is int
            and d['test_capture_limit_ms'] == limit
            and type(d.get('test_capture_remaining_ms')) is int
            and 0 < d['test_capture_remaining_ms'] <= limit,
            'device_test_capture_limit_not_active',
        )
        require(
            d.get('microphone_active') is True and ui.get('camera_open') is False,
            'device_capture_state_invalid',
        )

        if self.capture_clock_required:
            self.capture_clock_evidence = summarize_capture_clock(d.get('capture_clock_evidence'))
            require(
                self.capture_clock_evidence['status'] == 'observed',
                'capture_clock_provenance_unconfirmed',
            )

    def playback_probe(self):
        h = self.health()
        require(
            h.get('microphone_active') is True
            and h.get('playing') is False
            and h.get('calibrating') is False,
            'playback_probe_requires_idle_capture',
        )
        result = self.request({'operation': 'playback_probe'})
        evidence = result.get('evidence')
        require(isinstance(evidence, dict), 'playback_probe_evidence_missing')
        # Keep the numeric result even on a failed observation. An absent match
        # is not proof that the physical speaker was silent.
        self.playback_probe_evidence = evidence
        require(result.get('status') == 'completed', 'playback_probe_not_completed')
        require(evidence.get('status') == 'observed', 'playback_not_acoustically_corroborated')
        require(
            not self.clock_evidence
            or evidence.get('render_clock_evidence', {}).get('status') == 'observed',
            'render_clock_provenance_unconfirmed',
        )
        require(
            not self.leading_acoustic_evidence
            or (
                type(evidence.get('render_clock_resets')) is int
                and evidence['render_clock_resets'] == 0
                and valid_leading_evidence(
                    evidence.get('leading_acoustic_evidence'),
                    evidence.get('matched_window_delay_ms'),
                )
            ),
            'leading_acoustic_evidence_unconfirmed',
        )

    def home(self):
        self.device.home()
        self.wait(
            lambda: self.health().get('microphone_active') is False, 15, 'home_did_not_stop_capture'
        )
        before = self.health().get('mic_frames')
        self.sleep(2)
        require(self.health().get('microphone_active') is False, 'home_restarted_capture')
        require(self.health().get('mic_frames') == before, 'home_microphone_frames_increasing')

    def idle(self):
        began = self.clock()
        self.start_capture()
        deadline = self.clock() + 970
        while True:
            h = self.health()
            require(
                h.get('microphone_active') is True and h.get('connected') is True,
                'idle_capture_or_connection_lost',
            )
            d, ui = self.device.status()
            require(ui.get('camera_open') is False, 'unexpected_camera_open')
            if ui.get('screen_sleeping') is True:
                require(d.get('foreground') is True, 'idle_left_foreground')
                require(self.clock() - began >= 895, 'idle_black_screen_too_early')
                break
            require(self.clock() < deadline, 'idle_black_screen_timeout')
            self.checkpoint(self.report('running'))
            self.sleep(5)

    def acoustic_probe(self):
        # A computer speaker is a synthetic source with unknown room geometry.
        # This can verify the live chain, never a human recall rate or dB level.
        d, ui = self.device.status()
        require(
            ui.get('screen_sleeping') is True and d.get('microphone_active') is True,
            'acoustic_probe_requires_idle_capture',
        )
        ids = {
            r.get('request_id')
            for r in self.request({'operation': 'diagnostics'}).get('requests', [])
        }
        self.device.speak_probe()

        def received():
            health = self.health()
            require(
                health.get('connected') is True and health.get('microphone_active') is True,
                'acoustic_capture_or_connection_lost',
            )
            p = self.request({'operation': 'diagnostics'})
            require(
                isinstance(p.get('requests'), list)
                and all(isinstance(r, dict) for r in p['requests']),
                'diagnostics_unavailable',
            )
            rows = [
                r
                for r in p['requests']
                if r.get('request_id') not in ids and r.get('origin') == 'voice'
            ]
            groups = classify_voice_records(rows)
            admitted, rejected = groups['admitted'], groups['rejected']
            # Keep only fixed stage counts, never request IDs, transcripts,
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
            require(not groups['invalid'], 'synthetic_voice_candidate_not_rejected')
            require(
                all(
                    r.get('outcome') in (None, 'completed') and r.get('error') is None
                    for r in admitted
                ),
                'synthetic_voice_request_not_completed',
            )
            return any(
                r.get('outcome') == 'completed'
                and {'wake_detected', 'asr_end', 'brain_start', 'playback_done'}
                <= set(r.get('stages', {}))
                for r in admitted
            )

        self.wait(received, 120, 'synthetic_acoustic_chain_not_confirmed')
        # An immediate cleanup could hide a post-answer echo turn. Keep one
        # bounded observation window. An extra admitted dialogue fails, while
        # a transient candidate must resolve and a rejected pause must resume.
        # Counts remain visible; success never implies pause-free playback.
        quiet_started = self.clock()
        while self.clock() - quiet_started < 20:
            self.sleep(min(0.5, 20 - (self.clock() - quiet_started)))
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
            'acoustic_probe_did_not_wake_foreground',
        )

    def cleanup(self):
        if not self.touched:
            return
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
            self.check(CHECKS[2], self.playback)
            self.check(CHECKS[3], self.handover)
            if self.capture:
                self.check(CHECKS[4], self.start_capture)
                if self.playback_evidence:
                    self.check(CHECKS[9], self.playback_probe)
                self.check(CHECKS[5], self.home)
                self.check(CHECKS[6], self.maintenance_open)
            if self.standby:
                self.check(CHECKS[7], self.idle)
            if self.acoustic:
                self.check(CHECKS[8], self.acoustic_probe)
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
    if args.standby and not args.capture or args.acoustic and not args.standby:
        parser.error('--standby requires --capture; --acoustic requires --standby')
    if args.playback_evidence and not args.capture:
        parser.error('--playback-evidence requires --capture')
    if args.clock_evidence and not args.playback_evidence:
        parser.error('--clock-evidence requires --playback-evidence')
    if args.capture_clock_evidence and not args.capture:
        parser.error('--capture-clock-evidence requires --capture')
    if args.leading_acoustic_evidence and not args.playback_evidence:
        parser.error('--leading-acoustic-evidence requires --playback-evidence')
    plan = {
        'checks': list(CHECKS[:4])
        + (
            [CHECKS[4]] + ([CHECKS[9]] if args.playback_evidence else []) + list(CHECKS[5:7])
            if args.capture
            else []
        )
        + ([CHECKS[7]] if args.standby else [])
        + ([CHECKS[8]] if args.acoustic else []),
        'execute': args.execute,
        'capture': args.capture,
        'playback_evidence': args.playback_evidence,
        'clock_evidence': args.clock_evidence,
        'capture_clock_evidence': args.capture_clock_evidence,
        'leading_acoustic_evidence': args.leading_acoustic_evidence,
        'full_p0_acceptance': False,
    }
    if not args.execute:
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
            name = (
                'device-'
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
                        }
                    )

                run = SmokeRun(
                    Device(),
                    lambda p: rpc(
                        socket_path,
                        p,
                        125 if 'text' in p else 25 if p == {'operation': 'playback_probe'} else 5,
                    ),
                    capture=args.capture,
                    standby=args.standby,
                    acoustic=args.acoustic,
                    playback_evidence=args.playback_evidence,
                    clock_evidence=args.clock_evidence,
                    capture_clock_evidence=args.capture_clock_evidence,
                    leading_acoustic_evidence=args.leading_acoustic_evidence,
                    checkpoint=save,
                    expected_apk=(manifest.assistant.version, manifest.assistant.version_code),
                )
                print('自动设备检查已开始；结果仅保存状态，不留录音、图像或对话正文。', flush=True)
                keep_awake = None
                try:
                    if args.standby and sys.platform == 'darwin':
                        keep_awake = subprocess.Popen(
                            ['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    result = run.run()
                finally:
                    if keep_awake is not None and keep_awake.poll() is None:
                        keep_awake.terminate()
                        keep_awake.wait(timeout=5)
                print(
                    json.dumps(
                        {
                            'state': result['state'],
                            'checks': result['checks'],
                            'cleanup_microphone_off_verified': result[
                                'cleanup_microphone_off_verified'
                            ],
                        },
                        ensure_ascii=False,
                    )
                )
                print(output.path)
                return 0 if result['state'] == 'passed' else 2
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        parser.exit(2, '自动设备检查未启动或未完成；未自动重复设备动作。\n')


if __name__ == '__main__':
    raise SystemExit(main())

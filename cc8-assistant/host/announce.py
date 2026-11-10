"""Explicit local text-to-CC8 announcements; never starts microphone capture."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import argparse
import base64
import hashlib
import re
import select
import time
import json
import os
import socket
import stat
import sys
import threading
from typing import Callable
from vision import jpeg_dimensions
from qa_speech import tool_sample_request
from known_speech_receipt import valid_request as known_speech_request
from wake_input_receipt import valid_request as wake_input_request
from request_text import validate_request_text

SOCKET = Path(__file__).resolve().parents[2] / 'device-access/private/voice-runtime/announce.sock'


@dataclass
class Announcement:
    generation: int
    text: str
    done: threading.Event = field(default_factory=threading.Event)
    result: str = 'interrupted'
    segments: tuple[str, ...] = ()
    on_segment: Callable[[int], None] | None = None

    def discard_segments(self):
        # The response worker can keep its last job while awaiting the next
        # queue item. Do not let a terminal receipt retain result excerpts.
        if self.segments:
            self.text, self.segments, self.on_segment = '', (), None


def validate_text(text):
    return validate_request_text(text, 500)


class AnnouncementServer:
    # owner-only Unix IPC 复用既有认证通道做桌面控制，不提供开麦操作。
    def __init__(self, bridge, path=SOCKET):
        self.bridge = bridge
        self.path = path
        self.stop = threading.Event()
        self.socket = None
        self.bound_identity = None
        self.slots = threading.BoundedSemaphore(4)

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError('Unsafe announcement socket path')
            self.path.unlink()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.path))
        info = self.path.lstat()
        self.bound_identity = (info.st_dev, info.st_ino)
        os.chmod(self.path, 0o600)
        self.socket.listen(2)
        self.socket.settimeout(0.5)
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        stream = self.socket
        while not self.stop.is_set():
            try:
                connection, _ = stream.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.slots.acquire(blocking=False):
                connection.close()
                continue
            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()

    def handle(self, connection):
        try:
            with connection:
                try:
                    connection.settimeout(5)
                    data = b''
                    while b'\n' not in data and len(data) <= 4096:
                        chunk = connection.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                    if len(data) > 4096:
                        raise ValueError('Oversized request')
                    request = json.loads(data)
                    if request.get('operation') == 'health':
                        result = self.bridge.health()
                    elif request.get('operation') == 'diagnostics':
                        result = self.bridge.diagnostics_report()
                    elif request.get('operation') == 'connection_diagnostics':
                        if request != {'operation': 'connection_diagnostics'}:
                            raise ValueError('Connection diagnostics accepts no arguments')
                        result = self.bridge.connection_diagnostics()
                    elif request.get('operation') == 'transport_status':
                        result = {'ok': True, **self.bridge.transport_hub.diagnostics()}
                    elif request.get('operation') == 'transport_test':
                        result = self.bridge.transport_hub.suppress_usb_for_test(
                            request.get('seconds', 15)
                        )
                    elif request.get('operation') == 'transport_all_test':
                        if request != {'operation': 'transport_all_test'}:
                            raise ValueError('Transport outage accepts no arguments')
                        result = self.bridge.transport_hub.suppress_all_for_test()
                    elif request.get('operation') == 'camera_snapshot':
                        result = self.snapshot(connection, request)
                    elif request.get('operation') == 'camera_route_interrupt':
                        result = self.camera_route_interrupt(request)
                    elif request.get('operation') == 'playback_probe':
                        result = self.playback_probe(connection, request)
                    elif request.get('operation') in (
                        'tool_sample_arm',
                        'tool_sample_compare_arm',
                        'tool_sample_diagnose_arm',
                        'tool_sample_begin',
                        'tool_sample_release',
                    ):
                        if not tool_sample_request(request):
                            raise ValueError('Tool sample accepts only a fixed case or its token')
                        operation = request['operation']
                        if operation == 'tool_sample_diagnose_arm':
                            result = self.bridge.session.arm_tool_sample(
                                request['case'], diagnose_engine=request['engine']
                            )
                        elif operation in ('tool_sample_arm', 'tool_sample_compare_arm'):
                            engine = (
                                request.get('engine', 'whisper')
                                if operation == 'tool_sample_compare_arm'
                                else None
                            )
                            result = self.bridge.session.arm_tool_sample(
                                request['case'], compare_engine=engine
                            )
                        elif operation == 'tool_sample_begin':
                            result = self.bridge.session.begin_tool_sample(request['token'])
                        else:
                            result = self.bridge.session.release_tool_sample(request['token'])
                    elif request.get('operation') == 'response_probe_arm':
                        if set(request) != {'operation', 'case'}:
                            raise ValueError('Response probe accepts only a public case')
                        result = self.bridge.arm_response_probe(request['case'])
                    elif request.get('operation') == 'response_probe_result':
                        if set(request) != {'operation', 'token'}:
                            raise ValueError('Response probe requires its token')
                        result = self.bridge.read_response_probe(request['token'])
                    elif request.get('operation') == 'known_speech_arm':
                        if not known_speech_request(request):
                            raise ValueError('Known speech probe accepts only a public fixture')
                        result = self.bridge.arm_known_speech(
                            self.path.parent / 'public-speech-fixtures',
                            request['directory'],
                            request['metadata'],
                            request.get('generation'),
                            near_only=request.get('near_only', False),
                        )
                    elif request.get('operation') == 'known_speech_stimulus':
                        if not known_speech_request(request):
                            raise ValueError('Known speech boundary requires its token')
                        if request.get('verify_far'):
                            result = self.bridge.mark_known_speech_stimulus(
                                request['token'], verify_far=True
                            )
                        else:
                            result = self.bridge.mark_known_speech_stimulus(request['token'])
                    elif request.get('operation') == 'known_speech_result':
                        if not known_speech_request(request):
                            raise ValueError('Known speech probe requires its token')
                        result = self.bridge.read_known_speech(request['token'])
                    elif request.get('operation') in ('wake_input_arm', 'wake_input_result'):
                        if not wake_input_request(request):
                            raise ValueError('Invalid wake input probe request')
                        if request['operation'] == 'wake_input_arm':
                            result = self.bridge.probes.arm_wake_input_probe(
                                self.path.parent / 'public-speech-fixtures',
                                request['directory'],
                                request['metadata'],
                            )
                        else:
                            result = self.bridge.wake.read_input_probe(request['token'])
                    elif request.get('operation') == 'device_control':
                        from voice_tools import VoiceTools

                        result = VoiceTools(self.bridge.control_device).call(
                            'cc8_control', request.get('arguments')
                        )
                    elif request.get('operation') == 'text_request':
                        result = self.bridge.submit_text(request.get('text'))
                    elif request.get('operation') == 'workspace_status':
                        result = {'ok': True, **self.bridge.workspace.snapshot()}
                    elif request.get('operation') == 'reminder_action':
                        result = self.bridge.reminder_action(request.get('arguments'))
                    else:
                        text = validate_text(request.get('text'))
                        job = self.bridge.announce(text)
                        finished = job.done.wait(120)
                        if not finished and self.bridge.generation == job.generation:
                            self.bridge.cancel('announcement_timeout')
                        result = {
                            'status': job.result if finished else 'timeout',
                            'characters': len(text),
                        }
                except (ValueError, TypeError, AttributeError):
                    result = {'status': 'invalid_request'}
                except RuntimeError:
                    result = {
                        'status': 'unavailable',
                        'reason': 'Keep the assistant foreground and an authenticated USB or Wi-Fi connection ready; wait until calibration ends.',
                    }
                except OSError:
                    return
                try:
                    connection.sendall((json.dumps(result) + '\n').encode())
                except OSError:
                    pass
        finally:
            self.slots.release()

    def snapshot(self, connection, request):
        # 调用端断开即取消未完成拍照；成功仍须匹配本次 ID，不能返回上次图片。
        ident = request.get('request_id')
        if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident):
            raise ValueError('Invalid request ID')
        fields = {'operation', 'request_id'}
        metadata_only = 'metadata_only' in request
        if metadata_only:
            fields |= {'metadata_only', 'expected_instance', 'expected_generation'}
        if (
            set(request) != fields
            or request.get('operation') != 'camera_snapshot'
            or metadata_only
            and request['metadata_only'] is not True
            or metadata_only
            and (
                not isinstance(request.get('expected_instance'), str)
                or not re.fullmatch('[a-f0-9]{32}', request['expected_instance'])
                or type(request.get('expected_generation')) is not int
                or request['expected_generation'] < 0
            )
        ):
            raise ValueError('Invalid camera request fields')
        scope = {'metadata_only': True} if metadata_only else {}
        if metadata_only:
            # Fence the actual side effect on the server, not only an earlier
            # health read: a restarted backend/new owner must never open a lens.
            with self.bridge.guard:
                health = self.bridge.health()
                if (
                    health.get('runtime_instance') != request['expected_instance']
                    or self.bridge.generation != request['expected_generation']
                    or any(
                        health.get(key) is not False
                        for key in (
                            'microphone_active',
                            'playing',
                            'calibrating',
                            'text_request_active',
                        )
                    )
                ):
                    raise RuntimeError('Camera probe context changed')
                job = self.bridge.request_snapshot(metadata_only=True)
        else:
            job = self.bridge.request_snapshot()
        try:
            deadline = time.monotonic() + 25
            while not job.done.wait(0.05):
                # Closing the calling chat's client must cancel an unfinished photo.
                if select.select([connection], [], [], 0)[0]:
                    self.bridge.cancel_snapshot(job)
                    return {'status': 'interrupted', 'request_id': ident, **scope}
                if time.monotonic() > deadline:
                    self.bridge.cancel_snapshot(job)
                    return {'status': 'timeout', 'request_id': ident, **scope}
            with self.bridge.guard:
                if (
                    job.result != 'completed'
                    or job.image is None
                    or self.bridge.generation != job.generation
                ):
                    return {
                        'status': job.result if job.result != 'completed' else 'interrupted',
                        'request_id': ident,
                        'reason': job.error or 'capture_interrupted',
                        **scope,
                    }
                image = job.image
                width, height = jpeg_dimensions(image, policy=job.policy)
                result = {
                    'status': 'completed',
                    'request_id': ident,
                    'media_type': 'image/jpeg',
                    'capture_version': job.policy.version,
                    'width': width,
                    'height': height,
                    'bytes': len(image),
                    'sha256': hashlib.sha256(image).hexdigest(),
                    **scope,
                }
                if not metadata_only:
                    result['image_base64'] = base64.b64encode(image).decode('ascii')
                return result
        finally:
            if not job.done.is_set():
                self.bridge.cancel_snapshot(job)
            job.image = None

    def camera_route_interrupt(self, request):
        if (
            set(request) != {'operation', 'expected_instance', 'expected_generation', 'route'}
            or request.get('operation') != 'camera_route_interrupt'
            or request.get('route') not in ('wifi_handover', 'all_routes_lost')
            or type(request.get('expected_generation')) is not int
            or request['expected_generation'] < 0
            or not isinstance(request.get('expected_instance'), str)
            or not re.fullmatch('[a-f0-9]{32}', request['expected_instance'])
        ):
            raise ValueError('Invalid camera route interruption')
        # Recheck at the actual route side effect. A user can start a new
        # conversation or cancel the preview after the tool's health read.
        with self.bridge.guard:
            health = self.bridge.health()
            job = self.bridge.visual.snapshot_job
            if (
                health.get('runtime_instance') != request['expected_instance']
                or self.bridge.generation != request['expected_generation']
                or job is None
                or not job.metadata_only
                or job.generation != request['expected_generation']
                or job.done.is_set()
                or any(
                    health.get(key) is not False
                    for key in (
                        'microphone_active',
                        'playing',
                        'calibrating',
                        'text_request_active',
                    )
                )
            ):
                raise RuntimeError('Camera route context changed')
            hub = self.bridge.transport_hub
            return (
                hub.suppress_usb_for_test(12)
                if request['route'] == 'wifi_handover'
                else hub.suppress_all_for_test()
            )

    def playback_probe(self, connection, request):
        if request != {'operation': 'playback_probe'}:
            raise ValueError('Playback probe accepts only the fixed public phrase')
        job, probe = self.bridge.playback_probe()
        try:
            deadline = time.monotonic() + 22
            while not (job.done.is_set() and probe.done.is_set()):
                if select.select([connection], [], [], 0.05)[0]:
                    return {'status': 'interrupted'}
                if time.monotonic() >= deadline:
                    return {'status': 'timeout'}
                # If playback is interrupted or fails, don't retain microphone
                # samples waiting for a reference that can no longer arrive.
                if job.done.is_set() and job.result != 'completed':
                    self.bridge.cancel_playback_probe(job, probe)
            return {
                'status': job.result,
                'evidence': {
                    **probe.result,
                    'render_clock_evidence': probe.render_clock.snapshot(final=True),
                },
            }
        finally:
            if not job.done.is_set() or not probe.done.is_set():
                self.bridge.cancel_playback_probe(job, probe)
            self.bridge.finish_playback_probe(probe)

    def close(self):
        self.stop.set()
        try:
            if self.socket:
                self.socket.close()
        finally:
            self.socket = None
            identity, self.bound_identity = self.bound_identity, None
            if identity is not None:
                try:
                    info = self.path.lstat()
                    if (
                        stat.S_ISSOCK(info.st_mode)
                        and info.st_uid == os.getuid()
                        and (info.st_dev, info.st_ino) == identity
                    ):
                        self.path.unlink()
                except FileNotFoundError:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--text')
    parser.add_argument('--file', type=Path)
    args = parser.parse_args()
    if args.text is not None and args.file:
        parser.error('Use --text, --file, or stdin')
    text = validate_text(
        args.text
        if args.text is not None
        else args.file.read_text()
        if args.file
        else sys.stdin.read(2000)
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(125)
            connection.connect(str(SOCKET))
            connection.sendall((json.dumps({'text': text}, ensure_ascii=False) + '\n').encode())
            data = b''
            while b'\n' not in data and len(data) < 4096:
                chunk = connection.recv(1024)
                if not chunk:
                    break
                data += chunk
        result = json.loads(data)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('status') == 'completed' else 1
    except (OSError, ValueError):
        print(
            'Announcement service unavailable; start the CC8 voice service first.', file=sys.stderr
        )
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

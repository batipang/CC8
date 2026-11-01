"""CC8 voice terminal: local AEC, MiniMax ASR/TTS and owner-configured Dot.

Microphone samples stay in RAM. Only explicitly enabled smoke/replay tools
read audio files. The companion APK never starts capture from a host command.
"""

from __future__ import annotations
import argparse
from collections import deque
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import numpy as np

from protocol import Packet, EVENT, SPEAKER, decode_event, validate_device_event
from transport import Pairing, TransportHub
from dsp import (
    RATE,
    WebRtcEchoCanceller,
    VoiceDetector,
    NearSpeechDetector,
    resembles_playback,
)
from fast_stop import FastStopDetector
from brain_router import BrainRouter
from announce import Announcement, AnnouncementServer, validate_text
from vision import visual_request, desktop_signature
from vision_controller import VisionController, VisionHooks
from playback_controller import PlaybackController, PlaybackHooks
from capture_pipeline import CapturePipeline, CaptureHooks
from echo_pipeline import EchoPipeline, EchoHooks
from endpoint_pipeline import EndpointPipeline, EndpointHooks
from minimax_client import MiniMaxBrain, MiniMaxError
from workspace import Workspace
from diagnostics import TraceBook
from runtime_guard import RuntimeLease, RuntimeConflict
from configuration import load_config
from config_schema import ConfigError
from release_info import RELEASE
from speech_providers import (
    LocalSpeech as LocalSpeech,
    DirectSpeech as DirectSpeech,
    model_path as model_path,
    wav_samples as wav_samples,
)
from reply_types import (
    Reply as Reply,
    VisualReply as VisualReply,
    TextReply as TextReply,
    mood as mood,
)
from usb_transport import (
    USBPipe as USBPipe,
    usb_target as usb_target,
    usb_present as usb_present,
    connect_device as connect_device,
)
from dot_client import runtime_status as dot_runtime_status

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / 'device-access/private/voice-runtime'
LIBRARY = ROOT / '.toolchains/cc8/speexdsp-1.2.1/libspeexdsp/.libs/libspeexdsp.dylib'
WAKE_WORD = '大黄蜂'
STARTUP_GREETING = '汽车人，变形！'
WAKE_PATTERN = re.compile(
    r'大黄蜂|大黃蜂|大黄峰|邦布比|班布比|(?<![A-Za-z])bumble[\s-]*bee(?![A-Za-z])', re.I
)


class Bridge:
    # Mac 端协调收音、消回声/断句、ASR、大脑及 TTS。
    # epoch 标识设备录音会话；generation 标识可被打断的回答，两者不可互换。
    def __init__(
        self, speech, brain, library=LIBRARY, fast_stop=None, echo_check=False, workspace=None
    ):
        self.diagnostics = TraceBook()
        self.started_at = time.monotonic()
        self.speech = speech
        self.brain = brain
        self.library = library
        self.workspace = workspace or Workspace()
        self.reminder_job = None
        self.reminder_poll = 0
        self.text_generation = None
        self.connection = None
        self.transport_hub = None
        self.transport_kind = 'none'
        self.rebind_seen = False
        self.write_lock = threading.Lock()
        self.guard = threading.RLock()
        self.active = False
        self.epoch = 0
        self.generation = 100
        self.calibrating = False
        self.announcement_generation = None
        self.wake = False
        self.awake = True
        self.history = deque(maxlen=8)
        self.queue = queue.Queue(maxsize=2)
        self.metrics = {
            'mic_frames': 0,
            'render_frames': 0,
            'sequence_gaps': 0,
            'interruptions': 0,
            'utterances': 0,
            'raw_audio_saved': False,
        }
        self.response_queue = queue.Queue(maxsize=2)
        self.status_lock = threading.Lock()
        self.last_interaction = time.monotonic()
        self.screen_sleeping = False
        self.response_busy = False
        self.device_requests = {}
        self.device_request_serial = 0
        if isinstance(self.brain, BrainRouter):
            self.brain.dot.device_control = self.control_device
            self.brain.dot.reminders = self.reminder_action
        self.echo_check_pending = echo_check
        self.echo_check_running = False
        self.echo_check_failed = False
        self.recognition_busy = False
        self.visual = VisionController(
            VisionHooks(
                lock=self.guard,
                connected=lambda: self.connection is not None,
                calibrating=lambda: self.calibrating,
                has_transport=lambda: self.transport_hub is not None,
                brain_ready=lambda: isinstance(self.brain, BrainRouter),
                session_generation=lambda: self.generation,
                valid=lambda generation: self.valid(generation),
                cancel_session=lambda reason: self.cancel(reason),
                touch=lambda: self.touch_activity(),
                event=lambda name, **fields: self.event(name, **fields),
                status=lambda phase, message: self.status(phase, message),
                save_status=lambda: self.save_status(),
                authorize=lambda challenge, nonce, generation: desktop_signature(
                    self.transport_hub.pairing.token, challenge, nonce, generation
                ),
                enqueue=lambda job: self.enqueue_response(job),
                answer=lambda text, generation, image: self.answer(text, generation, image=image),
                answer_completed=lambda: self.brain.last_metrics.get('completed'),
                metric=self.record_metric,
                log=lambda name, **fields: self.log(name, **fields),
            )
        )
        self.playback = PlaybackController(
            PlaybackHooks(
                lock=self.guard,
                generation=lambda: self.generation,
                active=lambda: self.active,
                announcement_generation=lambda: self.announcement_generation,
                valid=lambda generation: self.valid(generation),
                cancel_session=lambda reason: self.cancel(reason),
                event=lambda name, **fields: self.event(name, **fields),
                send=lambda packet: self.send(packet),
                status=lambda phase, message: self.status(phase, message),
                log=lambda name, **fields: self.log(name, **fields),
                metric=self.record_metric,
                diagnostics=lambda: self.diagnostics,
                trace=lambda: self.capture.endpoint.voice_trace,
                echo_metrics=lambda: dict(
                    input_dbfs=self.metrics.get('input_dbfs'),
                    residual_dbfs=self.metrics.get('output_dbfs'),
                    raw_coherence=round(self.capture.endpoint.near_speech.correlation, 3),
                    residual_coherence=round(
                        self.capture.endpoint.near_speech.residual_correlation, 3
                    ),
                ),
                utterance_paused=lambda token: (
                    self.capture.endpoint.vad.talking
                    and self.capture.endpoint.turn_pause_token == token
                ),
                speech=lambda: self.speech,
                cancel_timeline_pause=lambda: self.capture.echo.cancel_pause(),
                pause_timeline=lambda nanos: self.capture.echo.pause_at(nanos),
                resume_timeline=lambda nanos: self.capture.echo.resume_at(nanos),
            )
        )
        self.capture = CapturePipeline(
            CaptureHooks(
                generation=lambda: self.generation,
                epoch=lambda: self.epoch,
                active=lambda: self.active,
                calibrating=lambda: self.calibrating,
                diagnostics=lambda: self.diagnostics,
                metric=self.record_metric,
                add_metric=self.add_capture_metric,
                log=lambda name, **fields: self.log(name, **fields),
                save_status=lambda: self.save_status(),
            ),
            EchoPipeline(
                EchoHooks(
                    lock=self.guard,
                    active=lambda: self.active,
                    valid=lambda generation: self.valid(generation),
                    factory=lambda **kwargs: WebRtcEchoCanceller(**kwargs),
                    fast_stop_enabled=lambda: bool(self.capture.endpoint.fast_stop),
                    metric=self.record_metric,
                    log=lambda name, **fields: self.log(name, **fields),
                )
            ),
            EndpointPipeline(
                EndpointHooks(
                    generation=lambda: self.generation,
                    awake=lambda: self.awake,
                    playing=lambda: self.playback.playing,
                    last_audio_end=lambda: self.playback.last_audio_end,
                    last_spoken=lambda: self.playback.last_spoken,
                    previous_spoken=lambda: self.playback.previous_spoken,
                    previous_spoken_until=lambda: self.playback.previous_spoken_until,
                    recent_spoken=lambda: [text for _, text in tuple(self.playback.spoken)],
                    pause_token=lambda: (
                        self.playback.pause_state['token'] if self.playback.pause_state else None
                    ),
                    paused=lambda: self.playback.pause_state is not None,
                    pause_voice=lambda: self.pause_for_voice(),
                    cancel_session=lambda reason: self.cancel(reason),
                    event=lambda name, **fields: self.event(name, **fields),
                    status=lambda phase, message: self.status(phase, message),
                    log=lambda name, **fields: self.log(name, **fields),
                    save_status=lambda: self.save_status(),
                    metric=self.record_metric,
                    add_metric=self.add_capture_metric,
                    metric_value=lambda name: self.metrics[name],
                    diagnostics=lambda: self.diagnostics,
                    enqueue=lambda job: self.enqueue_recognition(job),
                    discard_recognition=lambda: self.discard_recognition(),
                ),
                fast_stop,
            ),
        )
        self.worker = threading.Thread(target=self.work, daemon=True)
        self.worker.start()
        self.responder = threading.Thread(target=self.respond_work, daemon=True)
        self.responder.start()

    def log(self, event, **fields):
        print(
            json.dumps(
                {'event': event, 'monotonic_seconds': round(time.monotonic(), 3), **fields},
                ensure_ascii=False,
            ),
            flush=True,
        )

    def send(self, p):
        with self.write_lock:
            conn = self.connection
            if conn is None:
                raise EOFError('No device connection')
            if p.kind == SPEAKER and p.generation != self.generation:
                return
            conn.sendall(p.encode())

    def event(self, event, generation=None, **fields):
        if generation is not None and generation < self.generation:
            return
        self.send(
            Packet.event(event, self.generation if generation is None else generation, **fields)
        )

    def change_transport(self, channel):
        # 先作废旧回答并重置参考时钟；只可 rebind 已有录音，重连不能自行开麦。
        old = self.connection
        if old is channel:
            return
        was_active = self.active
        try:
            self.cancel('transport_switch')
        except (OSError, EOFError):
            pass
        self.connection = channel
        self.visual.clear_challenge()
        self.rebind_seen = False
        self.transport_kind = channel.kind if channel else 'none'
        self.capture.reset_transport(self.transport_kind)
        self.playback.clear_transport_pause()
        if channel is None:
            self.stop()
        elif was_active:
            # AudioRecord was already enabled locally. The new channel must
            # explicitly confirm the same capture epoch; it cannot start a mic.
            self.metrics['capture_preserved_switches'] = (
                self.metrics.get('capture_preserved_switches', 0) + 1
            )
        self.metrics['transport_changes'] = self.metrics.get('transport_changes', 0) + 1

    def status(self, phase, message):
        if self.connection is not None:
            self.event('status', phase=phase, message=message)

    def valid(self, generation):
        return (
            (
                self.active
                or generation == self.text_generation
                or generation == self.announcement_generation
                or generation == self.visual.generation
            )
            and self.connection is not None
            and generation == self.generation
        )

    def touch_activity(self):
        self.last_interaction = time.monotonic()
        if self.screen_sleeping:
            self.screen_sleeping = False
            self.event('screen_wake')

    def idle_tick(self):
        self.reminder_tick()
        if (
            self.active
            and not self.calibrating
            and not self.playback.playing
            and not self.response_busy
            and not self.recognition_busy
            and not self.screen_sleeping
            and time.monotonic() - self.last_interaction >= 900
        ):
            self.awake = False
            self.screen_sleeping = True
            self.event('screen_sleep')
            self.log('screen_idle_sleep')

    def heartbeat_tick(self):
        try:
            if self.connection is not None:
                self.idle_tick()
                self.event('ping')
        except (OSError, EOFError, ValueError, RuntimeError) as error:
            # A failed send or reminder-store write must not end the timer thread.
            self.log('heartbeat_retry', error_type=type(error).__name__)

    def publish_workspace(self):
        if self.connection is not None:
            self.event('workspace', **self.workspace.snapshot())

    def health(self):
        # 白名单输出就绪位，不导出 metrics 内可能存在的自检文字和用户内容。
        return {
            'ok': True,
            'bridge_version': RELEASE.backend_version,
            'release_id': RELEASE.release_id,
            'protocol_version': RELEASE.protocol_version,
            'uptime_seconds': round(time.monotonic() - self.started_at, 1),
            'model_ready': True,
            'recognition_worker': self.worker.is_alive(),
            'response_worker': self.responder.is_alive(),
            'connected': self.connection is not None,
            'transport': self.transport_kind,
            'microphone_active': self.active,
            'calibrating': self.calibrating,
            'asr_engine': getattr(getattr(self.speech, 'recognizer', None), 'engine', None),
            'brain_provider': getattr(self.brain, 'selected', 'echo'),
            'dot_runtime': dot_runtime_status(),
            'playing': self.playback.playing,
            'mic_frames': self.metrics.get('mic_frames', 0),
            'render_frames': self.metrics.get('render_frames', 0),
        }

    def diagnostics_report(self):
        return {'ok': True, 'health': self.health(), **self.diagnostics.report()}

    def reminder_action(self, args, is_current=lambda: True):
        with self.guard:
            result = self.workspace.reminder(args, is_current)
            if result.get('ok'):
                self.publish_workspace()
            return result

    def submit_text(self, text):
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 6000
            or any(ord(c) < 32 and c not in '\n\t' for c in text)
        ):
            raise ValueError('Text must contain 1–6000 characters')
        with self.guard:
            if self.connection is None or self.calibrating:
                raise RuntimeError('Text request unavailable')
            generation = self.cancel('text_request')
            self.text_generation = generation
            self.touch_activity()
            self.diagnostics.begin('text', generation)
            self.event('text_begin', generation=generation, text=text.strip())
            self.enqueue_response(TextReply(generation, text.strip()))
            return {'ok': True, 'generation': generation}

    def reminder_tick(self):
        # Do not interrupt conversations or turn on capture to deliver a reminder.
        with self.guard:
            job = self.reminder_job
            if job is not None:
                if not job.done.is_set():
                    return
                self.workspace.delivered(
                    job.reminder_id, job.result == 'completed', revision=job.reminder_revision
                )
                self.reminder_job = None
                self.publish_workspace()
            if time.monotonic() < self.reminder_poll:
                return
            self.reminder_poll = time.monotonic() + 5
            if (
                self.connection is None
                or self.calibrating
                or self.playback.playing
                or self.response_busy
                or self.recognition_busy
                or self.text_generation is not None
                or self.visual.generation is not None
                or self.announcement_generation is not None
                or self.capture.endpoint.vad.talking
                or not self.response_queue.empty()
            ):
                return
            item = self.workspace.due()
            if item is None:
                return
            self.touch_activity()
            job = self.announce('提醒你：' + item['title'])
            job.reminder_id = item['id']
            job.reminder_revision = item['revision']
            self.reminder_job = job

    def control_device(self, action, percent=None):
        if action not in ('status', 'volume', 'stop', 'wake_screen'):
            raise ValueError('Unsupported device action')
        if self.connection is None:
            raise RuntimeError('CC8 disconnected')
        if action == 'stop':
            self.cancel('dot_device_stop')
        with self.guard:
            self.device_request_serial += 1
            ident = str(self.device_request_serial)
            waiter = queue.Queue(maxsize=1)
            self.device_requests[ident] = waiter
        try:
            fields = {'request_id': ident, 'action': action}
            if percent is not None:
                fields['percent'] = percent
            self.event('device_control', **fields)
            try:
                result = waiter.get(timeout=5)
                if result.get('ok') and action == 'wake_screen':
                    self.touch_activity()
                return result
            except queue.Empty:
                raise RuntimeError('CC8 control was not acknowledged') from None
        finally:
            with self.guard:
                self.device_requests.pop(ident, None)

    def pause_for_voice(self):
        return self.playback.pause_for_voice()

    def resume_after_rejection(self, token, generation):
        return self.playback.resume_after_rejection(token, generation)

    def wait_playback(self, generation):
        return self.playback.wait_playback(generation)

    def announce(self, text):
        text = validate_text(text)
        with self.guard:
            if (
                self.connection is None
                or self.calibrating
                or self.echo_check_running
                or self.announcement_generation is not None
            ):
                raise RuntimeError('Announcement unavailable')
            generation = self.cancel('announcement')
            self.announcement_generation = generation
            self.diagnostics.begin('announcement', generation)
            job = Announcement(generation, text)
            try:
                self.response_queue.put_nowait(job)
            except queue.Full:
                self.announcement_generation = None
                raise RuntimeError('Announcement queue busy') from None
            return job

    def read_announcement(self, job):
        generation = job.generation
        try:
            if not self.valid(generation):
                return
            self.event('announcement_begin', generation=generation)
            self.status('thinking', '正在准备播报 · 麦克风状态保持不变')
            # Read user-provided text literally, without an LLM rewrite or lookup.
            pieces = [job.text[i : i + 120] for i in range(0, len(job.text), 120)]
            for text in pieces:
                if not self.valid(generation) or not self.speak(text, generation):
                    return
            if self.valid(generation):
                job.result = 'completed'
        except Exception as error:
            job.result = 'failed'
            if self.valid(generation):
                self.failure('这次播报没有完成', error)
        finally:
            if self.connection is not None:
                try:
                    self.event('announcement_done', generation=generation, result=job.result)
                except (OSError, EOFError):
                    pass
            if self.announcement_generation == generation:
                self.announcement_generation = None
            self.metrics['last_announcement'] = {'status': job.result, 'characters': len(job.text)}
            self.diagnostics.finish(
                generation,
                'completed'
                if job.result == 'completed'
                else 'failed'
                if job.result == 'failed'
                else 'cancelled',
            )
            try:
                self.save_status()
            finally:
                job.done.set()

    def cancel(self, reason='speech'):
        # 推进代次使旧模型、音频、字幕和看图结果失效，但不能撤销已执行的工具动作。
        with self.guard:
            self.diagnostics.finish(self.generation, 'cancelled')
            workspace_changed = self.workspace.cancel_generation(self.generation)
            self.generation += 1
            generation = self.generation
            was_playing = self.playback.stop_output()
            self.announcement_generation = None
            self.text_generation = None
            self.visual.invalidate()
            self.playback.clear_cancelled_pause(was_playing)
        if self.brain:
            self.brain.cancel()
        cancel_speech = getattr(self.speech, 'cancel', None)
        if cancel_speech:
            cancel_speech()
        if self.connection is not None:
            self.event('cancel', generation=generation)
        if workspace_changed:
            self.publish_workspace()
        self.playback.notify()
        if was_playing:
            self.metrics['interruptions'] += 1
            self.log('playback_cancelled', reason=reason, generation=generation)
        return generation

    def stop(self):
        self.active = False
        self.calibrating = False
        self.screen_sleeping = False
        self.cancel('session_stop')
        self.capture.close()
        self.save_status()

    def on_event(self, j):
        validate_device_event(j)
        event = j.get('event')
        if event in (
            'camera_image_begin',
            'camera_image_chunk',
            'camera_image_end',
            'camera_error',
        ):
            self.visual.feed(j)
        elif event == 'vision_request':
            nonce = j.get('nonce')
            if isinstance(nonce, str) and re.fullmatch('[a-f0-9]{32}', nonce):
                self.begin_visual(
                    '请描述当前摄像头画面中的主要事物，说明你能确定的内容；看不清时请说明。',
                    'button',
                    nonce,
                )
        elif event == 'vision_cancel':
            if self.visual.matches_cancel(j):
                self.cancel('vision_cancel')
                self.status('listening' if self.active else 'idle', '已停止看图')
        elif event == 'device_ready':
            self.visual.update_peer(j)
            self.playback.set_pause_capability(bool(j.get('supports_playback_pause')))
            if self.active and not j.get('capture_active', False):
                self.stop()
            if 'wake_mode' in j:
                self.wake = bool(j['wake_mode'])
            self.event(
                'hello',
                message='我在听'
                if self.active
                else '回声自检已准备 · 点击开始语音后保持安静'
                if self.echo_check_pending
                else '大黄蜂已就绪 · 点击开始语音',
                transport=self.transport_kind,
                asr_engine=getattr(
                    getattr(self.speech, 'recognizer', None), 'engine', 'sensevoice'
                ),
            )
            self.log('device_handshake_verified', transport=self.transport_kind)
            self.event('brain_provider', provider=getattr(self.brain, 'selected', 'minimax'))
            self.publish_workspace()
            self.save_status()
        elif event == 'workspace_get':
            self.publish_workspace()
        elif event == 'text_request':
            try:
                self.submit_text(j.get('text'))
            except (ValueError, RuntimeError):
                self.event(
                    'workspace_result',
                    ok=False,
                    message='文字指令未提交，请连接 Mac 并等待校准结束',
                )
        elif event == 'reminder_action':
            try:
                self.reminder_action(j.get('arguments'))
                self.event('workspace_result', ok=True, message='提醒已更新')
            except (ValueError, OSError):
                self.event(
                    'workspace_result', ok=False, message='提醒未保存，请检查时间、内容及 Mac 存储'
                )
        elif event == 'task_cancel':
            task = next(
                (
                    t
                    for t in self.workspace.snapshot()['tasks']
                    if t['id'] == j.get('id') and t['status'] == 'running'
                ),
                None,
            )
            if task and task['generation'] == self.generation:
                self.cancel('task_button')
                self.status(
                    'listening' if self.active else 'idle', '已停止任务等待；已执行的操作不会撤销'
                )
        elif event == 'session_rebind' and j.get('capture_active'):
            if self.rebind_seen:
                return
            self.rebind_seen = True
            if self.active and int(j.get('epoch', -1)) == self.epoch:
                if self.calibrating:
                    self.capture.echo.clear_calibration_frames()
                    threading.Thread(
                        target=self.calibrate, args=(self.generation,), daemon=True
                    ).start()
                else:
                    self.status('listening', '连接已切换，我在听')
            else:
                self.on_event({**j, 'event': 'session_start'})
        elif event == 'device_control_result':
            with self.guard:
                waiter = self.device_requests.get(str(j.get('request_id')))
            if waiter:
                try:
                    waiter.put_nowait(
                        {
                            k: v
                            for k, v in j.items()
                            if k
                            in (
                                'ok',
                                'volume_percent',
                                'microphone_active',
                                'foreground',
                                'error',
                                'transport',
                                'wifi_ready',
                            )
                        }
                    )
                except queue.Full:
                    pass
        elif event == 'screen_touch':
            self.touch_activity()
        elif event == 'settings':
            if 'wake_mode' in j:
                self.wake = bool(j['wake_mode'])
                self.awake = not self.wake
            if 'brain_provider' in j and hasattr(self.brain, 'select'):
                provider = j['brain_provider']
                if provider in ('minimax', 'dot') and not self.calibrating:
                    self.cancel('brain_change')
                    self.brain.select(provider)
                    self.event('brain_provider', provider=provider)
                    self.status(
                        'listening' if self.active else 'idle',
                        '已选择 Dot / Codex · 思考与执行'
                        if provider == 'dot'
                        else '已选择 MiniMax · 日常对话与联网',
                    )
            self.save_status()
        elif event == 'session_start':
            self.rebind_seen = True
            # A route rebind can arrive before the capture worker's first start.
            # Both describe the same native recording epoch; never restart it.
            if self.active and int(j['epoch']) == self.epoch:
                return
            if self.visual.generation is not None or self.text_generation is not None:
                self.cancel('microphone_start')
            if self.active or self.announcement_generation is not None:
                self.stop()
            self.epoch = int(j['epoch'])
            self.active = True
            self.generation += 1
            self.capture.reset_session()
            self.playback.reset_session()
            self.capture.echo.open()
            self.calibrating = True
            self.awake = not self.wake
            self.screen_sleeping = False
            self.last_interaction = time.monotonic()
            for k in [
                'mic_frames',
                'render_frames',
                'sequence_gaps',
                'interruptions',
                'utterances',
                'fast_stop_count',
                'echo_transcripts_rejected',
            ]:
                self.metrics[k] = 0
            generation = self.generation
            threading.Thread(target=self.calibrate, args=(generation,), daemon=True).start()
            self.log('foreground_capture_started')
        elif event == 'session_stop':
            # A stopped worker can finish after a new foreground capture starts.
            if self.active and int(j.get('epoch', self.epoch)) == self.epoch:
                self.stop()
                self.log('foreground_capture_stopped')
        elif event == 'interrupt':
            if (
                self.active
                or self.text_generation is not None
                or self.announcement_generation is not None
                or self.visual.generation is not None
            ):
                self.cancel('button')
                self.status('listening' if self.active else 'idle', '播报已停止')
        elif event in ('play_done', 'playback_paused', 'playback_resumed', 'playback_error'):
            self.playback.device_event(j)
        elif event == 'capture_gap':
            self.log('capture_read_gap', milliseconds=j.get('milliseconds'))

    def on_packet(self, p):
        if p.kind == EVENT:
            try:
                self.on_event(decode_event(p.data))
            except (KeyError, TypeError, OverflowError):
                raise ValueError('Invalid device event fields') from None
            return
        self.capture.on_packet(p)

    def process_microphone(self, p):
        return self.capture.process_microphone(p)

    def add_capture_metric(self, name, amount):
        self.metrics[name] += amount

    def discard_recognition(self):
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break

    def enqueue_recognition(self, job):
        try:
            self.queue.put_nowait(job)
        except queue.Full:
            try:
                old = self.queue.get_nowait()
                if len(old) > 6:
                    self.diagnostics.finish(old[6], 'cancelled')
            except queue.Empty:
                pass
            self.queue.put_nowait(job)

    def apply_fast_stop(self, keyword, stamp):
        return self.capture.endpoint.apply_fast_stop(keyword, stamp)

    def save_status(self):
        PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(PRIVATE, 0o700)
        data = {
            'connected': self.connection is not None,
            'active': self.active,
            'calibrating': self.calibrating,
            'playing': self.playback.playing,
            'generation': self.generation,
            'wake_mode': self.wake,
            'wake_word': WAKE_WORD,
            'bridge_version': RELEASE.backend_version,
            'release_id': RELEASE.release_id,
            'protocol_version': RELEASE.protocol_version,
            'desktop_camera_enabled': bool(self.visual.challenge),
            'desktop_snapshot_active': self.visual.snapshot_job is not None,
            'noise_profile': 'moderate',
            'speech_energy_floor_dbfs': NearSpeechDetector.MIN_DBFS,
            'speech_noise_margin_db': NearSpeechDetector.NOISE_MARGIN_DB,
            'speech_onset_ms': VoiceDetector.ONSET_FRAMES * 10,
            'barge_confirm_min_ms': NearSpeechDetector.CONFIRM_MIN_FRAMES * 10,
            'vision_active': self.visual.generation is not None,
            'vision_enabled': isinstance(self.brain, BrainRouter),
            'transport': self.transport_kind,
            **(self.transport_hub.diagnostics() if self.transport_hub else {}),
            'screen_sleeping': self.screen_sleeping,
            'idle_sleep_seconds': 900,
            'asr_engine': getattr(getattr(self.speech, 'recognizer', None), 'engine', None),
            'barge_in_enabled': self.playback.device_can_pause,
            'playback_paused': self.playback.pause_state is not None,
            'announcement_active': self.announcement_generation is not None,
            'brain_provider': getattr(self.brain, 'selected', 'minimax')
            if self.brain
            else 'echo test',
            'dot_enabled': isinstance(self.brain, BrainRouter),
            'web_search_enabled': self.brain is not None,
            'fast_stop_enabled': self.capture.endpoint.fast_stop is not None,
            'fast_stop_input': 'AEC3 without additional NS',
            'fast_stop_gain': getattr(self.capture.endpoint.fast_stop, 'gain', None),
            'aec_engine': 'WebRTC AEC3 + NS; AGC disabled',
            'render_asr_comparison': True,
            'residual_echo_suppression': True,
            'echo_check_pending': self.echo_check_pending,
            'echo_check_failed': self.echo_check_failed,
            'speech_voice': getattr(getattr(self.speech, 'voice', None), 'voice', None),
            'speech_speed': getattr(getattr(self.speech, 'voice', None), 'speed', None),
            'speech_pitch': getattr(getattr(self.speech, 'voice', None), 'pitch', None),
            'speech_emotion': getattr(getattr(self.speech, 'voice', None), 'emotion', None),
            **self.metrics,
        }
        with self.status_lock:
            temporary = PRIVATE / 'status.tmp'
            temporary.write_text(json.dumps(data, indent=2) + '\n')
            temporary.replace(PRIVATE / 'status.json')

    def play(self, pcm, text, generation, role='assistant'):
        return self.playback.play(pcm, text, generation, role)

    def play_stream(self, chunks, text, generation, role='assistant', timing=None):
        return self.playback.play_stream(chunks, text, generation, role, timing)

    def speak(self, text, generation):
        return self.playback.speak(text, generation)

    def startup_reference(self):
        # 开场口号同时提供扬声器参考，不再使用检测器的固定女声素材。
        pcm = np.asarray(self.speech.synthesize(STARTUP_GREETING))
        if (
            pcm.ndim != 1
            or pcm.dtype != np.dtype('int16')
            or not RATE <= len(pcm) <= RATE * 6
            or not np.any(pcm)
        ):
            raise ValueError('Invalid startup reference PCM')
        return pcm

    def calibrate(self, generation):
        try:
            self.status('calibrating', '正在准备大黄蜂开场音…')
            pcm = self.startup_reference()
            with self.guard:
                if not self.valid(generation):
                    return
                # 云端合成可能耗时，时钟对齐只使用真正播放开始后的收音。
                self.capture.echo.clear_calibration_frames()
            started = time.monotonic()
            self.status('calibrating', '声音校准中，请稍候…')
            if not self.play(pcm, STARTUP_GREETING, generation, 'calibration'):
                return
            if not self.valid(generation):
                return
            self.status('calibrating', '声音校准中，请稍候…')
            # 保留原 6 秒采样窗口和房间尾音时间；后半段安静等待，不再播放额外提示。
            time.sleep(max(0.4, 6.4 - (time.monotonic() - started)))
            if not self.valid(generation):
                return
            if not self.align_reference(generation):
                return
            with self.guard:
                if not self.valid(generation):
                    return
                self.calibrating = False
                self.capture.endpoint.reset_voice_activity()
            if self.echo_check_pending:
                self.echo_check_pending = False
                self.check_echo(generation)
            elif self.wake:
                self.status('listening', '请说“大黄蜂”或“Bumblebee”开始聊天')
            else:
                self.status('listening', '我在听，可以开始说话')
            self.save_status()
            self.log('calibration_finished')
        except Exception as e:
            if self.valid(generation):
                self.failure('校准未完成，请停止后重试', e)
        finally:
            if generation == self.generation:
                self.calibrating = False

    def check_echo(self, generation):
        """One-shot diagnostic, ONLY after the device's foreground Start.

        ASR and the normal echo gate stay active. A would-be reply is counted
        as a failure and withheld, so the diagnostic cannot start a web loop.
        No audio is saved and no microphone-start command exists.
        """
        self.echo_check_running = True
        before = self.metrics.get('echo_transcripts_rejected', 0)
        start = time.monotonic()
        report = {
            'state': 'running',
            'unmatched_candidates': 0,
            'echo_rejections': 0,
            'seconds': 0,
            'diagnostic_text_pairs': [],
        }
        self.metrics['echo_self_check'] = report
        lines = [
            '下面是回声自检，内容只是声音示例，不是实时新闻。请保持安静。',
            '报告称，长三角已形成从底层算力、核心算法到终端应用、高端装备的完整产业生态。',
            '现在测试英文和数字，MiniMax、OpenAI、GPU，百分之二点八六和百分之二十九。',
            '金额示例为十五亿元，日期示例为十月三十一日。',
            '声音示例已经播完，请继续保持安静二十秒，等屏幕显示自检结果。',
        ]
        try:
            self.log('echo_self_check_started')
            self.save_status()
            for line in lines:
                if not self.valid(generation) or not self.speak(line, generation):
                    report['state'] = 'interrupted'
                    return
            self.status('listening', '回声自检收尾 · 请保持安静20秒')
            deadline = time.monotonic() + 20
            while self.valid(generation) and time.monotonic() < deadline:
                time.sleep(0.1)
            if not self.valid(generation):
                report['state'] = 'interrupted'
                return
            drain = time.monotonic() + 20
            while (
                self.valid(generation)
                and (
                    self.recognition_busy
                    or not self.queue.empty()
                    or self.capture.endpoint.vad.talking
                )
                and time.monotonic() < drain
            ):
                time.sleep(0.1)
            if not self.valid(generation):
                report['state'] = 'interrupted'
                return
            complete = (
                not self.recognition_busy
                and self.queue.empty()
                and not self.capture.endpoint.vad.talking
            )
            passed = complete and report['unmatched_candidates'] == 0
            report['state'] = 'passed' if passed else ('failed' if complete else 'incomplete')
            self.echo_check_failed = not passed
            self.status(
                'listening' if passed else 'error',
                '回声自检通过 · 你现在可以说话'
                if passed
                else '自检仍发现疑似回声 · 已暂停自动回答，等待调试',
            )
        except Exception:
            report['state'] = 'error'
            self.echo_check_failed = True
            raise
        finally:
            report['echo_rejections'] = self.metrics.get('echo_transcripts_rejected', 0) - before
            report['seconds'] = round(time.monotonic() - start, 1)
            self.echo_check_running = False
            self.log('echo_self_check_finished', **report)
            self.save_status()

    def align_reference(self, generation=None):
        return self.capture.echo.align_reference(generation)

    def failure(self, message, error, trace_key=None):
        key = self.generation if trace_key is None else trace_key
        self.diagnostics.failure(key, self.diagnostics.current_stage(key), error)
        # MiniMaxError messages are our own fixed strings, never provider
        # bodies, user text, URLs or credentials. Other exception text is not logged.
        self.log(
            'runtime_error',
            error_type=type(error).__name__,
            reason=str(error) if isinstance(error, MiniMaxError) else None,
            api_code=getattr(error, 'code', None),
            http_status=getattr(error, 'http_status', None),
        )
        try:
            self.status('error', message)
        except (OSError, EOFError):
            pass

    def work(self):
        while True:
            job = self.queue.get()
            generation, audio = job[:2]
            during_playback = job[2] if len(job) > 2 else False
            references = job[3] if len(job) > 3 else []
            evidence = job[4] if len(job) > 4 else None
            render_audio = job[5] if len(job) > 5 else None
            if not self.valid(generation):
                if len(job) > 6:
                    self.diagnostics.finish(job[6], 'cancelled')
                continue
            trace_id = job[6] if len(job) > 6 else self.diagnostics.begin('voice')
            accepted = False
            self.recognition_busy = True
            rejected = False
            try:
                if not self.playback.playing:
                    self.status('thinking', '正在本机识别…')
                self.diagnostics.mark(trace_id, 'asr_start')
                text = self.speech.recognize(audio)
                self.diagnostics.mark(trace_id, 'asr_end')
                asr = getattr(self.speech, 'recognizer', None)
                if asr:
                    self.metrics['last_asr'] = dict(asr.last_metrics)
                if not self.valid(generation):
                    continue
                clean = re.sub(r'\W', '', text)
                if not clean or clean in ['谢谢观看', '感谢观看', '字幕由Amaraorg社区提供']:
                    rejected = True
                    self.status('listening', '我在听')
                    continue
                # Production jobs always carry immutable capture evidence.
                # The legacy fallback is only for old offline replay jobs.
                risky = (
                    evidence['echo_risk']
                    if evidence is not None
                    else (during_playback or time.monotonic() - self.playback.last_audio_end < 8)
                )
                available = (
                    references
                    if evidence is not None
                    else references + [line for _, line in tuple(self.playback.spoken)]
                )
                self.metrics['last_voice_gate'] = {
                    'echo_risk': risky,
                    'references': len(available),
                    'characters': len(clean),
                    'capture_age_ms': round((time.monotonic() - evidence['captured_at']) * 1000)
                    if evidence
                    else None,
                    'rejected': False,
                }
                echo = (
                    risky
                    and (len(clean) > 2 or during_playback)
                    and resembles_playback(text, available)
                )
                render_text = ''
                if (
                    risky
                    and not echo
                    and render_audio is not None
                    and len(render_audio) >= RATE // 3
                    and np.count_nonzero(np.abs(render_audio) > 0.001) >= RATE // 5
                ):
                    # Decode only the captured render interval, not the entire
                    # answer. ASR may spell TTS names/numbers differently from
                    # their captions. This stays local and sequential.
                    self.metrics['render_asr_checks'] = self.metrics.get('render_asr_checks', 0) + 1
                    render_text = self.speech.recognize(render_audio)
                    if not self.valid(generation):
                        continue
                    echo = bool(render_text) and resembles_playback(text, [render_text])
                    if echo:
                        self.metrics['render_asr_rejections'] = (
                            self.metrics.get('render_asr_rejections', 0) + 1
                        )
                if risky and ('明镜与点点' in clean or '明镜与点栏目' in clean):
                    echo = True
                if self.echo_check_running and render_text:
                    # Diagnostic only: user was explicitly asked to be quiet.
                    # Bounded text pairs help diagnose a failed self-check;
                    # no PCM and no normal conversations are persisted.
                    pairs = self.metrics['echo_self_check']['diagnostic_text_pairs']
                    if len(pairs) < 8:
                        pairs.append(
                            {'heard': text[:200], 'render_asr': render_text[:200], 'rejected': echo}
                        )
                if echo:
                    rejected = True
                    self.metrics['last_voice_gate']['rejected'] = True
                    self.metrics['echo_transcripts_rejected'] = (
                        self.metrics.get('echo_transcripts_rejected', 0) + 1
                    )
                    self.log(
                        'reference_like_transcript_rejected',
                        characters=len(clean),
                        references=len(available),
                        capture_age_ms=round((time.monotonic() - evidence['captured_at']) * 1000)
                        if evidence
                        else None,
                    )
                    if not self.playback.playing:
                        self.status('listening', '我在听')
                    continue
                if (self.echo_check_running or self.echo_check_failed) and clean not in {
                    '停',
                    '停下',
                    '停一下',
                    '停止',
                    '暂停',
                    '打住',
                    '等等',
                    '别说了',
                    '听我说',
                }:
                    if self.echo_check_running:
                        self.metrics['echo_self_check']['unmatched_candidates'] += 1
                    self.log(
                        'echo_self_check_candidate_withheld',
                        characters=len(clean),
                        echo_risk=risky,
                        references=len(available),
                    )
                    continue
                if not self.awake:
                    found = WAKE_PATTERN.search(text)
                    if not found:
                        rejected = True
                        self.status('listening', '请说“大黄蜂”或“Bumblebee”开始聊天')
                        continue
                    self.touch_activity()
                    self.awake = True
                    text = text[found.end() :].lstrip('，。 ,') or '你好'
                    generation = self.cancel('wake_word')
                if self.wake and '休息' in text and (WAKE_PATTERN.search(text) or len(text) < 8):
                    self.awake = False
                    self.cancel('wake_sleep')
                    self.status('listening', '请说“大黄蜂”或“Bumblebee”开始聊天')
                    continue
                if risky:
                    generation = self.cancel('verified_voice')
                self.diagnostics.bind(trace_id, generation)
                accepted = True
                self.touch_activity()
                self.event('transcript', text=text)
                self.log(
                    'user_turn_recognized',
                    characters=len(text),
                    echo_risk=risky,
                    references=len(available),
                )
                if re.sub(r'\W', '', text) in {
                    '停',
                    '停下',
                    '停一下',
                    '停止',
                    '暂停',
                    '打住',
                    '等等',
                    '别说了',
                    '听我说',
                }:
                    self.diagnostics.finish(trace_id, 'completed')
                    self.status('listening', '已停止播报，我在听')
                    continue
                if visual_request(text):
                    self.begin_visual(text, 'voice')
                else:
                    self.enqueue_response(Reply(generation, text))
            except Exception as e:
                self.diagnostics.failure(trace_id, 'asr', e)
                if self.valid(generation):
                    try:
                        if (
                            self.playback.pause_state
                            and evidence
                            and evidence.get('pause_token') == self.playback.pause_state['token']
                        ):
                            self.cancel('barge_asr_error')
                    except (OSError, EOFError):
                        pass
                    self.failure('这次识别没有完成，请再说一次', e, trace_key=trace_id)
            finally:
                self.recognition_busy = False
                if not accepted:
                    self.diagnostics.finish(trace_id, 'rejected' if rejected else 'cancelled')
                if evidence and rejected:
                    try:
                        self.resume_after_rejection(evidence.get('pause_token'), generation)
                    except (OSError, EOFError) as error:
                        self.log('playback_resume_disconnected', error_type=type(error).__name__)

    def record_metric(self, name, value=None, *, increment=False):
        self.metrics[name] = self.metrics.get(name, 0) + 1 if increment else value

    def request_snapshot(self):
        return self.visual.request_snapshot()

    def cancel_snapshot(self, job):
        return self.visual.cancel_snapshot(job)

    def capture_snapshot(self, job, capture):
        return self.visual.capture_snapshot(job, capture)

    def begin_visual(self, text, origin, nonce=''):
        return self.visual.begin_visual(text, origin, nonce)

    def answer_visual(self, job):
        return self.visual.answer_visual(job)

    def enqueue_response(self, job):
        while True:
            try:
                self.response_queue.put_nowait(job)
                return
            except queue.Full:
                try:
                    old = self.response_queue.get_nowait()
                except queue.Empty:
                    continue
                if isinstance(old, Announcement):
                    old.done.set()

    def respond_work(self):
        # 单消费者串行播报，识别另在线程运行，因此暂停时仍能继续听插话。
        while True:
            job = self.response_queue.get()
            try:
                self.respond_job(job)
            except Exception as error:
                if self.valid(job.generation):
                    self.failure('这次回答没有完成，请再说一次', error)
                else:
                    self.log('response_worker_error', error_type=type(error).__name__)

    def respond_job(self, job):
        if isinstance(job, Announcement):
            self.read_announcement(job)
            return
        if not self.valid(job.generation):
            return
        self.response_busy = True
        try:
            if isinstance(job, VisualReply):
                self.answer_visual(job)
            else:
                self.answer(job.text, job.generation)
        finally:
            self.response_busy = False
            if isinstance(job, TextReply) and self.text_generation == job.generation:
                self.text_generation = None
                if self.connection is not None:
                    self.event('text_done', generation=job.generation)

    def answer(self, text, generation, image=None):
        # 任务成功以大脑真实回执为准，后续停止朗读不能把已完成工作改为取消。
        self.diagnostics.ensure(generation, 'vision' if image is not None else 'text')
        self.diagnostics.mark(generation, 'brain_start')
        task_id = self.workspace.task_start(generation, text)
        result_text = ['']
        clauses = None

        def web_update(kind, payload):
            if not self.valid(generation):
                return
            if kind == 'progress':
                message = payload.get('message', '正在处理…')
                self.workspace.task_stage(
                    task_id, 'analysis', '连接助理' if '连接' in message else '分析请求', 'running'
                )
                self.publish_workspace()
                self.event('status', generation=generation, phase='thinking', message=message)
            elif kind == 'dot_result':
                self.diagnostics.task_result(generation, 'completed')
                self.diagnostics.link_turn(generation, self.brain.last_metrics.get('turn_id'))
                result_text[0] = payload.get('text', '')
                # The Dot turn receipt closes the task, independently of the
                # remaining audio or device delivery. A disconnected display
                # cannot turn successfully executed work into a failed task.
                self.workspace.task_finish(task_id, 'completed', result_text[0])
                self.event('dot_result', generation=generation, **payload)
                self.publish_workspace()
            elif kind == 'task_stage':
                self.diagnostics.scenario(
                    generation, 'web' if payload.get('kind') == 'webSearch' else 'tool'
                )
                self.workspace.task_stage(
                    task_id, payload['id'], payload['title'], payload['status']
                )
                self.publish_workspace()
            elif kind == 'searching':
                self.diagnostics.scenario(generation, 'web')
                self.workspace.task_stage(task_id, 'search', '联网检索', 'running')
                self.publish_workspace()
                self.event(
                    'status', generation=generation, phase='thinking', message='正在联网查询…'
                )
            elif kind == 'failed':
                self.log('web_search_failed', **payload)
            elif kind in ('clear', 'sources'):
                self.event(
                    'web_sources',
                    generation=generation,
                    items=payload.get('items', []),
                    retrieved_at=payload.get('retrieved_at', ''),
                )
                if kind == 'clear':
                    self.event('dot_result', generation=generation, text='', truncated=False)
                if kind == 'sources':
                    self.workspace.task_stage(task_id, 'search', '检索来源已返回', 'completed')
                    self.publish_workspace()
                    self.log('web_search_ready', sources=len(payload['items']))

        completed = True
        spoken = []
        try:
            self.publish_workspace()
            self.status('thinking', '正在准备回答…')
            clauses = (
                self.brain.vision_clauses(
                    text, image, self.history, lambda: self.valid(generation), web_update
                )
                if image is not None
                else self.brain.clauses(
                    text, self.history, lambda: self.valid(generation), web_update
                )
                if self.brain
                else iter(['语音识别已完成。你刚才说：' + text])
            )
            for clause in clauses:
                if not self.valid(generation):
                    completed = False
                    break
                self.diagnostics.mark(generation, 'first_clause')
                if self.brain:
                    self.diagnostics.link_turn(generation, self.brain.last_metrics.get('turn_id'))
                if not self.speak(clause, generation):
                    completed = False
                    break
                spoken.append(clause)
        finally:
            close = getattr(clauses, 'close', None)
            if close:
                close()
            failed = bool(
                self.brain
                and (
                    self.brain.last_metrics.get('completed') is False
                    or self.brain.last_metrics.get('approval_required')
                )
            )
            status = (
                'cancelled'
                if not self.valid(generation)
                else 'failed'
                if sys.exc_info()[0] or not completed or failed
                else 'completed'
            )
            self.workspace.task_finish(task_id, status, result_text[0] or ''.join(spoken))
            self.diagnostics.task_result(generation, status)
            self.diagnostics.finish(generation, status)
            self.publish_workspace()
        if completed and self.valid(generation):
            answer = ''.join(spoken)
            self.history.extend(
                [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': answer}]
            )
            self.status(
                'listening' if self.active else 'idle',
                '我在听' if self.active else '回答已完成 · 麦克风关闭',
            )
            if self.brain:
                self.metrics['last_chat_api'] = self.brain.last_metrics
            voice = getattr(self.speech, 'voice', None)
            if voice:
                self.metrics['last_speech_api'] = voice.last_metrics


def main():
    # 锁覆盖模型加载到退出全程；直接执行 bridge.py 也不能绕过单实例检查。
    try:
        with RuntimeLease(ROOT):
            run_backend()
    except BlockingIOError:
        raise SystemExit('已有大黄蜂后台或环境准备任务占用运行锁，未启动第二份模型。')
    except RuntimeConflict as error:
        raise SystemExit(str(error)) from None


def run_backend():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brain', choices=['minimax', 'echo'], default='minimax')
    parser.add_argument('--speech-smoke', action='store_true')
    parser.add_argument(
        '--no-fast-stop',
        action='store_true',
        help='Use the slower utterance-confirmation stop route only',
    )
    parser.add_argument(
        '--echo-check-after-start',
        action='store_true',
        help='Run a one-shot echo diagnostic only after the foreground microphone button',
    )
    args = parser.parse_args()
    try:
        config = load_config()
    except ConfigError as error:
        parser.error(str(error))
    except OSError:
        parser.error('CC8 configuration file unavailable; check path and owner access')
    speech = DirectSpeech(config=config) if args.brain == 'minimax' else LocalSpeech(config=config)
    print('Loading local Chinese ASR (' + speech.recognizer.engine + ')…', flush=True)
    speech.recognize(np.zeros(RATE, dtype=np.float32))
    print('Local ASR ready; raw microphone audio is not saved.', flush=True)
    if args.speech_smoke:
        phrase = '你好，语音链路已准备好。'
        pcm = speech.synthesize(phrase)
        recognized = speech.recognize(pcm.astype(float) / 32768)
        print(json.dumps({'tts_frames': len(pcm), 'recognized': recognized}, ensure_ascii=False))
        return
    fast_stop = None if args.no_fast_stop else FastStopDetector()
    print(
        'Local streaming stop commands ready.'
        if fast_stop
        else 'Streaming stop commands disabled.',
        flush=True,
    )
    bridge = Bridge(
        speech,
        BrainRouter(MiniMaxBrain(config=config), selected=config.brain)
        if args.brain == 'minimax'
        else None,
        fast_stop=fast_stop,
        echo_check=args.echo_check_after_start,
        workspace=Workspace(PRIVATE / 'reminders.json'),
    )
    hub = TransportHub(
        Pairing(port=config.tls_port),
        connect_device,
        usb_present,
        bridge.log,
        tls_port=config.tls_port,
    )
    bridge.transport_hub = hub
    hub.start()
    bridge.save_status()
    announcements = AnnouncementServer(bridge)
    announcements.start()
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(PRIVATE, 0o700)
    (PRIVATE / 'bridge.pid').write_text(str(os.getpid()) + '\n')
    stop = threading.Event()

    def shutdown(*unused):
        stop.set()
        bridge.active = False
        bridge.playback.stop_output()
        bridge.announcement_generation = None
        try:
            bridge.cancel('shutdown')
        except (OSError, EOFError):
            pass
        announcements.close()
        hub.close()
        if bridge.brain:
            close = getattr(bridge.brain, 'close', bridge.brain.cancel)
            close()
        cancel_speech = getattr(bridge.speech, 'cancel', None)
        if cancel_speech:
            cancel_speech()
        c = bridge.connection
        if c:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def retry_echo_check(*unused):
        # This signal can rerun the diagnostic on an already active session;
        # it deliberately cannot open the microphone or resume a stopped one.
        if not bridge.active or bridge.calibrating or bridge.echo_check_running or bridge.playing:
            bridge.log('echo_self_check_retry_ignored')
            return
        generation = bridge.cancel('echo_check_retry')
        bridge.calibrating = True
        bridge.echo_check_pending = True
        bridge.echo_check_failed = False
        bridge.capture.echo.clear_calibration_frames()
        bridge.capture.endpoint.reset_voice_activity()
        threading.Thread(target=bridge.calibrate, args=(generation,), daemon=True).start()

    signal.signal(signal.SIGUSR1, retry_echo_check)

    def heartbeat():
        while not stop.wait(5):
            bridge.heartbeat_tick()

    threading.Thread(target=heartbeat, daemon=True).start()
    print(
        'Waiting for CC8: USB priority, paired TLS Wi-Fi standby. Capture requires foreground Start.',
        flush=True,
    )
    while not stop.is_set():
        try:
            try:
                kind, channel, packet = hub.events.get(timeout=0.25)
            except queue.Empty:
                kind = channel = packet = None
            preferred = hub.preferred()
            if preferred is not hub.active:
                bridge.change_transport(preferred)
                hub.selected(preferred)
                bridge.save_status()
            elif kind == 'up' and channel is not hub.active:
                channel.event('transport_standby')
            if kind in ('up', 'down'):
                bridge.save_status()
            if kind == 'packet' and channel is hub.active and not channel.closed:
                bridge.on_packet(packet)
        except (OSError, EOFError, ValueError, RuntimeError, subprocess.SubprocessError):
            if channel is not None:
                channel.close()
        if stop.is_set():
            break
    hub.close()
    bridge.connection = None
    if bridge.active:
        bridge.stop()
    bridge.save_status()


if __name__ == '__main__':
    main()

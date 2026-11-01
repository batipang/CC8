"""CC8 voice terminal: local AEC, MiniMax ASR/TTS and owner-configured Dot.

Microphone samples stay in RAM. Only explicitly enabled smoke/replay tools
read audio files. The companion APK never starts capture from a host command.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
import re
import signal as signal
import subprocess as subprocess
import threading
import time

from protocol import EVENT, decode_event, validate_device_event
from transport import Pairing, TransportHub
from dsp import (
    WebRtcEchoCanceller,
    VoiceDetector,
    NearSpeechDetector,
)
from fast_stop import FastStopDetector
from brain_router import BrainRouter
from announce import AnnouncementServer
from calibration_controller import STARTUP_GREETING as STARTUP_GREETING
from composition import build_components, build_session
from app_runtime import BackendDependencies, run_backend as run_app
from minimax_client import MiniMaxBrain, MiniMaxError
from workspace import Workspace
from diagnostics import TraceBook
from runtime_guard import RuntimeLease, RuntimeConflict
from configuration import load_config
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
        self.transport_hub = None
        self.metrics = {
            'mic_frames': 0,
            'render_frames': 0,
            'sequence_gaps': 0,
            'interruptions': 0,
            'utterances': 0,
            'raw_audio_saved': False,
        }
        self.status_lock = threading.Lock()
        self.session = build_session(self)
        if isinstance(self.brain, BrainRouter):
            self.brain.dot.device_control = self.control_device
            self.brain.dot.reminders = self.reminder_action
        components = build_components(
            self,
            echo_factory=lambda **kwargs: WebRtcEchoCanceller(**kwargs),
            wake_pattern=WAKE_PATTERN,
            fast_stop=fast_stop,
            echo_check=echo_check,
        )
        self.calibration = components.calibration
        self.visual = components.visual
        self.playback = components.playback
        self.capture = components.capture
        self.recognition = components.recognition
        self.response = components.response
        self.announcements = components.announcements
        self.reminders = components.reminders
        self.device = components.device
        self.recognition.start()
        self.response.start()

    @property
    def generation(self):
        return self.session.generation

    @property
    def guard(self):
        return self.session.guard

    def log(self, event, **fields):
        print(
            json.dumps(
                {'event': event, 'monotonic_seconds': round(time.monotonic(), 3), **fields},
                ensure_ascii=False,
            ),
            flush=True,
        )

    def send(self, p):
        return self.session.send(p)

    def event(self, event, generation=None, **fields):
        return self.session.event(event, generation, **fields)

    def change_transport(self, channel):
        return self.session.change_transport(channel)

    def status(self, phase, message):
        return self.session.status(phase, message)

    def valid(self, generation):
        return self.session.valid(generation)

    def touch_activity(self):
        return self.session.touch_activity()

    def idle_tick(self):
        return self.session.idle_tick()

    def heartbeat_tick(self):
        try:
            if self.session.connection is not None:
                self.idle_tick()
                self.event('ping')
        except (OSError, EOFError, ValueError, RuntimeError) as error:
            # A failed send or reminder-store write must not end the timer thread.
            self.log('heartbeat_retry', error_type=type(error).__name__)

    def publish_workspace(self):
        if self.session.connection is not None:
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
            'recognition_worker': self.recognition.worker.is_alive(),
            'response_worker': self.response.worker.is_alive(),
            'connected': self.session.connection is not None,
            'transport': self.session.transport_kind,
            'microphone_active': self.session.active,
            'calibrating': self.calibration.active,
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
        return self.reminders.action(args, is_current)

    def submit_text(self, text):
        return self.session.submit_text(text)

    def reminder_tick(self):
        return self.reminders.tick()

    def control_device(self, action, percent=None):
        return self.device.control(action, percent)

    def pause_for_voice(self):
        return self.playback.pause_for_voice()

    def resume_after_rejection(self, token, generation):
        return self.playback.resume_after_rejection(token, generation)

    def wait_playback(self, generation):
        return self.playback.wait_playback(generation)

    def announce(self, text):
        return self.announcements.announce(text)

    def read_announcement(self, job):
        return self.announcements.read_announcement(job)

    def cancel(self, reason='speech'):
        return self.session.cancel(reason)

    def stop(self):
        return self.session.stop()

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
                self.status('listening' if self.session.active else 'idle', '已停止看图')
        elif event == 'device_ready':
            self.session.ready(j)
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
            if task and task['generation'] == self.session.generation:
                self.cancel('task_button')
                self.status(
                    'listening' if self.session.active else 'idle',
                    '已停止任务等待；已执行的操作不会撤销',
                )
        elif event == 'session_rebind' and j.get('capture_active'):
            self.session.rebind(j)
        elif event == 'device_control_result':
            self.device.receive(j)
        elif event == 'screen_touch':
            self.touch_activity()
        elif event == 'settings':
            self.session.settings(j)
        elif event == 'session_start':
            self.session.start_capture(j)
        elif event == 'session_stop':
            # A stopped worker can finish after a new foreground capture starts.
            self.session.stop_capture(j)
        elif event == 'interrupt':
            self.session.interrupt(j)
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
        return self.recognition.discard()

    def enqueue_recognition(self, job):
        return self.recognition.enqueue(job)

    def apply_fast_stop(self, keyword, stamp):
        return self.capture.endpoint.apply_fast_stop(keyword, stamp)

    def save_status(self):
        PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(PRIVATE, 0o700)
        data = {
            'connected': self.session.connection is not None,
            'active': self.session.active,
            'calibrating': self.calibration.active,
            'playing': self.playback.playing,
            'generation': self.session.generation,
            'wake_mode': self.session.wake,
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
            'transport': self.session.transport_kind,
            **(self.transport_hub.diagnostics() if self.transport_hub else {}),
            'screen_sleeping': self.session.screen_sleeping,
            'idle_sleep_seconds': 900,
            'asr_engine': getattr(getattr(self.speech, 'recognizer', None), 'engine', None),
            'barge_in_enabled': self.playback.device_can_pause,
            'playback_paused': self.playback.pause_state is not None,
            'announcement_active': self.announcements.generation is not None,
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
            'echo_check_pending': self.calibration.pending,
            'echo_check_failed': self.calibration.failed,
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
        return self.calibration.startup_reference()

    def calibrate(self, generation):
        return self.calibration.calibrate(generation)

    def check_echo(self, generation):
        return self.calibration.check_echo(generation)

    def align_reference(self, generation=None):
        return self.capture.echo.align_reference(generation)

    def failure(self, message, error, trace_key=None):
        key = self.session.generation if trace_key is None else trace_key
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
        return self.recognition.work()

    def set_awake(self, value):
        return self.session.set_awake(value)

    def set_metric_field(self, name, field, value):
        self.metrics[name][field] = value

    def add_metric_field(self, name, field, value):
        self.metrics[name][field] += value

    def record_echo_pair(self, text, render_text, echo):
        # Only the explicitly armed echo diagnostic records these bounded pairs.
        pairs = self.metrics['echo_self_check']['diagnostic_text_pairs']
        if len(pairs) < 8:
            pairs.append({'heard': text[:200], 'render_asr': render_text[:200], 'rejected': echo})

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

    def finish_text_response(self, generation):
        return self.session.finish_text_response(generation)

    def enqueue_response(self, job):
        return self.response.enqueue(job)

    def respond_work(self):
        return self.response.work()

    def respond_job(self, job):
        return self.response.respond_job(job)

    def answer(self, text, generation, image=None):
        return self.response.answer(text, generation, image=image)


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
    return run_app(
        BackendDependencies(
            private=PRIVATE,
            description=__doc__,
            load_config=load_config,
            direct_speech=DirectSpeech,
            local_speech=LocalSpeech,
            fast_stop=FastStopDetector,
            brain_router=BrainRouter,
            minimax_brain=MiniMaxBrain,
            bridge=Bridge,
            workspace=Workspace,
            transport_hub=TransportHub,
            pairing=Pairing,
            connect_device=connect_device,
            usb_present=usb_present,
            announcement_server=AnnouncementServer,
        )
    )


if __name__ == '__main__':
    main()

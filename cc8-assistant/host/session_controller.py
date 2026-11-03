"""Own one foreground capture session, generations, wake state and connection.

Generation reads intentionally do not acquire the session lock: playback ACKs
may arrive while the completion condition is held. Cancellation follows the
existing session-lock then component-lock order, never the reverse.
"""

from __future__ import annotations
from dataclasses import dataclass
import threading
import time
from typing import Callable
from protocol import Packet, SPEAKER, JsonObject
from reply_types import TextReply
from diagnostics import TraceBook
from workspace import Workspace
from playback_controller import PlaybackController
from capture_pipeline import CapturePipeline
from vision_controller import VisionController
from calibration_controller import CalibrationController
from announcement_controller import AnnouncementController
from session_ports import EventSink, MetricSink
from wake_controller import WAITING


@dataclass(frozen=True)
class SessionView:
    connected: bool
    active: bool
    generation: int
    transport_kind: str
    wake: bool
    screen_sleeping: bool
    awake: bool


@dataclass(frozen=True)
class SessionHooks:
    diagnostics: Callable[[], TraceBook]
    workspace: Callable[[], Workspace]
    playback: Callable[[], PlaybackController]
    capture: Callable[[], CapturePipeline]
    visual: Callable[[], VisionController]
    calibration: Callable[[], CalibrationController]
    announcements: Callable[[], AnnouncementController]
    brain: Callable[[], object]
    speech: Callable[[], object]
    response_busy: Callable[[], bool]
    recognition_busy: Callable[[], bool]
    send: Callable[[Packet], None]
    event: EventSink
    status: Callable[[str, str], None]
    valid: Callable[[int], bool]
    cancel: Callable[[str], int]
    stop: Callable[[], None]
    touch_activity: Callable[[], None]
    reminder_tick: Callable[[], None]
    save_status: Callable[[], None]
    publish_workspace: Callable[[], None]
    enqueue_response: Callable[[TextReply], None]
    route_event: Callable[[JsonObject], None]
    calibrate: Callable[[], Callable[[int], None]]
    log: EventSink
    metric: MetricSink
    metric_value: Callable[[str], int]
    wake_reset: Callable[[str], None]
    wake_touch: Callable[[], None]
    awake_changed: Callable[[bool], None]
    wake_tick: Callable[[], None]
    prepare_brain: Callable[[int], None]
    cancel_observation: Callable[[], None] = lambda: None


class SessionController:
    def __init__(self, hooks: SessionHooks):
        self.hooks = hooks
        self.text_generation = None
        self.connection = None
        self.transport_kind = 'none'
        self.rebind_seen = False
        self.write_lock = threading.Lock()
        self.guard = threading.RLock()
        self.active = False
        self.epoch = 0
        self.generation = 100
        self.wake = False
        self.awake = True
        self.last_interaction = time.monotonic()
        self.screen_sleeping = False

    def snapshot(self) -> SessionView:
        return SessionView(
            self.connection is not None,
            self.active,
            self.generation,
            self.transport_kind,
            self.wake,
            self.screen_sleeping,
            self.awake,
        )

    def deactivate(self):
        self.active = False

    def unbind(self):
        self.connection = None

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
        self.hooks.send(
            Packet.event(event, self.generation if generation is None else generation, **fields)
        )

    def change_transport(self, channel):
        # 先作废旧回答并重置参考时钟；只可 rebind 已有录音，重连不能自行开麦。
        old = self.connection
        if old is channel:
            return
        was_active = self.active
        try:
            self.hooks.cancel('transport_switch')
        except (OSError, EOFError):
            pass
        self.connection = channel
        self.hooks.visual().clear_challenge()
        self.rebind_seen = False
        self.transport_kind = channel.kind if channel else 'none'
        self.hooks.capture().reset_transport(self.transport_kind)
        self.hooks.wake_reset('transport')
        self.hooks.playback().clear_transport_pause()
        if channel is None:
            self.hooks.stop()
        elif was_active:
            # AudioRecord was already enabled locally. The new channel must
            # explicitly confirm the same capture epoch; it cannot start a mic.
            self.hooks.metric(
                'capture_preserved_switches',
                self.hooks.metric_value('capture_preserved_switches') + 1,
            )
        self.hooks.metric('transport_changes', self.hooks.metric_value('transport_changes') + 1)

    def status(self, phase, message):
        if self.connection is not None:
            self.hooks.event('status', phase=phase, message=message)

    def valid(self, generation):
        return (
            (
                self.active
                or generation == self.text_generation
                or generation == self.hooks.announcements().generation
                or generation == self.hooks.visual().generation
            )
            and self.connection is not None
            and generation == self.generation
        )

    def touch_activity(self):
        self.last_interaction = time.monotonic()
        self.hooks.wake_touch()
        if self.screen_sleeping:
            self.screen_sleeping = False
            self.hooks.event('screen_wake')

    def idle_tick(self):
        self.hooks.reminder_tick()
        self.hooks.wake_tick()
        if (
            self.active
            and not self.hooks.calibration().active
            and not self.hooks.playback().playing
            and not self.hooks.response_busy()
            and not self.hooks.recognition_busy()
            and not self.screen_sleeping
            and time.monotonic() - self.last_interaction >= 900
        ):
            self.set_awake(False)
            self.screen_sleeping = True
            self.hooks.event('screen_sleep')
            self.hooks.log('screen_idle_sleep')

    def submit_text(self, text):
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 6000
            or any(ord(c) < 32 and c not in '\n\t' for c in text)
        ):
            raise ValueError('Text must contain 1–6000 characters')
        with self.guard:
            if self.connection is None or self.hooks.calibration().active:
                raise RuntimeError('Text request unavailable')
            generation = self.hooks.cancel('text_request')
            self.text_generation = generation
            self.hooks.touch_activity()
            self.hooks.diagnostics().begin('text', generation)
            self.hooks.event('text_begin', generation=generation, text=text.strip())
            self.hooks.enqueue_response(TextReply(generation, text.strip()))
            return {'ok': True, 'generation': generation}

    def cancel(self, reason='speech'):
        # 推进代次使旧模型、音频、字幕和看图结果失效，但不能撤销已执行的工具动作。
        with self.guard:
            # A failed calibration output has no successor worker; unlike a
            # transport handover, it must not leave capture stuck calibrating.
            if reason in ('audio_stream_error', 'completion_timeout', 'playback_error'):
                self.hooks.calibration().stop()
            self.hooks.cancel_observation()
            self.hooks.diagnostics().finish(self.generation, 'cancelled')
            workspace_changed = self.hooks.workspace().cancel_generation(self.generation)
            self.generation += 1
            generation = self.generation
            was_playing = self.hooks.playback().stop_output()
            self.hooks.announcements().invalidate()
            self.text_generation = None
            self.hooks.visual().invalidate()
            self.hooks.playback().clear_cancelled_pause(was_playing)
            self.hooks.wake_reset('cancel')
        # Wake local waiters independently of provider/transport cancellation.
        self.hooks.playback().notify()
        if self.hooks.brain():
            self.hooks.brain().cancel()
        cancel_speech = getattr(self.hooks.speech(), 'cancel', None)
        if cancel_speech:
            cancel_speech()
        try:
            if self.connection is not None:
                self.hooks.event('cancel', generation=generation)
            if workspace_changed:
                self.hooks.publish_workspace()
        except (OSError, EOFError, ValueError) as error:
            # Local invalidation already succeeded; a broken notification must
            # never prevent Stop from releasing capture or block an old waiter.
            self.hooks.log('cancel_notification_failed', error_type=type(error).__name__)
        if was_playing:
            self.hooks.metric('interruptions', self.hooks.metric_value('interruptions') + 1)
            self.hooks.log('playback_cancelled', reason=reason, generation=generation)
        return generation

    def stop(self):
        self.active = False
        self.hooks.calibration().stop()
        self.screen_sleeping = False
        try:
            self.hooks.cancel('session_stop')
        finally:
            try:
                self.hooks.capture().close()
            finally:
                self.hooks.save_status()

    def set_awake(self, value):
        with self.guard:
            self.awake = value
            self.hooks.awake_changed(value)

    def finish_text_response(self, generation):
        with self.guard:
            if self.text_generation == generation:
                self.text_generation = None
                if self.connection is not None:
                    self.hooks.event('text_done', generation=generation)

    def ready(self, j):
        self.hooks.visual().update_peer(j)
        self.hooks.playback().set_pause_capability(bool(j.get('supports_playback_pause')))
        self.hooks.playback().set_playout_capability(
            type(j.get('playout_receipt_version')) is int and j['playout_receipt_version'] == 1
        )
        self.hooks.playback().set_completion_capability(
            type(j.get('playback_done_id_version')) is int and j['playback_done_id_version'] == 1
        )
        self.hooks.playback().set_clock_report_capability(
            type(j.get('render_clock_report_version')) is int
            and j['render_clock_report_version'] == 1
        )
        if self.active and not j.get('capture_active', False):
            self.hooks.stop()
        if 'wake_mode' in j and bool(j['wake_mode']) != self.wake:
            with self.guard:
                self.hooks.cancel('wake_mode_change')
                self.wake = bool(j['wake_mode'])
                self.awake = not self.wake
                self.hooks.wake_reset('mode')
        self.hooks.event(
            'hello',
            message='我在听'
            if self.active
            else '回声自检已准备 · 点击开始语音后保持安静'
            if self.hooks.calibration().pending
            else '大黄蜂已就绪 · 点击开始语音',
            transport=self.transport_kind,
            supports_dot_chat_handoff=True,
            supports_saved_task_results=True,
            asr_engine=getattr(
                getattr(self.hooks.speech(), 'recognizer', None), 'engine', 'sensevoice'
            ),
        )
        self.hooks.log('device_handshake_verified', transport=self.transport_kind)
        self.hooks.event(
            'brain_provider', provider=getattr(self.hooks.brain(), 'selected', 'minimax')
        )
        self.hooks.publish_workspace()
        self.hooks.save_status()

    def rebind(self, j):
        if self.rebind_seen:
            return
        self.rebind_seen = True
        if self.active and int(j.get('epoch', -1)) == self.epoch:
            if self.hooks.calibration().active:
                self.hooks.capture().echo.clear_calibration_frames()
                threading.Thread(
                    target=self.hooks.calibrate(), args=(self.generation,), daemon=True
                ).start()
            else:
                self.hooks.status('listening', '连接已切换，我在听')
        else:
            self.hooks.route_event({**j, 'event': 'session_start'})

    def settings(self, j):
        if 'wake_mode' in j and bool(j['wake_mode']) != self.wake:
            with self.guard:
                self.hooks.cancel('wake_mode_change')
                self.wake = bool(j['wake_mode'])
                self.awake = not self.wake
                self.hooks.wake_reset('mode')
            if self.active and not self.hooks.calibration().active:
                self.hooks.status('listening', WAITING if self.wake else '我在听，可以开始说话')
        if 'brain_provider' in j and hasattr(self.hooks.brain(), 'select'):
            provider = j['brain_provider']
            if provider in ('minimax', 'dot') and not self.hooks.calibration().active:
                self.hooks.cancel('brain_change')
                self.hooks.brain().select(provider)
                self.hooks.event('brain_provider', provider=provider)
                self.hooks.status(
                    'listening' if self.active else 'idle',
                    '已选择 Dot / Codex · 思考与执行'
                    if provider == 'dot'
                    else '已选择 MiniMax · 日常对话与联网',
                )
        self.hooks.save_status()

    def start_capture(self, j):
        self.rebind_seen = True
        # A route rebind can arrive before the capture worker's first start.
        # Both describe the same native recording epoch; never restart it.
        if self.active and int(j['epoch']) == self.epoch:
            return
        if self.hooks.visual().generation is not None or self.text_generation is not None:
            self.hooks.cancel('microphone_start')
        if self.active or self.hooks.announcements().generation is not None:
            self.hooks.stop()
        self.epoch = int(j['epoch'])
        self.active = True
        self.generation += 1
        self.hooks.capture().reset_session()
        self.hooks.playback().reset_session()
        self.hooks.capture().echo.open()
        self.hooks.calibration().begin()
        self.awake = not self.wake
        self.screen_sleeping = False
        self.last_interaction = time.monotonic()
        self.hooks.wake_reset('start')
        for k in [
            'mic_frames',
            'render_frames',
            'sequence_gaps',
            'interruptions',
            'utterances',
            'fast_stop_count',
            'echo_transcripts_rejected',
            'weak_near_rejections',
        ]:
            self.hooks.metric(k, 0)
        generation = self.generation
        self.hooks.prepare_brain(generation)
        threading.Thread(target=self.hooks.calibrate(), args=(generation,), daemon=True).start()
        self.hooks.log('foreground_capture_started')

    def stop_capture(self, j):
        if self.active and int(j.get('epoch', self.epoch)) == self.epoch:
            self.hooks.stop()
            self.hooks.log('foreground_capture_stopped')

    def interrupt(self, j):
        if (
            self.active
            or self.text_generation is not None
            or self.hooks.announcements().generation is not None
            or self.hooks.visual().generation is not None
        ):
            self.hooks.cancel('button')
            self.hooks.status('listening' if self.active else 'idle', '播报已停止')

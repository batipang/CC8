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
    voice_busy: Callable[[], bool]
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
        self.capture_start_required = False
        self.capture_rejection_supported = False
        self.rejected_capture_id = None
        self.playback_interrupted_by_transport = False
        self.write_lock = threading.RLock()
        self.guard = threading.RLock()
        self.active = False
        self.epoch = 0
        self.generation = 100
        self.wake = False
        self.awake = True
        self.last_interaction = time.monotonic()
        self.screen_sleeping = False
        self.screen_wake_pending = False

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
        packet = Packet.event(
            event, self.generation if generation is None else generation, **fields
        )
        with self.write_lock:
            # Match the speaker send boundary. A route change or cancellation
            # may have advanced the generation while this event was queued.
            if generation is None or generation >= self.generation:
                self.hooks.send(packet)

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
        self.capture_rejection_supported = False
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
        with self.guard:
            self.last_interaction = time.monotonic()
            self.hooks.wake_touch()
            if self.screen_sleeping:
                self.screen_sleeping = False
                self.screen_wake_pending = True
            self._flush_screen_wake()

    def _flush_screen_wake(self):
        if self.screen_wake_pending and self.connection is not None:
            self.hooks.event('screen_wake')
            self.screen_wake_pending = False

    def idle_tick(self):
        self.hooks.reminder_tick()
        self.hooks.wake_tick()
        # Commit the state and device event under the same guard as a local
        # wake or touch, so a stale idle decision cannot undo fresh activity.
        with self.guard:
            if (
                self.active
                and not self.screen_sleeping
                and time.monotonic() - self.last_interaction >= 900
                and not self.hooks.voice_busy()
            ):
                self.set_awake(False)
                self.screen_sleeping = True
                self.screen_wake_pending = False
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
            workspace_changed = self.hooks.workspace().cancel_generation(
                self.generation, unconfirmed=reason == 'transport_switch'
            )
            self.generation += 1
            generation = self.generation
            was_playing = self.hooks.playback().stop_output()
            if reason == 'transport_switch' and was_playing and not self.hooks.calibration().active:
                self.playback_interrupted_by_transport = True
            self.hooks.announcements().invalidate()
            self.text_generation = None
            self.hooks.visual().invalidate()
            self.hooks.playback().clear_cancelled_pause(was_playing)
            self.hooks.wake_reset('cancel')
        # Wake local waiters independently of provider/transport cancellation.
        self.hooks.playback().notify()
        try:
            try:
                if brain := self.hooks.brain():
                    brain.cancel()
            finally:
                cancel_speech = getattr(self.hooks.speech(), 'cancel', None)
                if cancel_speech:
                    cancel_speech()
        finally:
            # Only CC8 can discard PCM already queued on its side.
            try:
                if self.connection is not None:
                    self.hooks.event('cancel', generation=generation)
                if workspace_changed:
                    self.hooks.publish_workspace()
            except (OSError, EOFError, ValueError) as error:
                # A broken notification must still allow local resource cleanup.
                self.hooks.log('cancel_notification_failed', error_type=type(error).__name__)
            if was_playing:
                self.hooks.metric('interruptions', self.hooks.metric_value('interruptions') + 1)
                self.hooks.log('playback_cancelled', reason=reason, generation=generation)
        return generation

    def stop(self):
        self.capture_start_required = True
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
            if self.active and self.awake and not value:
                self.hooks.capture().discard_buffered_endpoint_audio()
            self.awake = value
            self.hooks.awake_changed(value)

    def finish_text_response(self, generation):
        with self.guard:
            if self.text_generation == generation:
                self.text_generation = None
                if self.connection is not None:
                    self.hooks.event('text_done', generation=generation)

    def ready(self, j):
        self.capture_rejection_supported = (
            type(j.get('capture_reject_version')) is int and j['capture_reject_version'] == 1
        )
        self.hooks.visual().update_peer(j)
        self.hooks.playback().set_pause_capability(
            bool(j.get('supports_playback_pause')),
            applied_receipt=type(j.get('playback_pause_applied_version')) is int
            and j['playback_pause_applied_version'] == 1,
        )
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
        self.update_wake_mode(bool(j.get('wake_mode', self.wake)))
        interrupted = self.playback_interrupted_by_transport
        if interrupted:
            message = (
                '播报因连接切换中断。已完成的结果可在任务页查看。'
                '如需继续，可以在语音开启后重新提问；执行结果不明的操作请先核对。'
            )
        elif self.active:
            message = '我在听' if self.awake else WAITING
        elif self.hooks.calibration().pending:
            message = '回声自检已准备 · 点击开始语音后保持安静'
        else:
            message = '大黄蜂已就绪 · 点击开始语音'
        self.hooks.event(
            'hello',
            message=message,
            transport=self.transport_kind,
            playback_interrupted_by_transport=interrupted,
            supports_dot_chat_handoff=True,
            supports_saved_task_results=True,
            asr_engine=getattr(
                getattr(self.hooks.speech(), 'recognizer', None), 'engine', 'sensevoice'
            ),
        )
        # A failed send leaves the notice for the next authenticated route.
        self.playback_interrupted_by_transport = False
        with self.guard:
            # Replay only an unsent wake, without changing capture ownership.
            self._flush_screen_wake()
        self.hooks.log('device_handshake_verified', transport=self.transport_kind)
        self.hooks.event(
            'brain_provider', provider=getattr(self.hooks.brain(), 'selected', 'minimax')
        )
        self.hooks.publish_workspace()
        self.hooks.save_status()

    def resend_capture_rejection(self, capture_id):
        if not self.capture_rejection_supported:
            return False
        if capture_id is None:
            raise ValueError('Missing capture ID on supporting device')
        if capture_id != self.rejected_capture_id:
            return False
        self.hooks.event('capture_rejected', capture_id=capture_id)
        return True

    def rebind(self, j):
        if self.resend_capture_rejection(j.get('capture_id')):
            return
        # A rebind can recover existing capture, but cannot undo a stop.
        if self.rebind_seen or self.capture_start_required:
            return
        self.rebind_seen = True
        if self.active and int(j.get('epoch', -1)) == self.epoch:
            if self.hooks.calibration().active:
                self.hooks.capture().echo.clear_calibration_frames()
                threading.Thread(
                    target=self.hooks.calibrate(), args=(self.generation,), daemon=True
                ).start()
            else:
                self.hooks.status('listening', '连接已切换，我在听' if self.awake else WAITING)
        else:
            self.hooks.route_event({**j, 'event': 'session_start'})

    def update_wake_mode(self, value):
        with self.guard:
            if value == self.wake:
                return False
            self.hooks.cancel('wake_mode_change')
            if self.active and value:
                self.hooks.capture().discard_buffered_endpoint_audio()
            self.wake = value
            self.awake = not value
            self.hooks.wake_reset('mode')
            return True

    def settings(self, j):
        if self.update_wake_mode(bool(j.get('wake_mode', self.wake))):
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
        capture_id = j.get('capture_id')
        if self.resend_capture_rejection(capture_id):
            return
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
        self.capture_start_required = True
        self.generation += 1
        self.hooks.capture().reset_session()
        self.hooks.playback().reset_session()
        try:
            self.hooks.capture().echo.open()
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            if not self.capture_rejection_supported:
                if isinstance(error, ImportError):
                    raise RuntimeError('Local echo processor unavailable') from None
                raise
            self.rejected_capture_id = capture_id
            self.hooks.log('capture_initialization_failed', error_type=type(error).__name__)
            self.hooks.save_status()
            # Keep the authenticated link on a local processor failure. If
            # sending fails, a rebind can resend to this same recording only.
            self.resend_capture_rejection(capture_id)
            return
        self.capture_start_required = False
        self.active = True
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
        if not self.active:
            # A native capture worker can stop before its Start reaches us.
            self.capture_start_required = True
        elif int(j.get('epoch', self.epoch)) == self.epoch:
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

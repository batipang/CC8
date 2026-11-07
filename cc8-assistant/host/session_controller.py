"""Own one foreground capture session, generations, wake state and connection.

Generation reads intentionally do not acquire the session lock: playback ACKs
may arrive while the completion condition is held. Cancellation follows the
existing session-lock then component-lock order, never the reverse.
"""

from __future__ import annotations
from collections import deque
import threading
import time
import uuid
from protocol import Packet, SPEAKER
from reply_types import TextReply
from session_ports import SessionHooks, SessionView
from device_session import (
    apply_settings,
    complete_handshake,
    rebind_capture,
    start_capture,
    stop_capture,
)
from tool_sample_gate import ToolSampleGate
from capture_frontend import requested_frontend, accepts_frontend


class SessionController:
    def __init__(self, hooks: SessionHooks, *, capture_frontend='mono'):
        if capture_frontend not in ('mono', 'paired'):
            raise ValueError('Unsupported capture frontend preference')
        self.capture_frontend_preference = capture_frontend
        self.capture_frontend_requested = 'mono'
        self.capture_frontend = 'mono'
        self.hooks = hooks
        self.text_generation = None
        self.connection = None
        self.transport_kind = 'none'
        self.rebind_seen = False
        self.capture_start_required = False
        self.capture_rejection_supported = False
        self.rejected_capture_id = None
        self.capture_start_ack = False
        self.capture_start_token = uuid.uuid4().hex
        self.capture_id = None
        self.tool_samples = ToolSampleGate()
        # One device can only retry its current explicitly requested capture.
        # Remember recent consumed IDs across route loss and failed native starts.
        self.consumed_capture_ids = deque(maxlen=64)
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
        self.pending_screen_event = None

    def snapshot(self) -> SessionView:
        return SessionView(
            self.connection is not None,
            self.active,
            self.generation,
            self.transport_kind,
            self.wake,
            self.screen_sleeping,
            self.awake,
            self.capture_frontend,
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
        # Admission must see the route unconfirmed before cancellation advances
        # the generation, including while cancellation notifies the old route.
        with self.guard:
            self.rebind_seen = False
            self.capture_rejection_supported = False
            self.capture_start_ack = False
            self.capture_frontend_requested = 'mono'
        try:
            self.hooks.cancel('transport_switch')
        except (OSError, EOFError):
            pass
        self.connection = channel
        self.hooks.visual().clear_challenge()
        self.transport_kind = channel.kind if channel else 'none'
        self.hooks.capture().reset_transport(self.transport_kind)
        self.hooks.wake_reset('transport')
        self.hooks.playback().clear_transport_pause()
        if channel is None:
            self.stop(transport_lost=True)
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
                self.pending_screen_event = 'screen_wake'
            self.flush_screen_event()

    def flush_screen_event(self):
        """Publish the latest screen update with the session guard held."""
        event = self.pending_screen_event
        if event is not None and self.connection is not None:
            # A failed write invalidates the route. Keep the latest screen
            # intent for the next ready route until this send succeeds.
            self.hooks.event(event)
            self.pending_screen_event = None
            if event == 'screen_sleep':
                self.hooks.log('screen_idle_sleep')

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
                self.pending_screen_event = 'screen_sleep'
                self.flush_screen_event()

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
            self.tool_samples.advance_generation(generation, reason)
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

    def stop(self, *, transport_lost=False):
        with self.guard:
            self.capture_start_required = True
            self.active = False
            self.capture_frontend = 'mono'
            self.capture_id = None
            pending_sample = self.tool_samples.stop_capture()
            self.screen_sleeping = False
            # A wake can restore the display after disconnect. Sleep belongs
            # to the capture that just ended.
            if self.pending_screen_event == 'screen_sleep':
                self.pending_screen_event = None
        self.hooks.calibration().stop()
        try:
            reason = 'transport_disconnect' if transport_lost and pending_sample else 'session_stop'
            self.hooks.cancel(reason)
        finally:
            try:
                self.hooks.capture().close()
            finally:
                self.hooks.save_status()

    def negotiate_capture_frontend(self, peer):
        self.capture_frontend_requested = requested_frontend(peer, self.capture_frontend_preference)
        return self.capture_frontend_requested

    def accepts_capture_frontend(self, event):
        actual = event.get('capture_frontend', 'mono')
        if not accepts_frontend(event, self.capture_frontend_requested):
            return False
        # Read live capture ownership after format validation, as before.
        return not (
            self.active and event.get('epoch') == self.epoch and actual != self.capture_frontend
        )

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
        complete_handshake(self, j)

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
        rebind_capture(self, j)

    def reject_capture_start(self, capture_id):
        if self.capture_rejection_supported and isinstance(capture_id, str):
            self.hooks.event('capture_rejected', capture_id=capture_id)

    def capture_claim_matches(self, j):
        return (
            self.capture_start_ack
            and self.active
            and j.get('capture_id') == self.capture_id
            and self.capture_id is not None
            and j.get('capture_start_token') == self.capture_start_token
            and j.get('epoch') == self.epoch
        )

    def confirm_capture_start(self):
        self.hooks.event('capture_started', capture_id=self.capture_id, epoch=self.epoch)

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
        apply_settings(self, j)

    def arm_tool_sample(self, case, *, compare_engine=None, diagnose_engine=None):
        return self.tool_samples.arm(
            self, case, compare_engine=compare_engine, diagnose_engine=diagnose_engine
        )

    def begin_tool_sample(self, token):
        return self.tool_samples.begin(self, token)

    def release_tool_sample(self, token):
        return self.tool_samples.release(self, token)

    def tool_sample_rejection(self, text, local_wake, captured_at):
        return self.tool_samples.rejection(self, text, local_wake, captured_at)

    def tool_sample_comparison(self, reason='comparison_only'):
        return self.tool_samples.comparison(self, reason)

    def start_capture(self, j):
        start_capture(self, j)

    def stop_capture(self, j):
        stop_capture(self, j)

    def interrupt(self, j):
        if (
            self.active
            or self.text_generation is not None
            or self.hooks.announcements().generation is not None
            or self.hooks.visual().generation is not None
        ):
            self.hooks.cancel('button')
            self.hooks.status('listening' if self.active else 'idle', '播报已停止')

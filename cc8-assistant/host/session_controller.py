"""Own one foreground capture session, generations, wake state and connection.

Generation reads intentionally do not acquire the session lock: playback ACKs
may arrive while the completion condition is held. Cancellation follows the
existing session-lock then component-lock order, never the reverse.
"""

from __future__ import annotations
from collections import deque
from dataclasses import dataclass
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
from capture_evidence import TOOL_TRANSCRIPT_MISMATCHES

from qa_speech import (
    SCENARIO_QUESTIONS,
    TOOL_COMPARE_ENGINES,
    normalize_question,
    tool_sample_request,
    tool_transcript_rejection,
)


@dataclass
class ToolSample:
    token: str
    expected: str
    generation: int
    arm_until: float
    comparison: tuple[int, str] | None = None
    rejection_diagnosis: tuple[int, str] | None = None
    capture_id: str | None = None
    state: str = 'pending'
    opened_at: float = 0.0
    closed_reason: str = 'not_open'


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
        self.capture_start_ack = False
        self.capture_start_token = uuid.uuid4().hex
        self.capture_id = None
        self.tool_sample: ToolSample | None = None
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
            sample = self.tool_sample
            if sample is not None:
                if sample.state == 'pending':
                    if reason in ('transport_switch', 'transport_disconnect') and (
                        sample.generation == generation
                    ):
                        # An Activity reopen may reconnect both routes before Start.
                        sample.generation = generation + 1
                elif (
                    reason == 'transport_switch'
                    and sample.state == 'bound'
                    and sample.generation == generation - 1
                ):
                    # No stimulus is authorized yet. Begin still needs a valid
                    # rebind of this exact capture on the authenticated USB route.
                    sample.generation = generation
                elif reason == 'voice' and sample.state == 'open':
                    # The endpoint may advance a live turn when VAD begins.
                    sample.generation = generation
                elif sample.state != 'closed':
                    sample.state = 'closed'
                    sample.closed_reason = (
                        'route_changed' if reason == 'transport_switch' else 'generation_changed'
                    )
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
            self.capture_id = None
            # An idle stop may race the already requested QA capture start.
            # Keep that reservation so the late start cannot bypass its gate.
            pending_sample = self.tool_sample is not None and self.tool_sample.state == 'pending'
            if not pending_sample:
                self.tool_sample = None
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
        if not tool_sample_request({'operation': 'tool_sample_arm', 'case': case}):
            raise ValueError('Invalid tool sample case')
        engines = [engine for engine in (compare_engine, diagnose_engine) if engine is not None]
        if len(engines) > 1 or any(engine not in TOOL_COMPARE_ENGINES for engine in engines):
            raise ValueError('Invalid tool comparison engine')
        with self.guard:
            existing = self.tool_sample
            now = time.monotonic()
            if (
                self.active
                or self.connection is None
                or self.transport_kind != 'usb'
                or not self.capture_start_ack
                or not self.wake
                or self.hooks.voice_busy()
                or (existing is not None and now < existing.arm_until)
            ):
                raise RuntimeError('Tool sample requires an idle USB session')
            token = uuid.uuid4().hex
            self.tool_sample = ToolSample(
                token,
                normalize_question(SCENARIO_QUESTIONS['tool'][case - 1]),
                self.generation + 1,
                now + 30,
                comparison=(case, compare_engine) if compare_engine is not None else None,
                rejection_diagnosis=(case, diagnose_engine)
                if diagnose_engine is not None
                else None,
            )
            return {'status': 'armed', 'token': token}

    def begin_tool_sample(self, token):
        with self.guard:
            sample = self.tool_sample
            if (
                sample is None
                or sample.token != token
                or sample.state != 'bound'
                or not self.active
                or sample.capture_id != self.capture_id
                or sample.generation != self.generation
                or self.transport_kind != 'usb'
                or not self.capture_start_ack
                or not self.rebind_seen
                or time.monotonic() >= sample.arm_until
                or not self.wake
                or self.awake
                or self.hooks.calibration().active
                or self.hooks.voice_busy()
            ):
                return {'status': 'unavailable'}
            sample.opened_at = time.monotonic()
            sample.state = 'open'
            return {'status': 'ready'}

    def release_tool_sample(self, token):
        with self.guard:
            sample = self.tool_sample
            # Only capture teardown can remove a guard already bound to it.
            if sample is not None and sample.token == token and sample.state == 'pending':
                self.tool_sample = None
            return {'status': 'released' if self.tool_sample is None else 'unavailable'}

    def tool_sample_rejection(self, text, local_wake, captured_at):
        # Called at the final ASR admission boundary under the session guard.
        sample = self.tool_sample
        if sample is None:
            return None
        if sample.state != 'open':
            return sample.closed_reason
        sample.state = 'closed'
        if not self.active or sample.capture_id != self.capture_id:
            return 'capture_changed'
        if sample.generation != self.generation:
            return 'generation_changed'
        if self.transport_kind != 'usb' or not self.capture_start_ack or not self.rebind_seen:
            return 'route_changed'
        if not local_wake:
            return 'local_wake_missing'
        if not (
            type(captured_at) in (int, float)
            and sample.opened_at <= captured_at <= sample.opened_at + 20
        ):
            return 'capture_window'
        if sample.comparison is not None:
            return 'comparison_only'
        return tool_transcript_rejection(text, sample.expected)

    def tool_sample_comparison(self, reason='comparison_only'):
        sample = self.tool_sample
        if sample is None:
            return None
        if reason == 'comparison_only':
            return sample.comparison
        if (
            reason in TOOL_TRANSCRIPT_MISMATCHES
            and sample.state == 'closed'
            and self.active
            and sample.capture_id == self.capture_id
            and sample.generation == self.generation
            and self.transport_kind == 'usb'
            and self.capture_start_ack
            and self.rebind_seen
        ):
            return sample.rejection_diagnosis
        return None

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

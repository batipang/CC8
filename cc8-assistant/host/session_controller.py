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
import re
from protocol import Packet
from reply_types import TextReply, ResultRead, CorrectedReply
from saved_tasks import ident as result_ident
from request_text import validate_request_text
from diagnostics import error_kind
from failure_feedback import feedback
from session_ports import SessionHooks, SessionView
from device_session import (
    apply_settings,
    complete_handshake,
    rebind_capture,
    start_capture,
    stop_capture,
)
from tool_sample_gate import ToolSampleGate
from voice_preferences import VoicePreferences
from capture_frontend import requested_frontend, accepts_frontend
from route_output import bind_host_packet, host_packet_current, same_route, screen_route_current


class SessionController:
    def __init__(self, hooks: SessionHooks, *, capture_frontend='mono'):
        if capture_frontend not in ('mono', 'paired'):
            raise ValueError('Unsupported capture frontend preference')
        self.capture_frontend_preference = capture_frontend
        self.capture_frontend_requested = 'mono'
        self.capture_frontend = 'mono'
        self.hooks = hooks
        self.text_generation = None
        self.connection, self.bound_route_token = None, None
        self.transport_kind = 'none'
        self.rebind_seen = False
        self.capture_route_pending = False
        self.capture_start_required = False
        self.capture_rejection_supported = False
        self.correction_ui_supported = False
        self.rejected_capture_id = None
        self.capture_start_ack = False
        self.capture_start_token = uuid.uuid4().hex
        self.capture_id = None
        self.tool_samples = ToolSampleGate()
        self.voice_preferences = VoicePreferences()
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
        self.screen_synced_owner: tuple[object | None, str | None] = (None, None)

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
            self.text_generation is not None,
        )

    def deactivate(self):
        self.active = False

    def unbind(self):
        self.connection = None

    def send(self, p):
        owner = self.connection, self.bound_route_token
        p = bind_host_packet(p, owner, self.generation)
        with self.write_lock:
            conn = owner[0]
            if conn is None:
                raise EOFError('No device connection')
            if not same_route(
                owner, self.connection, self.bound_route_token
            ) or not host_packet_current(p, owner, self.generation):
                return
            conn.sendall(p.encode())

    def event(self, event, generation=None, **fields):
        owner = self.connection, self.bound_route_token
        if generation is not None and generation < self.generation:
            return
        event_generation = self.generation if generation is None else generation
        packet = bind_host_packet(Packet.event(event, event_generation, **fields), owner)
        with self.write_lock:
            # Match the speaker send boundary. A route change or cancellation
            # may have advanced the generation while this event was queued.
            if same_route(owner, self.connection, self.bound_route_token) and (
                generation is None or generation >= self.generation
            ):
                self.hooks.send(packet)

    def change_transport(self, channel):
        # 先作废旧回答并重置参考时钟；只可 rebind 已有录音，重连不能自行开麦。
        old = self.connection
        route_token = getattr(channel, 'route_token', None)
        if old is channel and route_token == self.bound_route_token:
            return
        was_active = self.active
        # Admission must see the route unconfirmed before cancellation advances
        # the generation, including while cancellation notifies the old route.
        with self.guard:
            self.rebind_seen = False
            self.capture_route_pending = True
            self.capture_rejection_supported = False
            self.correction_ui_supported = False
            self.capture_start_ack = False
            self.capture_frontend_requested = 'mono'
        try:
            self.hooks.cancel('transport_switch')
        except (OSError, EOFError):
            pass
        with self.write_lock:
            self.connection, self.bound_route_token = channel, route_token
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
        if phase != 'listening':
            if self.connection is not None:
                self.hooks.event('status', phase=phase, message=message)
            return
        with self.guard:
            if self.connection is not None:
                # Prose is not a protocol state. Publish this capture snapshot
                # atomically without changing other phases' lock contract.
                state = 'unknown' if not self.active else 'awake' if self.awake else 'waiting'
                self.hooks.event(
                    'status',
                    phase=phase,
                    message=message,
                    listening_state=state,
                    capture_id=self.capture_id if self.active else None,
                    epoch=self.epoch,
                )

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
        owner = self.connection, self.bound_route_token
        if event is not None and screen_route_current(
            owner, self.connection, self.bound_route_token
        ):
            # A failed write invalidates the route. Keep the latest screen
            # intent for the next ready route until this send succeeds.
            self.hooks.event(event)
            if screen_route_current(owner, self.connection, self.bound_route_token):
                self.pending_screen_event = None
                self.screen_synced_owner = owner
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

    def submit_text(
        self,
        text,
        *,
        result_id=None,
        result_restart=False,
        corrected=False,
        _job_factory=None,
        _workflow_nonce='',
    ):
        text = validate_request_text(text, 6000)
        if type(corrected) is not bool or corrected and result_id is not None:
            raise ValueError('Invalid corrected input mode')
        if _job_factory is not None and (
            not callable(_job_factory) or corrected or result_id is not None
        ):
            raise ValueError('Invalid trusted text work factory')
        if (
            not isinstance(_workflow_nonce, str)
            or _workflow_nonce
            and (_job_factory is None or not re.fullmatch('[a-f0-9]{32}', _workflow_nonce))
        ):
            raise ValueError('Invalid workflow camera confirmation')
        if result_id is not None:
            result_ident(result_id)
            if type(result_restart) is not bool:
                raise ValueError('Invalid result reading mode')
        with self.guard:
            if (
                self.connection is None
                or self.hooks.calibration().active
                or self.hooks.capture_reserved()
            ):
                raise RuntimeError('Text request unavailable')
            previous = self.generation
            try:
                generation = self.hooks.cancel('text_request')
            except Exception as error:
                if self.generation == previous + 1:
                    try:
                        self.hooks.diagnostics().begin('text', previous + 1)
                        self.hooks.diagnostics().failure(previous + 1, 'runtime', error)
                    except Exception as secondary:
                        self._log_text_submission_cleanup('diagnostics', secondary)
                self._text_start_failure(previous + 1, 'runtime', error)
                raise

            def current():
                return self.generation == generation and self.text_generation == generation

            def superseded():
                self.hooks.diagnostics().finish(generation, 'cancelled')
                return {'ok': False, 'generation': generation, 'status': 'superseded'}

            # Cancellation can synchronously publish into a replacement route
            # or admit newer work. Never overwrite that newer text owner.
            if self.generation != generation:
                return superseded()
            if self.connection is None:
                raise RuntimeError('Text request unavailable')
            self.text_generation = generation
            stage = 'runtime'
            try:
                self.hooks.diagnostics().begin('text', generation)
                stage = 'transport'
                self.hooks.touch_activity()
                if not current():
                    return superseded()
                self.hooks.event(
                    'text_begin',
                    generation=generation,
                    text=text,
                    **(
                        {'input_origin': 'workflow', 'workflow_camera_nonce': _workflow_nonce}
                        if _job_factory is not None
                        else {'input_origin': 'correction'}
                        if corrected
                        else {}
                    ),
                )
                if not current():
                    return superseded()
                if self.connection is None:
                    raise RuntimeError('Text request unavailable')
                stage = 'runtime'
                job = (
                    _job_factory(generation)
                    if _job_factory is not None
                    else (
                        (CorrectedReply if corrected else TextReply)(generation, text)
                        if result_id is None
                        else ResultRead(generation, text, result_id, result_restart)
                    )
                )
                if (
                    not isinstance(job, TextReply)
                    or job.generation != generation
                    or job.text != text
                ):
                    raise ValueError('Invalid trusted response job')
                if not current():
                    return superseded()
                self.hooks.enqueue_response(job)
                if not current():
                    return superseded()
            except Exception as error:
                if not current():
                    return superseded()
                self._abort_text_submission(generation, stage, error)
                raise
            return {'ok': True, 'generation': generation}

    def _abort_text_submission(self, generation, stage, error):
        # Caller holds the session guard. Clear first, then invalidate any
        # ambiguous queue admission. Diagnostic callbacks may admit newer work.
        if self.generation != generation or self.text_generation != generation:
            return
        self.text_generation = None
        try:
            self.hooks.diagnostics().failure(generation, stage, error)
        except Exception as secondary:
            self._log_text_submission_cleanup('diagnostics', secondary)
        if self.generation == generation:
            try:
                self.hooks.cancel('text_submit_failed')
            except Exception as secondary:
                self._log_text_submission_cleanup('cancellation', secondary)
        self._text_start_failure(generation + 1, stage, error)

    def _text_start_failure(self, generation, stage, error):
        # Cancellation increments once before provider/transport cleanup. Only
        # that successor can display this error; a new request owns later ones.
        if (
            self.generation != generation
            or self.text_generation is not None
            or self.connection is None
        ):
            return
        try:
            self.hooks.event(
                'status',
                generation=generation,
                phase='error',
                request_start_failed=True,
                message='本次文字指令未启动。' + feedback(stage, error_kind(error)).message,
            )
        except Exception as secondary:
            self._log_text_submission_cleanup('feedback', secondary)

    def _log_text_submission_cleanup(self, phase, error):
        # Optional diagnostics must not mask the original submission failure.
        try:
            self.hooks.log(
                'text_submission_cleanup_failed', phase=phase, error_type=type(error).__name__
            )
        except Exception:
            pass

    def cancel(self, reason='speech', *, expected_generation=None, expected_epoch=None):
        # 推进代次使旧模型、音频、字幕和看图结果失效，但不能撤销已执行的工具动作。
        with self.guard:
            if expected_generation is not None or expected_epoch is not None:
                if (
                    type(expected_generation) is not int
                    or type(expected_epoch) is not int
                    or expected_generation != self.generation
                    or expected_epoch != self.epoch
                ):
                    return None
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
            cleared_correction = False
            if reason not in ('voice', 'verified_voice'):
                # Erase the draft without display callbacks during cleanup.
                # The fenced cancel packet closes its UI after output stops;
                # stopping output alone may keep the recent original question.
                cleared_correction = self.hooks.discard_corrections(
                    reason in ('streaming_stop_keyword', 'button', 'correction_cancel')
                )
            self.tool_samples.advance_generation(generation, reason)
            was_playing = self.hooks.playback().stop_output()
            if reason == 'transport_switch' and was_playing and not self.hooks.calibration().active:
                self.playback_interrupted_by_transport = True
            self.hooks.announcements().invalidate()
            self.hooks.invalidate_workflows(reason)
            self.hooks.invalidate_meetings(reason)
            self.text_generation = None
            self.hooks.visual().invalidate(
                reason=reason, new_generation=generation, capture_owner=self.capture_owner()
            )
            self.hooks.playback().clear_cancelled_pause(was_playing)
            self.hooks.wake_reset('cancel')
        # Wake local waiters independently of provider/transport cancellation.
        self.hooks.playback().notify()
        try:
            # A later cancellation may already have admitted a newer request
            # while notify ran outside the guard. It owns provider cleanup now.
            # Serialize both providers with request admission so the old brain
            # cancellation cannot be followed by cancelling a new speech slot.
            with self.guard:
                if self.generation == generation:
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
                    self.hooks.event(
                        'cancel',
                        generation=generation,
                        **({'speech_correction_cleared': True} if cleared_correction else {}),
                    )
                    self.hooks.visual().publish_followup(generation)
                if workspace_changed:
                    self.hooks.publish_workspace()
            except (OSError, EOFError, ValueError) as error:
                # A broken notification must still allow local resource cleanup.
                self.hooks.log('cancel_notification_failed', error_type=type(error).__name__)
            if was_playing:
                self.hooks.metric('interruptions', self.hooks.metric_value('interruptions') + 1)
                self.hooks.log('playback_cancelled', reason=reason, generation=generation)
        return generation

    def capture_owner(self):
        if not self.active or self.connection is None:
            return None
        return (id(self.connection), self.bound_route_token, self.epoch, self.capture_id)

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

    def voice_preferences_state(self):
        supported = callable(getattr(self.hooks.speech(), 'set_voice_rate', None))
        speech = self.hooks.speech()
        recognition_supported = getattr(
            speech, 'recognition_language_supported', False
        ) is True and callable(getattr(speech, 'set_recognition_language', None))
        environment_supported = callable(
            getattr(self.hooks.capture().endpoint, 'set_environment', None)
        )
        return self.voice_preferences.snapshot(
            supported, recognition_supported, environment_supported
        )

    def set_environment_profile(self, request):
        # Share revision and the full idle gate with language/voice settings.
        # The local gate changes only future speech; model text stays original.
        with self.guard:
            preferences = self.voice_preferences
            ok = False
            if not self.voice_preferences_state()['environment_profiles_supported']:
                message = '当前后台不支持听音环境预设'
            elif request['revision'] != preferences.revision:
                message = '设置已变化，请重新打开听音环境'
            elif self.active or self.text_generation is not None or self.hooks.voice_busy():
                message = '请先暂停语音，并等待当前任务和播报结束，再修改听音环境'
            else:
                self.hooks.capture().endpoint.set_environment(request['profile'])
                preferences.environment_profile = request['profile']
                preferences.revision += 1
                # Publish diagnostics for the same applied gate while ownership
                # is held. Cache I/O failures are isolated by StatusReporter;
                # they cannot undo or misreport a successful live setting.
                self.hooks.save_status()
                ok = True
                message = '听音环境已应用 · 麦克风保持关闭'
            self.hooks.event(
                'voice_preferences',
                voice_preferences=self.voice_preferences_state(),
                ok=ok,
                message=message,
            )

    def set_recognition_language(self, request):
        # Share the voice-setting revision, session lock and full idle gate.
        # This changes only future utterances; it never captures or retries one.
        with self.guard:
            preferences = self.voice_preferences
            ok = False
            if not self.voice_preferences_state()['recognition_language_supported']:
                message = '当前识别提供方不支持语言提示'
            elif request['revision'] != preferences.revision:
                message = '设置已变化，请重新打开识别语言'
            elif self.active or self.text_generation is not None or self.hooks.voice_busy():
                message = '请先暂停语音，并等待当前任务和播报结束，再修改识别语言'
            else:
                self.hooks.speech().set_recognition_language(request['language'])
                preferences.recognition_language = request['language']
                preferences.revision += 1
                ok = True
                message = '识别语言已应用 · 麦克风保持关闭'
            self.hooks.event(
                'voice_preferences',
                voice_preferences=self.voice_preferences_state(),
                ok=ok,
                message=message,
            )

    def set_voice_preferences(self, request):
        # Serialize with text/vision/announcement admission, not just the device
        # event reader. A queued/stopped worker may still be releasing its TTS.
        with self.guard:
            preferences = self.voice_preferences
            ok = False
            if not self.voice_preferences_state()['supported']:
                message = '当前语音提供方不支持这些设置'
            elif request['revision'] != preferences.revision:
                message = '设置已变化，请重新打开声音设置'
            elif self.active or self.text_generation is not None or self.hooks.voice_busy():
                message = '请先暂停语音，并等待当前任务和播报结束，再修改声音设置'
            else:
                preferences.apply(request, self.hooks.speech())
                ok = True
                message = '声音设置已应用 · 麦克风保持关闭'
            self.hooks.event(
                'voice_preferences',
                voice_preferences=self.voice_preferences_state(),
                ok=ok,
                message=message,
            )

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

    def tool_sample_asr_rejection(self, local_wake, captured_at):
        return self.tool_samples.asr_rejection(self, local_wake, captured_at)

    def tool_sample_comparison(self, reason='comparison_only'):
        return self.tool_samples.comparison(self, reason)

    def claim_meeting_generation(self):
        """A confirmed exclusive meeting fences old idle cleanup without a voice Start.

        MeetingController holds this same guard and has checked all other audio
        pipelines idle. This only advances ownership; it starts no ordinary
        microphone, provider cancellation, calibration, output or voice session.
        """
        with self.guard:
            if (
                self.connection is None
                or self.active
                or self.text_generation is not None
                or self.hooks.calibration().active
                or self.hooks.announcements().generation is not None
                or self.hooks.visual().generation is not None
            ):
                raise RuntimeError('Meeting audio is not idle')
            self.generation += 1
            return self.generation

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

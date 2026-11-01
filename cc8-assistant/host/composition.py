"""Passive composition root: bind component ports, without mutating session state.

This is the only module that sees the complete Bridge for dependency wiring.
Components themselves receive frozen narrow hooks, never the Bridge. All
constructors are passive; Bridge publishes the bundle before starting workers.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING, Pattern
from brain_router import BrainRouter
from vision import desktop_signature
from calibration_controller import CalibrationController, CalibrationHooks
from vision_controller import VisionController, VisionHooks
from playback_controller import PlaybackController, PlaybackHooks
from capture_pipeline import CapturePipeline, CaptureHooks
from echo_pipeline import EchoPipeline, EchoHooks, EchoFactory
from endpoint_pipeline import EndpointPipeline, EndpointHooks
from recognition_worker import RecognitionWorker, RecognitionHooks
from response_worker import ResponseWorker, ResponseHooks
from fast_stop import FastStopDetector
from announcement_controller import AnnouncementController, AnnouncementHooks
from reminder_scheduler import ReminderScheduler, ReminderHooks

if TYPE_CHECKING:
    from bridge import Bridge


@dataclass(frozen=True)
class Components:
    calibration: CalibrationController
    visual: VisionController
    playback: PlaybackController
    capture: CapturePipeline
    recognition: RecognitionWorker
    response: ResponseWorker
    announcements: AnnouncementController
    reminders: ReminderScheduler


def build_components(
    session: Bridge,
    *,
    echo_factory: EchoFactory,
    wake_pattern: Pattern[str],
    fast_stop: FastStopDetector | None,
    echo_check: bool,
) -> Components:
    calibration = CalibrationController(
        CalibrationHooks(
            lock=session.guard,
            generation=lambda: session.generation,
            valid=lambda generation: session.valid(generation),
            wake=lambda: session.wake,
            speech=lambda: session.speech,
            clear_frames=lambda: session.capture.echo.clear_calibration_frames(),
            reset_activity=lambda: session.capture.endpoint.reset_voice_activity(),
            startup_reference=lambda: session.startup_reference(),
            play=lambda pcm, text, generation, role: session.play(pcm, text, generation, role),
            speak=lambda text, generation: session.speak(text, generation),
            align_reference=lambda generation: session.align_reference(generation),
            check_echo=lambda generation: session.check_echo(generation),
            recognition_pending=lambda: (
                session.recognition.busy or not session.recognition.queue.empty()
            ),
            talking=lambda: session.capture.endpoint.vad.talking,
            status=lambda phase, message: session.status(phase, message),
            save_status=lambda: session.save_status(),
            log=lambda name, **fields: session.log(name, **fields),
            failure=lambda message, error: session.failure(message, error),
            metric=session.record_metric,
            metric_value=lambda name: session.metrics.get(name, 0),
        ),
        echo_check=echo_check,
    )
    visual = VisionController(
        VisionHooks(
            lock=session.guard,
            connected=lambda: session.connection is not None,
            calibrating=lambda: session.calibration.active,
            has_transport=lambda: session.transport_hub is not None,
            brain_ready=lambda: isinstance(session.brain, BrainRouter),
            session_generation=lambda: session.generation,
            valid=lambda generation: session.valid(generation),
            cancel_session=lambda reason: session.cancel(reason),
            touch=lambda: session.touch_activity(),
            event=lambda name, **fields: session.event(name, **fields),
            status=lambda phase, message: session.status(phase, message),
            save_status=lambda: session.save_status(),
            authorize=lambda challenge, nonce, generation: desktop_signature(
                session.transport_hub.pairing.token, challenge, nonce, generation
            ),
            enqueue=lambda job: session.enqueue_response(job),
            answer=lambda text, generation, image: session.answer(text, generation, image=image),
            answer_completed=lambda: session.brain.last_metrics.get('completed'),
            metric=session.record_metric,
            log=lambda name, **fields: session.log(name, **fields),
        )
    )
    playback = PlaybackController(
        PlaybackHooks(
            lock=session.guard,
            generation=lambda: session.generation,
            active=lambda: session.active,
            announcement_generation=lambda: session.announcements.generation,
            valid=lambda generation: session.valid(generation),
            cancel_session=lambda reason: session.cancel(reason),
            event=lambda name, **fields: session.event(name, **fields),
            send=lambda packet: session.send(packet),
            status=lambda phase, message: session.status(phase, message),
            log=lambda name, **fields: session.log(name, **fields),
            metric=session.record_metric,
            diagnostics=lambda: session.diagnostics,
            trace=lambda: session.capture.endpoint.voice_trace,
            echo_metrics=lambda: dict(
                input_dbfs=session.metrics.get('input_dbfs'),
                residual_dbfs=session.metrics.get('output_dbfs'),
                raw_coherence=round(session.capture.endpoint.near_speech.correlation, 3),
                residual_coherence=round(
                    session.capture.endpoint.near_speech.residual_correlation, 3
                ),
            ),
            utterance_paused=lambda token: (
                session.capture.endpoint.vad.talking
                and session.capture.endpoint.turn_pause_token == token
            ),
            speech=lambda: session.speech,
            cancel_timeline_pause=lambda: session.capture.echo.cancel_pause(),
            pause_timeline=lambda nanos: session.capture.echo.pause_at(nanos),
            resume_timeline=lambda nanos: session.capture.echo.resume_at(nanos),
        )
    )
    capture = CapturePipeline(
        CaptureHooks(
            generation=lambda: session.generation,
            epoch=lambda: session.epoch,
            active=lambda: session.active,
            calibrating=lambda: session.calibration.active,
            diagnostics=lambda: session.diagnostics,
            metric=session.record_metric,
            add_metric=session.add_capture_metric,
            log=lambda name, **fields: session.log(name, **fields),
            save_status=lambda: session.save_status(),
        ),
        EchoPipeline(
            EchoHooks(
                lock=session.guard,
                active=lambda: session.active,
                valid=lambda generation: session.valid(generation),
                factory=lambda **kwargs: echo_factory(**kwargs),
                fast_stop_enabled=lambda: bool(session.capture.endpoint.fast_stop),
                metric=session.record_metric,
                log=lambda name, **fields: session.log(name, **fields),
            )
        ),
        EndpointPipeline(
            EndpointHooks(
                generation=lambda: session.generation,
                awake=lambda: session.awake,
                playing=lambda: session.playback.playing,
                last_audio_end=lambda: session.playback.last_audio_end,
                last_spoken=lambda: session.playback.last_spoken,
                previous_spoken=lambda: session.playback.previous_spoken,
                previous_spoken_until=lambda: session.playback.previous_spoken_until,
                recent_spoken=lambda: [text for _, text in tuple(session.playback.spoken)],
                pause_token=lambda: (
                    session.playback.pause_state['token'] if session.playback.pause_state else None
                ),
                paused=lambda: session.playback.pause_state is not None,
                pause_voice=lambda: session.pause_for_voice(),
                cancel_session=lambda reason: session.cancel(reason),
                event=lambda name, **fields: session.event(name, **fields),
                status=lambda phase, message: session.status(phase, message),
                log=lambda name, **fields: session.log(name, **fields),
                save_status=lambda: session.save_status(),
                metric=session.record_metric,
                add_metric=session.add_capture_metric,
                metric_value=lambda name: session.metrics[name],
                diagnostics=lambda: session.diagnostics,
                enqueue=lambda job: session.enqueue_recognition(job),
                discard_recognition=lambda: session.discard_recognition(),
            ),
            fast_stop,
        ),
    )
    recognition = RecognitionWorker(
        RecognitionHooks(
            valid=lambda generation: session.valid(generation),
            diagnostics=lambda: session.diagnostics,
            playing=lambda: session.playback.playing,
            last_audio_end=lambda: session.playback.last_audio_end,
            recent_spoken=lambda: [line for _, line in tuple(session.playback.spoken)],
            pause_state=lambda: session.playback.pause_state,
            speech=lambda: session.speech,
            metric=session.record_metric,
            set_metric_field=session.set_metric_field,
            add_metric_field=session.add_metric_field,
            record_echo_pair=session.record_echo_pair,
            echo_running=lambda: session.calibration.running,
            echo_failed=lambda: session.calibration.failed,
            awake=lambda: session.awake,
            wake=lambda: session.wake,
            set_awake=session.set_awake,
            wake_pattern=wake_pattern,
            touch=lambda: session.touch_activity(),
            event=lambda name, **fields: session.event(name, **fields),
            status=lambda phase, message: session.status(phase, message),
            log=lambda name, **fields: session.log(name, **fields),
            cancel_session=lambda reason: session.cancel(reason),
            begin_visual=lambda text, origin: session.begin_visual(text, origin),
            enqueue_response=lambda job: session.enqueue_response(job),
            failure=lambda message, error, **fields: session.failure(message, error, **fields),
            resume_rejected=lambda token, generation: session.resume_after_rejection(
                token, generation
            ),
        )
    )
    response = ResponseWorker(
        ResponseHooks(
            valid=lambda generation: session.valid(generation),
            diagnostics=lambda: session.diagnostics,
            workspace=lambda: session.workspace,
            brain=lambda: session.brain,
            speech=lambda: session.speech,
            active=lambda: session.active,
            metric=session.record_metric,
            dispatch=lambda job: session.respond_job(job),
            answer=lambda text, generation: session.answer(text, generation),
            answer_visual=lambda job: session.answer_visual(job),
            read_announcement=lambda job: session.read_announcement(job),
            finish_text=session.finish_text_response,
            publish_workspace=lambda: session.publish_workspace(),
            event=lambda name, **fields: session.event(name, **fields),
            status=lambda phase, message: session.status(phase, message),
            log=lambda name, **fields: session.log(name, **fields),
            failure=lambda message, error: session.failure(message, error),
            speak=lambda text, generation: session.speak(text, generation),
        )
    )
    announcements = AnnouncementController(
        AnnouncementHooks(
            lock=session.guard,
            connected=lambda: session.connection is not None,
            calibration_busy=lambda: session.calibration.active or session.calibration.running,
            cancel_session=lambda reason: session.cancel(reason),
            valid=lambda generation: session.valid(generation),
            offer=lambda job: session.response.offer(job),
            diagnostics=lambda: session.diagnostics,
            event=lambda name, **fields: session.event(name, **fields),
            status=lambda phase, message: session.status(phase, message),
            speak=lambda text, generation: session.speak(text, generation),
            failure=lambda message, error: session.failure(message, error),
            metric=session.record_metric,
            save_status=lambda: session.save_status(),
        )
    )
    reminders = ReminderScheduler(
        ReminderHooks(
            lock=session.guard,
            workspace=lambda: session.workspace,
            publish_workspace=lambda: session.publish_workspace(),
            can_deliver=lambda: (
                session.connection is not None
                and not session.calibration.active
                and not session.playback.playing
                and not session.response.busy
                and not session.recognition.busy
                and session.text_generation is None
                and session.visual.generation is None
                and session.announcements.generation is None
                and not session.capture.endpoint.vad.talking
                and session.response.queue.empty()
            ),
            touch=lambda: session.touch_activity(),
            announce=lambda text: session.announce(text),
        )
    )
    return Components(
        calibration, visual, playback, capture, recognition, response, announcements, reminders
    )

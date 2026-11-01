"""Passive composition root: bind component ports, without mutating session state.

This is the only module that sees the complete Bridge for dependency wiring.
Components themselves receive frozen narrow hooks, never the Bridge. All
constructors are passive; Bridge publishes the bundle before starting workers.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING, Pattern, Callable
from pathlib import Path
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
from session_controller import SessionController, SessionHooks
from status_reporter import StatusReporter, ReportingHooks
from announcement_controller import AnnouncementController, AnnouncementHooks
from reminder_scheduler import ReminderScheduler, ReminderHooks
from device_controller import DeviceController, DeviceControlHooks

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
    device: DeviceController
    reporting: StatusReporter


def build_components(
    host: Bridge,
    *,
    echo_factory: EchoFactory,
    wake_pattern: Pattern[str],
    fast_stop: FastStopDetector | None,
    echo_check: bool,
    started_at: float,
    private_directory: Callable[[], Path],
    wake_word: str,
    dot_runtime: Callable[[], dict[str, object]],
) -> Components:
    calibration = CalibrationController(
        CalibrationHooks(
            lock=host.session.guard,
            generation=lambda: host.session.generation,
            valid=lambda generation: host.valid(generation),
            wake=lambda: host.session.wake,
            speech=lambda: host.speech,
            clear_frames=lambda: host.capture.echo.clear_calibration_frames(),
            reset_activity=lambda: host.capture.endpoint.reset_voice_activity(),
            startup_reference=lambda: host.startup_reference(),
            play=lambda pcm, text, generation, role: host.play(pcm, text, generation, role),
            speak=lambda text, generation: host.speak(text, generation),
            align_reference=lambda generation: host.align_reference(generation),
            check_echo=lambda generation: host.check_echo(generation),
            recognition_pending=lambda: host.recognition.busy or not host.recognition.queue.empty(),
            talking=lambda: host.capture.endpoint.vad.talking,
            status=lambda phase, message: host.status(phase, message),
            save_status=lambda: host.save_status(),
            log=lambda name, **fields: host.log(name, **fields),
            failure=lambda message, error: host.failure(message, error),
            metric=host.record_metric,
            metric_value=lambda name: host.metrics.get(name, 0),
        ),
        echo_check=echo_check,
    )
    visual = VisionController(
        VisionHooks(
            lock=host.session.guard,
            connected=lambda: host.session.connection is not None,
            calibrating=lambda: host.calibration.active,
            has_transport=lambda: host.transport_hub is not None,
            brain_ready=lambda: isinstance(host.brain, BrainRouter),
            session_generation=lambda: host.session.generation,
            valid=lambda generation: host.valid(generation),
            cancel_session=lambda reason: host.cancel(reason),
            touch=lambda: host.touch_activity(),
            event=lambda name, **fields: host.event(name, **fields),
            status=lambda phase, message: host.status(phase, message),
            save_status=lambda: host.save_status(),
            authorize=lambda challenge, nonce, generation: desktop_signature(
                host.transport_hub.pairing.token, challenge, nonce, generation
            ),
            enqueue=lambda job: host.enqueue_response(job),
            answer=lambda text, generation, image: host.answer(text, generation, image=image),
            answer_completed=lambda: host.brain.last_metrics.get('completed'),
            metric=host.record_metric,
            log=lambda name, **fields: host.log(name, **fields),
        )
    )
    playback = PlaybackController(
        PlaybackHooks(
            lock=host.session.guard,
            generation=lambda: host.session.generation,
            active=lambda: host.session.active,
            announcement_generation=lambda: host.announcements.generation,
            valid=lambda generation: host.valid(generation),
            cancel_session=lambda reason: host.cancel(reason),
            event=lambda name, **fields: host.event(name, **fields),
            send=lambda packet: host.send(packet),
            status=lambda phase, message: host.status(phase, message),
            log=lambda name, **fields: host.log(name, **fields),
            metric=host.record_metric,
            diagnostics=lambda: host.diagnostics,
            trace=lambda: host.capture.endpoint.voice_trace,
            echo_metrics=lambda: dict(
                input_dbfs=host.metrics.get('input_dbfs'),
                residual_dbfs=host.metrics.get('output_dbfs'),
                raw_coherence=round(host.capture.endpoint.near_speech.correlation, 3),
                residual_coherence=round(host.capture.endpoint.near_speech.residual_correlation, 3),
            ),
            utterance_paused=lambda token: (
                host.capture.endpoint.vad.talking
                and host.capture.endpoint.turn_pause_token == token
            ),
            speech=lambda: host.speech,
            cancel_timeline_pause=lambda: host.capture.echo.cancel_pause(),
            pause_timeline=lambda nanos: host.capture.echo.pause_at(nanos),
            resume_timeline=lambda nanos: host.capture.echo.resume_at(nanos),
        )
    )
    capture = CapturePipeline(
        CaptureHooks(
            generation=lambda: host.session.generation,
            epoch=lambda: host.session.epoch,
            active=lambda: host.session.active,
            calibrating=lambda: host.calibration.active,
            diagnostics=lambda: host.diagnostics,
            metric=host.record_metric,
            add_metric=host.add_capture_metric,
            log=lambda name, **fields: host.log(name, **fields),
            save_status=lambda: host.save_status(),
        ),
        EchoPipeline(
            EchoHooks(
                lock=host.session.guard,
                active=lambda: host.session.active,
                valid=lambda generation: host.valid(generation),
                factory=lambda **kwargs: echo_factory(**kwargs),
                fast_stop_enabled=lambda: bool(host.capture.endpoint.fast_stop),
                metric=host.record_metric,
                log=lambda name, **fields: host.log(name, **fields),
            )
        ),
        EndpointPipeline(
            EndpointHooks(
                generation=lambda: host.session.generation,
                awake=lambda: host.session.awake,
                playing=lambda: host.playback.playing,
                last_audio_end=lambda: host.playback.last_audio_end,
                last_spoken=lambda: host.playback.last_spoken,
                previous_spoken=lambda: host.playback.previous_spoken,
                previous_spoken_until=lambda: host.playback.previous_spoken_until,
                recent_spoken=lambda: [text for _, text in tuple(host.playback.spoken)],
                pause_token=lambda: (
                    host.playback.pause_state['token'] if host.playback.pause_state else None
                ),
                paused=lambda: host.playback.pause_state is not None,
                pause_voice=lambda: host.pause_for_voice(),
                cancel_session=lambda reason: host.cancel(reason),
                event=lambda name, **fields: host.event(name, **fields),
                status=lambda phase, message: host.status(phase, message),
                log=lambda name, **fields: host.log(name, **fields),
                save_status=lambda: host.save_status(),
                metric=host.record_metric,
                add_metric=host.add_capture_metric,
                metric_value=lambda name: host.metrics[name],
                diagnostics=lambda: host.diagnostics,
                enqueue=lambda job: host.enqueue_recognition(job),
                discard_recognition=lambda: host.discard_recognition(),
            ),
            fast_stop,
        ),
    )
    recognition = RecognitionWorker(
        RecognitionHooks(
            valid=lambda generation: host.valid(generation),
            diagnostics=lambda: host.diagnostics,
            playing=lambda: host.playback.playing,
            last_audio_end=lambda: host.playback.last_audio_end,
            recent_spoken=lambda: [line for _, line in tuple(host.playback.spoken)],
            pause_state=lambda: host.playback.pause_state,
            speech=lambda: host.speech,
            metric=host.record_metric,
            set_metric_field=host.set_metric_field,
            add_metric_field=host.add_metric_field,
            record_echo_pair=host.record_echo_pair,
            echo_running=lambda: host.calibration.running,
            echo_failed=lambda: host.calibration.failed,
            awake=lambda: host.session.awake,
            wake=lambda: host.session.wake,
            set_awake=host.set_awake,
            wake_pattern=wake_pattern,
            touch=lambda: host.touch_activity(),
            event=lambda name, **fields: host.event(name, **fields),
            status=lambda phase, message: host.status(phase, message),
            log=lambda name, **fields: host.log(name, **fields),
            cancel_session=lambda reason: host.cancel(reason),
            begin_visual=lambda text, origin: host.begin_visual(text, origin),
            enqueue_response=lambda job: host.enqueue_response(job),
            failure=lambda message, error, **fields: host.failure(message, error, **fields),
            resume_rejected=lambda token, generation: host.resume_after_rejection(
                token, generation
            ),
        )
    )
    response = ResponseWorker(
        ResponseHooks(
            valid=lambda generation: host.valid(generation),
            diagnostics=lambda: host.diagnostics,
            workspace=lambda: host.workspace,
            brain=lambda: host.brain,
            speech=lambda: host.speech,
            active=lambda: host.session.active,
            metric=host.record_metric,
            dispatch=lambda job: host.respond_job(job),
            answer=lambda text, generation: host.answer(text, generation),
            answer_visual=lambda job: host.answer_visual(job),
            read_announcement=lambda job: host.read_announcement(job),
            finish_text=host.finish_text_response,
            publish_workspace=lambda: host.publish_workspace(),
            event=lambda name, **fields: host.event(name, **fields),
            status=lambda phase, message: host.status(phase, message),
            log=lambda name, **fields: host.log(name, **fields),
            failure=lambda message, error: host.failure(message, error),
            speak=lambda text, generation: host.speak(text, generation),
        )
    )
    announcements = AnnouncementController(
        AnnouncementHooks(
            lock=host.session.guard,
            connected=lambda: host.session.connection is not None,
            calibration_busy=lambda: host.calibration.active or host.calibration.running,
            cancel_session=lambda reason: host.cancel(reason),
            valid=lambda generation: host.valid(generation),
            offer=lambda job: host.response.offer(job),
            diagnostics=lambda: host.diagnostics,
            event=lambda name, **fields: host.event(name, **fields),
            status=lambda phase, message: host.status(phase, message),
            speak=lambda text, generation: host.speak(text, generation),
            failure=lambda message, error: host.failure(message, error),
            metric=host.record_metric,
            save_status=lambda: host.save_status(),
        )
    )
    reminders = ReminderScheduler(
        ReminderHooks(
            lock=host.session.guard,
            workspace=lambda: host.workspace,
            publish_workspace=lambda: host.publish_workspace(),
            can_deliver=lambda: (
                host.session.connection is not None
                and not host.calibration.active
                and not host.playback.playing
                and not host.response.busy
                and not host.recognition.busy
                and host.session.text_generation is None
                and host.visual.generation is None
                and host.announcements.generation is None
                and not host.capture.endpoint.vad.talking
                and host.response.queue.empty()
            ),
            touch=lambda: host.touch_activity(),
            announce=lambda text: host.announce(text),
        )
    )
    device = DeviceController(
        DeviceControlHooks(
            lock=host.session.guard,
            connected=lambda: host.session.connection is not None,
            cancel_session=lambda reason: host.cancel(reason),
            event=lambda name, **fields: host.event(name, **fields),
            touch=lambda: host.touch_activity(),
        )
    )
    reporting = StatusReporter(
        ReportingHooks(
            session=lambda: host.session.snapshot(),
            diagnostics=lambda: host.diagnostics,
            playback=lambda: host.playback,
            capture=lambda: host.capture,
            calibration=lambda: host.calibration,
            visual=lambda: host.visual,
            announcements=lambda: host.announcements,
            brain=lambda: host.brain,
            speech=lambda: host.speech,
            recognition_alive=lambda: host.recognition.worker.is_alive(),
            response_alive=lambda: host.response.worker.is_alive(),
            metrics=lambda: dict(host.metrics),
            transport_diagnostics=lambda: (
                host.transport_hub.diagnostics() if host.transport_hub else {}
            ),
            dot_runtime=dot_runtime,
            private_directory=private_directory,
            wake_word=wake_word,
            log=lambda name, **fields: host.log(name, **fields),
            status=lambda phase, message: host.status(phase, message),
        ),
        started_at=started_at,
    )
    return Components(
        calibration,
        visual,
        playback,
        capture,
        recognition,
        response,
        announcements,
        reminders,
        device,
        reporting,
    )


def build_session(host: Bridge) -> SessionController:
    return SessionController(
        SessionHooks(
            diagnostics=lambda: host.diagnostics,
            workspace=lambda: host.workspace,
            playback=lambda: host.playback,
            capture=lambda: host.capture,
            visual=lambda: host.visual,
            calibration=lambda: host.calibration,
            announcements=lambda: host.announcements,
            brain=lambda: host.brain,
            speech=lambda: host.speech,
            response_busy=lambda: host.response.busy,
            recognition_busy=lambda: host.recognition.busy,
            send=lambda packet: host.send(packet),
            event=lambda name, **fields: host.event(name, **fields),
            status=lambda phase, message: host.status(phase, message),
            valid=lambda generation: host.valid(generation),
            cancel=lambda reason: host.cancel(reason),
            stop=lambda: host.stop(),
            touch_activity=lambda: host.touch_activity(),
            reminder_tick=lambda: host.reminder_tick(),
            save_status=lambda: host.save_status(),
            publish_workspace=lambda: host.publish_workspace(),
            enqueue_response=lambda job: host.enqueue_response(job),
            route_event=lambda event: host.on_event(event),
            calibrate=lambda: host.calibrate,
            log=lambda name, **fields: host.log(name, **fields),
            metric=host.record_metric,
            metric_value=lambda name: host.metrics.get(name, 0),
        )
    )

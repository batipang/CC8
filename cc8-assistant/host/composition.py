"""Passive composition root: bind component ports, without mutating session state.

Only the composition modules see the complete Bridge for dependency wiring.
Components themselves receive frozen narrow hooks, never the Bridge. All
constructors are passive; Bridge publishes the bundle before starting workers.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING, Pattern, Callable
from pathlib import Path
import time
from brain_router import BrainRouter
from composition_audio import build_audio_pipeline
from vision import desktop_signature
from calibration_controller import CalibrationController, CalibrationHooks
from vision_controller import VisionController, VisionHooks
from playback_controller import PlaybackController, PlaybackHooks
from capture_pipeline import CapturePipeline
from echo_pipeline import EchoFactory
from recognition_worker import RecognitionWorker
from response_worker import ResponseWorker
from fast_stop import FastStopDetector
from session_controller import SessionController
from session_ports import SessionHooks
from status_reporter import StatusReporter, ReportingHooks
from wake_controller import WakeController, WakeHooks, Detector as WakeDetectorPort
from announcement_controller import AnnouncementController, AnnouncementHooks
from reminder_scheduler import ReminderScheduler, ReminderHooks
from desktop_notifications import DesktopNotifier, DesktopDelivery
from device_controller import DeviceController, DeviceControlHooks
from device_event_router import DeviceEventRouter
from chat_controller import ChatController, ChatHooks
from saved_tasks import SavedTasks
from saved_task_controller import SavedTaskController, SavedTaskHooks
from preference_memory import PreferenceMemory
from preference_controller import PreferenceController, PreferenceHooks
from recognition_vocabulary import VocabularyStore
from vocabulary_controller import VocabularyController, VocabularyHooks

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
    wake: WakeController
    chats: ChatController
    saved_tasks: SavedTaskController
    preferences: PreferenceController
    vocabulary: VocabularyController


def voice_busy(host: Bridge) -> bool:
    return (
        host.calibration.active
        or host.calibration.running
        or host.playback.playing
        or host.playback.pause_state is not None
        or host.recognition.busy
        or not host.recognition.queue.empty()
        or host.response.busy
        or not host.response.queue.empty()
        or host.visual.generation is not None
        or host.announcements.generation is not None
        or host.capture.endpoint.vad.talking
        or host.capture.endpoint.waiting_chinese_busy
    )


def build_components(
    host: Bridge,
    *,
    echo_factory: EchoFactory,
    wake_pattern: Pattern[str],
    fast_stop: FastStopDetector | None,
    echo_check: bool,
    wake_detector: WakeDetectorPort | None,
    wake_window_seconds: int,
    started_at: float,
    private_directory: Callable[[], Path],
    wake_word: str,
    dot_runtime: Callable[[], dict[str, object]],
    preference_memory: PreferenceMemory | None = None,
    vocabulary_store: VocabularyStore | None = None,
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
            wake_allowed=lambda: host.wake.allowed(),
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
            diagnostics=lambda: host.diagnostics,
            failure=lambda message, error, **fields: host.failure(message, error, **fields),
            capture_owner=lambda: host.session.capture_owner(),
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
            onset=lambda: host.capture.endpoint.voice_onset_ns,
            echo_metrics=lambda: dict(
                input_dbfs=host.metrics.get('input_dbfs'),
                residual_dbfs=host.metrics.get('output_dbfs'),
                raw_coherence=round(host.capture.endpoint.near_speech.correlation, 3),
                residual_coherence=round(host.capture.endpoint.near_speech.residual_correlation, 3),
            ),
            failure=lambda message, error, **fields: host.failure(message, error, **fields),
            utterance_paused=lambda token: (
                host.capture.endpoint.vad.talking
                and host.capture.endpoint.turn_pause_token == token
            ),
            speech=lambda: host.speech,
            cancel_timeline_pause=lambda: host.capture.echo.cancel_pause(),
            pause_timeline=lambda nanos: host.capture.echo.pause_at(nanos),
            resume_timeline=lambda nanos: host.capture.echo.resume_at(nanos),
            begin_response_output=lambda generation: host.capture.begin_response_output(generation),
            begin_clock_report=lambda generation, playback_id: host.capture.begin_render_clock(
                generation, playback_id
            ),
            complete_clock_report=lambda generation, playback_id, value, frames: (
                host.capture.complete_render_clock(generation, playback_id, value, frames)
            ),
        )
    )
    capture, recognition, response = build_audio_pipeline(
        host, echo_factory=echo_factory, wake_pattern=wake_pattern, fast_stop=fast_stop
    )
    announcements = AnnouncementController(
        AnnouncementHooks(
            lock=host.session.guard,
            connected=lambda: host.session.connection is not None,
            calibration_busy=lambda: host.calibration.active or host.calibration.running,
            cancel_session=lambda reason: host.cancel(reason),
            session_generation=lambda: host.session.generation,
            valid=lambda generation: host.valid(generation),
            offer=lambda job: host.response.offer(job),
            diagnostics=lambda: host.diagnostics,
            event=lambda name, **fields: host.event(name, **fields),
            status=lambda phase, message: host.status(phase, message),
            speak=lambda text, generation: host.speak(text, generation),
            failure=lambda message, error: host.failure(message, error),
            metric=host.record_metric,
            save_status=lambda: host.save_status(),
            log=lambda name, **fields: host.log(name, **fields),
        )
    )
    reminders = ReminderScheduler(
        ReminderHooks(
            lock=host.session.guard,
            workspace=lambda: host.workspace,
            publish_workspace=lambda: host.publish_workspace(),
            can_deliver=lambda: (
                host.session.connection is not None
                and not voice_busy(host)
                and host.session.text_generation is None
                and host.response.corrections.draft is None
            ),
            touch=lambda: host.touch_activity(),
            announce=lambda text: host.announce(text),
            desktop_notify=lambda ident, text, current: DesktopDelivery(
                DesktopNotifier(), ident, text, current
            ),
            delivery_owner=lambda: (
                host.session.connection,
                host.session.bound_route_token,
                host.session.generation,
                host.session.epoch,
                host.session.capture_id,
            ),
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
    wake = WakeController(
        WakeHooks(
            lock=host.session.guard,
            active=lambda: host.session.active,
            generation=lambda: host.session.generation,
            awake=lambda: host.session.awake,
            wake_mode=lambda: host.session.wake,
            capture_current=lambda nanos: host.capture.endpoint_audio_current(nanos),
            busy=lambda: voice_busy(host),
            render_risk=lambda: (
                host.playback.playing
                or time.monotonic() - host.playback.last_audio_end < 3
                or host.calibration.active
                or host.calibration.failed
            ),
            set_awake=lambda value: host.session.set_awake(value),
            touch_activity=lambda: host.touch_activity(),
            cancel=lambda reason: host.cancel(reason),
            status=lambda phase, message: host.status(phase, message),
            metric=host.record_metric,
            log=lambda name, **fields: host.log(name, **fields),
        ),
        wake_detector,
        window_seconds=wake_window_seconds,
    )

    def select_chat(value):
        host.brain.dot.set_handoff(value)
        host.brain.select('dot')

    chats = ChatController(
        ChatHooks(
            lock=host.session.guard,
            generation=lambda: host.session.generation,
            connection=lambda: host.session.connection,
            available=lambda: (
                isinstance(host.brain, BrainRouter) and hasattr(host.brain.dot, 'set_handoff')
            ),
            idle=lambda: (
                not (
                    host.calibration.active
                    or host.calibration.running
                    or host.playback.playing
                    or host.playback.pause_state is not None
                    or host.recognition.busy
                    or not host.recognition.queue.empty()
                    or host.response.busy
                    or not host.response.queue.empty()
                    or host.visual.generation is not None
                    or host.announcements.generation is not None
                    or host.capture.endpoint.vad.talking
                )
            ),
            select=select_chat,
            event=lambda name, **fields: host.event(name, **fields),
            transport_revision=lambda: host.metrics.get('transport_changes', 0),
        )
    )
    saved_tasks = SavedTaskController(
        SavedTaskHooks(
            lock=host.session.guard,
            connection=lambda: host.session.connection,
            tasks=lambda: host.workspace.snapshot()['tasks'],
            event=lambda name, **fields: host.event(name, **fields),
        ),
        SavedTasks(private_directory() / 'saved-task-results.json'),
    )
    preferences = PreferenceController(
        PreferenceHooks(
            lock=host.session.guard,
            connection=lambda: host.session.connection,
            busy=lambda: (
                host.session.active or host.session.text_generation is not None or voice_busy(host)
            ),
            event=lambda name, **fields: host.event(name, **fields),
        ),
        preference_memory
        if preference_memory is not None
        else PreferenceMemory(private_directory() / 'preference-memory.json'),
    )
    vocabulary = VocabularyController(
        VocabularyHooks(
            lock=host.session.guard,
            connection=lambda: host.session.connection,
            busy=lambda: (
                host.session.active or host.session.text_generation is not None or voice_busy(host)
            ),
            prepare=lambda term, alias, source_id: host.response.prepare_vocabulary(
                term, alias, source_id
            ),
            source_id=lambda: host.response.corrections.source_token(
                host.response.hooks.correction_owner()
            ),
            event=lambda name, **fields: host.event(name, **fields),
        ),
        vocabulary_store
        if vocabulary_store is not None
        else VocabularyStore(private_directory() / 'recognition-vocabulary.json'),
    )
    reporting = StatusReporter(
        ReportingHooks(
            session=lambda: host.session.snapshot(),
            wake=lambda: host.wake,
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
        wake,
        chats,
        saved_tasks,
        preferences,
        vocabulary,
    )


def build_device_events(host: Bridge) -> DeviceEventRouter:
    return DeviceEventRouter(
        session=host.session,
        capture=host.capture,
        chats=host.chats,
        device=host.device,
        playback=host.playback,
        saved_tasks=host.saved_tasks,
        visual=host.visual,
        workspace=host.workspace,
        begin_visual=lambda text, origin, nonce: host.begin_visual(text, origin, nonce),
        cancel=lambda reason: host.cancel(reason),
        status=lambda phase, message: host.status(phase, message),
        event=lambda name, **fields: host.event(name, **fields),
        publish_workspace=lambda: host.publish_workspace(),
        submit_text=lambda text: host.submit_text(text),
        reminder_action=lambda args: host.reminder_action(args),
        touch_activity=lambda: host.touch_activity(),
        log=lambda name, **fields: host.log(name, **fields),
        read_result=lambda data: host.response.request_read(data),
        correction_action=lambda data: host.response.request_correction(data),
        preference_action=lambda data: host.preferences.request(data),
        vocabulary_action=lambda data: host.vocabulary.request(data),
    )


def build_session(host: Bridge, *, capture_frontend: str = 'mono') -> SessionController:
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
            voice_busy=lambda: voice_busy(host),
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
            wake_reset=lambda reason: host.wake.reset(reason),
            wake_touch=lambda: host.wake.touch(),
            awake_changed=lambda value: host.wake.awake_changed(value),
            wake_tick=lambda: host.wake.tick(),
            prepare_brain=lambda generation: host.response.prepare(generation),
            cancel_observation=lambda: host.cancel_playback_observation(),
            discard_corrections=lambda preserve: host.response.clear_corrections(
                preserve_source=preserve, publish=False
            ),
        ),
        capture_frontend=capture_frontend,
    )

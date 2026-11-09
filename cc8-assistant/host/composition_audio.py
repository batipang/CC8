"""Bind capture, recognition and response to their existing session ports."""

from __future__ import annotations
from typing import TYPE_CHECKING, Pattern
from capture_pipeline import CapturePipeline, CaptureHooks
from echo_pipeline import EchoPipeline, EchoHooks, EchoFactory
from endpoint_pipeline import EndpointPipeline, EndpointHooks
from fast_stop import FastStopDetector
from recognition_worker import RecognitionWorker
from response_worker import ResponseWorker, ResponseHooks
from session_ports import RecognitionHooks

if TYPE_CHECKING:
    from bridge import Bridge


def build_audio_pipeline(
    host: Bridge,
    *,
    echo_factory: EchoFactory,
    wake_pattern: Pattern[str],
    fast_stop: FastStopDetector | None,
) -> tuple[CapturePipeline, RecognitionWorker, ResponseWorker]:
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
                wake_feed=lambda pcm, generation, **fields: host.wake.feed(
                    pcm, generation, **fields
                ),
                capture_current=lambda nanos: host.capture.endpoint_audio_current(nanos),
                wake_allowed=lambda: host.wake.allowed(),
                claim_wake=lambda: host.wake.claim_prefix(),
                reset_wake_gap=lambda: host.wake.reset('capture_gap'),
                wake_ticket=lambda stamp: host.wake.waiting_utterance(stamp),
                complete_english_ticket=lambda base, stamp: host.wake.complete_english_ticket(
                    base, stamp
                ),
                complete_chinese_enabled=lambda: host.wake.complete_chinese_enabled,
                complete_chinese_pending=lambda base: host.wake.pending_chinese_utterance(base),
                complete_chinese_guard=lambda: host.session.guard,
                complete_chinese_ticket=lambda base, stamp: host.wake.complete_chinese_ticket(
                    base, stamp
                ),
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
                pause_voice=lambda stamp: host.pause_for_voice(stamp),
                resume_rejected=lambda token, generation: host.playback.resume_after_rejection(
                    token, generation
                ),
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
            lock=host.session.guard,
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
            wake_allowed=lambda: host.wake.allowed(),
            recheck_wake=lambda ticket, pcm, generation: host.wake.recheck_utterance(
                ticket, pcm, generation
            ),
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
            tool_sample_rejection=host.session.tool_sample_rejection,
            tool_sample_comparison=host.session.tool_sample_comparison,
            tool_sample_asr_rejection=host.session.tool_sample_asr_rejection,
            bind_response_probe=lambda text, generation, input_end_ns: (
                host.capture.bind_response_probe(text, generation, input_end_ns)
            ),
        )
    )
    response = ResponseWorker(
        ResponseHooks(
            lock=host.session.guard,
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
            failure=lambda message, error, **fields: host.failure(message, error, **fields),
            speak=lambda text, generation: host.speak(text, generation),
            can_read_result=lambda: (
                host.session.connection is not None
                and not host.calibration.active
                and not host.calibration.running
                and not host.playback.playing
                and host.playback.pause_state is None
                and not host.response.busy
                and host.response.queue.empty()
                and not host.recognition.busy
                and host.recognition.queue.empty()
                and host.session.text_generation is None
                and host.visual.generation is None
                and host.announcements.generation is None
                and not host.capture.endpoint.vad.talking
            ),
            submit_read=lambda ident, restart: host.session.submit_text(
                '朗读已完成结果', result_id=ident, result_restart=restart
            ),
        )
    )
    return capture, recognition, response

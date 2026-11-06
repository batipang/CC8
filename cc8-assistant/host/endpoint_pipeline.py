"""Own VAD/near-speech evidence, local stop detection and utterance boundaries."""

from __future__ import annotations
from dataclasses import dataclass
import time
from typing import Callable, Protocol
import numpy as np
from diagnostics import TraceBook
from capture_evidence import CaptureEvidence, OnsetPrefix, FRAME_NS, FRAME_JITTER_NS
from dsp import VoiceDetector, ReferenceCapture, NearSpeechDetector, level
from fast_stop import can_fast_stop
from protocol import JsonValue
from session_ports import EventSink, MetricSink


class WakeFrame(Protocol):
    def __call__(
        self,
        pcm: np.ndarray,
        generation: int,
        *,
        capture_nanos: int,
        echo_risk: bool,
        render_text: str,
        alternate_pcm: np.ndarray | None = None,
    ) -> bool: ...


@dataclass(frozen=True)
class EndpointHooks:
    generation: Callable[[], int]
    awake: Callable[[], bool]
    playing: Callable[[], bool]
    last_audio_end: Callable[[], float]
    last_spoken: Callable[[], str]
    previous_spoken: Callable[[], str]
    previous_spoken_until: Callable[[], float]
    recent_spoken: Callable[[], list[str]]
    pause_token: Callable[[], int | None]
    paused: Callable[[], bool]
    pause_voice: Callable[[int], int | None]
    resume_rejected: Callable[[int | None, int], None]
    cancel_session: Callable[[str], int]
    event: EventSink
    status: Callable[[str, str], None]
    log: EventSink
    save_status: Callable[[], None]
    metric: MetricSink
    add_metric: Callable[[str, int], None]
    metric_value: Callable[[str], JsonValue]
    diagnostics: Callable[[], TraceBook]
    enqueue: Callable[[tuple[object, ...]], None]
    discard_recognition: Callable[[], None]
    wake_feed: WakeFrame
    capture_current: Callable[[int], bool]
    wake_allowed: Callable[[], bool]
    claim_wake: Callable[[], bool]
    reset_wake_gap: Callable[[], None]


class EndpointPipeline:
    def __init__(self, hooks: EndpointHooks, fast_stop=None):
        self.hooks = hooks
        self.fast_stop = fast_stop
        self.vad = VoiceDetector()
        self.reference_capture = ReferenceCapture()
        self.near_speech = NearSpeechDetector()
        self.voice_trace = None
        self.live_turn = 0
        self.turn_reference = []
        self.turn_local_wake = False
        self.turn_during_playback = False
        self.turn_echo_risk = False
        self.turn_capture_started = 0
        self.last_capture_render_ns = None
        self.turn_pause_token = None
        self.owns_turn_pause = False
        self.capture_evidence = None
        self.reset_voice_onset()
        self.last_fast_stop_ns = 0
        self.fast_stop_observation = None
        self.ignore_vad_until_ns = 0

    def reset_voice_onset(self):
        self.voice_onset_ns = None
        self.last_voice_frame_ns = None
        self.onset_prefix = OnsetPrefix()

    def observe_voice_onset(self, stamp, voiced):
        previous = self.last_voice_frame_ns
        self.last_voice_frame_ns = stamp
        if previous is not None and abs(stamp - previous - FRAME_NS) > FRAME_JITTER_NS:
            self.voice_onset_ns = None
        if not self.vad.talking:
            if not voiced:
                self.voice_onset_ns = None
            elif self.voice_onset_ns is None:
                self.voice_onset_ns = stamp

    def cancel_partial_trace(self):
        # Only the endpoint-owned, unfinished capture belongs here. After
        # handoff, recognition owns its trace and decides its terminal outcome.
        if self.voice_trace is not None:
            self.hooks.diagnostics().gate(self.voice_trace, 'stale')
            self.hooks.diagnostics().finish(self.voice_trace, 'cancelled')
            self.voice_trace = None

    def reset_transport(self):
        self.cancel_partial_trace()
        self.reset_voice_onset()
        self.fast_stop_observation = None
        self.vad.reset()
        self.reference_capture.reset()
        self.near_speech.reset()
        self.turn_pause_token = None
        self.owns_turn_pause = False
        self.capture_evidence = None

    def reset_session(self):
        self.cancel_partial_trace()
        self.reset_voice_onset()
        self.fast_stop_observation = None
        self.vad = VoiceDetector()
        self.turn_reference = []
        self.turn_local_wake = False
        self.turn_echo_risk = False
        self.last_capture_render_ns = None
        self.reference_capture.reset()
        self.near_speech.reset()
        self.turn_pause_token = None
        self.owns_turn_pause = False
        self.capture_evidence = None
        self.last_fast_stop_ns = 0
        self.ignore_vad_until_ns = 0
        if self.fast_stop:
            self.fast_stop.reset()

    def reset_voice_activity(self):
        self.cancel_partial_trace()
        self.reset_voice_onset()
        self.fast_stop_observation = None
        self.vad.reset()
        self.reference_capture.reset()
        self.capture_evidence = None

    def discard_capture_gap(self):
        # Only the unfinished endpoint can give up a pause here. A complete
        # utterance already queued for ASR still owns its own disposition.
        token = (
            self.turn_pause_token if self.voice_trace is not None and self.owns_turn_pause else None
        )
        generation = self.live_turn
        self.reset_voice_activity()
        self.near_speech.reset()
        self.turn_pause_token = None
        self.owns_turn_pause = False
        if self.fast_stop:
            self.fast_stop.reset()
        self.hooks.reset_wake_gap()
        if token is not None:
            self.hooks.resume_rejected(token, generation)

    def consume(self, frame):
        stamp = frame.nanos
        reference = frame.reference
        cleaned = frame.cleaned
        stop_cleaned = frame.stop_cleaned
        if level(reference) > -65:
            self.last_capture_render_ns = stamp
        echo_risk = self.hooks.playing() or (
            self.last_capture_render_ns is not None
            and 0 <= stamp - self.last_capture_render_ns < 3_000_000_000
        )
        now = time.monotonic()
        recent_output = self.hooks.playing() or 0 <= now - self.hooks.last_audio_end() < 3
        render_text = self.hooks.last_spoken() if recent_output else ''
        if now < self.hooks.previous_spoken_until():
            # Preserve word boundaries and playback order across English clauses.
            render_text = self.hooks.previous_spoken() + '\n' + render_text
        # A completion receipt still identifies recent playback if the
        # microphone stream has not delivered the matching render reference.
        self.hooks.wake_feed(
            cleaned,
            self.hooks.generation(),
            capture_nanos=stamp,
            echo_risk=echo_risk or recent_output,
            render_text=render_text,
            alternate_pcm=stop_cleaned if self.fast_stop is not None else None,
        )
        if not self.hooks.capture_current(stamp):
            return
        if self.fast_stop and (self.hooks.awake() or echo_risk):
            self.hooks.metric('stop_input_dbfs', round(level(stop_cleaned), 1))
            keyword = self.fast_stop.feed(stop_cleaned)
            if keyword and stamp - self.last_fast_stop_ns > 1_200_000_000:
                recent = self.hooks.playing() or time.monotonic() - self.hooks.last_audio_end() < 1
                render_text = self.hooks.last_spoken() if recent else ''
                if time.monotonic() < self.hooks.previous_spoken_until():
                    render_text += self.hooks.previous_spoken()
                if can_fast_stop(keyword, render_text):
                    self.apply_fast_stop(keyword, stamp)
                else:
                    self.hooks.log('fast_stop_reference_suppressed')
        if stamp < self.ignore_vad_until_ns:
            return
        voiced, credible = self.near_speech.feed(cleaned, frame.raw, reference)
        self.observe_voice_onset(stamp, voiced)
        self.vad.external_voice = voiced
        was_talking = self.vad.talking
        began, utterance, _ = self.vad.feed(cleaned)
        render_audio = self.reference_capture.feed(reference, began, utterance is not None)
        claimed_wake = (
            self.hooks.claim_wake() if began or self.vad.talking or utterance is not None else False
        )
        if began:
            self.cancel_partial_trace()
            self.capture_evidence = CaptureEvidence(
                onset_prefix=self.onset_prefix.before(stamp, voiced=voiced)
            )
            self.onset_prefix = OnsetPrefix()
            self.turn_local_wake = claimed_wake
            self.voice_trace = self.hooks.diagnostics().begin('voice')
            self.hooks.diagnostics().mark(self.voice_trace, 'voice_detected')
            now = time.monotonic()
            self.turn_during_playback = self.hooks.playing()
            self.turn_echo_risk = echo_risk
            self.turn_capture_started = now
            self.turn_pause_token = self.hooks.pause_token()
            self.owns_turn_pause = False
            self.turn_reference = self.hooks.recent_spoken()
            self.live_turn = (
                self.hooks.generation()
                if self.turn_echo_risk or not self.hooks.awake()
                else self.hooks.cancel_session('voice')
            )
            if not self.turn_echo_risk and self.hooks.awake():
                self.hooks.status('listening', '我在听，你继续说')
        elif claimed_wake:
            self.turn_local_wake = True
        if not was_talking and not began:
            self.onset_prefix.feed(frame, voiced=voiced, credible=credible)
        if claimed_wake and self.voice_trace is not None:
            self.hooks.diagnostics().mark(self.voice_trace, 'wake_detected')
        if (
            self.vad.talking
            and credible
            and self.hooks.playing()
            and self.hooks.awake()
            and (not self.hooks.paused())
        ):
            self.turn_pause_token = self.hooks.pause_voice(stamp)
            self.owns_turn_pause = self.turn_pause_token is not None
        if self.vad.talking or utterance is not None:
            self.turn_echo_risk |= echo_risk
        if (
            began or self.vad.talking or utterance is not None
        ) and self.capture_evidence is not None:
            self.capture_evidence.feed(
                frame,
                self.near_speech,
                voiced=voiced,
                credible=credible,
                pause_requested=self.hooks.paused(),
            )
        if utterance is not None:
            self.reset_voice_onset()
            self.hooks.diagnostics().mark(self.voice_trace, 'utterance_end')
            self.hooks.add_metric('utterances', 1)
            input_end_ns = None
            if self.capture_evidence is not None:
                input_end_ns = self.capture_evidence.last_voiced_end()
                self.hooks.diagnostics().capture(
                    self.voice_trace,
                    self.capture_evidence.snapshot(
                        echo_risk=self.turn_echo_risk,
                        during_playback=self.turn_during_playback,
                        local_wake=self.turn_local_wake,
                    ),
                )
                self.capture_evidence = None
            references = list(dict.fromkeys([*self.turn_reference, *self.hooks.recent_spoken()]))[
                -64:
            ]
            evidence = {
                'echo_risk': self.turn_echo_risk,
                'captured_at': self.turn_capture_started,
                'pause_token': self.turn_pause_token,
                'local_wake': self.turn_local_wake,
                'input_end_ns': input_end_ns,
            }
            job = (
                self.live_turn,
                utterance,
                self.turn_during_playback,
                references,
                evidence,
                render_audio,
                self.voice_trace,
            )
            if not self.hooks.wake_allowed():
                self.hooks.diagnostics().gate(self.voice_trace, 'wake_closed')
                self.hooks.diagnostics().finish(self.voice_trace, 'rejected')
                self.hooks.metric('local_wake_utterances_withheld', increment=True)
                self.voice_trace = None
                return
            self.hooks.enqueue(job)
            self.voice_trace = None

    def apply_fast_stop(self, keyword, stamp):
        start = time.perf_counter()
        was_playing = self.hooks.playing()
        self.last_fast_stop_ns = stamp
        self.ignore_vad_until_ns = stamp + 160_000_000
        self.cancel_partial_trace()
        cancel_generation = self.hooks.cancel_session('streaming_stop_keyword')
        # Bind the device clock of the frame that produced the local hit to
        # this cancellation. It is not a timestamp for the last phoneme.
        self.fast_stop_observation = (cancel_generation, stamp) if was_playing else None
        self.vad.reset()
        self.capture_evidence = None
        self.live_turn = self.hooks.generation()
        self.reference_capture.reset()
        self.hooks.discard_recognition()
        self.hooks.event('transcript', text=keyword)
        self.hooks.status('listening', '已停止播报，我在听')
        self.hooks.metric('fast_stop_count', increment=True)
        self.hooks.metric(
            'fast_stop_control_dispatch_ms', round((time.perf_counter() - start) * 1000, 2)
        )
        self.hooks.log(
            'streaming_stop_keyword',
            command=keyword,
            control_dispatch_ms=self.hooks.metric_value('fast_stop_control_dispatch_ms'),
        )
        self.hooks.save_status()

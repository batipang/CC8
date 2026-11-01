"""Own VAD/near-speech evidence, local stop detection and utterance boundaries."""

from __future__ import annotations
from dataclasses import dataclass
import time
from typing import Callable
from diagnostics import TraceBook
from dsp import VoiceDetector, ReferenceCapture, NearSpeechDetector, level
from fast_stop import can_fast_stop
from protocol import JsonValue
from session_ports import EventSink, MetricSink


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
    pause_voice: Callable[[], int | None]
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
        self.turn_during_playback = False
        self.turn_echo_risk = False
        self.turn_capture_started = 0
        self.last_capture_render_ns = None
        self.turn_pause_token = None
        self.last_fast_stop_ns = 0
        self.ignore_vad_until_ns = 0

    def reset_transport(self):
        self.vad.reset()
        self.reference_capture.reset()
        self.near_speech.reset()
        self.turn_pause_token = None

    def reset_session(self):
        self.vad = VoiceDetector()
        self.turn_reference = []
        self.turn_echo_risk = False
        self.last_capture_render_ns = None
        self.reference_capture.reset()
        self.near_speech.reset()
        self.turn_pause_token = None
        self.last_fast_stop_ns = 0
        self.ignore_vad_until_ns = 0
        if self.fast_stop:
            self.fast_stop.reset()

    def reset_voice_activity(self):
        self.vad.reset()
        self.reference_capture.reset()

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
        if self.fast_stop:
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
        self.vad.external_voice = voiced
        began, utterance, _ = self.vad.feed(cleaned)
        render_audio = self.reference_capture.feed(reference, began, utterance is not None)
        if began:
            # This keeps the existing text-based wake path. Actual local wake
            # spotting before cloud ASR is a separate P0-03 feature, not this refactor.
            self.voice_trace = self.hooks.diagnostics().begin('voice')
            self.hooks.diagnostics().mark(self.voice_trace, 'voice_detected')
            now = time.monotonic()
            self.turn_during_playback = self.hooks.playing()
            self.turn_echo_risk = echo_risk
            self.turn_capture_started = now
            self.turn_pause_token = self.hooks.pause_token()
            self.turn_reference = self.hooks.recent_spoken()
            self.live_turn = (
                self.hooks.generation()
                if self.turn_echo_risk or not self.hooks.awake()
                else self.hooks.cancel_session('voice')
            )
            if not self.turn_echo_risk:
                self.hooks.status('listening', '我在听，你继续说')
        if (
            self.vad.talking
            and credible
            and self.hooks.playing()
            and self.hooks.awake()
            and (not self.hooks.paused())
        ):
            self.turn_pause_token = self.hooks.pause_voice()
        if self.vad.talking or utterance is not None:
            self.turn_echo_risk |= echo_risk
        if utterance is not None:
            self.hooks.diagnostics().mark(self.voice_trace, 'utterance_end')
            self.hooks.add_metric('utterances', 1)
            references = list(dict.fromkeys([*self.turn_reference, *self.hooks.recent_spoken()]))[
                -64:
            ]
            evidence = {
                'echo_risk': self.turn_echo_risk,
                'captured_at': self.turn_capture_started,
                'pause_token': self.turn_pause_token,
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
            self.hooks.enqueue(job)

    def apply_fast_stop(self, keyword, stamp):
        start = time.perf_counter()
        self.last_fast_stop_ns = stamp
        self.ignore_vad_until_ns = stamp + 160_000_000
        self.hooks.cancel_session('streaming_stop_keyword')
        self.vad.reset()
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

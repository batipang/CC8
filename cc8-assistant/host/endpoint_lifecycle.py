"""Endpoint-owned reset, discard and local-stop transitions.

EndpointPipeline keeps every mutable field. These transitions preserve the
original pause token, capture epoch, diagnostic and callback ordering.
"""

from __future__ import annotations
import time
from typing import TYPE_CHECKING
from dsp import VoiceDetector

if TYPE_CHECKING:
    from endpoint_pipeline import EndpointPipeline


def reset_transport(self: EndpointPipeline):
    self.complete_wake.reset()
    self.waiting_turn = None
    self.waiting_last_sample_ns = None
    self.vad_stamps.clear()
    self.cancel_partial_trace()
    self.reset_voice_onset()
    self.fast_stop_observation = None
    if self.fast_stop:
        # A new route cannot complete a keyword prefix from the old route.
        self.fast_stop.reset(clear_metrics=False)
    self.vad.reset()
    self.reference_capture.reset()
    self.near_speech.reset()
    self.turn_pause_token = None
    self.owns_turn_pause = False
    self.capture_evidence = None


def reset_session(self: EndpointPipeline):
    self.complete_wake.reset()
    self.waiting_turn = None
    self.waiting_last_sample_ns = None
    self.vad_stamps.clear()
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


def reset_voice_activity(self: EndpointPipeline):
    self.complete_wake.reset()
    self.waiting_turn = None
    self.waiting_last_sample_ns = None
    self.vad_stamps.clear()
    self.cancel_partial_trace()
    self.reset_voice_onset()
    self.fast_stop_observation = None
    self.vad.reset()
    self.reference_capture.reset()
    self.capture_evidence = None


def discard_capture_gap(self: EndpointPipeline):
    # Only the unfinished endpoint can give up a pause here. A complete
    # utterance already queued for ASR still owns its own disposition.
    token = self.turn_pause_token if self.voice_trace is not None and self.owns_turn_pause else None
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


def discard_overlong_utterance(self: EndpointPipeline):
    self.hooks.diagnostics().gate(self.voice_trace, 'utterance_too_long')
    self.hooks.diagnostics().finish(self.voice_trace, 'rejected')
    self.voice_trace = None
    self.reset_voice_onset()
    self.reference_capture.reset()
    self.capture_evidence = None
    token = self.turn_pause_token if self.owns_turn_pause else None
    self.turn_pause_token = None
    self.owns_turn_pause = False
    if token is not None:
        self.hooks.resume_rejected(token, self.live_turn)
    if (
        self.live_turn == self.hooks.generation()
        and self.hooks.wake_allowed()
        and not self.hooks.playing()
    ):
        # The shipped HUD reserves its listening label for the generic
        # indicator. Recoverable failures use the visible detail label.
        self.hooks.status('error', '这句话太长了，请分成几句说')


def apply_fast_stop(self: EndpointPipeline, keyword, stamp):
    self.complete_wake.reset()
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

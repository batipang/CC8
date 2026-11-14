"""Own VAD/near-speech evidence, local stop detection and utterance boundaries."""

from __future__ import annotations
from dataclasses import dataclass
from collections import deque
from contextlib import nullcontext
import time
from typing import Callable, Protocol, ContextManager
import numpy as np
from diagnostics import TraceBook
from capture_evidence import CaptureEvidence, OnsetPrefix, FRAME_NS, FRAME_JITTER_NS
from dsp import (
    UTTERANCE_TOO_LONG,
    MAX_UTTERANCE_FRAMES,
    VoiceDetector,
    ReferenceCapture,
    NearSpeechDetector,
    level,
)
from fast_stop import can_fast_stop
from protocol import JsonValue
from session_ports import EventSink, MetricSink
from wake_utterance import WaitingUtterance
from complete_wake_capture import CompleteWakeCapture


from endpoint_lifecycle import (
    reset_transport,
    reset_session,
    reset_voice_activity,
    discard_capture_gap,
    discard_overlong_utterance,
    apply_fast_stop,
)


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
        raw_pcm: np.ndarray | None = None,
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
    wake_ticket: Callable[[int | None], WaitingUtterance | None] = lambda _: None
    complete_chinese_enabled: Callable[[], bool] = lambda: False
    complete_chinese_pending: Callable[[WaitingUtterance | None], bool] = lambda _: False
    complete_chinese_guard: Callable[[], ContextManager[object]] = nullcontext
    natural_barge_enabled: Callable[[], bool] = lambda: True
    live_audio: Callable[[object, bool, bool, list[np.ndarray]], bool] = lambda *_: False
    complete_chinese_ticket: Callable[[WaitingUtterance, int], WaitingUtterance | None] = (
        lambda *_: None
    )
    complete_english_ticket: Callable[[WaitingUtterance, int | None], WaitingUtterance] = (
        lambda base, _: base
    )


class EndpointPipeline:
    def __init__(self, hooks: EndpointHooks, fast_stop=None):
        self.hooks = hooks
        self.fast_stop = fast_stop
        self.complete_wake = CompleteWakeCapture()
        self.vad = VoiceDetector()
        self.vad_stamps = deque(maxlen=VoiceDetector.PRE_ROLL_FRAMES)
        self.waiting_turn = None
        self.waiting_last_sample_ns = None
        self.reference_capture = ReferenceCapture()
        self.near_speech = NearSpeechDetector()
        self.voice_trace = None
        self.live_turn = 0
        self.turn_reference = []
        self.turn_local_wake = False
        self.turn_during_playback = False
        self.turn_echo_risk = False
        self.turn_prefix_echo_risk = False
        self.turn_capture_started = 0
        self.last_capture_render_ns = None
        self.turn_pause_token = None
        self.owns_turn_pause = False
        self.capture_evidence = None
        self.reset_voice_onset()
        self.last_fast_stop_ns = 0
        self.fast_stop_observation = None
        self.ignore_vad_until_ns = 0

    @property
    def waiting_chinese_busy(self):
        return self.complete_wake.vad.talking and self.hooks.complete_chinese_pending(
            self.complete_wake.ticket
        )

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
        return reset_transport(self)

    def set_environment(self, name):
        # An idle controller owns this transition. Reject direct changes while
        # a real utterance still owns timing, retained PCM or wake evidence.
        if self.vad.talking or self.waiting_turn is not None or self.voice_trace is not None:
            raise RuntimeError('A pending utterance owns the current environment')
        from recognition_environment import profile

        profile(name)
        self.reset_voice_activity()
        self.near_speech.set_environment(name)

    def reset_session(self):
        return reset_session(self)

    def reset_voice_activity(self):
        return reset_voice_activity(self)

    def discard_capture_gap(self):
        return discard_capture_gap(self)

    def discard_overlong_utterance(self):
        return discard_overlong_utterance(self)

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
        # Use that context for both wake and the utterance's admission gate.
        capture_echo_risk = echo_risk or recent_output
        woke = self.hooks.wake_feed(
            cleaned,
            self.hooks.generation(),
            capture_nanos=stamp,
            echo_risk=capture_echo_risk,
            render_text=render_text,
            alternate_pcm=stop_cleaned if self.fast_stop is not None else None,
            raw_pcm=frame.raw,
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
                    render_text = self.hooks.previous_spoken() + '\n' + render_text
                if can_fast_stop(keyword, render_text):
                    self.apply_fast_stop(keyword, stamp)
                else:
                    self.hooks.log('fast_stop_reference_suppressed')
        if stamp < self.ignore_vad_until_ns:
            self.complete_wake.reset()
            return
        voiced, credible = self.near_speech.feed(cleaned, frame.raw, reference)
        # Publish completion into the existing queue atomically with idle's
        # session decision; there must be no observable gap after VAD reset.
        with self.hooks.complete_chinese_guard():
            if (
                self.hooks.complete_chinese_enabled()
                and not self.hooks.awake()
                and not capture_echo_risk
            ):
                completed = self.complete_wake.feed(
                    cleaned,
                    self.near_speech.speech,
                    stamp,
                    self.hooks.wake_ticket,
                    self.hooks.complete_chinese_ticket,
                )
                if completed is not None:
                    trace = self.hooks.diagnostics().begin('voice')
                    self.hooks.diagnostics().mark(trace, 'utterance_end')
                    # No onset timestamp is invented: the ordinary voice-detected
                    # stage belongs to its own endpoint. This is a completed local
                    # waiting candidate, using the actual device input end stamp.
                    self.hooks.enqueue(
                        (
                            completed.ticket.generation,
                            completed.audio,
                            False,
                            [],
                            {
                                'echo_risk': False,
                                'captured_at': time.monotonic(),
                                'pause_token': None,
                                'local_wake': False,
                                'input_end_ns': completed.input_end_ns,
                            },
                            None,
                            trace,
                            completed.ticket,
                        )
                    )
            else:
                self.complete_wake.reset()
        # The keyword can be confirmed after the user has started the request.
        # Reuse only this local utterance's prefix after the streaming verdict.
        wake_prefix = []
        if (
            woke
            and self.vad.talking
            and self.live_turn == self.hooks.generation()
            and not capture_echo_risk
            and not self.turn_echo_risk
            and not self.turn_prefix_echo_risk
        ):
            wake_prefix = self.vad.audio
        if self.hooks.live_audio(frame, voiced, credible, wake_prefix):
            if self.vad.talking or self.waiting_turn is not None:
                self.reset_voice_activity()
            return
        self.observe_voice_onset(stamp, voiced)
        self.vad.external_voice = voiced
        was_talking = self.vad.talking
        retained_audio = getattr(self.vad, 'audio', None)
        retained = len(retained_audio) if retained_audio is not None else None
        self.vad_stamps.append((stamp, capture_echo_risk))
        began, utterance, _ = self.vad.feed(cleaned)
        if utterance is UTTERANCE_TOO_LONG:
            self.waiting_turn = None
            self.waiting_last_sample_ns = None
            self.discard_overlong_utterance()
            return
        render_audio = self.reference_capture.feed(reference, began, utterance is not None)
        claimed_wake = (
            self.hooks.claim_wake() if began or self.vad.talking or utterance is not None else False
        )
        if began:
            prefix_frames = len(getattr(self.vad, 'audio', ()))
            self.turn_prefix_echo_risk = (
                any(risk for _, risk in list(self.vad_stamps)[-prefix_frames:])
                if prefix_frames
                else False
            )
            first_stamp = (
                self.vad_stamps[-prefix_frames][0]
                if 0 < prefix_frames <= len(self.vad_stamps) and not self.turn_prefix_echo_risk
                else None
            )
            self.waiting_turn = self.hooks.wake_ticket(first_stamp)
            self.waiting_last_sample_ns = stamp
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
            self.turn_echo_risk = capture_echo_risk
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
        if was_talking and retained is not None and retained < MAX_UTTERANCE_FRAMES:
            # The endpoint can stop retaining trailing quiet at the cap.
            # Bind candidate receipts to the last actual PCM frame, not the
            # later completion trigger or an inferred length offset.
            self.waiting_last_sample_ns = stamp
        if not was_talking and not began:
            self.onset_prefix.feed(frame, voiced=voiced, credible=credible)
        if claimed_wake and self.voice_trace is not None:
            self.hooks.diagnostics().mark(self.voice_trace, 'wake_detected')
        if (
            self.vad.talking
            and credible
            and self.hooks.playing()
            and self.hooks.awake()
            and self.hooks.natural_barge_enabled()
            and (not self.hooks.paused())
        ):
            self.turn_pause_token = self.hooks.pause_voice(stamp)
            self.owns_turn_pause = self.turn_pause_token is not None
        if self.vad.talking or utterance is not None:
            self.turn_echo_risk |= capture_echo_risk
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
            if not self.hooks.wake_allowed() and (
                self.waiting_turn is None or self.turn_echo_risk or self.turn_during_playback
            ):
                self.hooks.diagnostics().gate(self.voice_trace, 'wake_closed')
                self.hooks.diagnostics().finish(self.voice_trace, 'rejected')
                self.hooks.metric('local_wake_utterances_withheld', increment=True)
                self.voice_trace = None
                return
            if not self.hooks.wake_allowed() or (
                self.waiting_turn is not None and not self.turn_local_wake
            ):
                # A different completed candidate may have just authorized
                # wake. This older endpoint must not borrow that authorization
                # and submit overlapping audio as a second ordinary question.
                ticket = self.waiting_turn
                if ticket is not None:
                    ticket = self.hooks.complete_english_ticket(ticket, self.waiting_last_sample_ns)
                job += (ticket,)
            self.hooks.enqueue(job)
            self.voice_trace = None

    def apply_fast_stop(self, keyword, stamp):
        return apply_fast_stop(self, keyword, stamp)

"""Local pre-ASR authorization and bounded wake conversation window.

Session owns awake/generation. Native decoding always finishes and releases its
lock before entering the session guard; resets use guard -> native lock order.
"""

from __future__ import annotations
from dataclasses import dataclass
from collections import deque
import time
import threading
from typing import Callable, ContextManager, Protocol
import numpy as np
from assistant_identity import WAKE_PATTERN
from session_ports import EventSink, MetricSink
from english_wake_candidates import EnglishWakeCandidates

from wake_confirmation import (
    complete_english_ticket,
    waiting_utterance,
    pending_chinese_utterance,
    complete_chinese_ticket,
    recheck_utterance,
)
from wake_model_status import (
    WAITING as WAITING,
    RECOVERING as RECOVERING,
    RECOVERY_FAILED as RECOVERY_FAILED,
    ENGLISH_RECOVERING as ENGLISH_RECOVERING,
    ENGLISH_RECOVERY_FAILED as ENGLISH_RECOVERY_FAILED,
    report_model_recovery,
    chinese_verifier_ready,
    english_input_samples,
)

KEYWORDS = frozenset(('大黄蜂', 'Bumblebee'))


class Detector(Protocol):
    def feed(self, pcm: np.ndarray, *, alternate_pcm: np.ndarray | None = None) -> str: ...
    def reset(self, *, clear_metrics: bool = True) -> None: ...


@dataclass(frozen=True)
class WakeHooks:
    lock: ContextManager[object]
    active: Callable[[], bool]
    generation: Callable[[], int]
    awake: Callable[[], bool]
    wake_mode: Callable[[], bool]
    capture_current: Callable[[int], bool]
    busy: Callable[[], bool]
    set_awake: Callable[[bool], None]
    touch_activity: Callable[[], None]
    cancel: Callable[[str], int]
    status: Callable[[str, str], None]
    metric: MetricSink
    log: EventSink
    render_risk: Callable[[], bool] = lambda: False


class WakeController:
    def __init__(
        self,
        hooks: WakeHooks,
        detector: Detector | None,
        *,
        window_seconds=60,
        clock=time.monotonic,
    ):
        if type(window_seconds) is not int or not 15 <= window_seconds <= 300:
            raise ValueError('Wake window must be 15–300 seconds')
        self.hooks = hooks
        self.detector = detector
        self.window_seconds = window_seconds
        self.clock = clock
        self.native_lock = threading.RLock()
        self.pending_prefix = False
        self.serial = 0
        self.deadline = None
        self.input_probe = None
        self.reading_input_probe = None
        self.pending_chinese = deque(maxlen=16)
        self.complete_chinese_jobs = {}
        self.complete_risk = False
        self.last_recovery_status = None
        self.english_candidates = EnglishWakeCandidates()

    @property
    def complete_english_enabled(self):
        return getattr(self.detector, 'complete_english_enabled', False) is True

    def _english_generation(self):
        value = getattr(self.detector, 'english_recovery_generation', 0)
        return value if type(value) is int and value >= 0 else 0

    def complete_english_ticket(self, base, last_sample_ns):
        return complete_english_ticket(self, base, last_sample_ns)

    @property
    def ready(self):
        return self.detector is not None

    @property
    def chinese_verifier_configured(self):
        return (
            getattr(self.detector, 'chinese_verifier', None) is not None
            or self.complete_chinese_enabled
        )

    @property
    def complete_chinese_enabled(self):
        return getattr(self.detector, 'complete_chinese_enabled', False) is True

    @property
    def complete_recovery_snapshot(self):
        value = getattr(self.detector, 'complete_recovery_snapshot', None)
        return value if isinstance(value, dict) else None

    def _model_generation(self):
        value = getattr(self.detector, 'complete_recovery_generation', 0)
        return value if type(value) is int and value >= 0 else 0

    def _report_model_recovery(self):
        return report_model_recovery(self)

    @property
    def chinese_verifier_ready(self):
        return chinese_verifier_ready(self)

    def english_input_samples(self):
        return english_input_samples(self)

    def allowed(self):
        # Continuous mode starts awake, but its 15-minute black-screen standby
        # also requires a local wake. Never fall back to cloud text spotting.
        return (
            self.hooks.active()
            and self.hooks.awake()
            and (not self.hooks.wake_mode() or self.ready)
        )

    @property
    def utterance_recheck_ready(self):
        return getattr(self.detector, 'utterance_recheck_supported', False) is True and callable(
            getattr(self.detector, 'recheck_utterance', None)
        )

    def waiting_utterance(self, first_sample_ns):
        return waiting_utterance(self, first_sample_ns)

    def pending_chinese_utterance(self, base):
        return pending_chinese_utterance(self, base)

    def complete_chinese_ticket(self, base, last_sample_ns):
        return complete_chinese_ticket(self, base, last_sample_ns)

    def recheck_utterance(self, ticket, pcm, generation):
        return recheck_utterance(self, ticket, pcm, generation)

    def arm_input_probe(self, pcm, *, chinese=False):
        from wake_input_probe import WakeInputProbe

        with self.hooks.lock:
            counts = self.english_input_samples()
            if counts is None or counts['alternate'] is None:
                raise RuntimeError('Wake input accounting unavailable')
            if self.reading_input_probe is not None or (
                self.input_probe is not None and not self.input_probe.closed
            ):
                raise RuntimeError('Wake input probe already armed')
            self.input_probe = WakeInputProbe(pcm, chinese=True) if chinese else WakeInputProbe(pcm)
            return {'status': 'armed', 'token': self.input_probe.token}

    def read_input_probe(self, token):
        with self.hooks.lock:
            probe = self.input_probe
            if probe is None or token != probe.token:
                return {'status': 'unavailable'}
            self.input_probe = None
            self.reading_input_probe = probe
            serial = self.serial
        try:
            receipt = probe.read()
        finally:
            with self.hooks.lock:
                self.reading_input_probe = None
                current = serial == self.serial and self.hooks.active() and not self.hooks.awake()
        return receipt if current else {'status': 'unavailable'}

    def _cancel_input_probe(self):
        probe, self.input_probe = self.input_probe, None
        if probe is not None:
            probe.cancel()
        if self.reading_input_probe is not None:
            self.reading_input_probe.cancel()

    def reset(self, reason):
        if reason not in ('start', 'stop', 'mode', 'cancel', 'transport', 'capture_gap', 'rebind'):
            raise ValueError('Invalid local wake reset reason')
        with self.hooks.lock:
            if reason == 'rebind':
                if self.hooks.active() and (start := getattr(self.detector, 'start_capture', None)):
                    with self.native_lock:
                        start()
                return
            self.serial += 1
            self.english_candidates.reset()
            self.pending_chinese.clear()
            self.complete_chinese_jobs.clear()
            self.complete_risk = False
            if reason in ('start', 'stop', 'transport'):
                self.last_recovery_status = None
            self.pending_prefix = False
            self._cancel_input_probe()
            if self.detector is not None:
                with self.native_lock:
                    if reason in ('stop', 'transport') or not self.hooks.active():
                        if stop := getattr(self.detector, 'stop_capture', None):
                            stop()
                    self.detector.reset(clear_metrics=reason == 'start')
                    if reason == 'start' and self.hooks.active():
                        if start := getattr(self.detector, 'start_capture', None):
                            start()
            if not self.hooks.active():
                self.deadline = None
            elif reason not in ('cancel', 'transport', 'capture_gap'):
                self.deadline = (
                    self.clock() + self.window_seconds
                    if self.hooks.active() and self.hooks.awake() and self.hooks.wake_mode()
                    else None
                )

    def awake_changed(self, awake):
        with self.hooks.lock:
            self.english_candidates.reset()
            self.pending_chinese.clear()
            self.complete_chinese_jobs.clear()
            self._cancel_input_probe()
            self.deadline = (
                self.clock() + self.window_seconds
                if awake and self.hooks.active() and self.hooks.wake_mode()
                else None
            )
            if not awake:
                self.pending_prefix = False
                self.serial += 1
                if self.detector is not None:
                    with self.native_lock:
                        self.detector.reset(clear_metrics=False)

    def claim_prefix(self):
        with self.hooks.lock:
            pending = self.pending_prefix
            self.pending_prefix = False
            return pending

    def touch(self):
        with self.hooks.lock:
            if self.hooks.active() and self.hooks.awake() and self.hooks.wake_mode():
                self.deadline = self.clock() + self.window_seconds

    def feed(
        self,
        pcm,
        generation,
        *,
        capture_nanos,
        echo_risk=False,
        render_text='',
        alternate_pcm=None,
        raw_pcm=None,
    ):
        with self.hooks.lock:
            if (
                self.detector is None
                or not self.hooks.active()
                or generation != self.hooks.generation()
                or not self.hooks.capture_current(capture_nanos)
            ):
                return False
            awake = self.hooks.awake()
            if awake and not (self.complete_chinese_enabled or self.complete_english_enabled):
                return False
            if (
                not awake
                and (self.complete_chinese_enabled or self.complete_english_enabled)
                and bool(echo_risk) != self.complete_risk
            ):
                self.complete_risk = bool(echo_risk)
                self.serial += 1
                self.english_candidates.reset()
                self.pending_chinese.clear()
                self.complete_chinese_jobs.clear()
                # A queued confirmation cannot survive a completed playback
                # interval, even if it is processed after the tail risk ends.
                with self.native_lock:
                    self.detector.reset(clear_metrics=False)
            serial = self.serial
            probe = self.input_probe
        # The model actor has its own capture lease and never calls the session.
        # Do not run it under native_lock: a commit/route callback can need that
        # lock while a model-generation transition waits on the commit guard.
        if self.complete_chinese_enabled or self.complete_english_enabled:
            poll = getattr(self.detector, 'poll_capture', None)
            if callable(poll):
                poll()
        # No session callbacks inside this native call or its lock.
        with self.native_lock:
            # A reset between admission and native entry must not inject an old
            # frame into a fresh stream, even though its result would be rejected.
            if serial != self.serial:
                return False
            if awake:
                # Never feed keyword streams during an awake conversation.
                return False
            # Snapshot under the same lock as the first observed decoder input.
            stream_samples = None
            if probe is not None and not probe.frames:
                stream_samples = getattr(
                    self.detector,
                    'chinese_stream_samples' if probe.chinese else 'english_stream_samples',
                    None,
                )
            if echo_risk:
                alternate_pcm = None
            english_generation = self._english_generation()
            if alternate_pcm is not None:
                keyword = self.detector.feed(pcm, alternate_pcm=alternate_pcm)
            else:
                keyword = self.detector.feed(pcm)
            english_candidate = (
                getattr(self.detector, 'english_candidate_hit', False) is True
                and english_generation == self._english_generation()
            )
        with self.hooks.lock:
            recognized = isinstance(keyword, str) and keyword in KEYWORDS
            if (
                serial != self.serial
                or not self.hooks.active()
                or self.hooks.awake()
                or generation != self.hooks.generation()
            ):
                return False
            if english_candidate and not echo_risk and self.complete_english_enabled:
                if self.english_candidates.mark(
                    capture_nanos,
                    serial=serial,
                    generation=generation,
                    model_generation=english_generation,
                ):
                    self.hooks.metric('local_wake_english_candidates', increment=True)
            if not recognized and probe is None:
                self._report_model_recovery()
                return False
            render_rejected = recognized and echo_risk and bool(WAKE_PATTERN.search(render_text))
            if probe is not None and probe is self.input_probe:
                if probe.chinese and echo_risk:
                    self._cancel_input_probe()
                else:
                    probe.observe(
                        pcm,
                        alternate_pcm,
                        raw_pcm if not echo_risk else None,
                        stream_samples=stream_samples,
                        english_render_rejected=render_rejected and keyword == 'Bumblebee',
                    )
            if not recognized:
                return False
            if keyword == '大黄蜂' and self.complete_chinese_enabled:
                if echo_risk:
                    self.pending_chinese.clear()
                    return False
                self.pending_chinese.append(capture_nanos)
                self.hooks.metric('local_wake_chinese_candidates', increment=True)
                return False
            if render_rejected:
                self.hooks.metric('wake_render_rejections', increment=True)
                self.hooks.log('local_wake_render_rejected')
                return False
            self.hooks.set_awake(True)
            self.pending_prefix = True
            self.hooks.touch_activity()
            self.deadline = self.clock() + self.window_seconds if self.hooks.wake_mode() else None
            self.hooks.metric('local_wake_count', increment=True)
            if keyword == 'Bumblebee':
                self.hooks.metric('local_wake_english_count', increment=True)
            self.hooks.log('local_wake_accepted', keyword=keyword)
            self.hooks.status('listening', '我在听，请继续说')
            return True

    def tick(self):
        with self.hooks.lock:
            if not self.hooks.active() or not self.hooks.awake():
                return
            if not self.hooks.wake_mode():
                self.deadline = None
                return
            now = self.clock()
            if self.hooks.busy() or self.deadline is None:
                self.deadline = now + self.window_seconds
                return
            if now < self.deadline:
                return
            self.hooks.set_awake(False)
            self.hooks.cancel('wake_timeout')
            self.deadline = None
            self.hooks.status('listening', WAITING)
            self.hooks.log('local_wake_window_expired')

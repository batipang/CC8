"""Local pre-ASR authorization and bounded wake conversation window.

Session owns awake/generation. Native decoding always finishes and releases its
lock before entering the session guard; resets use guard -> native lock order.
"""

from __future__ import annotations
from dataclasses import dataclass
from collections import deque
from contextlib import nullcontext
import time
import threading
import secrets
from typing import Callable, ContextManager, Protocol
import numpy as np
from assistant_identity import WAKE_PATTERN
from session_ports import EventSink, MetricSink
from wake_utterance import WaitingUtterance
from english_wake_candidates import (
    EnglishWakeCandidates,
    EnglishConfirmation,
    EnglishWaitingUtterance,
)

KEYWORDS = frozenset(('大黄蜂', 'Bumblebee'))
WAITING = '等待唤醒 · 请说“大黄蜂”或“Bumblebee”'
RECOVERING = '本地中文唤醒正在恢复，请稍后再说'
RECOVERY_FAILED = '本地中文唤醒复核暂不可用，请暂停语音后重新开始'


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
        with self.hooks.lock:
            if (
                not self.complete_english_enabled
                or type(base) is not EnglishWaitingUtterance
                or base.serial != self.serial
                or base.generation != self.hooks.generation()
                or not self.hooks.active()
                or self.hooks.awake()
                or self.hooks.render_risk()
                or not self.hooks.capture_current(base.first_sample_ns)
                or type(last_sample_ns) is not int
                or not self.hooks.capture_current(last_sample_ns)
            ):
                return base
            return (
                self.english_candidates.issue(
                    base, last_sample_ns, model_generation=self._english_generation()
                )
                or base
            )

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
        # Called only under the session guard for a current waiting frame.
        value = self.complete_recovery_snapshot
        if value is None:
            return
        state = value['state']
        key = (self._model_generation(), state)
        if (
            key == self.last_recovery_status
            or state == 'stopped'
            or self.hooks.render_risk()
            or self.hooks.busy()
        ):
            return
        self.last_recovery_status = key
        self.hooks.status(
            # The shipped HUD replaces listening detail with a generic label.
            # Use its visible recoverable-error surface until ready again.
            'listening' if state == 'ready' else 'error',
            WAITING if state == 'ready' else RECOVERY_FAILED if state == 'failed' else RECOVERING,
        )

    @property
    def chinese_verifier_ready(self):
        if not self.chinese_verifier_configured:
            return False
        verifier = getattr(self.detector, 'chinese_verifier', None)
        worker = (
            verifier.worker
            if verifier is not None
            else getattr(self.detector, 'chinese_complete', None)
        )
        if worker is None:
            return False
        recovery = self.complete_recovery_snapshot
        if recovery is not None:
            # Capture frames own readiness polling and recovery. Health must
            # not wait for a concurrent process spawn/close state lock.
            return worker.last_ready is True and recovery['state'] == 'ready'
        if not self.native_lock.acquire(blocking=False):
            return worker.last_ready is True
        try:
            return worker.ready is True
        finally:
            self.native_lock.release()

    def english_input_samples(self):
        # A stalled decoder must not block the service health endpoint.
        if not self.native_lock.acquire(timeout=0.01):
            return None
        try:
            counts = getattr(self.detector, 'english_input_samples', None)
            if type(counts) is not tuple or len(counts) != 2:
                return None
            primary, alternate = counts
            return {'primary': primary, 'alternate': alternate}
        finally:
            self.native_lock.release()

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
        with self.hooks.lock:
            if (
                type(first_sample_ns) is not int
                or first_sample_ns < 0
                or not self.utterance_recheck_ready
                or not self.hooks.active()
                or self.hooks.awake()
                or self.hooks.render_risk()
                or not self.hooks.capture_current(first_sample_ns)
            ):
                return None
            ticket = WaitingUtterance(
                self.serial,
                self.hooks.generation(),
                first_sample_ns,
                model_generation=self._model_generation(),
            )
            if self.complete_english_enabled:
                return EnglishWaitingUtterance(
                    ticket.serial,
                    ticket.generation,
                    ticket.first_sample_ns,
                    model_generation=ticket.model_generation,
                    verifier_generation=self._english_generation(),
                )
            return ticket

    def pending_chinese_utterance(self, base):
        """Read-only busy evidence, never an audio/cloud authorization."""
        with self.hooks.lock:
            return (
                self.complete_chinese_enabled
                and isinstance(base, WaitingUtterance)
                and base.serial == self.serial
                and base.generation == self.hooks.generation()
                and base.model_generation == self._model_generation()
                and self.hooks.active()
                and not self.hooks.awake()
                and self.hooks.capture_current(base.first_sample_ns)
                and any(stamp >= base.first_sample_ns for stamp in self.pending_chinese)
            )

    def complete_chinese_ticket(self, base, last_sample_ns):
        with self.hooks.lock:
            if (
                not self.complete_chinese_enabled
                or not isinstance(base, WaitingUtterance)
                or base.serial != self.serial
                or base.generation != self.hooks.generation()
                or base.model_generation != self._model_generation()
                or type(last_sample_ns) is not int
                or last_sample_ns < base.first_sample_ns
                or not self.hooks.active()
                or self.hooks.awake()
                or self.hooks.render_risk()
                or not self.hooks.capture_current(base.first_sample_ns)
                or not self.hooks.capture_current(last_sample_ns)
            ):
                return None
            matches = [
                stamp
                for stamp in self.pending_chinese
                if base.first_sample_ns <= stamp <= last_sample_ns
            ]
            if not matches:
                return None
            self.pending_chinese = deque(
                (stamp for stamp in self.pending_chinese if stamp > last_sample_ns), maxlen=16
            )
            token = secrets.token_hex(16)
            if len(self.complete_chinese_jobs) == 8:
                del self.complete_chinese_jobs[next(iter(self.complete_chinese_jobs))]
            self.complete_chinese_jobs[token] = (
                base.serial,
                base.generation,
                base.first_sample_ns,
                last_sample_ns,
                base.model_generation,
            )
            return WaitingUtterance(
                base.serial,
                base.generation,
                base.first_sample_ns,
                keyword='大黄蜂',
                last_sample_ns=last_sample_ns,
                candidate_token=token,
                model_generation=base.model_generation,
            )

    def recheck_utterance(self, ticket, pcm, generation):
        detector = self.detector
        chinese = isinstance(ticket, WaitingUtterance) and ticket.keyword == '大黄蜂'
        english = type(ticket) is EnglishConfirmation
        english_owned = False
        model_generation = None
        if english:
            with self.hooks.lock:
                english_owned = self.english_candidates.consume(ticket)
        if chinese:
            with self.hooks.lock:
                issued = self.complete_chinese_jobs.get(ticket.candidate_token)
                if (
                    issued is None
                    or len(issued) != 5
                    or issued[4] != ticket.model_generation
                    or issued[:4]
                    != (
                        ticket.serial,
                        ticket.generation,
                        ticket.first_sample_ns,
                        ticket.last_sample_ns,
                    )
                ):
                    return None
                model_generation = issued[4]
                del self.complete_chinese_jobs[ticket.candidate_token]

        def current_locked():
            return (
                isinstance(ticket, WaitingUtterance)
                and detector is self.detector
                and self.utterance_recheck_ready
                and ticket.serial == self.serial
                and ticket.generation == generation == self.hooks.generation()
                and self.hooks.active()
                and not self.hooks.awake()
                and not self.hooks.render_risk()
                and self.hooks.capture_current(ticket.first_sample_ns)
                and (
                    not chinese
                    or (
                        self.complete_chinese_enabled
                        and self._model_generation() == model_generation
                        and self.hooks.capture_current(ticket.last_sample_ns)
                    )
                )
            )

        def current():
            with self.hooks.lock:
                return current_locked()

        def english_current():
            with self.hooks.lock:
                return (
                    current_locked()
                    and english_owned
                    and self.complete_english_enabled
                    and ticket.verifier_generation == self._english_generation()
                    and self.hooks.capture_current(ticket.last_sample_ns)
                )

        decode = getattr(detector, 'recheck_chinese' if chinese else 'recheck_utterance', None)
        if not callable(decode) or not current():
            return None
        self.hooks.metric('local_wake_rechecks', increment=True)
        if chinese:
            self.hooks.metric('local_wake_chinese_rechecks', increment=True)
        started = time.perf_counter()
        english_used = False
        try:
            hit = decode(pcm, current)
            # Always preserve the original completed 8-path decision first.
            # Optional receipt/model failure may not veto its positives.
            if hit is False and english and english_current():
                confirm = getattr(detector, 'recheck_english', None)
                if callable(confirm):
                    english_used = True
                    self.hooks.metric('local_wake_english_confirmations', increment=True)
                    hit = confirm(pcm, english_current)
        except Exception as error:
            # The optional local path fails closed; its failure must neither
            # authorize cloud ASR nor report an ASR/provider error to the user.
            self.hooks.metric('local_wake_recheck_errors', increment=True)
            self.hooks.log('local_wake_recheck_failed', error_type=type(error).__name__)
            hit = None
        with self.hooks.lock:
            self.hooks.metric(
                'local_wake_recheck_ms', round((time.perf_counter() - started) * 1000, 2)
            )
            commit = (
                getattr(detector, 'english_recovery_commit', None)
                if english_used
                else getattr(detector, 'complete_recovery_commit', None)
                if chinese
                else None
            )
            with commit() if callable(commit) else nullcontext():
                if not current_locked() or (english_used and not english_current()):
                    return None
                if type(hit) is bool and hit:
                    # Model generation and the wake-state commit share only
                    # this short guard. Release it before any route callback.
                    self.hooks.set_awake(True)
                    self.pending_prefix = False
            if type(hit) is not bool:
                if chinese:
                    self.hooks.metric('local_wake_chinese_unavailable', increment=True)
                    recovery = self.complete_recovery_snapshot
                    self.hooks.status(
                        'error',
                        RECOVERING
                        if recovery and recovery['state'] not in ('failed', 'stopped')
                        else RECOVERY_FAILED,
                    )
                return None
            if hit:
                # This completed job owns the prefix; the next utterance must
                # not inherit it through the streaming prefix handoff.
                self.hooks.touch_activity()
                if (
                    not self.hooks.active()
                    or not self.hooks.awake()
                    or ticket.serial != self.serial
                    or generation != self.hooks.generation()
                ):
                    return None
                self.deadline = (
                    self.clock() + self.window_seconds if self.hooks.wake_mode() else None
                )
                self.hooks.metric('local_wake_count', increment=True)
                if not chinese:
                    self.hooks.metric('local_wake_english_count', increment=True)
                else:
                    self.hooks.metric('local_wake_chinese_recheck_hits', increment=True)
                self.hooks.metric('local_wake_recheck_hits', increment=True)
                self.hooks.log('local_wake_recheck_accepted')
                self.hooks.status('listening', '我在听，请继续说')
            return hit

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

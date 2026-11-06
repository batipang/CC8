"""Local pre-ASR authorization and bounded wake conversation window.

Session owns awake/generation. Native decoding always finishes and releases its
lock before entering the session guard; resets use guard -> native lock order.
"""

from __future__ import annotations
from dataclasses import dataclass
import time
import threading
from typing import Callable, ContextManager, Protocol
import numpy as np
from assistant_identity import WAKE_PATTERN
from session_ports import EventSink, MetricSink

KEYWORDS = frozenset(('大黄蜂', 'Bumblebee'))
WAITING = '等待唤醒 · 请说“大黄蜂”或“Bumblebee”'


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

    @property
    def ready(self):
        return self.detector is not None

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
        if reason not in ('start', 'stop', 'mode', 'cancel', 'transport', 'capture_gap'):
            raise ValueError('Invalid local wake reset reason')
        with self.hooks.lock:
            self.serial += 1
            self.pending_prefix = False
            self._cancel_input_probe()
            if self.detector is not None:
                with self.native_lock:
                    self.detector.reset(clear_metrics=reason == 'start')
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
                or self.hooks.awake()
                or generation != self.hooks.generation()
                or not self.hooks.capture_current(capture_nanos)
            ):
                return False
            serial = self.serial
            probe = self.input_probe
        # No session callbacks inside this native call or its lock.
        with self.native_lock:
            # A reset between admission and native entry must not inject an old
            # frame into a fresh stream, even though its result would be rejected.
            if serial != self.serial:
                return False
            if echo_risk:
                alternate_pcm = None
            if alternate_pcm is not None:
                keyword = self.detector.feed(pcm, alternate_pcm=alternate_pcm)
            else:
                keyword = self.detector.feed(pcm)
        with self.hooks.lock:
            recognized = isinstance(keyword, str) and keyword in KEYWORDS
            if not recognized and probe is None:
                return False
            if (
                serial != self.serial
                or not self.hooks.active()
                or self.hooks.awake()
                or generation != self.hooks.generation()
            ):
                return False
            if probe is not None and probe is self.input_probe:
                if probe.chinese and echo_risk:
                    self._cancel_input_probe()
                else:
                    probe.observe(pcm, alternate_pcm, raw_pcm if probe.chinese else None)
            if not recognized:
                return False
            if echo_risk and WAKE_PATTERN.search(render_text):
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

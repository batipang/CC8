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
    def feed(self, pcm: np.ndarray) -> str: ...
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

    @property
    def ready(self):
        return self.detector is not None

    def allowed(self):
        # Continuous mode starts awake, but its 15-minute black-screen standby
        # also requires a local wake. Never fall back to cloud text spotting.
        return (
            self.hooks.active()
            and self.hooks.awake()
            and (not self.hooks.wake_mode() or self.ready)
        )

    def reset(self, reason):
        if reason not in ('start', 'stop', 'mode', 'cancel', 'transport', 'capture_gap'):
            raise ValueError('Invalid local wake reset reason')
        with self.hooks.lock:
            self.serial += 1
            self.pending_prefix = False
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

    def feed(self, pcm, generation, *, capture_nanos, echo_risk=False, render_text=''):
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
        # No session callbacks inside this native call or its lock.
        with self.native_lock:
            # A reset between admission and native entry must not inject an old
            # frame into a fresh stream, even though its result would be rejected.
            if serial != self.serial:
                return False
            keyword = self.detector.feed(pcm)
        with self.hooks.lock:
            if (
                not isinstance(keyword, str)
                or keyword not in KEYWORDS
                or serial != self.serial
                or not self.hooks.active()
                or self.hooks.awake()
                or generation != self.hooks.generation()
            ):
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

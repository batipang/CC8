"""Own tentative barge-in pauses and their applied device receipts."""

from __future__ import annotations
from dataclasses import dataclass
import threading
import time
from typing import Callable, ContextManager

from diagnostics import TraceBook
from protocol import JsonValue
from session_ports import EventSink, MetricSink


@dataclass(frozen=True)
class PauseHooks:
    lock: ContextManager[object]
    generation: Callable[[], int]  # Read-only, non-locking session generation accessor.
    active: Callable[[], bool]
    valid: Callable[[int], bool]
    cancel_session: Callable[[str], int]
    event: EventSink
    status: Callable[[str, str], None]
    log: EventSink
    metric: MetricSink
    diagnostics: Callable[[], TraceBook]
    trace: Callable[[], str | None]
    onset: Callable[[], int | None]
    echo_metrics: Callable[[], dict[str, JsonValue]]
    utterance_paused: Callable[[int], bool]
    cancel_timeline_pause: Callable[[], None]
    pause_timeline: Callable[[int], None]
    resume_timeline: Callable[[int], None]


@dataclass
class PauseReceipt:
    generation: int
    trace: str | None
    trigger_nanos: int | None = None
    onset_nanos: int | None = None
    pause_nanos: int | None = None
    resume_requested: bool = False


class PlaybackPause:
    MAX_PENDING_PAUSE_RECEIPTS = 8

    def __init__(
        self,
        hooks: PauseHooks,
        *,
        publication_lock: ContextManager[object],
        done: threading.Condition,
        output_ready: Callable[[], bool],
    ):
        self.hooks = hooks
        # Playback supplies its existing locks. Pause admission keeps session
        # guard -> publication, and wait releases completion before cancelling.
        self.publication_lock = publication_lock
        self.done = done
        self.output_ready = output_ready
        self.pause_state = None
        self.pause_serial = 0
        self.pause_receipts: dict[int, PauseReceipt] = {}
        self.last_pause_receipt_nanos = None
        self.device_can_pause = False
        self.device_can_confirm_pause = False
        self.pause_cooldown = 0

    def clear(self):
        self.pause_state = None
        self.pause_receipts.clear()
        self.last_pause_receipt_nanos = None

    def clear_cancelled(self):
        self.clear()
        self.hooks.cancel_timeline_pause()

    def clear_transport(self):
        self.clear()
        self.device_can_confirm_pause = False

    def set_capability(self, enabled, *, applied_receipt=False):
        self.device_can_pause = enabled
        self.device_can_confirm_pause = enabled and applied_receipt

    def accept_receipt(self, name, event):
        token, generation, nanos = event['token'], event['generation'], event['nanos']
        with self.hooks.lock:
            receipt = self.pause_receipts.get(token)
            if (
                receipt is None
                or receipt.generation != generation
                or generation != self.hooks.generation()
                or (
                    self.last_pause_receipt_nanos is not None
                    and nanos < self.last_pause_receipt_nanos
                )
            ):
                return
            if name == 'playback_paused':
                if receipt.pause_nanos is not None:
                    return
                receipt.pause_nanos = nanos
                # Device receipts arrive on an ordered channel. Only a newer
                # device pause supersedes prior tokens, not a host request.
                for older in tuple(self.pause_receipts):
                    if older >= token:
                        break
                    self.pause_receipts.pop(older)
                self.hooks.diagnostics().pause_ack(
                    receipt.trace, receipt.trigger_nanos, nanos, onset_nanos=receipt.onset_nanos
                )
                self.hooks.pause_timeline(nanos)
            else:
                if receipt.pause_nanos is None or not receipt.resume_requested:
                    return
                self.pause_receipts.pop(token)
                self.hooks.diagnostics().mark(receipt.trace, 'resume_ack')
                self.hooks.resume_timeline(nanos)
            self.last_pause_receipt_nanos = nanos

    def pause_for_voice(self, trigger_nanos=None):
        # A tentative pause retains generation; confirmed speech cancels later.
        with self.hooks.lock, self.publication_lock:
            if (
                not self.device_can_pause
                or not self.hooks.active()
                or not self.output_ready()
                or self.pause_state
                or (time.monotonic() < self.pause_cooldown)
            ):
                return None
            self.pause_serial += 1
            token = self.pause_serial
            trace = self.hooks.trace()
            generation = self.hooks.generation()
            if len(self.pause_receipts) >= self.MAX_PENDING_PAUSE_RECEIPTS:
                self.pause_receipts.pop(next(iter(self.pause_receipts)))
            self.pause_receipts[token] = PauseReceipt(
                generation,
                trace,
                trigger_nanos if self.device_can_confirm_pause else None,
                self.hooks.onset() if self.device_can_confirm_pause else None,
            )
            self.pause_state = {
                'token': token,
                'generation': generation,
                'started': time.monotonic(),
            }
        self.hooks.diagnostics().mark(trace, 'pause_request')
        self.hooks.event('playback_pause', generation=generation, token=token)
        self.hooks.metric('barge_in_pauses', increment=True)
        self.hooks.log('barge_in_tentative_pause', token=token, **self.hooks.echo_metrics())
        return token

    def resume_after_rejection(self, token, generation):
        with self.hooks.lock:
            state = self.pause_state
            if (
                token is None
                or not state
                or state['token'] != token
                or (state['generation'] != generation)
                or (not self.hooks.valid(generation))
            ):
                return
            if self.hooks.utterance_paused(token):
                return
            receipt = self.pause_receipts.get(token)
            if receipt is not None:
                receipt.resume_requested = True
            self.pause_state = None
            self.pause_cooldown = time.monotonic() + 0.6
        self.hooks.event('playback_resume', generation=generation, token=token)
        self.hooks.metric('barge_in_resumed', increment=True)
        self.hooks.log('barge_in_rejected_resumed', token=token)
        with self.done:
            self.done.notify_all()

    def wait_playback(self, generation):
        if not self.pause_state:
            return 0.0
        start = time.monotonic()
        while self.hooks.valid(generation):
            state = self.pause_state
            if not state:
                break
            if time.monotonic() - state['started'] > 20:
                # Rejection can resume playback or give another pause ownership
                # within this generation while the timeout is being checked.
                with self.hooks.lock:
                    if not self.hooks.valid(generation):
                        break
                    if self.pause_state is not state:
                        continue
                    self.hooks.cancel_session('barge_in_timeout')
                    self.hooks.status('error', '插话识别超时，已停止旧播报')
                break
            with self.done:
                self.done.wait(0.02)
        return time.monotonic() - start

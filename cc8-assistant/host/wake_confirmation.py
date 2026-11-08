"""Complete-utterance wake admission, using the original controller as sole owner.

No state, locks or workers live here. Keep native decode outside the session
lock and the original model lease around only the final wake-state commit.
"""

from __future__ import annotations
from collections import deque
from contextlib import nullcontext
import secrets
import time
from typing import TYPE_CHECKING
from english_wake_candidates import EnglishConfirmation, EnglishWaitingUtterance
from wake_utterance import WaitingUtterance
from wake_model_status import RECOVERING, RECOVERY_FAILED

if TYPE_CHECKING:
    from wake_controller import WakeController


def complete_english_ticket(self: WakeController, base, last_sample_ns):
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


def waiting_utterance(self: WakeController, first_sample_ns):
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


def pending_chinese_utterance(self: WakeController, base):
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


def complete_chinese_ticket(self: WakeController, base, last_sample_ns):
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


def recheck_utterance(self: WakeController, ticket, pcm, generation):
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
        self.hooks.metric('local_wake_recheck_ms', round((time.perf_counter() - started) * 1000, 2))
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
            self.deadline = self.clock() + self.window_seconds if self.hooks.wake_mode() else None
            self.hooks.metric('local_wake_count', increment=True)
            if not chinese:
                self.hooks.metric('local_wake_english_count', increment=True)
            else:
                self.hooks.metric('local_wake_chinese_recheck_hits', increment=True)
            self.hooks.metric('local_wake_recheck_hits', increment=True)
            self.hooks.log('local_wake_recheck_accepted')
            self.hooks.status('listening', '我在听，请继续说')
        return hit

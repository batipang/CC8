"""Recover a local model only while its capture owner is still present.

This actor is polled by valid capture frames, never by health requests or an
independent timer. It neither starts recording nor replays a failed utterance.
The immutable view keeps health reporting independent of a slow process start.
"""

from dataclasses import dataclass
import threading
import time
from wake_verifier_process import STARTUP_SECONDS

BACKOFF_SECONDS = (2.0, 5.0)
MAX_BURST_STARTS = 3
STABLE_SECONDS = 30.0


@dataclass(frozen=True)
class RecoveryView:
    state: str = 'stopped'
    generation: int = 0
    start_attempts: int = 0
    budget_used: int = 0
    retry_at: float | None = None


class CaptureWakeRecovery:
    def __init__(self, worker, *, clock=time.monotonic):
        self.worker = worker
        self.clock = clock
        self._guard = threading.RLock()
        self._lease_guard = threading.RLock()
        self._wanted = False
        self._generation = self._starts = self._budget = 0
        self._retry_at = self._ready_since = None
        self._unready_since = None
        self._state = 'stopped'
        self._view = RecoveryView()

    def _publish(self):
        self._view = RecoveryView(
            self._state, self._generation, self._starts, self._budget, self._retry_at
        )

    @property
    def generation(self):
        return self._view.generation

    @property
    def available(self):
        return self._view.state == 'ready'

    @property
    def snapshot(self):
        # No guard/worker calls here: a reporter must not wait for spawn/close.
        view = self._view
        return {
            'state': view.state,
            'start_attempts': view.start_attempts,
            'budget_used': view.budget_used,
            'retry_in_ms': (
                max(0, min(5000, round((view.retry_at - self.clock()) * 1000)))
                if view.retry_at is not None
                else 0
            ),
        }

    def _attempt(self):
        with self._lease_guard:
            self._generation += 1
            self._starts += 1
            self._budget += 1
            self._retry_at = self._ready_since = None
            self._state = 'starting'
            self._unready_since = self.clock()
            self._publish()
        try:
            self.worker.start()
        except (OSError, ValueError, RuntimeError):
            # The process adapter normally contains these errors. Preserve the
            # bounded budget if a failed start raises before returning a handle.
            try:
                self.worker.close()
            except (OSError, ValueError, RuntimeError):
                self._state = 'failed'
                self._budget = MAX_BURST_STARTS
                self._publish()

    def start_capture(self):
        with self._guard:
            if self._wanted:
                return
            self._wanted = True
            self._starts = self._budget = 0
            self._retry_at = self._ready_since = None
            self._attempt()

    def stop_capture(self):
        with self._guard:
            with self._lease_guard:
                self._wanted = False
                self._generation += 1
                self._retry_at = self._ready_since = None
                self._state = 'stopped'
                self._publish()
            self.worker.close()

    def commit_guard(self):
        # Only fence the final wake-state mutation. Never hold this guard over
        # process spawn/close or activity/route publication callbacks.
        return self._lease_guard

    def poll_capture(self):
        with self._guard:
            if not self._wanted or self._state == 'failed':
                return
            now = self.clock()
            try:
                ready = self.worker.ready is True
                running = self.worker.running is True
            except (OSError, ValueError, RuntimeError):
                with self._lease_guard:
                    self._generation += 1
                    self._state = 'failed'
                    self._retry_at = self._ready_since = None
                    self._publish()
                try:
                    self.worker.close()
                except (OSError, ValueError, RuntimeError):
                    pass
                return
            if ready:
                self._unready_since = None
                self._state = 'ready'
                self._retry_at = None
                if self._ready_since is None:
                    self._ready_since = now
                if now - self._ready_since >= STABLE_SECONDS:
                    self._budget = 0
                self._publish()
                return
            self._ready_since = None
            if running:
                if self._unready_since is None:
                    self._unready_since = now
                # Includes a cold start and a killed process awaiting reap.
                # Never replace either before the process adapter releases it.
                if (
                    self._unready_since is not None
                    and now - self._unready_since > STARTUP_SECONDS + 1
                ):
                    with self._lease_guard:
                        self._generation += 1
                        self._state = 'failed'
                        self._retry_at = None
                        self._publish()
                    try:
                        self.worker.close()
                    except (OSError, ValueError, RuntimeError):
                        # Keep the circuit closed even if adapter teardown
                        # fails. A later capture owner must explicitly retry.
                        pass
                else:
                    self._state = 'starting'
                    self._publish()
                return
            if self._budget >= MAX_BURST_STARTS:
                self._state = 'failed'
                self._retry_at = None
                self._publish()
                return
            if self._retry_at is None:
                self._retry_at = now + BACKOFF_SECONDS[min(max(self._budget - 1, 0), 1)]
                self._state = 'retry_wait'
                self._publish()
                return
            if now >= self._retry_at:
                self._attempt()

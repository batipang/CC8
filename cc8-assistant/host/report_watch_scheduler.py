"""Explicit foreground grants, bounded background reads and receipt-only notice settlement."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable, ContextManager
from report_checkpoint import source_from
from report_watch_store import DETAILS
from dot_client import Cancelled


@dataclass(frozen=True)
class WatchHooks:
    lock: ContextManager[object]
    owner: Callable[[], object]
    activation: Callable[[], object]
    generation: Callable[[], int]
    blocked: Callable[[], bool]
    idle: Callable[[], bool]
    ready: Callable[[], bool]
    send: Callable
    error: Callable[[str], None] = lambda kind: None
    send_summary: Callable | None = None
    cancel: Callable[[object], None] = lambda job: None
    owner_access: Callable[[], bool] = lambda: True


class WatchScheduler:
    def __init__(self, store, reader, hooks, *, clock=time.time, monotonic=time.monotonic):
        self.store, self.reader, self.hooks = store, reader, hooks
        self.clock, self.monotonic = clock, monotonic
        self.grant = self.last_activation = None
        self.reading = self.worker = self.pending = None
        self.due = 0
        self.poll_id = None

    def arm(self):
        """Called only after explicit configuration consent, or a new normal Start identity."""
        with self.hooks.lock:
            if not self.hooks.owner_access():
                self.revoke()
                return
            owner = self.hooks.owner()
            if owner is None or self.hooks.blocked():
                return
            state = self.store.snapshot()
            if state['watch'] is not None and state['watch']['enabled']:
                self.grant = (owner, state['watch']['id'])
            self.last_activation = self.hooks.activation()

    def revoke(self):
        with self.hooks.lock:
            self.grant = self.reading = None
            self.clear_results()

    def clear_results(self):
        clear = getattr(self.reader, 'clear_results', None)
        if clear is not None:
            clear()

    def owner_current(self, grant):
        if not self.hooks.owner_access() or self.hooks.blocked():
            # A maintenance/test interval revokes its prior grant as well. Ending
            # the lease is not a new user Start and must not resume history reads.
            self.grant = self.reading = None
            self.clear_results()
            return False
        return self.grant == grant and grant is not None and grant[0] == self.hooks.owner()

    def settle(self):
        if self.pending is None:
            return
        watch_id, attempt_id, channel, job, deadline = self.pending
        done = job.done.is_set()
        if not done and self.monotonic() < deadline:
            return
        if not done and channel == 'cc8':
            # Invalidate a queued/blocked old announcement before freezing its
            # uncertainty. The host cancels only this job's exact generation.
            try:
                self.hooks.cancel(job)
            except Exception as error:
                self.hooks.error(type(error).__name__)
        expected = 'notification_center' if channel == 'desktop' else 'completed'
        completed = done and getattr(job, 'result', None) == expected
        detail = expected if completed else getattr(job, 'detail', 'unconfirmed')
        if not isinstance(detail, str):
            detail = 'unconfirmed'
        detail = {
            'permission_denied': 'not_authorized',
            'not_installed': 'unavailable',
            'unsupported': 'unavailable',
            'rejected': 'failed',
            'submitted_unconfirmed': 'unconfirmed',
        }.get(detail, detail)
        if detail not in DETAILS:
            detail = 'unconfirmed'
        # A disk error leaves the exact receipt pending; never send TTS again.
        self.store.settle(watch_id, attempt_id, completed, detail=detail)
        self.pending = None

    def tick(self):
        with self.hooks.lock:
            if not self.hooks.owner_access():
                # No owner store lookup or prepared notice while visiting.
                # In-flight jobs prevent idle-only switching; their receipts
                # remain available for owner settlement rather than replay.
                self.revoke()
                self.last_activation = self.hooks.activation()
                return
            expire = getattr(self.reader, 'expire_results', None)
            if expire is not None:
                expire()
            self.settle()
            owner, activation = self.hooks.owner(), self.hooks.activation()
            if self.grant is not None and self.grant[0] != owner:
                self.revoke()
            if activation is not None and activation != self.last_activation:
                self.last_activation = activation
                self.arm()
            grant = self.grant
            if (
                not self.owner_current(grant)
                or self.pending is not None
                or self.reading is not None
                or self.worker is not None
                and self.worker.is_alive()
                or not self.hooks.idle()
                or not self.hooks.ready()
            ):
                return
            state = self.store.snapshot()
            watch = state['watch']
            if watch is None or watch['id'] != grant[1] or not watch['enabled']:
                self.revoke()
                return
            if watch['attempt'] is not None:
                # No matching live job means a crash/uncertain send, never a replay ticket.
                self.store.settle(watch['id'], watch['attempt']['id'], False)
                self.revoke()
                return
            if self.poll_id != watch['id']:
                self.poll_id, self.due = watch['id'], 0
            now = int(self.clock())
            last = watch['last_check']
            boundary = watch['started_at'] if last is None else last['at']
            clock_error = now < boundary
            if not clock_error and (
                now - boundary < watch['period_seconds'] or self.monotonic() < self.due
            ):
                return
            token = self.reading = object()
            self.due = self.monotonic() + watch['period_seconds']
            generation = self.hooks.generation()
            worker = threading.Thread(
                target=self.run,
                args=(token, grant, generation, now),
                daemon=True,
                name='cc8-report-watch',
            )
            self.worker = worker
            try:
                worker.start()
            except BaseException:
                self.reading = None
                raise

    def run(self, token, grant, generation, now):
        def current():
            with self.hooks.lock:
                return (
                    self.reading is token
                    and self.owner_current(grant)
                    and generation == self.hooks.generation()
                    and self.hooks.idle()
                )

        try:
            notice = self.reader.prepare(now, current)
            if notice is None:
                return
            with self.hooks.lock:
                if not current():
                    return
                if notice.segments and self.hooks.send_summary is None:
                    raise RuntimeError('Scheduled summary delivery unavailable')
                state = self.reader.claim(notice, current)
                watch, job = state['watch'], None
                attempt = watch['attempt']

                def claimed_owner():
                    # Starting literal playback changes generation/idle itself. Bind
                    # this guard to the connection and durable claim, not that change.
                    with self.hooks.lock:
                        if not self.owner_current(grant):
                            return False
                        live = self.store.snapshot()['watch']
                        return (
                            live is not None
                            and live['id'] == watch['id']
                            and live['enabled']
                            and live['attempt'] is not None
                            and live['attempt']['id'] == attempt['id']
                            and live['attempt']['status'] == 'claimed'
                            and self.reader.source_current(source_from(live['source']))
                        )

                try:
                    if not current() or not claimed_owner():
                        raise Cancelled('Report notice owner changed')
                    if notice.segments:

                        def confirmed(index):
                            if type(index) is not int or not 0 <= index <= len(notice.pairs):
                                raise ValueError('Invalid summary segment receipt')
                            if index:
                                key, fingerprint = notice.pairs[index - 1]
                                with self.hooks.lock:
                                    self.store.part(
                                        watch['id'],
                                        attempt['id'],
                                        dict(key=list(key), fingerprint=fingerprint),
                                    )

                        job = self.hooks.send_summary(
                            attempt['id'], notice.segments, claimed_owner, confirmed
                        )
                    else:
                        job = self.hooks.send(
                            watch['channel'], attempt['id'], notice.text, claimed_owner
                        )
                    if not callable(getattr(getattr(job, 'done', None), 'is_set', None)):
                        raise RuntimeError('Report notice delivery receipt unavailable')
                    self.pending = (
                        watch['id'],
                        attempt['id'],
                        watch['channel'],
                        job,
                        self.monotonic() + (600 if notice.segments else 90),
                    )
                except Exception:
                    self.store.settle(watch['id'], attempt['id'], False)
                    raise
        except Cancelled:
            pass
        except Exception as error:
            # Ownership change and durable uncertainty are observable in the store;
            # selected history and raw RPC errors never enter logs or retry queues.
            self.hooks.error(type(error).__name__)
        finally:
            with self.hooks.lock:
                if self.reading is token:
                    self.reading = None

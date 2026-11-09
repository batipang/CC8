"""Idle-only reminder delivery; owns scheduling and versioned pending receipt."""

from __future__ import annotations
from dataclasses import dataclass
import time
from typing import Callable, ContextManager
from announce import Announcement
from workspace import Workspace


@dataclass(frozen=True)
class ReminderHooks:
    lock: ContextManager[object]
    workspace: Callable[[], Workspace]
    publish_workspace: Callable[[], None]
    can_deliver: Callable[[], bool]
    touch: Callable[[], None]
    announce: Callable[[str], Announcement]
    delivery_owner: Callable[[], object] = lambda: None


class ReminderScheduler:
    def __init__(self, hooks: ReminderHooks):
        self.hooks = hooks
        self.job = None
        self.claims = []
        self.pending_finish = None
        self.poll = 0

    def action(self, args, is_current=lambda: True):
        with self.hooks.lock:
            result = self.hooks.workspace().reminder(args, is_current)
            if result.get('ok'):
                self.hooks.publish_workspace()
            return result

    def tick(self):
        # Do not interrupt conversations or turn on capture to deliver a reminder.
        with self.hooks.lock:
            job = self.job
            if job is not None:
                if not job.done.is_set():
                    return
                # A failed disk commit retries only the same receipt, never TTS.
                self.hooks.workspace().finish_reminders(self.claims, job.result == 'completed')
                self.job = None
                self.claims = []
                self.hooks.publish_workspace()
            if self.pending_finish is not None:
                self.hooks.workspace().finish_reminders(self.pending_finish, False)
                self.pending_finish = None
                self.hooks.publish_workspace()
            if time.monotonic() < self.poll:
                return
            self.poll = time.monotonic() + 5
            if self.hooks.workspace().expire_reminders():
                self.hooks.publish_workspace()
            if not self.hooks.can_deliver():
                return
            owner = self.hooks.delivery_owner()
            claims = self.hooks.workspace().claim_reminders()
            if not claims:
                return
            # From here, a crash or ambiguous send freezes these occurrences.
            # Only explicit rescheduling can offer them again; do not replay
            # a possibly heard reminder after a lost playback receipt.
            self.pending_finish = claims
            try:
                self.hooks.touch()
                if (
                    not self.hooks.can_deliver()
                    or self.hooks.delivery_owner() != owner
                    or not self.hooks.workspace().reminder_claims_current(claims)
                ):
                    self.hooks.workspace().finish_reminders(claims, False)
                    self.pending_finish = None
                    self.hooks.publish_workspace()
                    return
                text = '提醒你：' + (
                    claims[0]['title']
                    if len(claims) == 1
                    else '；'.join(f'{i + 1}，{item["title"]}' for i, item in enumerate(claims))
                )
                job = self.hooks.announce(text)
            except Exception:
                self.hooks.workspace().finish_reminders(claims, False)
                self.pending_finish = None
                self.hooks.publish_workspace()
                raise
            job.reminder_id = claims[0]['id']
            job.reminder_revision = claims[0]['revision']
            self.claims = claims
            self.job = job
            self.pending_finish = None
            # Publish only after admission. A display callback may admit a new
            # user request; this scheduler must not then cancel that newer work.
            self.hooks.publish_workspace()

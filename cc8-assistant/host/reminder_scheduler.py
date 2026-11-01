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


class ReminderScheduler:
    def __init__(self, hooks: ReminderHooks):
        self.hooks = hooks
        self.job = None
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
                self.hooks.workspace().delivered(
                    job.reminder_id, job.result == 'completed', revision=job.reminder_revision
                )
                self.job = None
                self.hooks.publish_workspace()
            if time.monotonic() < self.poll:
                return
            self.poll = time.monotonic() + 5
            if not self.hooks.can_deliver():
                return
            item = self.hooks.workspace().due()
            if item is None:
                return
            self.hooks.touch()
            job = self.hooks.announce('提醒你：' + item['title'])
            job.reminder_id = item['id']
            job.reminder_revision = item['revision']
            self.job = job

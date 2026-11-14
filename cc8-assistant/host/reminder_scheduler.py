"""Idle-only reminder delivery; owns scheduling and versioned pending receipt."""

from __future__ import annotations
from dataclasses import dataclass
import time
from typing import Callable, ContextManager
from announce import Announcement
from desktop_notifications import DesktopDelivery
from workspace import Workspace


@dataclass(frozen=True)
class ReminderHooks:
    lock: ContextManager[object]
    workspace: Callable[[], Workspace]
    publish_workspace: Callable[[], None]
    can_deliver: Callable[[], bool]
    touch: Callable[[], None]
    announce_segments: Callable[..., Announcement]
    delivery_owner: Callable[[], object] = lambda: None
    desktop_notify: Callable[[str, str, Callable[[], bool]], DesktopDelivery] | None = None
    owner_access: Callable[[], bool] = lambda: True


class ReminderScheduler:
    def __init__(self, hooks: ReminderHooks):
        self.hooks = hooks
        self.job = None
        self.claims = []
        self.played_count = 0
        self.pending_finish = None
        self.poll = 0
        self.channel = 'cc8'

    def action(self, args, is_current=lambda: True):
        with self.hooks.lock:
            if not self.hooks.owner_access():
                raise ValueError('访客模式不能访问主人提醒')
            result = self.hooks.workspace().reminder(args, is_current)
            if result.get('ok'):
                self.hooks.publish_workspace()
            return result

    def tick(self):
        # Do not interrupt conversations or turn on capture to deliver a reminder.
        with self.hooks.lock:
            if not self.hooks.owner_access():
                return
            job = self.job
            if job is not None:
                if not job.done.is_set():
                    return
                # A failed disk commit retries only the same receipt, never TTS.
                workspace = self.hooks.workspace()
                if self.channel == 'desktop':
                    workspace.finish_reminders(
                        self.claims,
                        job.result == 'notification_center',
                        channel=self.channel,
                        detail=getattr(job, 'detail', 'unconfirmed'),
                    )
                else:
                    if self.played_count:
                        workspace.finish_reminders(self.claims[: self.played_count], True)
                    workspace.finish_reminders(self.claims[self.played_count :], False)
                self.job = None
                self.claims = []
                self.played_count = 0
                self.hooks.publish_workspace()
            if self.pending_finish is not None:
                self.hooks.workspace().finish_reminders(
                    self.pending_finish, False, channel=self.channel
                )
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
            workspace = self.hooks.workspace()
            claims = workspace.claim_reminders()
            if not claims:
                return
            # From here, a crash or ambiguous send freezes these occurrences.
            # Only explicit rescheduling can offer them again; do not replay
            # a possibly heard reminder after a lost playback receipt.
            self.channel = claims[0].get('channel', 'cc8')
            self.pending_finish = claims

            def claims_current():
                return (
                    self.hooks.owner_access()
                    and self.hooks.workspace() is workspace
                    and workspace.reminder_claims_current(claims[self.played_count :])
                )

            try:
                self.hooks.touch()
                if (
                    not self.hooks.can_deliver()
                    or self.hooks.delivery_owner() != owner
                    or not claims_current()
                ):
                    self.hooks.workspace().finish_reminders(claims, False, channel=self.channel)
                    self.pending_finish = None
                    self.hooks.publish_workspace()
                    return
                segments = (
                    ('提醒你：' + claims[0]['title'],)
                    if len(claims) == 1
                    else tuple(
                        ('提醒你：' if i == 0 else '')
                        + f'{i + 1}，{item["title"]}'
                        + ('；' if i + 1 < len(claims) else '')
                        for i, item in enumerate(claims)
                    )
                )
                if self.channel == 'desktop':
                    if self.hooks.desktop_notify is None:
                        raise RuntimeError('Desktop notification service unavailable')

                    def current():
                        with self.hooks.lock:
                            return (
                                self.hooks.can_deliver()
                                and self.hooks.delivery_owner() == owner
                                and claims_current()
                            )

                    job = self.hooks.desktop_notify(
                        claims[0]['attempt_id'], ''.join(segments), current
                    )
                else:

                    def played(index):
                        # Keep receipt evidence before attempting storage. If the write
                        # fails, stop sending and retry just the state update in tick.
                        self.played_count = index + 1
                        workspace.finish_reminders(claims[index : index + 1], True)

                    job = self.hooks.announce_segments(
                        segments, on_segment=played, before_playback=claims_current
                    )
            except Exception:
                self.hooks.workspace().finish_reminders(claims, False, channel=self.channel)
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

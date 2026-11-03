"""Literal owner announcements: bounded dispatch, one generation and final receipt."""

from __future__ import annotations
from dataclasses import dataclass
import queue
from typing import Callable, ContextManager
from announce import Announcement, validate_text
from diagnostics import TraceBook
from session_ports import EventSink, MetricSink


@dataclass(frozen=True)
class AnnouncementHooks:
    lock: ContextManager[object]
    connected: Callable[[], bool]
    calibration_busy: Callable[[], bool]
    cancel_session: Callable[[str], int]
    valid: Callable[[int], bool]
    offer: Callable[[Announcement], None]
    diagnostics: Callable[[], TraceBook]
    event: EventSink
    status: Callable[[str, str], None]
    speak: Callable[[str, int], bool]
    failure: Callable[[str, BaseException], None]
    metric: MetricSink
    save_status: Callable[[], None]


class AnnouncementController:
    def __init__(self, hooks: AnnouncementHooks):
        self.hooks = hooks
        self.generation = None

    def invalidate(self):
        self.generation = None

    def announce(self, text, *, prepare=None):
        text = validate_text(text)
        with self.hooks.lock:
            if (
                not self.hooks.connected()
                or self.hooks.calibration_busy()
                or self.generation is not None
            ):
                raise RuntimeError('Announcement unavailable')
            generation = self.hooks.cancel_session('announcement')
            self.generation = generation
            self.hooks.diagnostics().begin('announcement', generation)
            job = Announcement(generation, text)
            try:
                if prepare is not None:
                    prepare(generation)
                self.hooks.offer(job)
            except Exception as error:
                self.generation = None
                self.hooks.diagnostics().finish(generation, 'failed')
                if isinstance(error, queue.Full):
                    raise RuntimeError('Announcement queue busy') from None
                raise
            return job

    def read_announcement(self, job):
        generation = job.generation
        try:
            if not self.hooks.valid(generation):
                return
            self.hooks.event('announcement_begin', generation=generation)
            self.hooks.status('thinking', '正在准备播报 · 麦克风状态保持不变')
            # Read user-provided text literally, without an LLM rewrite or lookup.
            pieces = [job.text[i : i + 120] for i in range(0, len(job.text), 120)]
            for text in pieces:
                if not self.hooks.valid(generation) or not self.hooks.speak(text, generation):
                    return
            if self.hooks.valid(generation):
                job.result = 'completed'
        except Exception as error:
            job.result = 'failed'
            with self.hooks.lock:
                if self.hooks.valid(generation):
                    self.hooks.failure('这次播报没有完成', error)
        finally:
            if self.hooks.connected():
                try:
                    self.hooks.event('announcement_done', generation=generation, result=job.result)
                except (OSError, EOFError):
                    pass
            if self.generation == generation:
                self.generation = None
            self.hooks.metric(
                'last_announcement', {'status': job.result, 'characters': len(job.text)}
            )
            self.hooks.diagnostics().finish(
                generation,
                'completed'
                if job.result == 'completed'
                else 'failed'
                if job.result == 'failed'
                else 'cancelled',
            )
            try:
                self.hooks.save_status()
            finally:
                job.done.set()

"""Literal owner announcements: bounded dispatch, one generation and final receipt."""

from __future__ import annotations
from dataclasses import dataclass
import queue
from typing import Callable, ContextManager
from announce import Announcement, validate_text
from request_text import validate_request_text
from diagnostics import TraceBook, error_kind
from failure_feedback import feedback
from session_ports import EventSink, MetricSink


@dataclass(frozen=True)
class AnnouncementHooks:
    lock: ContextManager[object]
    connected: Callable[[], bool]
    calibration_busy: Callable[[], bool]
    cancel_session: Callable[[str], int]
    session_generation: Callable[[], int]
    valid: Callable[[int], bool]
    offer: Callable[[Announcement], None]
    diagnostics: Callable[[], TraceBook]
    event: EventSink
    status: Callable[[str, str], None]
    speak: Callable[[str, int], bool]
    failure: Callable[[str, BaseException], None]
    metric: MetricSink
    save_status: Callable[[], None]
    log: EventSink


class AnnouncementController:
    def __init__(self, hooks: AnnouncementHooks):
        self.hooks = hooks
        self.generation = None

    def invalidate(self):
        self.generation = None

    def announce(self, text, *, prepare=None):
        text = validate_text(text)
        return self._submit(text, prepare=prepare)

    def announce_segments(self, segments, *, prepare, on_segment):
        """Internal literal reports only; public owner IPC retains its 500-char limit."""
        if (
            not isinstance(segments, tuple)
            or not 1 <= len(segments) <= 4
            or not callable(on_segment)
        ):
            raise ValueError('Invalid segmented report')
        segments = tuple(validate_request_text(text, 1200) for text in segments)
        if sum(map(len, segments)) > 4000:
            raise ValueError('Segmented report exceeds limit')
        return self._submit(
            ''.join(segments), prepare=prepare, segments=segments, on_segment=on_segment
        )

    def _submit(self, text, *, prepare, segments=(), on_segment=None):
        with self.hooks.lock:
            if (
                not self.hooks.connected()
                or self.hooks.calibration_busy()
                or self.generation is not None
            ):
                raise RuntimeError('Announcement unavailable')
            previous = self.hooks.session_generation()
            try:
                generation = self.hooks.cancel_session('announcement')
            except Exception as error:
                if self.hooks.session_generation() == previous + 1:
                    try:
                        self.hooks.diagnostics().begin('announcement', previous + 1)
                        self.hooks.diagnostics().failure(previous + 1, 'runtime', error)
                    except Exception as secondary:
                        self._cleanup_error('diagnostics', secondary)
                self._start_failure(previous + 1, error)
                raise
            job = Announcement(generation, text, segments=segments, on_segment=on_segment)

            def current():
                return (
                    self.hooks.session_generation() == generation and self.generation == generation
                )

            def superseded():
                # Return an already settled receipt, never enqueue or retry old work.
                try:
                    self.hooks.diagnostics().finish(generation, 'cancelled')
                except Exception as secondary:
                    self._cleanup_error('superseded', secondary)
                finally:
                    job.discard_segments()
                    job.done.set()
                return job

            if self.hooks.session_generation() != generation:
                return superseded()
            if not self.hooks.connected():
                raise RuntimeError('Announcement unavailable')
            self.generation = generation
            try:
                self.hooks.diagnostics().begin('announcement', generation)
                if not current():
                    return superseded()
                if prepare is not None:
                    prepare(generation)
                if not current():
                    return superseded()
                self.hooks.offer(job)
                if not current():
                    return superseded()
            except Exception as error:
                if not current():
                    return superseded()
                self.generation = None
                job.result = 'failed'
                try:
                    try:
                        self.hooks.diagnostics().failure(generation, 'runtime', error)
                    except Exception as secondary:
                        self._cleanup_error('diagnostics', secondary)
                    # An offer may enqueue before raising. Invalidate that
                    # generation even if recording keeps session.valid true.
                    if self.hooks.session_generation() == generation:
                        try:
                            self.hooks.cancel_session('announcement_submit_failed')
                        except Exception as secondary:
                            self._cleanup_error('cancellation', secondary)
                    self._start_failure(generation + 1, error)
                finally:
                    job.discard_segments()
                    job.done.set()
                if isinstance(error, queue.Full):
                    raise RuntimeError('Announcement queue busy') from None
                raise
            return job

    def _start_failure(self, generation, error):
        if self.hooks.session_generation() != generation or not self.hooks.connected():
            return
        try:
            self.hooks.event(
                'status',
                generation=generation,
                phase='error',
                request_start_failed=True,
                message='本次播报未启动。' + feedback('runtime', error_kind(error)).message,
            )
        except Exception as secondary:
            self._cleanup_error('feedback', secondary)

    def _cleanup_error(self, phase, error):
        try:
            self.hooks.log(
                'announcement_cleanup_failed', phase=phase, error_type=type(error).__name__
            )
        except Exception:
            pass

    def read_announcement(self, job):
        generation = job.generation
        try:
            with self.hooks.lock:
                if not self.hooks.valid(generation):
                    return
                self.hooks.event('announcement_begin', generation=generation)
                if not self.hooks.valid(generation):
                    return
                self.hooks.status('thinking', '正在准备播报 · 麦克风状态保持不变')
            # Read user-provided text literally, without an LLM rewrite or lookup.
            for index, segment in enumerate(job.segments or (job.text,)):
                for begin in range(0, len(segment), 120):
                    if not self.hooks.valid(generation) or not self.hooks.speak(
                        segment[begin : begin + 120], generation
                    ):
                        return
                if job.on_segment is not None:
                    # Every chunk of this segment was confirmed by the device.
                    # A later cancellation cannot erase an already played item.
                    with self.hooks.lock:
                        job.on_segment(index)
            if self.hooks.valid(generation):
                job.result = 'completed'
        except Exception as error:
            job.result = 'failed'
            with self.hooks.lock:
                if self.hooks.valid(generation):
                    self.hooks.failure('这次播报没有完成', error)
        finally:
            try:
                with self.hooks.lock:
                    if self.generation == generation:
                        self.generation = None
                    self.hooks.diagnostics().finish(
                        generation,
                        'completed'
                        if job.result == 'completed'
                        else 'failed'
                        if job.result == 'failed'
                        else 'cancelled',
                    )
                    # A delayed old dispatch still settles its own waiter and
                    # trace, but cannot replace the current UI or metric.
                    if self.hooks.session_generation() == generation:
                        if self.hooks.connected():
                            try:
                                self.hooks.event(
                                    'announcement_done', generation=generation, result=job.result
                                )
                            except (OSError, EOFError):
                                pass
                        if self.hooks.session_generation() == generation:
                            self.hooks.metric(
                                'last_announcement',
                                {'status': job.result, 'characters': len(job.text)},
                            )
                            self.hooks.save_status()
            finally:
                job.discard_segments()
                job.done.set()

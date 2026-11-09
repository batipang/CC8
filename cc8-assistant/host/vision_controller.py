"""Own one foreground vision request, its bounded inbox and desktop waiter.

Session callbacks cross an explicit command boundary; this component never
stores or mutates the Bridge object, starts audio, or persists camera frames.
"""

from __future__ import annotations
from dataclasses import dataclass
import re
import threading
from typing import Callable, ContextManager
from reply_types import VisualReply
from vision import CameraInbox, DesktopSnapshot, VisionError, HD_IMAGE, LEGACY_IMAGE
from session_ports import EventSink, AnswerSink, MetricSink
from diagnostics import TraceBook, error_kind
from failure_feedback import FailureSink
from operation_failure import OperationFailure


@dataclass(frozen=True)
class VisionHooks:
    lock: ContextManager[object]
    connected: Callable[[], bool]
    calibrating: Callable[[], bool]
    has_transport: Callable[[], bool]
    brain_ready: Callable[[], bool]
    session_generation: Callable[[], int]
    valid: Callable[[int], bool]
    cancel_session: Callable[[str], int]
    touch: Callable[[], None]
    event: EventSink
    status: Callable[[str, str], None]
    save_status: Callable[[], None]
    authorize: Callable[[str, str, int], str]
    enqueue: Callable[[VisualReply], None]
    answer: AnswerSink
    answer_completed: Callable[[], bool | None]
    metric: MetricSink
    log: EventSink
    diagnostics: Callable[[], TraceBook]
    failure: FailureSink


class VisionController:
    def __init__(self, hooks: VisionHooks):
        self.hooks = hooks
        self.inbox = CameraInbox()
        self.snapshot_job = None
        self.challenge = ''
        self.generation = None
        self.nonce = ''
        self.image_policy = LEGACY_IMAGE
        self.review_capable = False

    def invalidate(self):
        # Caller holds the shared session guard; inbox owns its inner lock.
        self.generation = None
        self.nonce = ''
        self.inbox.cancel()
        job = self.snapshot_job
        self.snapshot_job = None
        if job is not None:
            job.image = None
            job.result = 'interrupted'
            job.done.set()

    def clear_challenge(self):
        self.challenge = ''
        self.image_policy = LEGACY_IMAGE
        self.review_capable = False

    def update_peer(self, event):
        self.review_capable = (
            type(event.get('vision_review_version')) is int and event['vision_review_version'] == 1
        )
        self.image_policy = (
            HD_IMAGE
            if type(event.get('vision_capture_version')) is int
            and event['vision_capture_version'] == 2
            else LEGACY_IMAGE
        )
        challenge = event.get('desktop_camera_challenge')
        self.challenge = (
            challenge
            if event.get('desktop_camera_version') == 1
            and isinstance(challenge, str)
            and re.fullmatch('[a-f0-9]{32}', challenge)
            else ''
        )

    def feed(self, event):
        return self.inbox.feed(event)

    def matches_cancel(self, event):
        return self.generation is not None and (
            event.get('vision_generation') == self.generation
            or (event.get('nonce') and event.get('nonce') == self.nonce)
        )

    def request_snapshot(self):
        # Only the owner-local IPC route enters here. No model camera tool or
        # startup/reconnect path may manufacture this authorization.
        with self.hooks.lock:
            if (
                not self.hooks.connected()
                or self.hooks.calibrating()
                or self.generation is not None
                or not self.challenge
                or not self.hooks.has_transport()
            ):
                raise RuntimeError('Camera unavailable or busy')
            generation = self.hooks.cancel_session('desktop_snapshot')
            capture = self.inbox.begin(generation, policy=self.image_policy)
            job = DesktopSnapshot(generation, capture.ident, policy=capture.policy)
            self.snapshot_job = job
            self.generation = generation
            self.nonce = capture.ident
            self.hooks.diagnostics().begin('vision', generation)
            self.hooks.touch()
            authorization = self.hooks.authorize(self.challenge, capture.ident, generation)
            self.hooks.event(
                'vision_begin',
                generation=generation,
                origin='desktop',
                nonce=capture.ident,
                camera_auth=authorization,
            )
            threading.Thread(target=self.capture_snapshot, args=(job, capture), daemon=True).start()
        self.hooks.save_status()
        return job

    def cancel_snapshot(self, job):
        with self.hooks.lock:
            if self.snapshot_job is job and self.hooks.session_generation() == job.generation:
                self.hooks.cancel_session('desktop_snapshot_cancel')

    def capture_snapshot(self, job, capture):
        generation = job.generation
        current = lambda: self.snapshot_job is job and self.hooks.valid(generation)
        try:
            if not current():
                return
            self.hooks.status('thinking', 'Dot 请求拍照 · 预览 5 秒后返回当前聊天')
            self.hooks.event(
                'camera_capture',
                generation=generation,
                request_id=capture.ident,
                origin='desktop',
                nonce=capture.ident,
                **capture.policy.fields(),
                review_allowed=False,
                review_window_ms=0,
            )
            image = self.inbox.wait(capture, current)
            with self.hooks.lock:
                if not current():
                    return
                job.image = image
                job.result = 'completed'
                self.hooks.metric('desktop_snapshots_captured', increment=True)
        except Exception as error:
            with self.hooks.lock:
                if current():
                    job.result = 'failed'
                    job.image = None
                    job.error = error.code if isinstance(error, VisionError) else error_kind(error)
                    failure = (
                        error
                        if isinstance(error, OperationFailure)
                        else OperationFailure(
                            'Camera capture failed', stage='vision', kind=error_kind(error)
                        )
                    )
                    self.hooks.failure('这次拍照没有完成', failure, trace_key=generation)
        finally:
            self.inbox.cancel_capture(capture)
            with self.hooks.lock:
                try:
                    if self.snapshot_job is job:
                        self.snapshot_job = None
                        if self.generation == generation:
                            self.generation = None
                            self.nonce = ''
                            if self.hooks.connected():
                                self.hooks.event(
                                    'vision_done',
                                    generation=generation,
                                    result=job.result,
                                    origin='desktop',
                                )
                    self.hooks.diagnostics().finish(
                        generation,
                        'completed'
                        if job.result == 'completed'
                        else 'failed'
                        if job.result == 'failed'
                        else 'cancelled',
                    )
                finally:
                    job.done.set()
            self.hooks.save_status()

    def begin_visual(self, text, origin, nonce=''):
        # The caller already accepted an explicit foreground button/voice intent.
        if not self.hooks.connected() or self.hooks.calibrating() or not self.hooks.brain_ready():
            self.hooks.event(
                'vision_unavailable', message='请先连接 Mac、结束校准，并确认 Dot 可用'
            )
            return
        with self.hooks.lock:
            generation = self.hooks.cancel_session('vision_request')
            self.generation = generation
            self.nonce = nonce
            self.hooks.diagnostics().begin('vision', generation)
            self.hooks.touch()
            self.hooks.event('vision_begin', generation=generation, nonce=nonce)
            self.hooks.enqueue(VisualReply(generation, text, origin, nonce))
        self.hooks.save_status()

    def answer_visual(self, job):
        generation = job.generation
        outcome = 'cancelled'
        image = None
        capture = None
        try:
            with self.hooks.lock:
                if not self.hooks.valid(generation):
                    return
                capture = self.inbox.begin(
                    generation, policy=self.image_policy, review=self.review_capable
                )
            self.hooks.status('thinking', '正在打开摄像头 · 预览 5 秒后拍摄给 Dot')
            self.hooks.event(
                'camera_capture',
                generation=generation,
                request_id=capture.ident,
                origin=job.origin,
                nonce=job.nonce,
                **capture.policy.fields(),
                review_allowed=capture.review_allowed,
                review_window_ms=70000 if capture.review_allowed else 0,
            )
            image = self.inbox.wait(capture, lambda: self.hooks.valid(generation))
            if not self.hooks.valid(generation):
                return
            self.hooks.metric('vision_frames_received', increment=True)
            self.hooks.log(
                'vision_frame_received',
                bytes=len(image),
                width=capture.width,
                height=capture.height,
            )
            self.hooks.answer(job.text, generation, image=image)
            outcome = 'failed' if self.hooks.answer_completed() is False else 'completed'
            if outcome == 'completed' and self.hooks.valid(generation):
                self.hooks.metric('vision_dot_completed', increment=True)
        except Exception as error:
            outcome = 'failed'
            with self.hooks.lock:
                if self.hooks.valid(generation):
                    self.hooks.metric(
                        'vision_last_error',
                        error.code if isinstance(error, VisionError) else error_kind(error),
                    )
                    stage = self.hooks.diagnostics().current_stage(
                        generation, default='vision' if image is None else 'brain'
                    )
                    failure = (
                        error
                        if isinstance(error, OperationFailure)
                        else OperationFailure(
                            'Vision operation failed', stage=stage, kind=error_kind(error)
                        )
                    )
                    self.hooks.failure('这次看图没有完成', failure, trace_key=generation)
        finally:
            image = None
            if capture is not None:
                self.inbox.cancel_capture(capture)
            with self.hooks.lock:
                if self.generation == generation:
                    self.generation = None
                    self.nonce = ''
                    self.hooks.metric('vision_last_result', outcome)
                    if self.hooks.connected():
                        self.hooks.event('vision_done', generation=generation, result=outcome)
                self.hooks.diagnostics().finish(generation, outcome)
            self.hooks.save_status()

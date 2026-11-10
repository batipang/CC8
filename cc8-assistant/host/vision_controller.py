"""Own one foreground vision request, its bounded inbox and desktop waiter.

Session callbacks cross an explicit command boundary; this component never
stores or mutates the Bridge object, starts audio, or persists camera frames.
"""

from __future__ import annotations
from dataclasses import dataclass
import re
import threading
import time
import secrets
from typing import Callable, ContextManager
from reply_types import VisualReply, PhotoReply
from photo_followup import PhotoFollowup, spoken_photo_question
from vision import CameraInbox, DesktopSnapshot, VisionError, HD_IMAGE, LEGACY_IMAGE
from session_ports import EventSink, AnswerSink, MetricSink
from diagnostics import TraceBook, error_kind
from failure_feedback import FailureSink
from operation_failure import OperationFailure
from window_input import WindowPicker, WindowSelectionEnded, available as window_available


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
    capture_owner: Callable[[], object | None] = lambda: None


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
        self.followup_capable = False
        self.followup = PhotoFollowup(self.photo_expired)
        self.followup_job = None
        self.followup_job_timer = None
        self.followup_capture_owner = None
        self.window_picker = None

    def invalidate(self, reason=None, new_generation=None, capture_owner=None):
        # Caller holds the shared session guard; inbox owns its inner lock.
        self.generation = None
        self.nonce = ''
        self.inbox.cancel()
        picker, self.window_picker = self.window_picker, None
        if picker is not None:
            picker.cancel()
        preserved = None
        if (
            reason in ('voice', 'verified_voice')
            and type(new_generation) is int
            and self.followup_capture_owner is not None
            and capture_owner == self.followup_capture_owner
        ):
            try:
                preserved = self.followup.advance_voice(new_generation - 1, new_generation)
            except Exception:
                pass
        if preserved is None:
            self.followup.clear()
            self.followup_capture_owner = None
        self.release_queued_photo()
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
        self.followup_capable = False

    def release_queued_photo(self):
        # Caller holds the session guard. A blocked response worker must not
        # extend retention simply because it has not dequeued this request.
        if self.followup_job is not None:
            self.followup_job.image = None
            self.followup_job = None
        timer, self.followup_job_timer = self.followup_job_timer, None
        if timer is not None:
            timer.cancel()

    def expire_queued_photo(self, job):
        with self.hooks.lock:
            if self.followup_job is job:
                job.image = None
                self.followup_job_timer = None

    def update_peer(self, event):
        self.followup_capable = (
            type(event.get('vision_followup_version')) is int
            and event['vision_followup_version'] == 1
        )
        if not self.followup_capable:
            self.followup.clear()
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

    def photo_expired(self, ident, generation):
        with self.hooks.lock:
            if self.hooks.connected() and self.hooks.session_generation() == generation:
                self.hooks.event('photo_followup_expired', generation=generation, photo_id=ident)

    def publish_followup(self, generation):
        with self.hooks.lock:
            if (
                not self.followup_capable
                or not self.hooks.connected()
                or self.hooks.session_generation() != generation
            ):
                return
            state = self.followup.metadata(generation)
            if state is not None:
                self.hooks.event('photo_followup_ready', **state)

    def voice_followup(self, text, generation, command=False):
        question = spoken_photo_question(text) if not command else None
        with self.hooks.lock:
            if not self.hooks.valid(generation):
                return True
            if question is None:
                self.followup.clear()
                self.followup_capture_owner = None
                return False
            state = self.followup.metadata(generation)
            if (
                not self.followup_capable
                or state is None
                or self.followup_capture_owner is None
                or self.hooks.capture_owner() != self.followup_capture_owner
            ):
                self.followup.clear()
                self.followup_capture_owner = None
                self.hooks.status('error', '没有可追问的照片，请明确允许保留并重新拍摄。')
                self.hooks.diagnostics().finish(generation, 'rejected')
                return True
            if not question:
                self.hooks.status('listening', '请说“追问这张照片”，并在后面附上问题。')
                self.hooks.diagnostics().finish(generation, 'rejected')
                return True
            self.begin_followup(
                dict(photo_id=state['photo_id'], nonce=secrets.token_hex(16), text=question),
                origin='followup_voice',
            )
            return True

    def forget_photo(self, request):
        with self.hooks.lock:
            generation = self.hooks.session_generation()
            state = self.followup.metadata(generation)
            if state is None or state['photo_id'] == request['photo_id']:
                if state is not None:
                    self.followup.clear()
                    self.followup_capture_owner = None
                self.hooks.event(
                    'photo_followup_expired', generation=generation, photo_id=request['photo_id']
                )
            else:
                self.hooks.event(
                    'photo_followup_forget_rejected',
                    generation=generation,
                    photo_id=request['photo_id'],
                )

    def begin_followup(self, request, origin='followup'):
        """Only the typed foreground request enters; never re-open a camera on failure."""
        with self.hooks.lock:
            photo = None
            if (
                self.followup_capable
                and self.hooks.connected()
                and not self.hooks.calibrating()
                and self.hooks.brain_ready()
                and self.generation is None
            ):
                photo = self.followup.consume(request['photo_id'], self.hooks.session_generation())
            if photo is None:
                self.hooks.event(
                    'photo_followup_rejected',
                    nonce=request['nonce'],
                    photo_id=request['photo_id'],
                    message='这张照片已不可追问，请明确重新拍摄。',
                )
                return
            photo_id = photo.ident
            job = PhotoReply(
                0,
                request['text'].strip(),
                origin,
                request['nonce'],
                photo.image,
                photo.policy,
                photo.expires,
            )
            photo = None
            generation = None
            try:
                generation = self.hooks.cancel_session('photo_followup')
                if not self.hooks.connected() or self.hooks.session_generation() != generation:
                    job.image = None
                    return
                job.generation = generation
                self.generation = generation
                self.nonce = request['nonce']
                self.followup_job = job
                self.hooks.diagnostics().begin('vision', generation)
                self.hooks.touch()
                self.hooks.event(
                    'vision_begin',
                    generation=generation,
                    origin=origin,
                    nonce=request['nonce'],
                    photo_id=photo_id,
                )
                if not self.hooks.valid(generation):
                    job.image = None
                    return
                timer = threading.Timer(
                    max(0, job.expires - time.monotonic()), self.expire_queued_photo, args=(job,)
                )
                timer.daemon = True
                self.followup_job_timer = timer
                timer.start()
                self.hooks.enqueue(job)
            except BaseException:
                job.image = None
                if self.followup_job is job:
                    self.release_queued_photo()
                # Ambiguous queue admission cannot carry pixels into a later
                # job, even if cancellation itself encounters a transport error.
                if generation is not None and self.hooks.session_generation() == generation:
                    try:
                        self.hooks.cancel_session('photo_followup_start_failed')
                    except Exception:
                        pass
                raise
        self.hooks.save_status()

    def matches_cancel(self, event):
        return self.generation is not None and (
            event.get('vision_generation') == self.generation
            or (event.get('nonce') and event.get('nonce') == self.nonce)
        )

    def request_snapshot(self, *, metadata_only=False):
        # Only the owner-local IPC route enters here. No model camera tool or
        # startup/reconnect path may manufacture this authorization.
        if type(metadata_only) is not bool:
            raise ValueError('Invalid snapshot mode')
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
            job = DesktopSnapshot(
                generation, capture.ident, policy=capture.policy, metadata_only=metadata_only
            )
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
            self.hooks.status(
                'thinking',
                '相机连接检查 · 正在取景'
                if job.metadata_only
                else 'Dot 请求拍照 · 预览 5 秒后返回当前聊天',
            )
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
        if origin == 'mac_window' and not window_available():
            self.hooks.event(
                'vision_unavailable',
                message='Mac 窗口入口未安装或系统不支持，请在 Mac 完成准备后重试。',
            )
            return
        with self.hooks.lock:
            if self.hooks.calibrating():
                self.hooks.event('vision_unavailable', message='音频正在使用，请先结束当前模式。')
                return
            generation = self.hooks.cancel_session('vision_request')
            # Provider/transport cleanup may synchronously admit replacement
            # work. Its generation owns the UI and queue, even on this thread.
            if self.hooks.session_generation() != generation or not self.hooks.connected():
                return
            self.generation = generation
            self.nonce = nonce
            self.hooks.diagnostics().begin('vision', generation)
            self.hooks.touch()
            self.hooks.event(
                'vision_begin',
                generation=generation,
                nonce=nonce,
                **({'origin': 'mac_window'} if origin == 'mac_window' else {}),
            )
            if self.generation != generation or not self.hooks.valid(generation):
                return
            self.hooks.enqueue(VisualReply(generation, text, origin, nonce))
        self.hooks.save_status()

    def capture_workflow(self, generation, nonce, current):
        """One explicit foreground workflow frame, using the existing 5s UI.

        The device sets its pending camera nonce only on the owner's fresh
        workflow confirmation. A saved definition, timer or model cannot do so.
        Unlike desktop photos, this path never saves or exports the JPEG.
        """
        capture = None
        outcome = 'failed'
        try:
            with self.hooks.lock:
                if (
                    not re.fullmatch('[a-f0-9]{32}', nonce)
                    or current() is not True
                    or not self.hooks.valid(generation)
                    or self.generation is not None
                    or self.snapshot_job is not None
                    or not self.hooks.connected()
                ):
                    raise VisionError('request_not_authorized')
                self.generation, self.nonce = generation, nonce
                self.hooks.event(
                    'vision_begin', generation=generation, nonce=nonce, origin='workflow'
                )
                if current() is not True or self.generation != generation:
                    raise VisionError('capture_cancelled')
                capture = self.inbox.begin(
                    generation, policy=self.image_policy, review=False, followup=False
                )
                self.hooks.event(
                    'camera_capture',
                    generation=generation,
                    request_id=capture.ident,
                    origin='workflow',
                    nonce=nonce,
                    **capture.policy.fields(),
                    review_allowed=False,
                    review_window_ms=0,
                    followup_allowed=False,
                )
            image = self.inbox.wait(capture, current)
            with self.hooks.lock:
                if current() is not True or self.generation != generation:
                    raise VisionError('capture_cancelled')
                self.hooks.metric('vision_frames_received', increment=True)
                outcome = 'completed'
                return image
        finally:
            if capture is not None:
                self.inbox.cancel_capture(capture)
            with self.hooks.lock:
                if self.generation == generation and self.nonce == nonce:
                    self.generation, self.nonce = None, ''
                    if self.hooks.valid(generation):
                        self.hooks.event(
                            'vision_done',
                            generation=generation,
                            result=outcome,
                            origin='workflow',
                        )

    def answer_visual(self, job):
        generation = job.generation
        outcome = 'cancelled'
        image = None
        capture = None
        retained = isinstance(job, PhotoReply)
        window = job.origin == 'mac_window'
        picker = None
        question = job.text
        try:
            with self.hooks.lock:
                if not self.hooks.valid(generation):
                    if retained:
                        job.image = None
                    return
                if retained:
                    image, job.image = job.image, None
                    if self.followup_job is job:
                        self.release_queued_photo()
                    if image is None or time.monotonic() >= job.expires:
                        raise VisionError('capture_timeout')
                elif window:
                    picker = WindowPicker()
                    self.window_picker = picker
                else:
                    capture = self.inbox.begin(
                        generation,
                        policy=self.image_policy,
                        review=self.review_capable,
                        followup=self.followup_capable,
                    )
            if window:
                self.hooks.status(
                    'thinking', '请在 Mac 选择一个窗口，预览后点击发送 · CC8 摄像头关闭'
                )
                selected = picker.choose(job.text, job.nonce, lambda: self.hooks.valid(generation))
                image = selected.image
                question = selected.prompt()
                selected = None
            elif not retained:
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
                    followup_allowed=capture.followup_allowed,
                )
                image = self.inbox.wait(capture, lambda: self.hooks.valid(generation))
            if not self.hooks.valid(generation):
                return
            if not retained and not window:
                self.hooks.metric('vision_frames_received', increment=True)
                self.hooks.log(
                    'vision_frame_received',
                    bytes=len(image),
                    width=capture.width,
                    height=capture.height,
                )
            self.hooks.answer(question, generation, image=image)
            outcome = 'failed' if self.hooks.answer_completed() is False else 'completed'
            with self.hooks.lock:
                if outcome == 'completed' and self.hooks.valid(generation):
                    self.hooks.metric('vision_dot_completed', increment=True)
                    if (
                        not window
                        and self.followup_capable
                        and (retained or capture.followup_requested)
                    ):
                        expires = job.expires if retained else None
                        if expires is None or time.monotonic() < expires:
                            try:
                                self.followup.arm(
                                    generation,
                                    image,
                                    job.policy if retained else capture.policy,
                                    consent=True,
                                    expires=expires,
                                )
                                self.followup_capture_owner = self.hooks.capture_owner()
                            except Exception:
                                # Image analysis is already complete. A failed
                                # optional retention cannot rewrite that receipt.
                                self.hooks.metric('photo_followup_retention_failed', increment=True)
        except Exception as error:
            outcome = 'failed'
            with self.hooks.lock:
                if self.hooks.valid(generation):
                    if (
                        window
                        and isinstance(error, WindowSelectionEnded)
                        and error.reason == 'cancelled'
                    ):
                        outcome = 'cancelled'
                        self.hooks.status(
                            'listening' if self.hooks.capture_owner() else 'idle',
                            '已取消 Mac 窗口输入，没有发送画面。',
                        )
                        return
                    self.hooks.metric(
                        'vision_last_error',
                        error.code
                        if isinstance(error, VisionError)
                        else 'timeout'
                        if isinstance(error, WindowSelectionEnded) and error.reason == 'expired'
                        else error_kind(error),
                    )
                    stage = self.hooks.diagnostics().current_stage(
                        generation, default='vision' if image is None else 'brain'
                    )
                    failure = (
                        error
                        if isinstance(error, OperationFailure)
                        else OperationFailure(
                            'Vision operation failed',
                            stage=stage,
                            kind='timeout'
                            if isinstance(error, WindowSelectionEnded) and error.reason == 'expired'
                            else error_kind(error),
                            input_source='mac_window' if window else None,
                        )
                    )
                    self.hooks.failure('这次看图没有完成', failure, trace_key=generation)
        finally:
            image = None
            if picker is not None:
                picker.cancel()
            if retained:
                job.image = None
            if capture is not None:
                self.inbox.cancel_capture(capture)
            with self.hooks.lock:
                if self.window_picker is picker:
                    self.window_picker = None
                if self.followup_job is job:
                    self.release_queued_photo()
                if self.generation == generation:
                    self.generation = None
                    self.nonce = ''
                    self.hooks.metric('vision_last_result', outcome)
                    if self.hooks.connected():
                        self.hooks.event('vision_done', generation=generation, result=outcome)
                        state = self.followup.metadata(generation)
                        if state is not None and self.hooks.session_generation() == generation:
                            self.hooks.event('photo_followup_ready', **state)
                self.hooks.diagnostics().finish(generation, outcome)
            self.hooks.save_status()

"""Own serial response dispatch, bounded queue and RAM-only conversation history.

Task receipts belong to Workspace; session validity and output remain ports.
Stopping narration must never downgrade a completed brain/tool receipt.
"""

from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import queue
import sys
import threading
from typing import Callable
from announce import Announcement
from diagnostics import TraceBook, error_kind
from failure_feedback import FailureSink, feedback
from operation_failure import OperationFailure
from reply_types import Reply, TextReply, VisualReply
from session_ports import EventSink, MetricSink
from workspace import Workspace


@dataclass(frozen=True)
class ResponseHooks:
    valid: Callable[[int], bool]
    diagnostics: Callable[[], TraceBook]
    workspace: Callable[[], Workspace]
    brain: Callable[[], object]
    speech: Callable[[], object]
    active: Callable[[], bool]
    metric: MetricSink
    dispatch: Callable[[Reply | Announcement], None]
    answer: Callable[[str, int], None]
    answer_visual: Callable[[VisualReply], None]
    read_announcement: Callable[[Announcement], None]
    finish_text: Callable[[int], None]
    publish_workspace: Callable[[], None]
    event: EventSink
    status: Callable[[str, str], None]
    log: EventSink
    failure: FailureSink
    speak: Callable[[str, int], bool]


class ResponseWorker:
    def __init__(self, hooks: ResponseHooks):
        self.hooks = hooks
        self.queue = queue.Queue(maxsize=2)
        self.busy = False
        self.history = deque(maxlen=8)
        self.worker = None
        self.start_lock = threading.Lock()

    def start(self):
        with self.start_lock:
            if self.worker is not None:
                raise RuntimeError('Response worker already started')
            self.worker = threading.Thread(target=self.work, daemon=True)
            self.worker.start()

    def offer(self, job):
        # Literal announcements must fail when busy, never evict another job.
        self.queue.put_nowait(job)

    def enqueue(self, job):
        while True:
            try:
                self.queue.put_nowait(job)
                return
            except queue.Full:
                try:
                    old = self.queue.get_nowait()
                except queue.Empty:
                    continue
                if isinstance(old, Announcement):
                    old.done.set()

    def work(self):
        # 单消费者串行播报，识别另在线程运行，因此暂停时仍能继续听插话。
        while True:
            job = self.queue.get()
            try:
                self.hooks.dispatch(job)
            except Exception as error:
                if self.hooks.valid(job.generation):
                    self.hooks.failure('这次回答没有完成', error, trace_key=job.generation)
                else:
                    self.hooks.log('response_worker_error', error_type=type(error).__name__)

    def respond_job(self, job):
        if isinstance(job, Announcement):
            self.hooks.read_announcement(job)
            return
        if not self.hooks.valid(job.generation):
            return
        self.busy = True
        try:
            if isinstance(job, VisualReply):
                self.hooks.answer_visual(job)
            else:
                self.hooks.answer(job.text, job.generation)
        finally:
            self.busy = False
            if isinstance(job, TextReply):
                self.hooks.finish_text(job.generation)

    def answer(self, text, generation, image=None):
        # 任务成功以大脑真实回执为准，后续停止朗读不能把已完成工作改为取消。
        self.hooks.diagnostics().ensure(generation, 'vision' if image is not None else 'text')
        self.hooks.diagnostics().mark(generation, 'brain_start')
        task_id = self.hooks.workspace().task_start(generation, text)
        result_text = ['']
        clauses = None

        def web_update(kind, payload):
            if not self.hooks.valid(generation):
                return
            if kind == 'progress':
                message = payload.get('message', '正在处理…')
                self.hooks.workspace().task_stage(
                    task_id, 'analysis', '连接助理' if '连接' in message else '分析请求', 'running'
                )
                self.hooks.publish_workspace()
                self.hooks.event('status', generation=generation, phase='thinking', message=message)
            elif kind == 'dot_result':
                self.hooks.diagnostics().task_result(generation, 'completed')
                self.hooks.diagnostics().link_turn(
                    generation, self.hooks.brain().last_metrics.get('turn_id')
                )
                result_text[0] = payload.get('text', '')
                # The Dot turn receipt closes the task, independently of the
                # remaining audio or device delivery. A disconnected display
                # cannot turn successfully executed work into a failed task.
                self.hooks.workspace().task_finish(task_id, 'completed', result_text[0])
                self.hooks.event('dot_result', generation=generation, **payload)
                self.hooks.publish_workspace()
            elif kind == 'task_stage':
                self.hooks.diagnostics().scenario(
                    generation, 'web' if payload.get('kind') == 'webSearch' else 'tool'
                )
                self.hooks.workspace().task_stage(
                    task_id, payload['id'], payload['title'], payload['status']
                )
                self.hooks.publish_workspace()
            elif kind == 'searching':
                self.hooks.diagnostics().scenario(generation, 'web')
                self.hooks.workspace().task_stage(task_id, 'search', '联网检索', 'running')
                self.hooks.publish_workspace()
                self.hooks.event(
                    'status', generation=generation, phase='thinking', message='正在联网查询…'
                )
            elif kind == 'failed':
                self.hooks.log('web_search_failed', **payload)
            elif kind in ('clear', 'sources'):
                self.hooks.event(
                    'web_sources',
                    generation=generation,
                    items=payload.get('items', []),
                    retrieved_at=payload.get('retrieved_at', ''),
                )
                if kind == 'clear':
                    self.hooks.event('dot_result', generation=generation, text='', truncated=False)
                if kind == 'sources':
                    self.hooks.workspace().task_stage(
                        task_id, 'search', '检索来源已返回', 'completed'
                    )
                    self.hooks.publish_workspace()
                    self.hooks.log('web_search_ready', sources=len(payload['items']))

        completed = True
        spoken = []
        try:
            self.hooks.publish_workspace()
            self.hooks.status('thinking', '正在准备回答…')
            clauses = (
                self.hooks.brain().vision_clauses(
                    text, image, self.history, lambda: self.hooks.valid(generation), web_update
                )
                if image is not None
                else self.hooks.brain().clauses(
                    text, self.history, lambda: self.hooks.valid(generation), web_update
                )
                if self.hooks.brain()
                else iter(['语音识别已完成。你刚才说：' + text])
            )
            for clause in clauses:
                if not self.hooks.valid(generation):
                    completed = False
                    break
                self.hooks.diagnostics().mark(generation, 'first_clause')
                if self.hooks.brain():
                    self.hooks.diagnostics().link_turn(
                        generation, self.hooks.brain().last_metrics.get('turn_id')
                    )
                if not self.hooks.speak(clause, generation):
                    completed = False
                    break
                spoken.append(clause)
        except Exception as error:
            stage = error.stage if isinstance(error, OperationFailure) else 'brain'
            self.hooks.diagnostics().failure(generation, stage, error)
            # Persist only the fixed guidance in this RAM-only task receipt.
            # A completed Dot receipt remains completed in Workspace.
            self.hooks.workspace().task_finish(
                task_id, 'failed', feedback(stage, error_kind(error)).message
            )
            raise
        finally:
            close = getattr(clauses, 'close', None)
            if close:
                close()
            failed = bool(
                self.hooks.brain()
                and (
                    self.hooks.brain().last_metrics.get('completed') is False
                    or self.hooks.brain().last_metrics.get('approval_required')
                )
            )
            status = (
                'cancelled'
                if not self.hooks.valid(generation)
                else 'failed'
                if sys.exc_info()[0] or not completed or failed
                else 'completed'
            )
            self.hooks.workspace().task_finish(task_id, status, result_text[0] or ''.join(spoken))
            self.hooks.diagnostics().task_result(generation, status)
            self.hooks.diagnostics().finish(generation, status)
            self.hooks.publish_workspace()
        if completed and self.hooks.valid(generation):
            answer = ''.join(spoken)
            self.history.extend(
                [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': answer}]
            )
            self.hooks.status(
                'listening' if self.hooks.active() else 'idle',
                '我在听' if self.hooks.active() else '回答已完成 · 麦克风关闭',
            )
            if self.hooks.brain():
                self.hooks.metric('last_chat_api', self.hooks.brain().last_metrics)
            voice = getattr(self.hooks.speech(), 'voice', None)
            if voice:
                self.hooks.metric('last_speech_api', voice.last_metrics)

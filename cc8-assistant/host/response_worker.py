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
from typing import Callable, ContextManager
from announce import Announcement
from diagnostics import ROUTE_COMPLETION_STAGES, TraceBook, error_kind
from failure_feedback import FailureSink, feedback
from dot_receipt import UnconfirmedTurnStart
from operation_failure import OperationFailure, KINDS
from reply_types import (
    Reply,
    TextReply,
    VisualReply,
    BrainPreparation,
    ResultRead,
    CorrectedReply,
    WorkflowReply,
    MeetingReply,
)
from speech_correction import (
    SpeechCorrection,
    CorrectionUnavailable,
    same_owner,
    command as correction_command,
)
from result_reading import ResultReading, ReadingUnavailable, reading_command
from saved_tasks import ident as result_ident
from session_ports import EventSink, MetricSink
from workspace import Workspace


@dataclass(frozen=True)
class ResponseHooks:
    lock: ContextManager[object]
    valid: Callable[[int], bool]
    diagnostics: Callable[[], TraceBook]
    workspace: Callable[[], Workspace]
    brain: Callable[[], object]
    speech: Callable[[], object]
    active: Callable[[], bool]
    metric: MetricSink
    dispatch: Callable[[Reply | Announcement | BrainPreparation], None]
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
    can_read_result: Callable[[], bool] = lambda: False
    submit_read: Callable[[str, bool], object] | None = None
    correction_owner: Callable[[], tuple | None] = lambda: None
    submit_corrected: Callable[[str], object] | None = None
    cancel_correction: Callable[[], object] = lambda: None
    run_workflow: Callable[[WorkflowReply], None] | None = None
    run_meeting: Callable[[MeetingReply], None] | None = None
    owner_access: Callable[[], bool] = lambda: True


class ResponseWorker:
    def __init__(self, hooks: ResponseHooks):
        self.hooks = hooks
        self.queue = queue.Queue(maxsize=2)
        self.busy = False
        self.history = deque(maxlen=8)
        self.worker = None
        self.start_lock = threading.Lock()
        self.reading = ResultReading()
        self.read_requests = deque(maxlen=64)
        self.corrections = SpeechCorrection()
        self.correction_requests = deque(maxlen=64)

    def publish_corrections(self):
        if not self.hooks.owner_access():
            return
        self.hooks.event(
            'speech_correction', **self.corrections.state(self.hooks.correction_owner())
        )

    def prepare_vocabulary(self, term, alias, source_id):
        # Only an explicit, freshly validated local wordlist pair enters here.
        # Literal uniqueness and numeric/token boundaries remain owned by the
        # existing correction engine; no ASR history or task is replaced now.
        with self.hooks.lock:
            if not self.hooks.owner_access():
                raise CorrectionUnavailable('访客模式不能读取主人纠错记录')
            owner = self.hooks.correction_owner()
            if not source_id or self.corrections.source_token(owner) != source_id:
                raise CorrectionUnavailable('Original recognition changed')
            draft = self.corrections.prepare(term + '，不是' + alias, owner)
            self.publish_corrections()
            if not same_owner(owner, self.hooks.correction_owner()):
                raise CorrectionUnavailable('Correction owner changed')
            self.corrections.current(owner, draft.ident)

    def clear_corrections(self, preserve_source=False, *, publish=True):
        with self.hooks.lock:
            changed = self.corrections.clear(preserve_source)
            if changed and publish:
                try:
                    self.publish_corrections()
                except (OSError, EOFError):
                    # Context is already erased. Optional UI cleanup cannot
                    # prevent cancellation of tools, capture or old playback.
                    pass
            return changed

    def expire_corrections(self):
        with self.hooks.lock:
            if (
                self.corrections.draft is None
                and self.corrections.source is not None
                and self.corrections.now() >= self.corrections.source[2]
            ):
                self.clear_corrections()
            if self.corrections.draft is not None:
                try:
                    self.corrections.current(self.hooks.correction_owner())
                except CorrectionUnavailable:
                    self.clear_corrections()

    def request_correction(self, data):
        with self.hooks.lock:
            try:
                if not self.hooks.owner_access():
                    raise CorrectionUnavailable('访客模式不能读取主人纠错记录')
                if data['request_id'] in self.correction_requests:
                    raise CorrectionUnavailable('这个更正请求已处理，不会重复提交。')
                owner = self.hooks.correction_owner()
                draft = self.corrections.current(owner, data['draft_id'])
            except CorrectionUnavailable as error:
                self.hooks.event('workspace_result', ok=False, message=str(error))
                return
            self.correction_requests.append(data['request_id'])
            if data['action'] == 'cancel':
                self.hooks.cancel_correction()
                if self.corrections.draft is draft:
                    self.clear_corrections(preserve_source=True)
                return
            self.submit_correction(owner, data['draft_id'])

    def submit_correction(self, owner, ident):
        if not self.hooks.owner_access():
            raise CorrectionUnavailable('访客模式不能提交主人纠错记录')
        if self.hooks.submit_corrected is None:
            raise CorrectionUnavailable('当前后台暂不能提交更正，请直接说完整的新问题。')
        text = self.corrections.consume(owner, ident)
        try:
            self.hooks.submit_corrected(text)
        except (OSError, RuntimeError, ValueError, queue.Full):
            # Existing text admission owns startup failure feedback and any
            # superseding generation. Consumed drafts are never retried.
            return
        finally:
            # Publish the current state only after admission. Display I/O
            # must not admit newer work that this submission then cancels.
            try:
                self.publish_corrections()
            except (OSError, EOFError):
                pass

    def handle_correction_voice(self, job):
        if not self.hooks.owner_access():
            return False
        action = correction_command(job.text)
        if action is None:
            return False
        with self.hooks.lock:
            if not self.hooks.owner_access():
                return False
            if not self.hooks.valid(job.generation):
                return True
            self.hooks.diagnostics().ensure(job.generation, 'text')
            owner = self.hooks.correction_owner()
            try:
                if action[0] == 'draft':
                    self.corrections.prepare(action[1], owner)
                    message = '更正已准备，请核对屏幕中的完整问题。'
                else:
                    draft = self.corrections.current(owner)
                    if action[0] == 'confirm':
                        self.submit_correction(owner, draft.ident)
                        return True
                    if action[0] == 'cancel':
                        self.clear_corrections(preserve_source=True)
                        message = '已撤回未提交的更正；原来已经执行的操作不会撤销。'
                    else:
                        message = '屏幕上是尚未提交的更正，请核对完整问题。'
            except CorrectionUnavailable as error:
                message = str(error)
            self.publish_corrections()
        played = self.hooks.speak(message, job.generation)
        with self.hooks.lock:
            self.hooks.diagnostics().finish(job.generation, 'completed' if played else 'cancelled')
        return True

    def result_task(self, ident):
        if not self.hooks.owner_access():
            raise ReadingUnavailable('访客模式不能读取主人任务结果')
        task = next(
            (
                t
                for t in self.hooks.workspace().snapshot()['tasks']
                if t['id'] == ident and t['status'] == 'completed'
            ),
            None,
        )
        if task is None:
            raise ReadingUnavailable(
                '仅能朗读本次后台仍持有的已完成结果；未确认的任务请先在电脑核对。'
            )
        return task

    def request_read(self, data):
        # The device supplies identity and mode only, never replacement text.
        request_id, ident, restart = data['request_id'], result_ident(data['id']), data['restart']
        with self.hooks.lock:
            try:
                if request_id in self.read_requests:
                    raise ReadingUnavailable('这个朗读请求已处理或结果未确认，不会重复提交。')
                if not self.hooks.can_read_result() or self.hooks.submit_read is None:
                    raise ReadingUnavailable('请等待当前任务、播报或校准结束后再朗读结果。')
                self.result_task(ident)
                self.reading.available(ident, restart)
            except ReadingUnavailable as error:
                self.hooks.event(
                    'workspace_result', request_id=request_id, ok=False, message=str(error)
                )
                return
            self.read_requests.append(request_id)
            try:
                result = self.hooks.submit_read(ident, restart)
                if isinstance(result, dict) and result.get('ok') is True:
                    self.reading.select(ident)
            except (OSError, RuntimeError, ValueError, queue.Full):
                # The existing text admission transaction owns generation-bound
                # startup failure UI/cleanup. Never publish a second late error
                # over a newer request admitted from one of its callbacks.
                return

    def read_result(self, ident, restart, generation):
        with self.hooks.lock:
            if not self.hooks.valid(generation):
                return
            self.hooks.diagnostics().ensure(generation, 'text')
            try:
                self.result_task(ident)
                plan = self.reading.plan(ident, restart)
            except ReadingUnavailable as error:
                self.hooks.status('error', str(error))
                self.hooks.diagnostics().finish(generation, 'rejected')
                return
            self.hooks.status(
                'thinking',
                ('收到的结果已截短。' if plan.truncated else '')
                + '正在朗读已完成结果；未确认的句段可能重复，不会重新执行任务。',
            )
        completed = False
        failed = False
        try:
            for offset in range(0, len(plan.text), 120):
                if not self.hooks.valid(generation):
                    return
                clause = plan.text[offset : offset + 120]
                diagnostics = self.hooks.diagnostics()
                before = diagnostics.confirmed_playback_count(generation)
                confirmed = False
                advanced = False
                try:
                    played = self.hooks.speak(clause, generation)
                finally:
                    confirmed = diagnostics.confirmed_playback_count(generation) > before
                    if confirmed:
                        advanced = self.reading.ack(plan, offset, clause)
                if not played:
                    return
                if not confirmed or not advanced:
                    failed = True
                    with self.hooks.lock:
                        if self.hooks.valid(generation):
                            self.hooks.status(
                                'error',
                                '这一句的播完或续读进度未确认，已停止朗读；不会自动重试或重新执行任务。',
                            )
                    return
            with self.hooks.lock:
                completed = self.hooks.valid(generation)
                if completed:
                    self.hooks.status(
                        'listening' if self.hooks.active() else 'idle',
                        '已读完本次收到的结果摘录，原结果可能已截短。'
                        if plan.truncated
                        else '结果朗读完成；原任务状态保持不变。',
                    )
        except Exception:
            failed = True
            raise
        finally:
            self.hooks.diagnostics().finish(
                generation, 'completed' if completed else 'failed' if failed else 'cancelled'
            )

    def start(self):
        with self.start_lock:
            if self.worker is not None:
                raise RuntimeError('Response worker already started')
            self.worker = threading.Thread(target=self.work, daemon=True)
            self.worker.start()

    def offer(self, job):
        # Literal announcements must fail when busy, never evict another job.
        self.queue.put_nowait(job)

    def prepare(self, generation):
        if not callable(getattr(self.hooks.brain(), 'prepare', None)):
            return
        try:
            self.offer(BrainPreparation(generation))
        except queue.Full:
            # Preparation is optional and cannot evict a real request.
            return

    def enqueue(self, job):
        with self.hooks.lock:
            if not self.hooks.valid(job.generation):
                self.discard_announcement(job)
                return
            if not self.hooks.owner_access():
                # Guest questions do not become an owner correction source.
                pass
            elif type(job) is Reply and correction_command(job.text) is not None:
                pass
            else:
                text = job.text if type(job) is Reply or isinstance(job, CorrectedReply) else ''
                owner = self.hooks.correction_owner() if text else None
                changed = self.corrections.note(text, owner)
                if changed or owner is not None:
                    try:
                        self.publish_corrections()
                    except (OSError, EOFError):
                        pass
            # Admission and eviction share generation ownership. In particular,
            # a correction display callback may synchronously admit newer work.
            while self.hooks.valid(job.generation):
                try:
                    self.queue.put_nowait(job)
                    return
                except queue.Full:
                    try:
                        old = self.queue.get_nowait()
                    except queue.Empty:
                        continue
                    self.discard_announcement(old)
            self.discard_announcement(job)

    @staticmethod
    def discard_announcement(job):
        if isinstance(job, Announcement):
            job.discard_segments()
            job.done.set()

    def work(self):
        # 单消费者串行播报，识别另在线程运行，因此暂停时仍能继续听插话。
        while True:
            job = self.queue.get()
            try:
                self.hooks.dispatch(job)
            except Exception as error:
                if isinstance(job, BrainPreparation):
                    # Even failed diagnostic logging is not a failed user task.
                    continue
                # Keep owner admission and failure publication atomic. Text
                # validity must remain live until its error has been sent.
                with self.hooks.lock:
                    if self.hooks.valid(job.generation):
                        self.hooks.failure('这次回答没有完成', error, trace_key=job.generation)
                    else:
                        self.hooks.log('response_worker_error', error_type=type(error).__name__)
            finally:
                if isinstance(job, TextReply):
                    try:
                        self.hooks.finish_text(job.generation)
                    except Exception as error:
                        # A broken connection during cleanup must not kill the
                        # serial worker or retry a task with possible effects.
                        self.hooks.log('text_finish_error', error_type=type(error).__name__)

    def respond_job(self, job):
        if not self.hooks.owner_access() and type(job) not in (Reply, TextReply):
            # Trusted internal work items still need authorization at dispatch;
            # an earlier enqueue is not a grant across an identity transition.
            self.discard_announcement(job)
            return
        if isinstance(job, BrainPreparation):
            if not self.hooks.active() or not self.hooks.valid(job.generation):
                return
            self.busy = True
            try:
                prepare = getattr(self.hooks.brain(), 'prepare', None)
                if callable(prepare) and prepare(lambda: self.hooks.valid(job.generation)):
                    self.hooks.log('brain_prepared')
            except Exception as error:
                # No user task exists yet. A later explicit request takes the
                # normal failure/recovery path; never invent a failed task now.
                self.hooks.log('brain_prepare_failed', error_type=type(error).__name__)
            finally:
                self.busy = False
            return
        if isinstance(job, Announcement):
            self.hooks.read_announcement(job)
            return
        if not self.hooks.valid(job.generation):
            return
        self.busy = True
        try:
            if type(job) is Reply and self.handle_correction_voice(job):
                pass
            elif isinstance(job, MeetingReply):
                if self.hooks.run_meeting is None:
                    raise RuntimeError('Meeting document executor unavailable')
                self.hooks.run_meeting(job)
            elif isinstance(job, WorkflowReply):
                if self.hooks.run_workflow is None:
                    raise RuntimeError('Workflow executor unavailable')
                self.hooks.run_workflow(job)
            elif isinstance(job, ResultRead):
                self.read_result(job.result_id, job.restart, job.generation)
            elif isinstance(job, VisualReply):
                self.hooks.answer_visual(job)
            else:
                self.hooks.answer(job.text, job.generation)
        finally:
            self.busy = False

    def answer(self, text, generation, image=None):
        command = reading_command(text) if image is None and self.hooks.owner_access() else None
        if command is not None:
            with self.hooks.lock:
                tasks = self.hooks.workspace().snapshot()['tasks']
                task = self.reading.target(tasks)
                if not self.hooks.valid(generation):
                    return
                if task is None:
                    self.hooks.diagnostics().ensure(generation, 'text')
                    self.hooks.diagnostics().finish(generation, 'rejected')
                    self.hooks.status(
                        'error',
                        '最近任务没有可确认的完成结果，请在任务详情明确选择已完成结果；不会改读其他任务或重新执行。',
                    )
                    return
            self.read_result(task['id'], command, generation)
            return
        # 任务成功以大脑真实回执为准，后续停止朗读不能把已完成工作改为取消。
        with self.hooks.lock:
            if not self.hooks.valid(generation):
                return
            self.hooks.diagnostics().ensure(generation, 'vision' if image is not None else 'text')
            self.hooks.diagnostics().mark(generation, 'brain_start')
            task_id = self.hooks.workspace().task_start(generation, text)
            self.reading.begin(task_id, generation)
        result_text = ['']
        clauses = None

        def web_update(kind, payload):
            if kind == 'brain_stage':
                with self.hooks.lock:
                    if not self.hooks.valid(generation):
                        return
                    if (
                        not isinstance(payload, dict)
                        or not {'stage', 'at'} <= payload.keys()
                        or payload.keys() - {'stage', 'at', 'reused'}
                    ):
                        raise ValueError('Invalid Dot timing signal')
                    self.hooks.diagnostics().dot_stage(
                        generation, payload['stage'], payload['at'], payload.get('reused')
                    )
                return
            if kind == 'route_completed':
                with self.hooks.lock:
                    scenario = payload.get('scenario')
                    if (
                        self.hooks.valid(generation)
                        and isinstance(scenario, str)
                        and scenario in ROUTE_COMPLETION_STAGES
                    ):
                        self.hooks.diagnostics().mark(generation, ROUTE_COMPLETION_STAGES[scenario])
                return
            if kind == 'dot_result':
                with self.hooks.lock:
                    if not self.hooks.valid(generation):
                        return
                    self.hooks.diagnostics().task_result(generation, 'completed')
                    self.hooks.diagnostics().link_turn(
                        generation, self.hooks.brain().last_metrics.get('turn_id')
                    )
                    result_text[0] = payload.get('text', '')
                    # The Dot turn receipt closes the task, independently of the
                    # remaining audio or device delivery. A disconnected display
                    # cannot turn successfully executed work into a failed task.
                    self.hooks.workspace().task_finish(task_id, 'completed', result_text[0])
                    self.reading.complete(task_id, result_text[0], payload.get('truncated', False))
                    self.hooks.event('dot_result', generation=generation, **payload)
                    self.hooks.publish_workspace()
                return
            if kind == 'result_provenance':
                with self.hooks.lock:
                    if self.hooks.valid(generation):
                        self.hooks.workspace().task_provenance(task_id, payload)
                        try:
                            self.hooks.publish_workspace()
                        except (OSError, EOFError):
                            # Display metadata cannot prevent the following
                            # authoritative result from committing locally.
                            pass
                return
            if not self.hooks.valid(generation):
                return
            if kind == 'progress':
                message = payload.get('message', '正在处理…')
                self.hooks.workspace().task_stage(
                    task_id, 'analysis', '连接助理' if '连接' in message else '分析请求', 'running'
                )
                self.hooks.publish_workspace()
                self.hooks.event('status', generation=generation, phase='thinking', message=message)
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
                category = payload.get('kind')
                category = (
                    category
                    if isinstance(category, str) and category in KINDS
                    else 'provider_or_runtime'
                )
                self.hooks.workspace().task_stage(task_id, 'search', '联网检索', 'failed')
                self.hooks.publish_workspace()
                self.hooks.log('web_search_failed', kind=category)
            elif kind in ('no_results', 'clarification'):
                title = '未找到可核实的网页结果' if kind == 'no_results' else '需要补充查询条件'
                self.hooks.workspace().task_stage(task_id, 'search', title, 'completed')
                self.hooks.publish_workspace()
            elif kind in ('clear', 'sources'):
                self.hooks.event(
                    'web_sources',
                    generation=generation,
                    items=payload.get('items', []),
                    retrieved_at=payload.get('retrieved_at', ''),
                    time_basis=payload.get('time_basis', ''),
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
        failure = None
        spoken = []
        try:
            with self.hooks.lock:
                if not self.hooks.valid(generation):
                    return
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
                # A successful TTS call is not delivery. Capture this clause's
                # immutable plan token while the router is suspended at yield;
                # advance only after a matched device play_done receipt.
                brain = self.hooks.brain()
                delivery = getattr(brain, 'current_report_delivery', None)
                diagnostics = self.hooks.diagnostics()
                before = diagnostics.confirmed_playback_count(generation)
                try:
                    played = self.hooks.speak(clause, generation)
                finally:
                    if diagnostics.confirmed_playback_count(generation) > before:
                        self.reading.original_ack(task_id, generation, clause)
                        if delivery:
                            owner, token = delivery
                            owner.confirm(token)
                if not played:
                    completed = False
                    break
                spoken.append(clause)
        except Exception as error:
            stage = error.stage if isinstance(error, OperationFailure) else 'brain'
            self.hooks.diagnostics().failure(generation, stage, error)
            failure = (stage, error)
            raise
        finally:
            close = getattr(clauses, 'close', None)
            if close:
                try:
                    close()
                except Exception as cleanup_error:
                    # The task result/primary speech error already has an owner.
                    # Optional iterator cleanup cannot skip its terminal receipt.
                    self.hooks.log(
                        'response_iterator_close_failed',
                        error_type=type(cleanup_error).__name__,
                    )
            # Successful close waits for receipt admission. On cleanup failure,
            # still finish the trace and preserve any result already committed.
            with self.hooks.lock:
                if failure:
                    stage, error = failure
                    unconfirmed = isinstance(error, UnconfirmedTurnStart)
                    self.hooks.workspace().task_finish(
                        task_id,
                        'failed',
                        feedback(stage, error_kind(error), unconfirmed=unconfirmed).message,
                        unconfirmed=unconfirmed,
                    )
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
                self.hooks.workspace().task_finish(
                    task_id, status, result_text[0] or ''.join(spoken)
                )
                if status == 'completed':
                    self.reading.complete(task_id, result_text[0] or ''.join(spoken))
                self.hooks.diagnostics().task_result(generation, status)
                self.hooks.diagnostics().finish(generation, status)
                try:
                    self.hooks.publish_workspace()
                except (OSError, EOFError) as delivery_error:
                    if failure is None:
                        raise
                    # Keep the original error for the response worker's guidance.
                    self.hooks.log(
                        'failed_response_workspace_undelivered',
                        error_type=type(delivery_error).__name__,
                    )
        # Do not let a new turn start between ownership admission and its
        # predecessor's final status or history update.
        with self.hooks.lock:
            if not completed or not self.hooks.valid(generation):
                return
            answer = ''.join(spoken)
            # Local report excerpts are not automatic context for a later model
            # request. Follow-up binds the separate, fixed RAM snapshot instead.
            if self.hooks.owner_access() and (
                not self.hooks.brain()
                or self.hooks.brain().last_metrics.get('provider')
                not in ('work_report', 'preference_memory')
            ):
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

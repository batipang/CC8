"""Foreground explicit workflow offers, serial execution and RAM-only result access."""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import time
import uuid
from typing import Callable, ContextManager

from report_checkpoint import describe
from reply_types import WorkflowReply
from workflow_actions import WorkflowActions, ActionHooks
from workflow_definition import fingerprint
from workflow_protocol import validate_request
from workflow_run import WorkflowRun
from workflow_scheduler import WorkflowScheduler
from web_search import SpeechClauses


class RequestRejected(ValueError):
    """Only fixed local guidance, never provider/transport exception content."""


@dataclass(frozen=True)
class WorkflowHooks:
    lock: ContextManager
    owner: Callable[[], tuple | None]  # connection, route, generation, brain, provider
    idle: Callable[[], bool]
    valid: Callable[[int], bool]
    source: Callable[[], object]
    brain: Callable[[], object]
    read_report: Callable
    source_current: Callable
    submit: Callable
    cancel: Callable
    capture: Callable
    workspace: Callable
    reading: Callable
    diagnostics: Callable
    speak: Callable
    event: Callable
    publish_workspace: Callable
    status: Callable
    active: Callable[[], bool] = lambda: False


def result_text(snapshot):
    lines = ['流程：' + snapshot['name']]
    for row in snapshot['steps']:
        lines.append(
            row['title']
            + '：'
            + (
                row['result']['text'] + ('\n[结果已截短]' if row['result']['truncated'] else '')
                if row.get('result') is not None
                else '尚未执行'
                if row['state'] == 'pending'
                else '结果未确认，请先核对，勿重复交办。'
            )
        )
    return '\n\n'.join(lines)


def narration_text(snapshot):
    # Bounded literal excerpts, not a model-created or independently verified
    # summary. Full per-step results stay available through explicit result UI.
    return (
        '流程已执行完成。以下只读每步结果摘录，完整内容请查看流程结果。\n'
        + '\n'.join(
            row['title'] + '：' + SpeechClauses.clean(row['result']['text'])[:320]
            for row in snapshot['steps']
        )
    )[:1600]


class WorkflowController:
    def __init__(self, hooks, store, *, clock=time.monotonic):
        self.hooks, self.store, self.clock = hooks, store, clock
        self.requests = deque(maxlen=64)
        self.offer = None
        self.active = None
        self.run_owner = None
        self.config_hash = None
        self.camera_nonce = ''
        self.task_id = None
        self.narrating = False
        self.scheduler = None
        self.journal_unconfirmed = False
        # No worker/timer/store read or write in this passive constructor.

    def owner(self):
        return self.hooks.owner()

    def current(self, run, generation):
        return (
            self.active is run
            and self.owner() == self.run_owner
            and self.hooks.valid(generation) is True
            and (
                run.origin != 'scheduled'
                or self.scheduler is not None
                and self.scheduler.authorized(run.definition_hash)
            )
        )

    def invalidate(self, reason='stop'):
        with self.hooks.lock:
            self.offer = None
            if self.active is not None:
                self.active.cancel()
                self.settle_execution(self.active)
                self.settle_delivery(self.active, False)
            if self.scheduler is not None and reason not in (
                'text_request',
                'voice',
                'verified_voice',
            ):
                self.scheduler.revoke()

    def bind_scheduler(self, hooks):
        self.scheduler = WorkflowScheduler(self.store, hooks)

    def submit_scheduled(self, flow, claim):
        run = WorkflowRun(flow, origin='scheduled', attempt=claim, owner_guard=self.hooks.lock)
        return self.hooks.submit(run, '', fingerprint(flow))

    def settle_execution(self, run):
        if run.origin != 'scheduled' or run.state in ('prepared', 'running'):
            return
        try:
            self.store.finish_execution(run.ident, run.state)
        except (OSError, ValueError):
            self.journal_unconfirmed = True
            if self.scheduler is not None:
                self.scheduler.revoke()

    def settle_delivery(self, run, delivered):
        if run.origin != 'scheduled' or run.state != 'completed':
            return
        try:
            self.store.finish_delivery(run.ident, delivered)
        except (OSError, ValueError):
            self.journal_unconfirmed = True
            if self.scheduler is not None:
                self.scheduler.revoke()

    def state(self):
        state = self.store.snapshot()
        return dict(
            revision=state['revision'],
            items=[
                dict(
                    id=flow['id'],
                    name=flow['name'],
                    step_count=len(flow['steps']),
                    scheduled=flow['schedule'] is not None and flow['schedule']['enabled'],
                )
                for flow in state['items']
            ],
            attempts=state['attempts'][-16:],
            run=self.active.snapshot() if self.active is not None else None,
            journal_unconfirmed=self.journal_unconfirmed,
        )

    def selected(self, ident):
        state = self.store.snapshot()
        flow = next((f for f in state['items'] if f['id'] == ident), None)
        if flow is None:
            raise ValueError('Workflow missing')
        return state, flow

    def prepare(self, request):
        if not self.hooks.idle():
            raise RequestRejected('请先暂停语音，等待当前任务和播报结束，再核对运行流程。')
        state, flow = self.selected(request['id'])
        if state['revision'] != request['revision']:
            raise ValueError('Workflow edited')
        frozen = deepcopy(flow)
        for step in frozen['steps']:
            if step['kind'] == 'work_report' and step['source'] is None:
                source = self.hooks.source()
                if source is None:
                    raise RequestRejected('请先在“工作汇报来源”明确选择来源，再核对流程。')
                step['source'] = describe(source)
        self.offer = dict(
            id=uuid.uuid4().hex,
            owner=self.owner(),
            expires=self.clock() + 90,
            flow=frozen,
            config_hash=fingerprint(flow),
        )
        return dict(
            id=self.offer['id'],
            definition=deepcopy(frozen),
            expires_in_seconds=90,
            camera_required=any(s['kind'] == 'ocr' for s in frozen['steps']),
        )

    def request(self, data):
        validate_request(data)
        with self.hooks.lock:
            owner = self.owner()
            if owner is None:
                return
            request_id, action = data['request_id'], data['action']
            ok, fields = False, {}
            cancelled_owner = None
            message = '流程状态未确认，请重新查看；不会自动重试或重复执行。'
            try:
                if request_id in self.requests:
                    raise ValueError('Already handled')
                self.requests.append(request_id)
                if action in ('put', 'delete', 'reset', 'clear_history', 'bind_source'):
                    if not self.hooks.idle():
                        raise RequestRejected('请先暂停语音，等待任务和播报结束，再修改流程。')
                    if self.scheduler is None and (
                        action == 'put'
                        and data['value']['schedule'] is not None
                        and data['value']['schedule']['enabled']
                    ):
                        raise ValueError('Workflow scheduler not enabled')
                    mutation, value, ident = action, data.get('value'), data.get('id')
                    if action == 'bind_source':
                        state, value = self.selected(data['id'])
                        if state['revision'] != data['revision']:
                            raise ValueError('Workflow edited')
                        selected = self.hooks.source()
                        if selected is None:
                            raise RequestRejected(
                                '请先在“工作汇报来源”明确选择来源，再绑定汇报步骤。'
                            )
                        source = describe(selected)
                        reports = [s for s in value['steps'] if s['kind'] == 'work_report']
                        if not reports:
                            raise RequestRejected('这个流程没有工作汇报步骤，无需绑定来源。')
                        for step in reports:
                            step['source'] = deepcopy(source)
                        value['schedule'] = None
                        mutation, ident = 'put', None
                    changed = self.store.apply(
                        mutation,
                        data['revision'],
                        value=value,
                        ident=ident,
                        current=lambda: self.owner() == owner,
                    )
                    self.offer = None
                    if action == 'bind_source':
                        fields['definition'] = next(
                            f for f in changed['items'] if f['id'] == data['id']
                        )
                    if self.scheduler is not None:
                        self.scheduler.arm()
                elif action == 'get':
                    fields['definition'] = deepcopy(self.selected(data['id'])[1])
                elif action == 'prepare':
                    fields['offer'] = self.prepare(data)
                elif action == 'run':
                    offer, self.offer = self.offer, None
                    if (
                        offer is None
                        or offer['id'] != data['offer_id']
                        or offer['owner'] != self.owner()
                        or self.clock() >= offer['expires']
                        or not self.hooks.idle()
                    ):
                        raise RequestRejected(
                            '运行确认已过期、已使用或当前连接／任务状态已变化，请重新核对流程。'
                        )
                    if fingerprint(self.selected(offer['flow']['id'])[1]) != offer['config_hash']:
                        raise ValueError('Workflow definition changed')
                    camera = any(s['kind'] == 'ocr' for s in offer['flow']['steps'])
                    if bool(data['camera_nonce']) != camera:
                        raise ValueError('Fresh camera consent missing or unexpected')
                    run = WorkflowRun(offer['flow'], owner_guard=self.hooks.lock)
                    result = self.hooks.submit(run, data['camera_nonce'], offer['config_hash'])
                    if not isinstance(result, dict) or result.get('ok') is not True:
                        raise ValueError('Workflow admission unconfirmed')
                    fields['run'] = run.snapshot()
                elif action == 'cancel':
                    if self.active is None or self.active.ident != data['run_id']:
                        raise ValueError('Workflow run changed')
                    if self.owner() != self.run_owner:
                        raise ValueError('Workflow owner changed')
                    if self.active.state in ('prepared', 'running') or self.narrating:
                        generation = self.hooks.cancel('workflow_cancel')
                        if generation == owner[2] + 1:
                            cancelled_owner = owner[:2] + (generation,) + owner[3:]
                elif action == 'result':
                    if self.active is None or self.active.ident != data['run_id']:
                        raise ValueError('Workflow result expired')
                    row = next(
                        (
                            s
                            for s in self.active.snapshot(include_results=True)['steps']
                            if s['id'] == data['step_id']
                        ),
                        None,
                    )
                    if row is None:
                        raise ValueError('Workflow result missing')
                    fields['result'] = row
                fields['state'] = self.state()
                ok = True
                message = (
                    '流程状态已核对。结果仅在本次后台内存中保存；已执行的操作不会因停止而撤回。'
                )
            except RequestRejected as error:
                message = str(error)
            except (OSError, ValueError, RuntimeError):
                pass
            if self.owner() != owner:
                admitted = fields.get('run')
                run_ack = (
                    action == 'run'
                    and isinstance(admitted, dict)
                    and self.active is not None
                    and admitted.get('id') == self.active.ident
                    and self.owner() == self.run_owner
                )
                cancel_ack = (
                    action == 'cancel'
                    and cancelled_owner is not None
                    and cancelled_owner == self.owner()
                    and self.active is not None
                    and self.active.ident == data['run_id']
                )
                if not run_ack and not cancel_ack:
                    return
            self.hooks.event(
                'workflow_result', request_id=request_id, ok=ok, message=message, **fields
            )

    def bind(self, run, generation, nonce, config_hash):
        # Called only by the text submission factory after generation-changing
        # cancellation, before queue admission, in the shared owner transaction.
        with self.hooks.lock:
            self.active, self.run_owner = run, self.owner()
            self.camera_nonce, self.config_hash = nonce, config_hash
            self.task_id = None
            return WorkflowReply(generation, '运行流程：' + run.flow['name'], run)

    def execute(self, job):
        run, generation = job.run, job.generation
        current = lambda: self.current(run, generation)
        image = None
        with self.hooks.lock:
            if not current():
                run.cancel()
                self.settle_execution(run)
                return
            try:
                unchanged = fingerprint(self.selected(run.flow['id'])[1]) == self.config_hash
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                run.cancel()
                self.settle_execution(run)
                return
            self.task_id = task_id = self.hooks.workspace().task_start(generation, job.text)
            self.hooks.reading().begin(task_id, generation)

        def publish(state):
            with self.hooks.lock:
                if self.active is not run:
                    return
                for row in state['steps']:
                    if row['state'] != 'pending':
                        self.hooks.workspace().task_stage(
                            task_id,
                            row['id'],
                            row['title'],
                            'failed' if row['state'] == 'unconfirmed' else row['state'],
                        )
                if state['state'] == 'completed':
                    full = result_text(run.snapshot(include_results=True))
                    self.hooks.workspace().task_finish(task_id, 'completed', full)
                    self.hooks.reading().complete(task_id, full)
                    self.hooks.diagnostics().task_result(generation, 'completed')
                    self.settle_execution(run)
                if current():
                    self.hooks.publish_workspace()
                    self.hooks.event('workflow_progress', generation=generation, run=state)
                    active = next(
                        (row for row in state['steps'] if row['state'] == 'running'), None
                    )
                    if active is not None and current():
                        self.hooks.status('thinking', '正在执行：' + active['title'])

        def execute_step(step, owned, deadline, accepted):
            nonlocal image
            try:
                actions = WorkflowActions(
                    ActionHooks(
                        self.hooks.brain,
                        self.hooks.read_report,
                        self.hooks.source_current,
                        image=lambda ident: image,
                    )
                )
                if step['id'] == run.flow['steps'][0]['id']:
                    actions.preflight(run.flow, owned)
                if step['kind'] == 'ocr':
                    image = self.hooks.capture(generation, self.camera_nonce, owned)
                    with self.hooks.lock:
                        if owned():
                            self.hooks.status('thinking', '摄像头已关闭，正在分析本次画面。')
                return actions.execute(step, owned, deadline, accepted)
            finally:
                image = None

        result = run.run(
            execute_step,
            current=current,
            publish=publish,
            authorize_camera=lambda flow_hash, step_id: bool(self.camera_nonce) and current(),
        )
        self.settle_execution(run)
        with self.hooks.lock:
            if result['state'] != 'completed':
                self.hooks.workspace().task_finish(
                    task_id,
                    'cancelled' if result['state'] == 'cancelled' else 'failed',
                    result_text(result),
                    unconfirmed=any(s['state'] == 'unconfirmed' for s in result['steps']),
                )
            if not current():
                return
            self.hooks.publish_workspace()
            if result['state'] != 'completed':
                self.hooks.status(
                    'error', '流程未全部完成；已完成步骤保留，请查看每步结果，不会自动重跑。'
                )
                self.hooks.diagnostics().finish(generation, result['state'])
                return
            self.narrating = True
        delivered = True
        try:
            # Native speak keeps its existing synchronized captions/mouth and
            # matched device ACK. No step is re-executed on narration failure.
            spoken = narration_text(result)
            for offset in range(0, len(spoken), 120):
                diagnostics = self.hooks.diagnostics()
                before = diagnostics.confirmed_playback_count(generation)
                if (
                    not current()
                    or not self.hooks.speak(spoken[offset : offset + 120], generation)
                    or diagnostics.confirmed_playback_count(generation) <= before
                ):
                    delivered = False
                    return
        except Exception:
            delivered = False
            raise
        finally:
            with self.hooks.lock:
                self.narrating = False
                self.settle_delivery(run, delivered)
                self.hooks.diagnostics().finish(
                    generation, 'completed' if delivered else 'cancelled'
                )
                if current():
                    self.hooks.status(
                        ('listening' if self.hooks.active() else 'idle') if delivered else 'error',
                        (
                            '流程执行已完成 · 我在听'
                            if self.hooks.active()
                            else '流程执行已完成 · 麦克风关闭'
                        )
                        if delivered
                        else '流程执行已完成，播完状态未确认；请查看每步结果，不会重跑。',
                    )

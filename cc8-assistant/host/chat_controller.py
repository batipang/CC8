"""Bound one explicit foreground chat-catalog operation; no automatic history reads."""

from dataclasses import dataclass
from collections import deque
import re
import threading
import uuid
from typing import Callable, ContextManager
from dot_handoff import ChatCatalog
from dot_client import Cancelled, DotError
from report_voice import ReportVoice, ReportVoiceHooks, report_text
from report_sources import REGISTRY_ACTIONS, MUTATIONS, ReportScope, validate_request


@dataclass(frozen=True)
class ChatHooks:
    lock: ContextManager[object]
    generation: Callable[[], int]
    connection: Callable[[], object]
    available: Callable[[], bool]
    idle: Callable[[], bool]
    select: Callable[[object], None]
    event: Callable[..., None]
    transport_revision: Callable[[], int] = lambda: 0
    capture_stopped: Callable[[], bool] = lambda: True


class ChatController:
    def __init__(self, hooks, catalog=None, sources=None):
        self.hooks = hooks
        self.catalog = catalog or ChatCatalog()
        self.lock = threading.Lock()
        self.busy = False
        self.selection = None
        self.active_request = None
        self.worker = None
        self.sources = sources
        self.report_scope = None
        self.source_requests = deque(maxlen=64)
        self.voice = ReportVoice(
            ReportVoiceHooks(
                lock=hooks.lock,
                context=lambda: (
                    self.report_context_source(),
                    hooks.connection(),
                    hooks.transport_revision(),
                ),
                read=self.read_voice_report,
            )
        )

    def report_source(self):
        return self.report_scope if self.report_scope is not None else self.selection

    def report_context_source(self):
        if self.report_scope is not None:
            try:
                if not self.scope_current(self.report_scope):
                    return None
            except (OSError, ValueError):
                return None
        return self.report_source()

    def scope_current(self, source):
        if not isinstance(source, ReportScope):
            return True
        if self.sources is None:
            return False
        rows = {row['id']: row for row in self.sources.snapshot()['items']}
        return all(
            member.registration_id in rows
            and (
                rows[member.registration_id]['thread_id'],
                rows[member.registration_id]['title'],
                rows[member.registration_id]['project'],
            )
            == (member.thread_id, member.title, member.project)
            for member in source.members
        )

    def read_voice_report(self, source, period, is_current):
        with self.hooks.lock:
            if (
                not is_current()
                or self.report_source() is not source
                or self.hooks.connection() is None
                or not self.scope_current(source)
            ):
                raise Cancelled('Work report cancelled')
            if not self.hooks.available():
                raise DotError('Work report is unavailable')
            with self.lock:
                if self.busy:
                    raise DotError('Work report is busy')
                self.busy = True
                ident = self.active_request = uuid.uuid4().hex
            connection = self.hooks.connection()

        def current():
            with self.hooks.lock:
                with self.lock:
                    return (
                        is_current()
                        and self.active_request == ident
                        and self.report_source() is source
                        and self.scope_current(source)
                        and connection is self.hooks.connection()
                    )

        try:
            return self.catalog.report(source, period, is_current=current)
        finally:
            with self.lock:
                self.busy = False
                self.active_request = None

    def cancel(self, request_id):
        with self.hooks.lock:
            with self.lock:
                if self.active_request == request_id:
                    self.active_request = None

    def state(self):
        return {
            'mode': 'summary_handoff' if self.selection else 'independent',
            'source_title': self.selection.source_title if self.selection else '',
            'shared_chat': False,
            'history_persisted': False,
            'work_reports_supported': True,
            'registered_sources_supported': self.sources is not None,
            'report_source_available': self.report_source() is not None,
            'report_source_title': self.report_source().source_title
            if self.report_source() is not None
            else '',
            'report_scope': 'registered' if self.report_scope is not None else 'selected_chat',
            'local_project_available': bool(self.selection and self.selection.cwd),
        }

    def publish(self, request_id='', **fields):
        self.hooks.event('dot_chat_state', request_id=request_id, **self.state(), **fields)

    def request(self, data):
        request_id = data.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
            raise ValueError('Invalid chat request ID')
        action = data.get('action')
        if action not in ('state', 'list', 'select', 'clear', 'report', *REGISTRY_ACTIONS):
            raise ValueError('Invalid chat action')
        if action in REGISTRY_ACTIONS:
            validate_request(data)
        if action == 'report' and data.get('period') not in ('half_hour', 'two_hours', 'today'):
            raise ValueError('Invalid report period')
        with self.hooks.lock:
            if not self.hooks.available() or self.hooks.connection() is None:
                self.publish(request_id, ok=False, message='Dot 聊天入口不可用')
                return
            if action == 'state':
                self.publish(request_id, ok=True)
                return
            if action in REGISTRY_ACTIONS and self.sources is None:
                self.publish(request_id, ok=False, message='当前后台未接入登记来源')
                return
            if action == 'report' and self.report_source() is None:
                self.publish(request_id, ok=False, message='请先选择一个 Dot 聊天作为汇报来源')
                return
            if not self.hooks.idle():
                self.publish(
                    request_id, ok=False, message='请等待当前任务、播报或校准结束后再选择聊天'
                )
                return
            if (
                action in MUTATIONS or action in ('use_sources', 'use_selected_report')
            ) and not self.hooks.capture_stopped():
                self.publish(request_id, ok=False, message='请先暂停语音，再修改工作汇报来源')
                return
            with self.lock:
                if self.busy:
                    self.publish(request_id, ok=False, message='正在读取聊天，请稍后再试')
                    return
                if action in REGISTRY_ACTIONS:
                    if request_id in self.source_requests:
                        self.publish(
                            request_id, ok=False, message='此来源请求已处理，请刷新查看，不自动重试'
                        )
                        return
                    self.source_requests.append(request_id)
                self.busy = True
                self.active_request = request_id
            report_revision = self.voice.begin() if action == 'report' else None
            generation, connection = self.hooks.generation(), self.hooks.connection()
        # RPC may take seconds. Never block the packet/capture reader on it.
        worker = threading.Thread(
            target=self.run,
            args=(request_id, action, data, generation, connection, report_revision),
            daemon=True,
            name='cc8-chat-catalog',
        )
        self.worker = worker
        try:
            worker.start()
        except BaseException:
            with self.lock:
                self.busy = False
                self.active_request = None
            raise

    def run(self, request_id, action, data, generation, connection, report_revision=None):
        report_owner = self.report_source() if action == 'report' else None

        def current():
            with self.hooks.lock:
                with self.lock:
                    return (
                        self.active_request == request_id
                        and generation == self.hooks.generation()
                        and connection is self.hooks.connection()
                        and (
                            action != 'report'
                            or self.report_source() is report_owner
                            and self.scope_current(report_owner)
                        )
                    )

        try:
            result, selected = {}, None
            if action == 'list':
                result = self.catalog.list(data.get('cursor'), is_current=current)
            elif action == 'select':
                selected = self.catalog.select(data.get('source_id'), is_current=current)
            elif action == 'report':
                result = {
                    'report': self.catalog.report(report_owner, data['period'], is_current=current)
                }
            elif action == 'sources':
                result = {'registry': self.sources.snapshot()}
            elif action == 'source_candidates':
                result = self.catalog.list(data.get('cursor'), is_current=current)
                result['registry'] = self.sources.snapshot()
            elif action == 'register_source':
                selected = self.catalog.registration(data['source_id'])
            with self.hooks.lock:
                with self.lock:
                    if self.active_request != request_id:
                        return
                if (
                    generation != self.hooks.generation()
                    or connection is not self.hooks.connection()
                    or not current()
                ):
                    return
                if action in ('select', 'clear'):
                    if not self.hooks.idle():
                        self.publish(
                            request_id, ok=False, message='当前任务已开始，聊天选择没有更改'
                        )
                        return
                    self.hooks.select(selected)
                    self.selection = selected
                    self.report_scope = None
                    self.voice.clear()
                    self.hooks.event('brain_provider', provider='dot')
                if action in MUTATIONS or action in ('use_sources', 'use_selected_report'):
                    if not self.hooks.idle() or not self.hooks.capture_stopped() or not current():
                        self.publish(
                            request_id, ok=False, message='当前任务已开始，登记来源没有更改'
                        )
                        return
                    if action in MUTATIONS:
                        change = {
                            'register_source': 'register',
                            'delete_source': 'delete',
                            'clear_sources': 'clear',
                        }[action]
                        result = {
                            'registry': self.sources.apply(
                                change,
                                data['revision'],
                                entry=selected if action == 'register_source' else None,
                                ident=data.get('registration_id'),
                                current=current,
                            )
                        }
                        if self.report_scope is not None and not self.scope_current(
                            self.report_scope
                        ):
                            self.report_scope = None
                            self.voice.clear()
                    elif action == 'use_sources':
                        state = self.sources.snapshot()
                        if state['revision'] != data['revision']:
                            raise ValueError('Registered report sources changed')
                        scope = ReportScope.from_state(state)
                        if (
                            self.report_scope is None
                            or self.report_scope.source_id != scope.source_id
                        ):
                            self.report_scope = scope
                            self.voice.clear()
                        result = {'registry': state}
                    else:
                        if self.report_scope is not None:
                            self.report_scope = None
                            self.voice.clear()
                        result = {'registry': self.sources.snapshot()}
                if action == 'report':
                    if not self.voice.valid_report(report_owner, result['report']):
                        raise DotError('Unconfirmed work report')
                    # A valid UI read may finish after ordinary conversation revoked
                    # numbered voice follow-up. Do not restore that older context.
                    result['report_snapshot_adopted'] = self.voice.remember(
                        report_owner, result['report'], report_revision
                    )
                    result['report_text'] = report_text(result['report'])
                self.publish(request_id, ok=True, action=action, **result)
        except Exception:
            with self.hooks.lock:
                # A rename may have committed even when durability/acknowledgment
                # failed. Re-read membership; never expose a removed source's old body.
                if self.report_scope is not None and self.report_context_source() is None:
                    self.report_scope = None
                    self.voice.clear()
                if (
                    generation == self.hooks.generation()
                    and connection is self.hooks.connection()
                    and self.active_request == request_id
                ):
                    # RPC errors and selected history never reach logs/UI.
                    try:
                        self.publish(
                            request_id,
                            ok=False,
                            message='无法确认聊天状态，请刷新查看；原聊天未追加消息',
                        )
                    except (OSError, EOFError, ValueError):
                        pass
        finally:
            with self.lock:
                self.busy = False
                self.active_request = None

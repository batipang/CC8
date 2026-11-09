"""Bound one explicit foreground chat-catalog operation; no automatic history reads."""

from dataclasses import dataclass
import re
import threading
import uuid
from typing import Callable, ContextManager
from dot_handoff import ChatCatalog
from dot_client import Cancelled, DotError
from report_voice import ReportVoice, ReportVoiceHooks


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


class ChatController:
    def __init__(self, hooks, catalog=None):
        self.hooks = hooks
        self.catalog = catalog or ChatCatalog()
        self.lock = threading.Lock()
        self.busy = False
        self.selection = None
        self.active_request = None
        self.worker = None
        self.voice = ReportVoice(
            ReportVoiceHooks(
                lock=hooks.lock,
                context=lambda: (self.selection, hooks.connection(), hooks.transport_revision()),
                read=self.read_voice_report,
            )
        )

    def read_voice_report(self, source, period, is_current):
        with self.hooks.lock:
            if not is_current() or self.selection is not source or self.hooks.connection() is None:
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
                        and self.selection is source
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
            'local_project_available': bool(self.selection and self.selection.cwd),
        }

    def publish(self, request_id='', **fields):
        self.hooks.event('dot_chat_state', request_id=request_id, **self.state(), **fields)

    def request(self, data):
        request_id = data.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
            raise ValueError('Invalid chat request ID')
        action = data.get('action')
        if action not in ('state', 'list', 'select', 'clear', 'report'):
            raise ValueError('Invalid chat action')
        if action == 'report' and data.get('period') not in ('half_hour', 'two_hours', 'today'):
            raise ValueError('Invalid report period')
        with self.hooks.lock:
            if not self.hooks.available() or self.hooks.connection() is None:
                self.publish(request_id, ok=False, message='Dot 聊天入口不可用')
                return
            if action == 'state':
                self.publish(request_id, ok=True)
                return
            if action == 'report' and self.selection is None:
                self.publish(request_id, ok=False, message='请先选择一个 Dot 聊天作为汇报来源')
                return
            if not self.hooks.idle():
                self.publish(
                    request_id, ok=False, message='请等待当前任务、播报或校准结束后再选择聊天'
                )
                return
            with self.lock:
                if self.busy:
                    self.publish(request_id, ok=False, message='正在读取聊天，请稍后再试')
                    return
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
        def current():
            with self.hooks.lock:
                with self.lock:
                    return (
                        self.active_request == request_id
                        and generation == self.hooks.generation()
                        and connection is self.hooks.connection()
                    )

        try:
            result, selected = {}, None
            if action == 'list':
                result = self.catalog.list(data.get('cursor'), is_current=current)
            elif action == 'select':
                selected = self.catalog.select(data.get('source_id'), is_current=current)
            elif action == 'report':
                result = {
                    'report': self.catalog.report(
                        self.selection, data['period'], is_current=current
                    )
                }
            with self.hooks.lock:
                with self.lock:
                    if self.active_request != request_id:
                        return
                if (
                    generation != self.hooks.generation()
                    or connection is not self.hooks.connection()
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
                    self.voice.clear()
                    self.hooks.event('brain_provider', provider='dot')
                self.publish(request_id, ok=True, action=action, **result)
                if action == 'report':
                    self.voice.remember(self.selection, result['report'], report_revision)
        except Exception:
            with self.hooks.lock:
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

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
from report_archive_protocol import (
    ACTIONS as ARCHIVE_ACTIONS,
    MUTATIONS as ARCHIVE_MUTATIONS,
    validate_request as validate_archive_request,
)
from report_archive import record as archive_record, historical_text
from thread_queue import queue_text
from report_checkpoint_protocol import (
    ACTIONS as PROGRESS_ACTIONS,
    MUTATIONS as PROGRESS_MUTATIONS,
    validate_request as validate_progress_request,
)
from report_checkpoint_controller import CheckpointController
from report_display import display_fields, paged_text
from report_display_protocol import CALENDAR_PERIODS, PERIODS, validate_request as validate_page
from report_display_protocol import validate_report_scope
from report_display_snapshot import DisplaySnapshot
from report_watch_controller import WatchController
from report_watch_protocol import (
    ACTIONS as WATCH_ACTIONS,
    MUTATIONS as WATCH_MUTATIONS,
    validate_request as validate_watch,
)


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
    def __init__(
        self, hooks, catalog=None, sources=None, archive=None, checkpoints=None, watch=None
    ):
        self.hooks = hooks
        self.catalog = catalog or ChatCatalog()
        self.lock = threading.Lock()
        self.busy = False
        self.selection = None
        self.active_request = None
        self.worker = None
        self.sources = sources
        self.archive = archive
        self.archive_offer = None
        self.report_scope = None
        self.source_requests = deque(maxlen=64)
        self.display_snapshot = DisplaySnapshot(
            lambda: (
                self.report_context_source(),
                hooks.generation(),
                hooks.connection(),
                hooks.transport_revision(),
                self.voice.revision,
            )
        )
        self.voice = ReportVoice(
            ReportVoiceHooks(
                lock=hooks.lock,
                context=lambda: (
                    self.report_context_source(),
                    hooks.connection(),
                    hooks.transport_revision(),
                ),
                read=self.read_voice_report,
                revoked=self.display_snapshot.clear,
            )
        )
        self.progress_ui = CheckpointController(self, checkpoints)
        self.watch_ui = WatchController(self, watch)

    def read_subscription(self, source, period, is_current):
        with self.hooks.lock:
            if not is_current() or not self.scope_current(source) or not self.hooks.idle():
                raise Cancelled('Report subscription owner changed')
            if not self.hooks.available():
                raise DotError('Report subscription unavailable')
            with self.lock:
                if self.busy:
                    raise DotError('Work report reader busy')
                self.busy = True
                ident = self.active_request = uuid.uuid4().hex

        def current():
            with self.hooks.lock:
                return (
                    self.active_request == ident
                    and is_current()
                    and self.scope_current(source)
                    and self.hooks.idle()
                )

        try:
            return self.catalog.report(source, period, is_current=current)
        finally:
            with self.lock:
                self.busy = False
                if self.active_request == ident:
                    self.active_request = None

    def report_source(self):
        return self.report_scope if self.report_scope is not None else self.selection

    def archive_offer_current(self, snapshot_id):
        offer = self.archive_offer
        return (
            offer is not None
            and offer[0] == snapshot_id
            and offer[1] == self.hooks.generation()
            and offer[2] is self.hooks.connection()
            and offer[3] == self.hooks.transport_revision()
            and offer[4] == self.voice.revision
        )

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

    def read_workflow_report(self, source, period, is_current):
        # The serial response queue owns this workflow. Its busy flag is
        # expected, so do not reuse subscription admission (which requires the
        # whole voice pipeline idle). Keep this catalog's own exclusive slot.
        with self.hooks.lock:
            if (
                is_current() is not True
                or self.hooks.connection() is None
                or not self.scope_current(source)
            ):
                raise Cancelled('Workflow report owner changed')
            if not self.hooks.available():
                raise DotError('Workflow report unavailable')
            with self.lock:
                if self.busy:
                    raise DotError('Workflow report reader busy')
                self.busy = True
                ident = self.active_request = uuid.uuid4().hex
            connection = self.hooks.connection()

        def current():
            with self.hooks.lock:
                with self.lock:
                    return (
                        self.active_request == ident
                        and is_current() is True
                        and connection is self.hooks.connection()
                        and self.scope_current(source)
                    )

        try:
            result = self.catalog.report(source, period, is_current=current)
            if not current():
                raise Cancelled('Workflow report owner changed before result admission')
            return result
        finally:
            with self.lock:
                self.busy = False
                if self.active_request == ident:
                    self.active_request = None

    def state(self):
        return {
            'mode': 'summary_handoff' if self.selection else 'independent',
            'source_title': self.selection.source_title if self.selection else '',
            'shared_chat': False,
            'history_persisted': False,
            'work_reports_supported': True,
            'work_report_calendar_supported': True,
            'work_report_selected_scope_supported': True,
            'registered_sources_supported': self.sources is not None,
            'report_watch_supported': self.watch_ui.scheduler is not None,
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
        if action not in (
            'state',
            'list',
            'select',
            'clear',
            'report',
            'report_page',
            *REGISTRY_ACTIONS,
            *ARCHIVE_ACTIONS,
            *PROGRESS_ACTIONS,
            *WATCH_ACTIONS,
        ):
            raise ValueError('Invalid chat action')
        if action in REGISTRY_ACTIONS:
            validate_request(data)
        if action in ARCHIVE_ACTIONS:
            validate_archive_request(data)
        if action in PROGRESS_ACTIONS:
            validate_progress_request(data)
        if action == 'report_page':
            validate_page(data)
        if action in WATCH_ACTIONS:
            validate_watch(data)
        if action == 'report':
            if data.get('period') not in (*PERIODS, *CALENDAR_PERIODS):
                raise ValueError('Invalid report period')
            validate_report_scope(data)
        selected_only = action == 'report' and data.get('report_scope') == 'selected_chat'
        report_source = (lambda: self.selection) if selected_only else self.report_source
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
            if action in ARCHIVE_ACTIONS and self.archive is None:
                self.publish(request_id, ok=False, message='当前后台不支持汇报快照')
                return
            if action in PROGRESS_ACTIONS and self.progress_ui.store is None:
                self.publish(request_id, ok=False, message='当前后台不支持保存补播进度')
                return
            if action in WATCH_ACTIONS and self.watch_ui.scheduler is None:
                self.publish(request_id, ok=False, message='当前后台不支持定时工作提示')
                return
            report_owner = report_source() if action == 'report' else None
            if action == 'report' and report_owner is None:
                self.publish(request_id, ok=False, message='请先选择一个 Dot 聊天作为汇报来源')
                return
            if not self.hooks.idle():
                self.publish(
                    request_id, ok=False, message='请等待当前任务、播报或校准结束后再选择聊天'
                )
                return
            if (
                action in MUTATIONS
                or action in ARCHIVE_MUTATIONS
                or action in PROGRESS_MUTATIONS
                or action in WATCH_MUTATIONS
                or action in ('use_sources', 'use_selected_report')
            ) and not self.hooks.capture_stopped():
                self.publish(request_id, ok=False, message='请先暂停语音，再修改工作汇报来源')
                return
            if action == 'save_report':
                try:
                    if not self.archive_offer_current(data['snapshot_id']):
                        raise ValueError('Report save offer owner changed')
                    self.voice.snapshot_for_save(data['snapshot_id'])
                except ValueError:
                    self.publish(
                        request_id,
                        ok=False,
                        message='这份汇报已过期或归属已变化，请重新读取后再保存',
                    )
                    return
            with self.lock:
                if self.busy:
                    self.publish(request_id, ok=False, message='正在读取聊天，请稍后再试')
                    return
                if (
                    action in REGISTRY_ACTIONS
                    or action in ARCHIVE_ACTIONS
                    or action in PROGRESS_ACTIONS
                    or action in WATCH_ACTIONS
                ):
                    if request_id in self.source_requests:
                        self.publish(
                            request_id, ok=False, message='此来源请求已处理，请刷新查看，不自动重试'
                        )
                        return
                    self.source_requests.append(request_id)
                self.busy = True
                self.active_request = request_id
            report_revision = (
                self.voice.begin()
                if report_owner is not None and report_owner is self.report_source()
                else None
            )
            generation, connection = self.hooks.generation(), self.hooks.connection()
        # RPC may take seconds. Never block the packet/capture reader on it.
        worker = threading.Thread(
            target=self.run,
            args=(
                request_id,
                action,
                data,
                generation,
                connection,
                report_source,
                report_owner,
                report_revision,
            ),
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

    def run(
        self,
        request_id,
        action,
        data,
        generation,
        connection,
        report_source,
        report_owner,
        report_revision,
    ):
        queue_owner = None

        def current():
            with self.hooks.lock:
                with self.lock:
                    if action == 'source_queue' and queue_owner is not None:
                        rows = self.sources.snapshot()['items']
                        if not any(r == queue_owner for r in rows):
                            return False
                    if action == 'save_report':
                        try:
                            if not self.archive_offer_current(data['snapshot_id']):
                                return False
                            self.voice.snapshot_for_save(data['snapshot_id'])
                        except ValueError:
                            return False
                    return (
                        self.active_request == request_id
                        and generation == self.hooks.generation()
                        and connection is self.hooks.connection()
                        and (
                            action != 'report'
                            or report_source() is report_owner
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
            elif action == 'source_queue':
                state = self.sources.snapshot()
                if state['revision'] != data['revision']:
                    raise ValueError('Registered source revision changed')
                queue_owner = next(
                    (r for r in state['items'] if r['id'] == data['registration_id']), None
                )
                if queue_owner is None:
                    raise ValueError('Registered source no longer exists')
                source = {
                    k: queue_owner[k] for k in ('provider', 'host', 'thread_id', 'title', 'project')
                }
                queue_report = self.catalog.queue(source, is_current=current)
                result = dict(source_queue=queue_report, queue_text=queue_text(queue_report))
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
                if action in ARCHIVE_ACTIONS:
                    # The same owner lock protects the last admission and commit;
                    # device payloads never supply report bodies or file paths.
                    if action in ARCHIVE_MUTATIONS:
                        if (
                            not self.hooks.idle()
                            or not self.hooks.capture_stopped()
                            or not current()
                        ):
                            raise ValueError('Saved report owner is busy')
                        item = None
                        if action == 'save_report':
                            source, report = self.voice.snapshot_for_save(data['snapshot_id'])
                            item = archive_record(source, report)
                        state = self.archive.apply(
                            {
                                'save_report': 'save',
                                'delete_report': 'delete',
                                'clear_reports': 'clear',
                            }[action],
                            data['revision'],
                            item=item,
                            ident=data.get('saved_id'),
                            current=current,
                        )
                    else:
                        state = self.archive.snapshot()
                    result = {'saved_reports': self.archive.listing(state)}
                    if action == 'saved_report':
                        item = next(
                            (r for r in state['items'] if r['id'] == data['saved_id']), None
                        )
                        if item is None:
                            raise ValueError('Saved report no longer exists')
                        if 'revision' in data and data['revision'] != state['revision']:
                            raise ValueError('Saved report revision changed')
                        result['saved_id'] = item['id']
                        result.update(
                            paged_text(
                                historical_text(item),
                                dict(
                                    request_id=request_id,
                                    ok=True,
                                    action=action,
                                    **self.state(),
                                    **result,
                                ),
                                page=data.get('page', 0),
                                field='saved_report_text',
                            )
                        )
                if action in PROGRESS_ACTIONS:
                    result = self.progress_ui.run(action, data, current)
                if action in WATCH_ACTIONS:
                    result = self.watch_ui.run(action, data, current)
                if action == 'report':
                    if not self.voice.valid_report(report_owner, result['report']):
                        raise DotError('Unconfirmed work report')
                    # A valid UI read may finish after ordinary conversation revoked
                    # numbered voice follow-up. Do not restore that older context.
                    result['report_snapshot_adopted'] = (
                        report_revision is not None
                        and self.voice.remember(report_owner, result['report'], report_revision)
                    )
                    result['report_text'] = report_text(result['report'], include_identity=True)
                    if report_revision is not None:
                        self.display_snapshot.remember(result['report'])
                    # A broken archive must not prevent an otherwise valid live
                    # read. It only removes the save affordance until repaired.
                    if result['report_snapshot_adopted'] and self.archive is not None:
                        try:
                            result['archive_revision'] = self.archive.snapshot()['revision']
                            self.archive_offer = (
                                result['report']['snapshot_id'],
                                generation,
                                connection,
                                self.hooks.transport_revision(),
                                self.voice.revision,
                            )
                        except (OSError, ValueError):
                            pass
                if action == 'report_page':
                    value = self.display_snapshot.read(data['snapshot_id'])
                    result = dict(
                        report=value, report_text=report_text(value, include_identity=True)
                    )
                    if self.archive is not None and self.archive_offer_current(data['snapshot_id']):
                        try:
                            self.voice.snapshot_for_save(data['snapshot_id'])
                            result['archive_revision'] = self.archive.snapshot()['revision']
                        except (OSError, ValueError):
                            pass
                if action in ('report', 'report_page'):
                    value, text = result.pop('report'), result.pop('report_text')
                    result.update(
                        display_fields(
                            value,
                            text,
                            dict(
                                request_id=request_id,
                                ok=True,
                                action=action,
                                **self.state(),
                                **result,
                            ),
                            page=data.get('page', 0),
                        )
                    )
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
                            message=(
                                '汇报快照状态未确认，请刷新查看，不自动重试。最多16份、总计256KiB；可先删除不需要的记录。原聊天和任务不变。'
                                if action in ARCHIVE_ACTIONS
                                else '补播进度未确认，请刷新查看，不自动重试。先暂停语音并选择原来源；最多8份、总计256KiB，原聊天和任务不变。'
                                if action in PROGRESS_ACTIONS
                                else '最近检查结果已过期或归属已变化，请等待下一次已启用的定时检查；查看不会重新查询聊天。'
                                if action in ('watch_result', 'watch_result_page')
                                else '定时工作提示状态未确认，请先暂停语音后重新查看，不自动重试。原聊天和任务不变。'
                                if action in WATCH_ACTIONS
                                else '无法确认聊天状态，请刷新查看；原聊天未追加消息'
                            ),
                        )
                    except (OSError, EOFError, ValueError):
                        pass
        finally:
            with self.lock:
                self.busy = False
                self.active_request = None

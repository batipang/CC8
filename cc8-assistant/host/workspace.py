"""Bounded live task receipts and owner-private, durable CC8 reminders.

Task transcripts are not persisted. Reminders are created only by an explicit
tool/UI request; the scheduler never opens a microphone or camera.
"""

from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timedelta
import json
import re
from pathlib import Path
import threading
import uuid
from zoneinfo import ZoneInfo
from typing import Callable, Literal, NotRequired, TypeAlias, TypedDict, cast
from result_provenance import Provenance, validate as validate_provenance
from reminder_policy import (
    ReminderPolicy,
    defaults as default_policy,
    quiet,
    validate as validate_policy,
)
from reminder_store import ReminderStore, ReminderCommitUncertain

ZONE = ZoneInfo('Asia/Shanghai')
REMINDER_STORE_VERSION = 3
EXECUTION_UNCONFIRMED_STEP = 'execution_unconfirmed'


class TaskStep(TypedDict):
    id: str
    title: str
    status: str


class TaskRecord(TypedDict):
    id: str
    generation: int
    title: str
    status: str
    stage: str
    steps: list[TaskStep]
    result: str
    started_at: str
    finished_at: NotRequired[str]
    provenance: NotRequired[Provenance]


class ReminderRecord(TypedDict):
    id: str
    title: str
    due: str
    repeat: str
    status: str
    revision: int
    channel: str
    retry_after: NotRequired[str]
    updated_at: NotRequired[str]
    delivery_attempt: NotRequired[DeliveryAttempt]


class DeliveryAttempt(TypedDict):
    id: str
    due: str
    started_at: str
    result: str
    channel: NotRequired[str]
    detail: NotRequired[str]
    finished_at: NotRequired[str]


class ReminderClaim(TypedDict):
    id: str
    revision: int
    attempt_id: str
    title: str
    channel: str


class Snapshot(TypedDict):
    revision: int
    tasks: list[TaskRecord]
    reminders: list[ReminderRecord]
    timezone: str
    server_time: str
    reminder_policy: ReminderPolicy
    reminder_history: list[ReminderRecord]
    reminder_channels_version: int


class ReminderResult(TypedDict):
    ok: Literal[True]
    reminder: ReminderRecord


class ListResult(Snapshot):
    ok: Literal[True]


class CancelledResult(TypedDict):
    ok: Literal[False]
    error: Literal['turn_cancelled']


class PolicyResult(TypedDict):
    ok: Literal[True]
    policy: ReminderPolicy


class ReminderHistory(TypedDict):
    ok: Literal[True]
    reminders: list[ReminderRecord]
    timezone: str


WorkspaceResult: TypeAlias = (
    ReminderResult | ListResult | CancelledResult | PolicyResult | ReminderHistory
)


def clock() -> datetime:
    return datetime.now(ZONE)


class Workspace:
    # 任务回执只存内存，明确创建的提醒才持久化到 owner-private 文件。
    # self.revision 刷新整个界面，提醒自身的 revision 绑定某次播报，二者用途不同。
    def __init__(self, path: str | Path | None = None, now: Callable[[], datetime] = clock) -> None:
        self.path = Path(path) if path else None
        self.store = ReminderStore(self.path) if self.path else None
        self.now = now
        self.lock = threading.RLock()
        self.tasks: list[TaskRecord] = []
        self.reminders: list[ReminderRecord] = []
        self.reminder_policy = default_policy()
        self.revision = 0
        data = self.store.load() if self.store else None
        if self.store is not None and self.store.expected is not None:
            # Do not silently replace a corrupt reminder store with an empty one.
            if (
                not isinstance(data, dict)
                or type(data.get('version')) is not int
                or data.get('version') not in (1, 2, 3)
                or not isinstance(data.get('reminders'), list)
                or len(data['reminders']) > 64
                or set(data)
                != (
                    {'version', 'reminders'}
                    if data.get('version') == 1
                    else {'version', 'reminders', 'policy'}
                )
            ):
                raise ValueError('Invalid reminder store')
            if data['version'] in (2, 3):
                self.reminder_policy = validate_policy(data.get('policy'))
            for raw_item in data['reminders']:
                if (
                    data['version'] < 3
                    and isinstance(raw_item, dict)
                    and (
                        'channel' in raw_item
                        or (
                            isinstance(raw_item.get('delivery_attempt'), dict)
                            and set(raw_item['delivery_attempt']) & {'channel', 'detail'}
                        )
                    )
                ):
                    raise ValueError('New reminder channel fields require schema3')
                self._validate(raw_item, existing=True)
                # The private JSON store is untyped until _validate establishes all
                # record fields; normalize the two legacy defaults at that boundary.
                item = cast(ReminderRecord, raw_item)
                item.setdefault('revision', 0)
                item.setdefault('repeat', 'none')
                item.setdefault('channel', 'cc8')
                if item['status'] == 'delivering':
                    # A previous process cannot prove physical playback completed.
                    # Keep the attempt, freeze automatic delivery, invalidate old ACKs.
                    item['status'] = 'unconfirmed'
                    item['revision'] += 1
                    item['delivery_attempt']['result'] = 'unconfirmed'
                    item['updated_at'] = self.now().isoformat(timespec='seconds')
                self.reminders.append(item)
            if len({r['id'] for r in self.reminders}) != len(self.reminders):
                raise ValueError('Duplicate reminder ID')
            if (
                sum(r['status'] in ('pending', 'delivering', 'unconfirmed') for r in self.reminders)
                > 32
            ):
                raise ValueError('Too many pending reminders')

    def _save(self) -> None:
        # 同目录临时文件写完后原子替换，避免半份 JSON；调用方负责失败时回滚内存。
        if self.store:
            try:
                self.store.save(
                    {'version': 3, 'reminders': self.reminders, 'policy': self.reminder_policy}
                )
            except ReminderCommitUncertain:
                self.revision += 1
                raise
        self.revision += 1

    @staticmethod
    def _date(value: object) -> datetime:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError('提醒时间需要带时区的 ISO 日期')
        try:
            result = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError('提醒时间格式不正确') from None
        if result.tzinfo is None:
            raise ValueError('提醒时间必须包含时区')
        try:
            return result.astimezone(ZONE)
        except OverflowError:
            raise ValueError('提醒时间超出可用范围') from None

    def _validate(self, item: object, existing: bool = False) -> datetime:
        if not isinstance(item, dict):
            raise ValueError('Invalid reminder')
        if set(item) - {
            'id',
            'title',
            'due',
            'repeat',
            'status',
            'revision',
            'retry_after',
            'updated_at',
            'delivery_attempt',
            'channel',
        }:
            raise ValueError('Unknown reminder fields')
        title = item.get('title')
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 120
            or any(ord(c) < 32 for c in title)
        ):
            raise ValueError('提醒内容应为 1 至 120 字')
        try:
            title.encode('utf-8')
        except UnicodeError:
            raise ValueError('提醒内容含无效字符') from None
        due = self._date(item.get('due'))
        if item.get('channel', 'cc8') not in ('cc8', 'desktop'):
            raise ValueError('提醒渠道需要选择 CC8 或 Mac 桌面')
        if item.get('repeat', 'none') not in ('none', 'daily', 'weekdays', 'weekly'):
            raise ValueError('不支持的重复方式')
        if not existing and item.get('repeat') == 'weekdays':
            while due.weekday() > 4:
                due += timedelta(days=1)
        if not existing and (due <= self.now() or due > self.now() + timedelta(days=366 * 5)):
            raise ValueError('请选择未来五年内的提醒时间')
        if existing and (
            not isinstance(item.get('id'), str)
            or len(item['id']) != 16
            or item.get('status')
            not in ('pending', 'delivering', 'unconfirmed', 'delivered', 'cancelled', 'missed')
        ):
            raise ValueError('Invalid reminder record')
        if type(item.get('revision', 0)) is not int or item.get('revision', 0) < 0:
            raise ValueError('Invalid reminder revision')
        if 'retry_after' in item:
            self._date(item['retry_after'])
        if 'updated_at' in item:
            self._date(item['updated_at'])
        attempt = item.get('delivery_attempt')
        if attempt is not None:
            if (
                not isinstance(attempt, dict)
                or set(attempt)
                - {'id', 'due', 'started_at', 'result', 'finished_at', 'channel', 'detail'}
                or not {'id', 'due', 'started_at', 'result'} <= set(attempt)
                or not isinstance(attempt['id'], str)
                or len(attempt['id']) != 32
                or any(c not in '0123456789abcdef' for c in attempt['id'])
                or attempt['result']
                not in ('pending_ack', 'played', 'notified', 'unconfirmed', 'missed')
            ):
                raise ValueError('Invalid reminder delivery attempt')
            if attempt.get('channel', 'cc8') not in ('cc8', 'desktop') or attempt.get(
                'detail', 'unconfirmed'
            ) not in (
                'unconfirmed',
                'permission_denied',
                'not_installed',
                'unsupported',
                'submitted_unconfirmed',
                'rejected',
            ):
                raise ValueError('Invalid reminder channel receipt')
            if attempt['result'] == 'notified' and attempt.get('channel') != 'desktop':
                raise ValueError('Desktop notification receipt missing channel')
            if attempt['result'] == 'played' and attempt.get('channel', 'cc8') != 'cc8':
                raise ValueError('Playback receipt cannot confirm desktop delivery')
            self._date(attempt['due'])
            self._date(attempt['started_at'])
            if 'finished_at' in attempt:
                self._date(attempt['finished_at'])
        if item.get('status') in ('delivering', 'unconfirmed') and (
            not isinstance(attempt, dict) or attempt['result'] not in ('pending_ack', 'unconfirmed')
        ):
            raise ValueError('Reminder attempt missing')
        return due

    def snapshot(self) -> Snapshot:
        with self.lock:
            reminders = sorted(
                (
                    r
                    for r in self.reminders
                    if r['status'] in ('pending', 'delivering', 'unconfirmed')
                ),
                key=lambda r: r['due'],
            )
            snapshot: Snapshot = {
                'revision': self.revision,
                'tasks': list(reversed(self.tasks)),
                'reminders': reminders,
                'timezone': 'Asia/Shanghai',
                'server_time': self.now().isoformat(timespec='seconds'),
                'reminder_policy': deepcopy(self.reminder_policy),
                'reminder_channels_version': 1,
                'reminder_history': cast(ReminderHistory, self.reminder({'action': 'history'}))[
                    'reminders'
                ],
            }
            data = deepcopy(snapshot)
            # The authenticated audio protocol has a 64 KiB packet ceiling.
            # Preserve current work/reminders, omit oldest receipts if needed.
            while (
                len(json.dumps(data, ensure_ascii=False).encode()) > 58000
                and data['reminder_history']
            ):
                data['reminder_history'].pop()
            while (
                len(json.dumps(data, ensure_ascii=False).encode()) > 58000
                and len(data['tasks']) > 1
            ):
                data['tasks'].pop()
            return data

    def task_start(self, generation: int, title: str) -> str:
        with self.lock:
            task: TaskRecord = {
                'id': uuid.uuid4().hex[:16],
                'generation': generation,
                'title': title[:80],
                'status': 'running',
                'stage': '正在连接助理',
                'steps': [],
                'result': '',
                'started_at': self.now().isoformat(timespec='seconds'),
            }
            self.tasks.append(task)
            self.tasks = self.tasks[-12:]
            self.revision += 1
            return task['id']

    def task_stage(self, ident: str | None, key: str, title: str, status: str) -> None:
        with self.lock:
            task = next((t for t in self.tasks if t['id'] == ident), None)
            if not task or task['status'] != 'running':
                return
            step = next((s for s in task['steps'] if s['id'] == key), None)
            if step is None:
                step = {'id': key, 'title': title[:40], 'status': status}
                task['steps'].append(step)
                task['steps'] = task['steps'][-5:]
            step['title'] = title[:40]
            step['status'] = status
            task['stage'] = title[:40]
            self.revision += 1

    def task_finish(
        self,
        ident: str | None,
        status: str,
        result: str = '',
        *,
        unconfirmed: bool = False,
        desktop_handoff: bool = False,
    ) -> None:
        # 只有 running 可进入终态，迟到的进度或停止朗读不能覆盖已有完成结果。
        with self.lock:
            task = next((t for t in self.tasks if t['id'] == ident), None)
            if not task or task['status'] != 'running':
                return
            task['status'] = status
            task['finished_at'] = self.now().isoformat(timespec='seconds')
            result = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', result)
            result = result.replace('\r\n', '\n').replace('\r', '\n')
            task['result'] = ''.join(c for c in result if ord(c) >= 32 or c in '\n\t')[:1600]
            task['stage'] = {
                'completed': '待桌面接续' if desktop_handoff else '请求已完成',
                'failed': '执行结果待核对' if unconfirmed else '请求未完成',
                'cancelled': (
                    '已停止等待，执行结果待核对；工具可能仍在运行。请先在电脑核对，勿重复交办。'
                    if unconfirmed
                    else '已停止等待；工具可能仍在运行，已执行的操作不会撤销。请先在电脑核对结果。'
                ),
            }.get(status, status)
            if status == 'completed' and desktop_handoff:
                task['steps'] = [
                    *task['steps'][-4:],
                    {'id': 'desktop_handoff', 'title': '接续指引已生成', 'status': 'completed'},
                ]
            if unconfirmed and status in ('failed', 'cancelled'):
                task['steps'] = [
                    *task['steps'][-4:],
                    {'id': EXECUTION_UNCONFIRMED_STEP, 'title': '执行结果待核对', 'status': status},
                ]
            for step in task['steps']:
                if step['status'] == 'running':
                    step['status'] = (
                        'cancelled'
                        if status == 'cancelled'
                        else 'failed'
                        if status == 'failed'
                        else 'completed'
                    )
            self.revision += 1

    def task_provenance(self, ident: str | None, value: object) -> None:
        with self.lock:
            task = next((t for t in self.tasks if t['id'] == ident), None)
            if task is not None and task['status'] == 'running':
                task['provenance'] = deepcopy(validate_provenance(value))
                self.revision += 1

    def cancel_generation(self, generation: int, *, unconfirmed: bool = False) -> bool:
        with self.lock:
            before = self.revision
            for task in self.tasks:
                if task['generation'] == generation:
                    self.task_finish(task['id'], 'cancelled', unconfirmed=unconfirmed)
            return self.revision != before

    def reminder(
        self, args: object, is_current: Callable[[], bool] = lambda: True
    ) -> WorkspaceResult:
        if not isinstance(args, dict) or set(args) - {
            'action',
            'id',
            'title',
            'due',
            'repeat',
            'policy',
            'channel',
        }:
            raise ValueError('提醒参数不正确')
        action = args.get('action')
        with self.lock:
            if not is_current():
                return {'ok': False, 'error': 'turn_cancelled'}
            if action == 'list':
                return {'ok': True, **self.snapshot()}
            if action in ('policy', 'set_policy'):
                if set(args) != ({'action'} if action == 'policy' else {'action', 'policy'}):
                    raise ValueError('提醒策略参数不正确')
                if action == 'set_policy':
                    candidate_policy = validate_policy(args['policy'])
                    if (
                        candidate_policy['revision'] != self.reminder_policy['revision']
                        or candidate_policy['revision'] >= 2**31 - 2
                    ):
                        raise ValueError('提醒策略已更新，请重新读取后修改')
                    candidate_policy['revision'] += 1
                    before_policy = self.reminder_policy
                    self.reminder_policy = candidate_policy
                    try:
                        self._save()
                    except ReminderCommitUncertain:
                        raise
                    except Exception:
                        self.reminder_policy = before_policy
                        raise
                return {'ok': True, 'policy': deepcopy(self.reminder_policy)}
            if action == 'history':
                if set(args) != {'action'}:
                    raise ValueError('提醒历史参数不正确')
                records = [
                    r for r in self.reminders if r['status'] != 'pending' or 'delivery_attempt' in r
                ]

                def history_time(item: ReminderRecord) -> tuple[bool, datetime]:
                    stamp = item.get('updated_at')
                    if stamp is None and 'delivery_attempt' in item:
                        attempt = item['delivery_attempt']
                        stamp = attempt.get('finished_at', attempt['started_at'])
                    return stamp is not None, self._date(stamp) if stamp is not None else datetime(
                        1970, 1, 1, tzinfo=ZONE
                    )

                return {
                    'ok': True,
                    'reminders': deepcopy(sorted(records, key=history_time, reverse=True)[:12]),
                    'timezone': 'Asia/Shanghai',
                }
            if 'policy' in args:
                raise ValueError('提醒策略需单独明确设置')
            before = deepcopy(self.reminders)
            if action == 'create':
                if (
                    len(
                        [
                            r
                            for r in self.reminders
                            if r['status'] in ('pending', 'delivering', 'unconfirmed')
                        ]
                    )
                    >= 32
                ):
                    raise ValueError('最多保留 32 条待提醒事项')
                raw_item: dict[str, object] = {
                    'id': uuid.uuid4().hex[:16],
                    'title': args.get('title'),
                    'due': args.get('due'),
                    'repeat': args.get('repeat', 'none'),
                    'channel': args.get('channel', 'cc8'),
                    'status': 'pending',
                    'revision': 1,
                    'updated_at': self.now().isoformat(timespec='seconds'),
                }
                raw_item['due'] = self._validate(raw_item).isoformat(timespec='seconds')
                # id/status/revision are generated here; user fields just passed validation.
                item = cast(ReminderRecord, raw_item)
                self.reminders.append(item)
                if len(self.reminders) > 64:
                    self.reminders = [
                        r
                        for r in self.reminders
                        if r['status'] in ('pending', 'delivering', 'unconfirmed')
                    ] + self.reminders[-16:]
                # Stable unique IDs, even after pruning completed records.
                self.reminders = list({r['id']: r for r in self.reminders}.values())
            elif action in ('update', 'cancel'):
                current = next(
                    (
                        r
                        for r in self.reminders
                        if r['id'] == args.get('id')
                        and r['status'] in ('pending', 'delivering', 'unconfirmed')
                    ),
                    None,
                )
                if current is None:
                    raise ValueError('这条提醒不存在或已经结束')
                item = current
                if action == 'cancel':
                    if item['status'] in ('delivering', 'unconfirmed'):
                        item['delivery_attempt']['result'] = 'unconfirmed'
                        item['delivery_attempt']['finished_at'] = self.now().isoformat(
                            timespec='seconds'
                        )
                    item['status'] = 'cancelled'
                else:
                    candidate: dict[str, object] = {
                        **item,
                        **{
                            k: v
                            for k, v in args.items()
                            if k in ('title', 'due', 'repeat', 'channel')
                        },
                    }
                    candidate['due'] = self._validate(candidate).isoformat(timespec='seconds')
                    item.update(cast(ReminderRecord, candidate))
                    if item['status'] in ('delivering', 'unconfirmed'):
                        attempt = item['delivery_attempt']
                        attempt['result'] = 'unconfirmed'
                        attempt['finished_at'] = self.now().isoformat(timespec='seconds')
                    item['status'] = 'pending'
                    item.pop('retry_after', None)
                item['revision'] = item.get('revision', 0) + 1
                item['updated_at'] = self.now().isoformat(timespec='seconds')
            else:
                raise ValueError('不支持的提醒操作')
            try:
                self._save()
            except ReminderCommitUncertain:
                raise
            except Exception:
                self.reminders = before
                raise
            return {'ok': True, 'reminder': deepcopy(item)}

    def _advance_repeat(self, item: ReminderRecord) -> None:
        due = self._date(item['due'])
        step = 7 if item['repeat'] == 'weekly' else 1
        try:
            # A completed occurrence always advances, even after a clock rollback.
            days = max(0, (self.now() - due).days)
            due += timedelta(days=(days // step + 1) * step)
            while item['repeat'] == 'weekdays' and due.weekday() > 4:
                due += timedelta(days=1)
        except OverflowError:
            raise ValueError('下一次重复提醒超出可用日期') from None
        item['due'] = due.isoformat(timespec='seconds')
        item['status'] = 'pending'
        item.pop('retry_after', None)

    def expire_reminders(self) -> bool:
        with self.lock:
            if self.reminder_policy['late_policy'] != 'skip':
                return False
            now = self.now()
            before = self.reminders
            working = deepcopy(self.reminders)
            changed = False
            for item in working:
                due = self._date(item['due'])
                if item['status'] != 'pending' or now - due <= timedelta(
                    minutes=self.reminder_policy['max_late_minutes']
                ):
                    continue
                item['delivery_attempt'] = {
                    'id': uuid.uuid4().hex,
                    'due': item['due'],
                    'started_at': now.isoformat(timespec='seconds'),
                    'finished_at': now.isoformat(timespec='seconds'),
                    'result': 'missed',
                }
                if item['repeat'] == 'none':
                    item['status'] = 'missed'
                else:
                    self._advance_repeat(item)
                item['revision'] += 1
                item['updated_at'] = self.now().isoformat(timespec='seconds')
                changed = True
            if changed:
                self.reminders = working
                try:
                    self._save()
                except ReminderCommitUncertain:
                    raise
                except Exception:
                    self.reminders = before
                    raise
            return changed

    def claim_reminders(self, limit: int = 3) -> list[ReminderClaim]:
        """Durably mark exact occurrences before any TTS or output request."""
        if type(limit) is not int or not 1 <= limit <= 3:
            raise ValueError('Invalid reminder delivery limit')
        with self.lock:
            self.expire_reminders()
            if quiet(self.reminder_policy, self.now()):
                return []
            before = self.reminders
            working = deepcopy(self.reminders)
            claims: list[ReminderClaim] = []
            channel = None
            for item in sorted(working, key=lambda r: r['due']):
                if item['status'] != 'pending' or self._date(item['due']) > self.now():
                    continue
                if item.get('retry_after') and self._date(item['retry_after']) > self.now():
                    continue
                if channel is None:
                    channel = item.get('channel', 'cc8')
                if item.get('channel', 'cc8') != channel:
                    continue
                ident = uuid.uuid4().hex
                item['status'] = 'delivering'
                item['revision'] += 1
                item['updated_at'] = self.now().isoformat(timespec='seconds')
                item['delivery_attempt'] = {
                    'id': ident,
                    'due': item['due'],
                    'started_at': self.now().isoformat(timespec='seconds'),
                    'result': 'pending_ack',
                    'channel': channel,
                }
                claims.append(
                    {
                        'id': item['id'],
                        'revision': item['revision'],
                        'attempt_id': ident,
                        'title': item['title'],
                        'channel': channel,
                    }
                )
                if len(claims) == limit:
                    break
            if claims:
                self.reminders = working
                try:
                    self._save()
                except ReminderCommitUncertain:
                    # No announcement was admitted. Keep uncertain durable
                    # claims frozen in RAM as well as after a process restart.
                    for item in self.reminders:
                        if any(c['id'] == item['id'] for c in claims):
                            item['status'] = 'unconfirmed'
                            item['delivery_attempt']['result'] = 'unconfirmed'
                    raise
                except Exception:
                    self.reminders = before
                    raise
            return claims

    def finish_reminders(
        self,
        claims: list[ReminderClaim],
        completed: bool,
        *,
        channel: str = 'cc8',
        detail: str = 'unconfirmed',
    ) -> bool:
        """Bind each terminal receipt to the claimed channel and exact occurrence."""
        if channel not in ('cc8', 'desktop') or detail not in (
            'unconfirmed',
            'permission_denied',
            'not_installed',
            'unsupported',
            'submitted_unconfirmed',
            'rejected',
        ):
            raise ValueError('Invalid reminder channel receipt')
        with self.lock:
            before = self.reminders
            working = deepcopy(self.reminders)
            changed = False
            for claim in claims:
                item = next((r for r in working if r['id'] == claim['id']), None)
                if (
                    item is None
                    or item['status'] != 'delivering'
                    or item['revision'] != claim['revision']
                    or item['delivery_attempt']['id'] != claim['attempt_id']
                    or item.get('channel', 'cc8') != channel
                    or claim.get('channel', 'cc8') != channel
                ):
                    continue
                item['delivery_attempt']['result'] = (
                    ('notified' if channel == 'desktop' else 'played')
                    if completed
                    else 'unconfirmed'
                )
                item['delivery_attempt']['channel'] = channel
                if not completed:
                    item['delivery_attempt']['detail'] = detail
                item['delivery_attempt']['finished_at'] = self.now().isoformat(timespec='seconds')
                if not completed:
                    item['status'] = 'unconfirmed'
                elif item['repeat'] == 'none':
                    item['status'] = 'delivered'
                else:
                    self._advance_repeat(item)
                item['revision'] += 1
                item['updated_at'] = self.now().isoformat(timespec='seconds')
                changed = True
            if changed:
                self.reminders = working
                try:
                    self._save()
                except ReminderCommitUncertain:
                    raise
                except Exception:
                    self.reminders = before
                    raise
            return changed

    def reminder_claims_current(self, claims: list[ReminderClaim]) -> bool:
        with self.lock:
            if quiet(self.reminder_policy, self.now()):
                return False
            return bool(claims) and all(
                any(
                    r['id'] == c['id']
                    and r['revision'] == c['revision']
                    and r['status'] == 'delivering'
                    and r['delivery_attempt']['id'] == c['attempt_id']
                    for r in self.reminders
                )
                for c in claims
            )

    def due(self) -> ReminderRecord | None:
        with self.lock:
            now = self.now()
            return next(
                (
                    deepcopy(r)
                    for r in sorted(self.reminders, key=lambda x: x['due'])
                    if r['status'] == 'pending'
                    and self._date(r['due']) <= now
                    and (not r.get('retry_after') or self._date(r['retry_after']) <= now)
                ),
                None,
            )

    def delivered(self, ident: str, success: bool, *, revision: object) -> bool:
        # Compatibility for legacy in-process receipt callers. Production
        # scheduling uses persisted claims plus finish_reminders tokens.
        # 旧回执不能结束编辑/稍后提醒后的新版本，也不能推进下一次重复提醒。
        with self.lock:
            item = next(
                (
                    r
                    for r in self.reminders
                    if r['id'] == ident
                    and r['status'] == 'pending'
                    and r.get('channel', 'cc8') == 'cc8'
                ),
                None,
            )
            # Bind playback receipts to the exact scheduled occurrence. Editing,
            # snoozing or advancing a repeat invalidates all older receipts.
            if item is None or type(revision) is not int or item.get('revision', 0) != revision:
                return False
            before = deepcopy(self.reminders)
            if not success:
                item['delivery_attempt'] = {
                    'id': uuid.uuid4().hex,
                    'due': item['due'],
                    'started_at': self.now().isoformat(timespec='seconds'),
                    'finished_at': self.now().isoformat(timespec='seconds'),
                    'result': 'unconfirmed',
                }
                item['status'] = 'unconfirmed'
            elif item['repeat'] == 'none':
                item['status'] = 'delivered'
            else:
                self._advance_repeat(item)
            item['revision'] = revision + 1
            item['updated_at'] = self.now().isoformat(timespec='seconds')
            try:
                self._save()
            except ReminderCommitUncertain:
                raise
            except Exception:
                self.reminders = before
                raise
            return True

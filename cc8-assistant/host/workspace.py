"""Bounded live task receipts and owner-private, durable CC8 reminders.

Task transcripts are not persisted. Reminders are created only by an explicit
tool/UI request; the scheduler never opens a microphone or camera.
"""

from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import threading
import uuid
from zoneinfo import ZoneInfo
from typing import Callable, Literal, NotRequired, TypeAlias, TypedDict, cast

ZONE = ZoneInfo('Asia/Shanghai')


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


class ReminderRecord(TypedDict):
    id: str
    title: str
    due: str
    repeat: str
    status: str
    revision: int
    retry_after: NotRequired[str]


class Snapshot(TypedDict):
    revision: int
    tasks: list[TaskRecord]
    reminders: list[ReminderRecord]
    timezone: str
    server_time: str


class ReminderResult(TypedDict):
    ok: Literal[True]
    reminder: ReminderRecord


class ListResult(Snapshot):
    ok: Literal[True]


class CancelledResult(TypedDict):
    ok: Literal[False]
    error: Literal['turn_cancelled']


WorkspaceResult: TypeAlias = ReminderResult | ListResult | CancelledResult


def clock() -> datetime:
    return datetime.now(ZONE)


class Workspace:
    # 任务回执只存内存，明确创建的提醒才持久化到 owner-private 文件。
    # self.revision 刷新整个界面，提醒自身的 revision 绑定某次播报，二者用途不同。
    def __init__(self, path: str | Path | None = None, now: Callable[[], datetime] = clock) -> None:
        self.path = Path(path) if path else None
        self.now = now
        self.lock = threading.RLock()
        self.tasks: list[TaskRecord] = []
        self.reminders: list[ReminderRecord] = []
        self.revision = 0
        if self.path and self.path.exists():
            # Do not silently replace a corrupt reminder store with an empty one.
            data: object = json.loads(self.path.read_text())
            if (
                not isinstance(data, dict)
                or data.get('version') != 1
                or not isinstance(data.get('reminders'), list)
                or len(data['reminders']) > 64
            ):
                raise ValueError('Invalid reminder store')
            for raw_item in data['reminders']:
                self._validate(raw_item, existing=True)
                # The private JSON store is untyped until _validate establishes all
                # record fields; normalize the two legacy defaults at that boundary.
                item = cast(ReminderRecord, raw_item)
                item.setdefault('revision', 0)
                item.setdefault('repeat', 'none')
                self.reminders.append(item)
            if len({r['id'] for r in self.reminders}) != len(self.reminders):
                raise ValueError('Duplicate reminder ID')
            if sum(r['status'] == 'pending' for r in self.reminders) > 32:
                raise ValueError('Too many pending reminders')

    def _save(self) -> None:
        # 同目录临时文件写完后原子替换，避免半份 JSON；调用方负责失败时回滚内存。
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            temporary = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                with temporary.open('x', encoding='utf-8') as f:
                    os.chmod(temporary, 0o600)
                    json.dump({'version': 1, 'reminders': self.reminders}, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
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
        title = item.get('title')
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 120
            or any(ord(c) < 32 for c in title)
        ):
            raise ValueError('提醒内容应为 1 至 120 字')
        due = self._date(item.get('due'))
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
            or item.get('status') not in ('pending', 'delivered', 'cancelled')
        ):
            raise ValueError('Invalid reminder record')
        if type(item.get('revision', 0)) is not int or item.get('revision', 0) < 0:
            raise ValueError('Invalid reminder revision')
        if 'retry_after' in item:
            self._date(item['retry_after'])
        return due

    def snapshot(self) -> Snapshot:
        with self.lock:
            reminders = sorted(
                (r for r in self.reminders if r['status'] == 'pending'), key=lambda r: r['due']
            )
            snapshot: Snapshot = {
                'revision': self.revision,
                'tasks': list(reversed(self.tasks)),
                'reminders': reminders,
                'timezone': 'Asia/Shanghai',
                'server_time': self.now().isoformat(timespec='seconds'),
            }
            data = deepcopy(snapshot)
            # The authenticated audio protocol has a 64 KiB packet ceiling.
            # Preserve current work/reminders, omit oldest receipts if needed.
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
        self, ident: str | None, status: str, result: str = '', *, unconfirmed: bool = False
    ) -> None:
        # 只有 running 可进入终态，迟到的进度或停止朗读不能覆盖已有完成结果。
        with self.lock:
            task = next((t for t in self.tasks if t['id'] == ident), None)
            if not task or task['status'] != 'running':
                return
            task['status'] = status
            task['result'] = result[:1600]
            task['stage'] = {
                'completed': '请求已完成',
                'failed': '执行结果待核对' if unconfirmed else '请求未完成',
                'cancelled': (
                    '连接已切换，执行结果待核对。请确认后再试。'
                    if unconfirmed
                    else '已停止等待；已执行的操作不会撤销'
                ),
            }.get(status, status)
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
        if not isinstance(args, dict) or set(args) - {'action', 'id', 'title', 'due', 'repeat'}:
            raise ValueError('提醒参数不正确')
        action = args.get('action')
        with self.lock:
            if not is_current():
                return {'ok': False, 'error': 'turn_cancelled'}
            if action == 'list':
                return {'ok': True, **self.snapshot()}
            before = deepcopy(self.reminders)
            if action == 'create':
                if len([r for r in self.reminders if r['status'] == 'pending']) >= 32:
                    raise ValueError('最多保留 32 条待提醒事项')
                raw_item: dict[str, object] = {
                    'id': uuid.uuid4().hex[:16],
                    'title': args.get('title'),
                    'due': args.get('due'),
                    'repeat': args.get('repeat', 'none'),
                    'status': 'pending',
                    'revision': 1,
                }
                raw_item['due'] = self._validate(raw_item).isoformat(timespec='seconds')
                # id/status/revision are generated here; user fields just passed validation.
                item = cast(ReminderRecord, raw_item)
                self.reminders.append(item)
                if len(self.reminders) > 64:
                    self.reminders = [
                        r for r in self.reminders if r['status'] == 'pending'
                    ] + self.reminders[-16:]
                # Stable unique IDs, even after pruning completed records.
                self.reminders = list({r['id']: r for r in self.reminders}.values())
            elif action in ('update', 'cancel'):
                current = next(
                    (
                        r
                        for r in self.reminders
                        if r['id'] == args.get('id') and r['status'] == 'pending'
                    ),
                    None,
                )
                if current is None:
                    raise ValueError('这条提醒不存在或已经结束')
                item = current
                if action == 'cancel':
                    item['status'] = 'cancelled'
                else:
                    candidate: dict[str, object] = {
                        **item,
                        **{k: v for k, v in args.items() if k in ('title', 'due', 'repeat')},
                    }
                    candidate['due'] = self._validate(candidate).isoformat(timespec='seconds')
                    item.update(cast(ReminderRecord, candidate))
                    item.pop('retry_after', None)
                item['revision'] = item.get('revision', 0) + 1
            else:
                raise ValueError('不支持的提醒操作')
            try:
                self._save()
            except Exception:
                self.reminders = before
                raise
            return {'ok': True, 'reminder': deepcopy(item)}

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
        # 旧回执不能结束编辑/稍后提醒后的新版本，也不能推进下一次重复提醒。
        with self.lock:
            item = next(
                (r for r in self.reminders if r['id'] == ident and r['status'] == 'pending'), None
            )
            # Bind playback receipts to the exact scheduled occurrence. Editing,
            # snoozing or advancing a repeat invalidates all older receipts.
            if item is None or type(revision) is not int or item.get('revision', 0) != revision:
                return False
            before = deepcopy(self.reminders)
            if not success:
                item['retry_after'] = (self.now() + timedelta(minutes=5)).isoformat(
                    timespec='seconds'
                )
            elif item['repeat'] == 'none':
                item['status'] = 'delivered'
            else:
                due = self._date(item['due'])
                step = 7 if item['repeat'] == 'weekly' else 1
                while due <= self.now() or (item['repeat'] == 'weekdays' and due.weekday() > 4):
                    due += timedelta(days=step)
                item['due'] = due.isoformat(timespec='seconds')
                item.pop('retry_after', None)
            item['revision'] = revision + 1
            try:
                self._save()
            except Exception:
                self.reminders = before
                raise
            return True

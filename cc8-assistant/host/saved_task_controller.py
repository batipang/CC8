"""One bounded archive worker; selected result writes do not replay tasks or audio."""

from copy import deepcopy
from dataclasses import dataclass
import re
import threading
from typing import Callable, ContextManager
from saved_tasks import ident, SavedTaskCapacityError


@dataclass(frozen=True)
class SavedTaskHooks:
    lock: ContextManager[object]
    connection: Callable[[], object]
    tasks: Callable[[], list]
    event: Callable[..., None]


class SavedTaskController:
    def __init__(self, hooks, store):
        self.hooks = hooks
        self.store = store
        self.lock = threading.Lock()
        self.busy = False
        self.worker = None

    def reply(self, request_id, **fields):
        self.hooks.event('saved_task_result', request_id=request_id, **fields)

    def finish_slot(self):
        with self.lock:
            if self.worker is threading.current_thread():
                self.busy = False

    def request(self, data):
        request_id, action = data.get('request_id'), data.get('action')
        if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
            raise ValueError('Invalid saved result request ID')
        if action not in ('list', 'get', 'save', 'delete'):
            raise ValueError('Invalid saved result action')
        task_id = ident(data.get('id')) if action != 'list' else None
        with self.hooks.lock:
            connection = self.hooks.connection()
            if connection is None:
                return
            task = None
            if action == 'save':
                task = next((deepcopy(t) for t in self.hooks.tasks() if t['id'] == task_id), None)
                if task is None or task['status'] not in ('completed', 'failed', 'cancelled'):
                    self.reply(
                        request_id,
                        ok=False,
                        message='仅支持保存当前任务页中已结束的任务；请刷新后再试',
                    )
                    return
            with self.lock:
                if self.busy:
                    self.reply(request_id, ok=False, message='正在处理保存记录，请稍后再试')
                    return
                self.busy = True
        self.worker = threading.Thread(
            target=self.run,
            args=(request_id, action, task_id, task, connection),
            daemon=True,
            name='cc8-saved-results',
        )
        try:
            self.worker.start()
        except BaseException:
            with self.lock:
                self.busy = False
            raise

    def run(self, request_id, action, task_id, task, connection):
        try:
            fields = {'ok': True, 'action': action}
            if action == 'save':
                item = self.store.save(task)
                fields['id'] = item['id']
            elif action == 'delete':
                fields['deleted'] = self.store.delete(task_id)
            elif action == 'list':
                fields['items'] = [
                    {key: item[key] for key in ('id', 'title', 'status', 'saved_at')}
                    for item in reversed(self.store.snapshot())
                ]
            else:
                item = next((t for t in self.store.snapshot() if t['id'] == task_id), None)
                if item is None:
                    raise ValueError('Saved task no longer exists')
                fields['item'] = item
            with self.hooks.lock:
                self.finish_slot()
                if connection is self.hooks.connection():
                    self.reply(request_id, **fields)
        except Exception as error:
            with self.hooks.lock:
                self.finish_slot()
                if connection is self.hooks.connection():
                    try:
                        self.reply(
                            request_id,
                            ok=False,
                            message='最多保存 32 条结果，请先删除不再需要的保存记录。原记录不变。'
                            if isinstance(error, SavedTaskCapacityError)
                            else '保存记录状态未确认，请刷新查看；异常文件保留，原任务和已执行操作不会撤销',
                        )
                    except (OSError, EOFError, ValueError):
                        pass
        finally:
            # A delete ACK can immediately trigger a new list request. Never
            # clear that successor's slot from this previous worker's finally.
            self.finish_slot()

"""One bounded archive worker; selected result writes do not replay tasks or audio."""

from copy import deepcopy
from collections import deque
from dataclasses import dataclass
import re
import threading
from typing import Callable, ContextManager
from saved_tasks import (
    desktop_handoff_pending,
    desktop_handoff_source,
    execution_unconfirmed,
    ident,
    SavedTaskCapacityError,
)
from result_files import reveal_file
from desktop_handoff import can_open_chat, confirm_open, launch_chat


REVEAL_ACTIONS = ('reveal_live', 'reveal_saved')
OPEN_HANDOFF = 'open_handoff'
OPEN_LIVE_HANDOFF = 'open_live_handoff'
OPEN_HANDOFF_ACTIONS = (OPEN_HANDOFF, OPEN_LIVE_HANDOFF)
DESKTOP_ACTIONS = (*REVEAL_ACTIONS, *OPEN_HANDOFF_ACTIONS)


def handoff_can_open(item):
    source = desktop_handoff_source(item)
    return source is not None and can_open_chat(source['thread_id'])


@dataclass(frozen=True)
class SavedTaskHooks:
    lock: ContextManager[object]
    connection: Callable[[], object]
    tasks: Callable[[], list]
    event: Callable[..., None]
    reveal: Callable = reveal_file
    launch_chat: Callable = launch_chat
    owner_scope: Callable = lambda: None


class SavedTaskController:
    def __init__(self, hooks, store):
        self.hooks = hooks
        self.store = store
        self.lock = threading.Lock()
        self.busy = False
        self.worker = None
        self.desktop_requests = deque(maxlen=64)

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
        if action not in ('list', 'get', 'save', 'delete', *DESKTOP_ACTIONS):
            raise ValueError('Invalid saved result action')
        task_id = ident(data.get('id')) if action != 'list' else None
        file_index = data.get('file_index')
        revealing = action in REVEAL_ACTIONS
        if revealing and (type(file_index) is not int or not 0 <= file_index < 8):
            raise ValueError('Invalid result file index')
        with self.hooks.lock:
            connection = self.hooks.connection()
            if connection is None:
                return
            task = None
            if action in ('save', 'reveal_live'):
                task = next((deepcopy(t) for t in self.hooks.tasks() if t['id'] == task_id), None)
                if task is None or task['status'] not in ('completed', 'failed', 'cancelled'):
                    self.reply(
                        request_id,
                        ok=False,
                        message='仅支持保存当前任务页中已结束的任务；请刷新后再试'
                        if action == 'save'
                        else '请重新打开当前任务页中的已结束结果，再选择文件定位。',
                    )
                    return
            with self.lock:
                if self.busy:
                    self.reply(request_id, ok=False, message='正在处理保存记录，请稍后再试')
                    return
                if action in DESKTOP_ACTIONS:
                    if request_id in self.desktop_requests:
                        self.reply(
                            request_id,
                            ok=False,
                            message='这个桌面请求已经处理或结果未确认，请在 Mac 查看。',
                        )
                        return
                    self.desktop_requests.append(request_id)
                owner = self.hooks.owner_scope()
                self.busy = True
        self.worker = threading.Thread(
            target=self.run,
            args=(request_id, action, task_id, task, connection, file_index, owner),
            daemon=True,
            name='cc8-saved-results',
        )
        try:
            self.worker.start()
        except BaseException:
            with self.lock:
                self.busy = False
            raise

    def run(self, request_id, action, task_id, task, connection, file_index=None, owner=None):
        def current():
            return connection is self.hooks.connection() and owner == self.hooks.owner_scope()

        try:
            fields = {'ok': True, 'action': action}
            if action in DESKTOP_ACTIONS:
                if action in ('reveal_saved', OPEN_HANDOFF):
                    task = next((t for t in self.store.snapshot() if t['id'] == task_id), None)
                with self.hooks.lock:
                    if not current():
                        return
                    if action == OPEN_LIVE_HANDOFF:
                        task = next((t for t in self.hooks.tasks() if t['id'] == task_id), None)
                    if task is None:
                        raise ValueError('Result no longer exists')
                    if action in OPEN_HANDOFF_ACTIONS:
                        source = desktop_handoff_source(task)
                        if source is None:
                            raise ValueError('No matching desktop handoff')
                        # Commit the local launch against the authenticated owner.
                        # Waiting for LaunchServices must stay off the route lock.
                        process = self.hooks.launch_chat(source['thread_id'])
                if action in OPEN_HANDOFF_ACTIONS:
                    confirm_open(process)
                    fields['open_requested'] = True
                else:
                    self.hooks.reveal(task, file_index)
                    fields['revealed'] = True
            elif action == 'save':
                item = self.store.save(task)
                fields['id'] = item['id']
            elif action == 'delete':
                fields['deleted'] = self.store.delete(task_id)
            elif action == 'list':
                fields['items'] = [
                    {
                        **{key: item[key] for key in ('id', 'title', 'status', 'saved_at')},
                        'desktop_handoff_pending': desktop_handoff_pending(item),
                        'execution_unconfirmed': execution_unconfirmed(item),
                    }
                    for item in reversed(self.store.snapshot())
                ]
            else:
                item = next((t for t in self.store.snapshot() if t['id'] == task_id), None)
                if item is None:
                    raise ValueError('Saved task no longer exists')
                fields['item'] = {
                    **item,
                    'desktop_handoff_pending': desktop_handoff_pending(item),
                    'desktop_handoff_can_open': handoff_can_open(item),
                    'execution_unconfirmed': execution_unconfirmed(item),
                }
            with self.hooks.lock:
                self.finish_slot()
                if current():
                    self.reply(request_id, **fields)
        except Exception as error:
            with self.hooks.lock:
                self.finish_slot()
                if current():
                    try:
                        self.reply(
                            request_id,
                            ok=False,
                            message='最多保存 32 条结果，请先删除不再需要的保存记录。原记录不变。'
                            if isinstance(error, SavedTaskCapacityError)
                            else '文件定位未确认：文件可能已移动、删除或不是可定位的普通文件。请在 Mac 核对；不会自动重试或打开文件内容。'
                            if action in REVEAL_ACTIONS
                            else '无法确认已请求打开原聊天，请在 Mac 打开 Codex 并核对。'
                            if action in OPEN_HANDOFF_ACTIONS
                            else '保存记录状态未确认，请刷新查看；异常文件保留，原任务和已执行操作不会撤销',
                        )
                    except (OSError, EOFError, ValueError):
                        pass
        finally:
            # A delete ACK can immediately trigger a new list request. Never
            # clear that successor's slot from this previous worker's finally.
            self.finish_slot()

"""Authenticated explicit memory actions; no model-inferred write capability."""

from collections import deque
from dataclasses import dataclass
from typing import Callable, ContextManager

from minimax_client import Cancelled
from operation_failure import OperationFailure
from preference_memory import command
from preference_protocol import validate_request, field


@dataclass(frozen=True)
class PreferenceHooks:
    lock: ContextManager
    connection: Callable
    busy: Callable
    event: Callable


class PreferenceController:
    def __init__(self, hooks, store):
        self.hooks, self.store = hooks, store
        self.requests = deque(maxlen=64)

    def request(self, data):
        validate_request(data)
        with self.hooks.lock:
            if self.hooks.connection() is None:
                return
            request_id = data['request_id']
            request = {k: v for k, v in data.items() if k not in ('event', 'request_id')}
            ok, message = False, ''
            try:
                if request_id in self.requests:
                    raise ValueError('Already handled')
                self.requests.append(request_id)
                if request['action'] != 'list':
                    if self.hooks.busy():
                        message = '请先暂停语音，等待当前任务和播报结束，再修改偏好记忆。'
                        raise ValueError('Pipeline busy')
                    self.store.apply(request, 'device')
                state = self.store.snapshot()
                ok = True
            except (OSError, ValueError):
                state = None
                message = message or '记忆状态未确认，请重新打开核对；不会自动重试或覆盖异常文件。'
            self.hooks.event(
                'preference_result', request_id=request_id, ok=ok, state=state, message=message
            )

    def context(self):
        return self.store.context()

    def voice(self, text, is_current):
        request = command(text)
        if request is None:
            return None
        with self.hooks.lock:
            if not is_current() or self.hooks.connection() is None:
                raise Cancelled('Memory request cancelled')
            action = request['action']
            if action == 'help':
                return (
                    '请明确说“记住，称呼是老王”，或“把记忆中的称呼改成老李”。普通聊天不会自动保存。'
                )
            if action == 'clear_help':
                return '清空会删除全部偏好。确认的话，请说“确认清空偏好记忆”，也可在设置中操作。'
            try:
                state = self.store.snapshot()
                if action == 'list':
                    return (
                        '偏好记忆已开启。'
                        if state['enabled']
                        else '偏好记忆已关闭，保存内容不会注入新的回答。'
                    ) + (
                        '当前没有保存的偏好。'
                        if not state['items']
                        else '当前保存：'
                        + '；'.join(
                            item['key'] + '是' + item['value'] for item in state['items'][:3]
                        )
                        + (
                            '。其余内容可在设置的偏好记忆中查看。'
                            if len(state['items']) > 3
                            else '。'
                        )
                    )
                if action in ('replace', 'delete'):
                    request['key'] = field(request['key'], 40)
                    if not any(
                        item['key'].casefold() == request['key'].casefold()
                        for item in state['items']
                    ):
                        return '没有找到这个确切名称的偏好，请先查看偏好记忆，再说明要改哪一条。'
                    if action == 'replace':
                        request['action'] = 'put'
                elif action in ('enable', 'disable'):
                    request = {'action': 'set_enabled', 'enabled': action == 'enable'}
                request['revision'] = state['revision']
                if not is_current():
                    raise Cancelled('Memory request cancelled')
                saved = self.store.apply(request, 'voice')
                reply = {
                    'put': '这条偏好已明确保存在这台 Mac。',
                    'replace': '这条偏好已更新，后续对话采用新内容。',
                    'delete': '这条偏好已删除，不会再从偏好库提供给后续回答。',
                    'clear': '本机偏好记忆已清空。原聊天中的历史文字不会同步删除。',
                    'enable': '偏好记忆已开启。',
                    'disable': '偏好记忆已关闭，内容保留，可以查看或删除。',
                }[action]
                if action in ('put', 'replace'):
                    selected = next(
                        item
                        for item in saved['items']
                        if item['key'].casefold() == field(request['key'], 40).casefold()
                    )
                    reply += '内容是：' + selected['key'] + '，' + selected['value'] + '。'
                if action in ('put', 'replace') and not saved['enabled']:
                    reply += '记忆目前已关闭，开启后才会提供给后续回答。'
                return reply
            except (OSError, ValueError):
                raise OperationFailure(
                    'Preference state unconfirmed; inspect settings before retry',
                    stage='brain',
                    kind='transport_or_io',
                ) from None

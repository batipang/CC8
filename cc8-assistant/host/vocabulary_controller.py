"""Device-explicit wordlist CRUD and preparation; confirmation belongs to SpeechCorrection."""

from collections import deque
from dataclasses import dataclass
from typing import Callable, ContextManager
from recognition_vocabulary import validate_request


@dataclass(frozen=True)
class VocabularyHooks:
    lock: ContextManager
    connection: Callable
    busy: Callable
    prepare: Callable
    source_id: Callable
    matches: Callable
    event: Callable


class VocabularyController:
    def __init__(self, hooks, store):
        self.hooks, self.store = hooks, store
        self.requests = deque(maxlen=64)

    def request(self, data):
        validate_request(data)
        with self.hooks.lock:
            if self.hooks.connection() is None:
                return
            request_id, action = data['request_id'], data['action']
            ok, prepared, state = False, False, None
            message = '词表状态未确认，请重新打开核对；不会自动重试。'
            try:
                if request_id in self.requests:
                    raise ValueError('Already handled')
                self.requests.append(request_id)
                if action == 'prepare':
                    message = '无法唯一替换最近识别的旧词，或会话已变化。请重新说完整问题；没有自动改写或提交。'
                    term, alias = self.store.pair(data)
                    self.hooks.prepare(term, alias, data['source_id'])
                    prepared = True
                elif action != 'list':
                    if self.hooks.busy():
                        message = '请先暂停语音，等任务和播报结束，再修改词表。'
                        raise ValueError('Pipeline busy')
                    self.store.apply(data)
                state = None if prepared else self.store.snapshot()
                ok = True
                message = (
                    '更正草稿已准备，请核对原文和更正内容，再明确确认提交。'
                    if prepared
                    else '词表已核对。未向 MiniMax 发送热词，也未改写识别原文。'
                )
            except (OSError, ValueError):
                pass
            self.hooks.event(
                'vocabulary_result',
                request_id=request_id,
                ok=ok,
                state=state,
                prepared=prepared,
                message=message,
                source_id=self.hooks.source_id() or '',
                suggestions=self.hooks.matches(state['items']) if ok and state else [],
            )

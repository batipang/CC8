"""Opt-in guest text dialogue: no owner history, tools, search, files or Dot references."""

from collections import deque
import threading

from minimax_client import Cancelled, MiniMaxError, VisibleText
from request_text import validate_request_text
from web_search import SpeechClauses, now

SYSTEM = (
    '你是大黄蜂（Bumblebee）的访客问答模式，使用 MiniMax。用自然中文简短回答。'
    '你只能看到当前访客会话提供的文字，没有主人聊天、偏好、提醒、文件或桌面操作工具。'
    '不能查询主人资料、调用工具、拍照、设置提醒或替用户执行动作，不声称已执行操作。'
    '当前访客模式没有联网检索结果，需要实时信息时如实说明，不能假装已经查询。'
)


class GuestBrain:
    def __init__(self, transport_factory, model):
        self.transport_factory = transport_factory
        self.channel = None
        self.model = model
        self.lock = threading.RLock()
        self.active = False
        self.epoch = 0
        self.run = 0
        self.history = deque(maxlen=8)
        self.last_metrics = {}

    def activate(self):
        with self.lock:
            self.epoch += 1
            self.run += 1
            self.history.clear()
            self.active = True
            channel, self.channel = self.channel, None
        if channel is not None:
            channel.cancel()

    def deactivate(self):
        with self.lock:
            self.epoch += 1
            self.run += 1
            self.history.clear()
            self.active = False
            channel, self.channel = self.channel, None
        if channel is not None:
            channel.cancel()

    def cancel(self):
        with self.lock:
            self.run += 1
            channel, self.channel = self.channel, None
        if channel is not None:
            channel.cancel()

    def internal_current(self, ticket):
        return self.active and ticket == (self.epoch, self.run)

    def clauses(self, text, history, is_current=lambda: True, on_web=None):
        # Deliberately never inspect the caller's history; it can be the owner's.
        text = validate_request_text(text, 4000)
        with self.lock:
            if not self.active:
                raise Cancelled('Guest dialogue is inactive')
            self.run += 1
            ticket = self.epoch, self.run
            messages = [dict(row) for row in self.history]
            self.last_metrics = {'provider': 'guest_minimax', 'completed': False}

        def current():
            # Never call a session-lock predicate while holding this component lock.
            if not is_current():
                return False
            with self.lock:
                return self.internal_current(ticket)

        def check():
            if not current():
                raise Cancelled('Guest dialogue cancelled')

        check()
        if on_web:
            on_web('clear', {})
        check()
        body = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': SYSTEM + '当前北京时间：' + now()},
                *messages,
                {'role': 'user', 'content': text},
            ],
            'stream': True,
            'stream_options': {'include_usage': True},
            'max_completion_tokens': 768,
            'reasoning_split': True,
        }
        if self.model == 'MiniMax-M3':
            body['thinking'] = {'type': 'disabled'}
        else:
            body['reasoning_effort'] = 'low'
        visible, speech = VisibleText(), SpeechClauses()
        answer, complete = '', False
        # Each run gets its own request owner. A delayed cancellation must not
        # close a transport created for a newer run.
        channel = self.transport_factory()
        with self.lock:
            if not self.internal_current(ticket):
                raise Cancelled('Guest dialogue superseded before request')
            self.channel = channel
        stream = channel.events('/v1/chat/completions', body, current)
        try:
            for event in stream:
                check()
                choices = event.get('choices') if isinstance(event, dict) else None
                if choices == [] or choices is None:
                    continue  # Usage-only SSE record.
                if not isinstance(choices, list) or not isinstance(choices[0], dict):
                    raise MiniMaxError('Invalid guest dialogue event')
                choice = choices[0]
                delta = choice.get('delta')
                if delta is None:
                    delta = {}
                if (
                    not isinstance(delta, dict)
                    or delta.get('tool_calls')
                    or delta.get('function_call')
                ):
                    raise MiniMaxError('Guest dialogue cannot call tools')
                reason = choice.get('finish_reason')
                if reason not in (None, 'stop'):
                    raise MiniMaxError('Guest dialogue did not complete')
                complete |= reason == 'stop'
                content = delta.get('content')
                if content is None:
                    continue
                if not isinstance(content, str):
                    raise MiniMaxError('Invalid guest dialogue content')
                piece = visible.feed(content)
                answer += piece
                if len(answer) > 10000:
                    raise MiniMaxError('Guest dialogue exceeds text limit')
                for clause in speech.feed(piece):
                    check()
                    yield validate_request_text(clause, 10000)
        finally:
            try:
                close = getattr(stream, 'close', None)
                if callable(close):
                    close()
            finally:
                with self.lock:
                    if self.channel is channel:
                        self.channel = None

        check()
        if not complete or not answer.strip():
            raise MiniMaxError('Guest dialogue ended without complete text')
        validate_request_text(answer, 10000)
        for clause in speech.feed('', final=True):
            check()
            yield validate_request_text(clause, 10000)
        check()
        with self.lock:
            if not self.internal_current(ticket):
                raise Cancelled('Guest dialogue superseded')
            self.history.extend(
                ({'role': 'user', 'content': text}, {'role': 'assistant', 'content': answer})
            )
            self.last_metrics = {
                'provider': 'guest_minimax',
                'completed': True,
                'tools_available': False,
            }
        if on_web:
            check()
            on_web('dot_result', {'text': answer, 'truncated': False, 'handoff_source_title': ''})

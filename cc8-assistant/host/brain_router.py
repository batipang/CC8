"""Explicit provider selection and voice delegation; no silent fallback."""

import re
from dot_client import DotBrain, DotError, DotApprovalRequired
from minimax_client import Cancelled

DELEGATE = re.compile(
    r'^\s*(?:请\s*)?(?:让|叫|交给|用)\s*(?:dot|codex|多特)(?![A-Za-z])\s*(?:来|帮我|帮忙)?[，,：:\s]*',
    re.I,
)


class BrainRouter:
    # 文字按用户选择或显式委派路由，图像统一交给 Dot；失败时报告错误而不暗中换大脑。
    label = 'MiniMax + Dot'

    def __init__(self, minimax, dot=None):
        self.minimax = minimax
        self.dot = dot or DotBrain()
        self.selected = 'dot'
        self.last_metrics = {}

    def select(self, provider):
        if provider not in ('minimax', 'dot'):
            raise ValueError('Unknown brain provider')
        self.selected = provider

    def cancel(self):
        self.minimax.cancel()
        self.dot.cancel()

    def close(self):
        self.cancel()
        self.dot.close()

    def vision_clauses(self, text, image, history, is_current=lambda: True, on_web=None):
        # Camera input always goes to Dot, regardless of the text-chat selector.
        try:
            yield from self.dot.clauses(text, history, is_current, on_web, image=image)
            self.last_metrics = {**self.dot.last_metrics, 'provider': 'dot', 'vision': True}
        except DotApprovalRequired:
            if not is_current():
                raise Cancelled('Turn cancelled') from None
            self.last_metrics = {
                'provider': 'dot',
                'vision': True,
                'completed': False,
                'approval_required': True,
            }
            yield 'Dot 看图需要额外权限，请到电脑端处理。'
        except DotError:
            if not is_current():
                raise Cancelled('Turn cancelled') from None
            self.last_metrics = {'provider': 'dot', 'vision': True, 'completed': False}
            yield '画面已采集，但 Dot 这次没有完成图像分析，请稍后再试。'

    def clauses(self, text, history, is_current=lambda: True, on_web=None):
        if not is_current():
            raise Cancelled('Turn cancelled')
        match = DELEGATE.match(text)
        provider = 'dot' if match else self.selected
        prompt = text[match.end() :].strip() if match else text
        target = self.dot if provider == 'dot' else self.minimax
        if not prompt:
            if on_web:
                on_web('clear', {})
            yield '你想让 Dot 帮你查询或分析什么？'
            return
        try:
            yield from target.clauses(prompt, history, is_current, on_web)
            self.last_metrics = {**target.last_metrics, 'provider': provider}
        except DotApprovalRequired:
            if not is_current():
                raise Cancelled('Turn cancelled') from None
            self.last_metrics = {'provider': 'dot', 'approval_required': True}
            yield '这项任务需要额外的工具权限，当前语音连接未获批准，请在电脑端处理。'
        except DotError:
            if not is_current():
                raise Cancelled('Turn cancelled') from None
            self.last_metrics = {'provider': 'dot', 'completed': False}
            yield 'Dot 这次没有完成请求，请稍后重试，或切回 MiniMax。'

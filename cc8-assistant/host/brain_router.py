"""Explicit provider selection and voice delegation; no silent fallback."""

import re
from lifecycle_cleanup import cleanup_all
from dot_client import DotBrain, DotError, DotApprovalRequired
from diagnostics import error_kind
from operation_failure import OperationFailure
from minimax_client import Cancelled, MiniMaxSearchError

DELEGATE = re.compile(
    r'^\s*(?:请\s*)?(?:让|叫|交给|用)\s*(?:dot|codex|多特)(?![A-Za-z])\s*(?:来|帮我|帮忙)?[，,：:\s]*',
    re.I,
)


class BrainRouter:
    # 文字按用户选择或显式委派路由，图像统一交给 Dot；失败时报告错误而不暗中换大脑。
    label = 'MiniMax + Dot'

    def __init__(self, minimax, dot=None, selected='dot'):
        self.minimax = minimax
        self.dot = dot or DotBrain()
        self.work_reports = None
        self.current_report_delivery = None
        self.select(selected)
        self.last_metrics = {}

    def select(self, provider):
        if provider not in ('minimax', 'dot'):
            raise ValueError('Unknown brain provider')
        self.selected = provider

    def cancel(self):
        cleanup_all(self.minimax.cancel, self.dot.cancel)

    def close(self):
        cleanup_all(
            self.cancel,
            self.dot.close,
            lambda: self.work_reports.clear() if self.work_reports else None,
        )

    def prepare(self, is_current=lambda: True):
        if self.selected != 'dot':
            return False
        return self.dot.prepare(is_current)

    def updates(self, target, provider, on_web, *, vision=False):
        if on_web is None:
            return None

        def update(kind, payload):
            # Receive-path receipts may arrive while this router's generator is
            # suspended in TTS. Expose current bounded provider metrics before
            # forwarding, rather than waiting for yield-from to finish.
            self.last_metrics = {**target.last_metrics, 'provider': provider}
            if vision:
                self.last_metrics['vision'] = True
            on_web(kind, payload)

        return update

    def vision_clauses(self, text, image, history, is_current=lambda: True, on_web=None):
        self.current_report_delivery = None
        if self.work_reports:
            self.work_reports.forget()
        self.last_metrics = {'provider': 'dot', 'vision': True}
        # Camera input always goes to Dot, regardless of the text-chat selector.
        try:
            yield from self.dot.clauses(
                text,
                history,
                is_current,
                self.updates(self.dot, 'dot', on_web, vision=True),
                image=image,
            )
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
            raise
        except DotError:
            if not is_current():
                raise Cancelled('Turn cancelled') from None
            self.last_metrics = {'provider': 'dot', 'vision': True, 'completed': False}
            raise
        except Cancelled:
            raise
        except Exception as error:
            if not is_current():
                raise Cancelled('Turn cancelled') from None
            self.last_metrics = {'provider': 'dot', 'vision': True, 'completed': False}
            raise OperationFailure(
                'Vision request failed', stage='brain', kind=error_kind(error)
            ) from None

    def clauses(self, text, history, is_current=lambda: True, on_web=None):
        self.current_report_delivery = None
        if not is_current():
            raise Cancelled('Turn cancelled')
        match = DELEGATE.match(text)
        prompt = text[match.end() :].strip() if match else text
        command = self.work_reports.command(prompt) if self.work_reports else None
        if command is not None:
            self.last_metrics = {'provider': 'work_report', 'completed': False}
            try:
                if on_web:
                    on_web('clear', {})
                    on_web('progress', {'message': '正在读取所选聊天的工作汇报…'})
                reply = self.work_reports.answer(command, is_current)
                if not is_current():
                    raise Cancelled('Report cancelled')
                self.last_metrics = {
                    'provider': 'work_report',
                    'completed': True,
                    'operation': reply.operation,
                    'items': reply.items,
                }
                if on_web:
                    on_web(
                        'dot_result',
                        {
                            'text': reply.text,
                            'truncated': False,
                            'handoff_source_title': reply.source_title,
                        },
                    )
                for index, clause in enumerate(reply.spoken):
                    if not is_current():
                        raise Cancelled('Report cancelled')
                    token = reply.delivery[index] if reply.delivery else None
                    marker = (self.work_reports, token) if token is not None else None
                    self.current_report_delivery = marker
                    try:
                        yield clause
                    finally:
                        if self.current_report_delivery is marker:
                            self.current_report_delivery = None
            except Exception:
                self.last_metrics = {'provider': 'work_report', 'completed': False}
                raise
            return
        if self.work_reports:
            self.work_reports.forget()
        provider = 'dot' if match else self.selected
        target = self.dot if provider == 'dot' else self.minimax
        self.last_metrics = {'provider': provider}
        if not prompt:
            if on_web:
                on_web('clear', {})
            yield '你想让 Dot 帮你查询或分析什么？'
            return
        try:
            yield from target.clauses(
                prompt, history, is_current, self.updates(target, provider, on_web)
            )
            self.last_metrics = {**target.last_metrics, 'provider': provider}
        except DotApprovalRequired:
            if not is_current():
                raise Cancelled('Turn cancelled') from None
            self.last_metrics = {'provider': 'dot', 'completed': False, 'approval_required': True}
            raise
        except DotError:
            if not is_current():
                raise Cancelled('Turn cancelled') from None
            self.last_metrics = {'provider': 'dot', 'completed': False}
            raise
        except Cancelled:
            raise
        except Exception as error:
            if not is_current():
                raise Cancelled('Turn cancelled') from None
            self.last_metrics = {'provider': provider, 'completed': False}
            search_failure = isinstance(error, MiniMaxSearchError)
            raise OperationFailure(
                'Web search failed' if search_failure else 'Brain request failed',
                stage='search' if search_failure else 'brain',
                kind=error_kind(error),
            ) from None

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

    def __init__(self, minimax, dot=None, selected='dot', *, guest=None):
        self.minimax = minimax
        self.dot = dot or DotBrain()
        self.guest = guest
        self.identity = ('owner', 0)
        self.work_reports = None
        self.preferences = None
        self.preference_context = ''
        self.current_report_delivery = None
        self.select(selected)
        self.last_metrics = {}

    def select(self, provider):
        if self.identity[0] != 'owner' or provider not in ('minimax', 'dot'):
            raise ValueError('Unknown brain provider')
        self.selected = provider

    def cancel(self):
        if self.identity[0] == 'guest':
            if self.guest is not None:
                self.guest.cancel()
            return
        cleanup_all(
            self.minimax.cancel,
            self.dot.cancel,
            lambda: self.guest.cancel() if self.guest is not None else None,
        )

    def set_identity(self, mode):
        """Internal idle-only transition, called by the identity controller.

        Never expose this method as a model tool or a device-selected provider.
        The outer session guard owns admission, workspace isolation and ACKs.
        """
        if mode not in ('owner', 'guest', 'blocked') or (mode == 'guest' and self.guest is None):
            raise ValueError('Guest dialogue is unavailable')
        epoch = self.identity[1] + 1
        self.identity = ('blocked', epoch)
        self.current_report_delivery = None
        self.preference_context = ''
        if self.guest is not None:
            if mode == 'guest':
                self.guest.activate()
            else:
                self.guest.deactivate()
        self.identity = (mode, epoch)

    def bind_identity(self, is_current):
        identity = self.identity

        def current():
            return is_current() and identity == self.identity and identity[0] != 'blocked'

        return current

    def close(self):
        cleanup_all(
            self.minimax.cancel,
            self.dot.cancel,
            self.dot.close,
            lambda: self.guest.deactivate() if self.guest is not None else None,
            lambda: self.work_reports.clear() if self.work_reports else None,
        )

    def prepare(self, is_current=lambda: True):
        if self.identity[0] != 'owner' or self.selected != 'dot':
            return False
        return self.dot.prepare(self.bind_identity(is_current))

    def updates(self, target, provider, on_web, *, vision=False, is_current=lambda: True):
        if on_web is None:
            return None

        def update(kind, payload):
            if not is_current():
                return
            # Receive-path receipts may arrive while this router's generator is
            # suspended in TTS. Expose current bounded provider metrics before
            # forwarding, rather than waiting for yield-from to finish.
            self.last_metrics = {**target.last_metrics, 'provider': provider}
            if vision:
                self.last_metrics['vision'] = True
            on_web(kind, payload)

        return update

    @staticmethod
    def scoped_clauses(clauses, is_current):
        # Providers are not responsible for role authorization. Recheck at the
        # router's yield boundary as well, including a provider's late final row.
        iterator = iter(clauses)
        try:
            for clause in iterator:
                if not is_current():
                    raise Cancelled('Dialogue identity changed')
                yield clause
            if not is_current():
                raise Cancelled('Dialogue identity changed')
        finally:
            close = getattr(iterator, 'close', None)
            if callable(close):
                close()

    def apply_preferences(self, history, target):
        context = self.preferences.context() if self.preferences else ''
        if context != self.preference_context:
            # Our RAM conversation can contain a previously personalized answer.
            # Start fresh rather than re-inject it after deletion or disable.
            clear = getattr(history, 'clear', None)
            if callable(clear):
                clear()
            history = []
            self.preference_context = context
        target.preference_context = context
        return history

    def vision_clauses(self, text, image, history, is_current=lambda: True, on_web=None):
        if self.identity[0] != 'owner':
            raise Cancelled('Vision is unavailable outside owner mode')
        is_current = self.bind_identity(is_current)
        if not is_current():
            raise Cancelled('Vision cancelled')
        self.current_report_delivery = None
        if self.work_reports:
            self.work_reports.forget()
        self.last_metrics = {'provider': 'dot', 'vision': True}
        # Camera input always goes to Dot, regardless of the text-chat selector.
        try:
            history = self.apply_preferences(history, self.dot)
            yield from self.scoped_clauses(
                self.dot.clauses(
                    text,
                    history,
                    is_current,
                    self.updates(self.dot, 'dot', on_web, vision=True, is_current=is_current),
                    image=image,
                ),
                is_current,
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

    def bind_report_followup(self, text):
        if self.identity[0] == 'owner' and self.work_reports is not None:
            return self.work_reports.bind_followup(DELEGATE.sub('', text, count=1).strip())
        return None

    def clauses(
        self,
        text,
        history,
        is_current=lambda: True,
        on_web=None,
        *,
        force_dot=False,
        report_followup=None,
    ):
        is_current = self.bind_identity(is_current)
        self.current_report_delivery = None
        if not is_current():
            raise Cancelled('Turn cancelled')
        if self.identity[0] == 'guest':
            # This branch precedes delegation, memory commands, report readers
            # and the owner's selected provider. Never pass caller history.
            target = self.guest
            if target is None:
                raise Cancelled('Guest dialogue is unavailable')
            self.last_metrics = {'provider': 'guest_minimax', 'completed': False}
            yield from self.scoped_clauses(
                target.clauses(
                    text,
                    (),
                    is_current,
                    self.updates(target, 'guest_minimax', on_web, is_current=is_current),
                ),
                is_current,
            )
            if not is_current():
                raise Cancelled('Guest dialogue superseded')
            self.last_metrics = {**target.last_metrics, 'provider': 'guest_minimax'}
            return
        match = DELEGATE.match(text)
        prompt = text[match.end() :].strip() if match else text
        command = report_followup.command if report_followup is not None else None
        if self.preferences and command is None:
            answer = self.preferences.voice(prompt, is_current)
            if answer is not None:
                self.last_metrics = {'provider': 'preference_memory', 'completed': True}
                if on_web:
                    on_web('clear', {})
                    on_web(
                        'dot_result',
                        {'text': answer, 'truncated': False, 'handoff_source_title': ''},
                    )
                if not is_current():
                    raise Cancelled('Memory answer cancelled')
                yield answer
                return
        if report_followup is None and self.work_reports is not None:
            report_followup = self.work_reports.bind_followup(prompt, include_other_commands=True)
            command = report_followup.command
        if command is not None:
            self.last_metrics = {'provider': 'work_report', 'completed': False}
            try:
                if on_web:
                    on_web('clear', {})
                    message = {
                        'item_evidence': '正在核对本次汇报的执行回执…',
                        'active_sources': '正在查看本次汇报中的来源状态…',
                        'item_queue': '正在读取该条汇报的原聊天队列…',
                        'item_stop': '正在核对要停止的原任务…',
                        'continue': '正在核对本次汇报的原聊天…',
                    }.get(command.kind, '正在读取所选聊天的工作汇报…')
                    on_web('progress', {'message': message})
                reply = self.work_reports.answer(report_followup or command, is_current)
                if not is_current():
                    raise Cancelled('Report cancelled')
                self.last_metrics = {
                    'provider': 'work_report',
                    'completed': True,
                    'operation': reply.operation,
                    'items': reply.items,
                }
                if on_web:
                    if reply.provenance is not None:
                        on_web('result_provenance', reply.provenance)
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
        provider = 'dot' if match or force_dot else self.selected
        target = self.dot if provider == 'dot' else self.minimax
        self.last_metrics = {'provider': provider}
        if not prompt:
            if on_web:
                on_web('clear', {})
            yield '你想让 Dot 帮你查询或分析什么？'
            return
        try:
            history = self.apply_preferences(history, target)
            yield from self.scoped_clauses(
                target.clauses(
                    prompt,
                    history,
                    is_current,
                    self.updates(target, provider, on_web, is_current=is_current),
                ),
                is_current,
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

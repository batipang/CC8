"""Existing provider actions for the serial response queue, never an extra brain worker.

Scheduled steps use only the search transport or explicitly scoped report reader.
The orchestrator never appends prior step results as follow-up instructions.
Existing provider context policies stay in force. Read-only search/report text
stays outside later model inputs; pictures come from the foreground owner.
"""

from dataclasses import dataclass
import time
import uuid
from typing import Callable

from minimax_client import SEARCH_ROUTE, check_api_result
from report_checkpoint import source_from
from report_voice import ReportVoice, report_text
from web_search import normalize, now as search_time
from work_reports import ReportWindow
from workflow_run import StepOutput, WorkflowCancelled, WorkflowUnconfirmed


@dataclass(frozen=True)
class ActionHooks:
    brain: Callable[[], object]
    read_report: Callable[..., dict]
    source_current: Callable[[object], bool]
    image: Callable[[str], bytes | None] = lambda step_id: None
    wall_clock: Callable[[], float] = time.time


class WorkflowActions:
    def __init__(self, hooks):
        self.hooks = hooks

    @staticmethod
    def check(current):
        if current() is not True:
            raise WorkflowCancelled('Workflow operation owner changed')

    def execute(self, step, current, deadline, accepted):
        # The run owns the absolute monotonic deadline in current(). Network
        # clients check it during bounded I/O; no timer retries a lost operation.
        self.check(current)
        kind = step['kind']
        if kind == 'web_search':
            return self.search(step, current)
        if kind == 'work_report':
            return self.report(step, current)
        if kind in ('assistant', 'ocr'):
            return self.assistant(step, current, accepted)
        raise WorkflowUnconfirmed('Unsupported workflow action')

    def preflight(self, flow, current):
        self.check(current)
        for step in flow['steps']:
            if step['kind'] == 'work_report':
                source = source_from(step['source'])
                if self.hooks.source_current(source) is not True:
                    raise WorkflowCancelled('Workflow report source revoked')
        self.check(current)

    def search(self, step, current):
        brain = self.hooks.brain()
        # The existing MiniMax transport supplies configured credentials and
        # deadlines. A literal search is not a general model/tool request.
        data = brain.minimax.transport.post_json(SEARCH_ROUTE, {'q': step['text']}, current)
        self.check(current)
        check_api_result(data, require_status=True)
        rows = normalize(data)
        retrieved = search_time()
        lines = [
            f'检索时间：{retrieved}（北京时间）。以下为搜索结果摘录，网页正文和内容尚未独立核验。'
        ]
        for i, row in enumerate(rows, 1):
            lines.append(
                f'{i}. {row["title"]}\n来源：{row["url"]}\n'
                + (f'来源标注日期：{row["published_at"]}\n' if row['published_at'] else '')
                + row['snippet']
            )
        if not rows:
            lines.append('本次搜索未返回可用结果，不能据此断言没有相关信息。')
        self.check(current)
        return StepOutput('web_response', uuid.uuid4().hex, '\n\n'.join(lines), True)

    def report(self, step, current):
        source = source_from(step['source'])

        def owned():
            return current() is True and self.hooks.source_current(source) is True

        self.check(owned)
        period = ReportWindow.period(step['period'], self.hooks.wall_clock())
        report = self.hooks.read_report(source, period, owned)
        self.check(owned)
        if not ReportVoice.valid_report(source, report) or (
            report['window_start'],
            report['window_end'],
        ) != (period.start, period.end):
            raise WorkflowUnconfirmed('Report source or time window mismatched')
        return StepOutput('report_read', uuid.uuid4().hex, report_text(report), True)

    def assistant(self, step, current, accepted):
        brain = self.hooks.brain()
        is_vision = step['kind'] == 'ocr'
        image = self.hooks.image(step['id']) if is_vision else None
        if is_vision and (not isinstance(image, bytes) or not image):
            raise WorkflowUnconfirmed('No freshly authorized workflow frame')
        receipt_kind = 'vision_turn' if is_vision else 'assistant_turn'
        observed = []
        operation_id = uuid.uuid4().hex

        def update(kind, payload):
            if kind != 'dot_result' or not isinstance(payload, dict):
                return
            self.check(current)
            metrics = brain.last_metrics
            provider = metrics.get('provider')
            # Native Dot receive-path admission emits this only for a matched
            # completed turn/final answer; model prose never enters this port.
            if provider == 'dot':
                ident = metrics.get('turn_id')
                if not isinstance(ident, str) or not ident:
                    raise WorkflowUnconfirmed('No completed Dot turn identity')
            elif (
                not is_vision
                and provider in ('work_report', 'preference_memory')
                and metrics.get('completed') is True
            ):
                ident = operation_id
            elif (
                not is_vision
                and provider == 'minimax'
                and metrics.get('completed') is True
                and type(metrics.get('characters')) is int
                and metrics['characters'] > 0
            ):
                ident = operation_id
            else:
                raise WorkflowUnconfirmed('Unknown workflow operation producer')
            output = StepOutput(
                receipt_kind, ident, payload.get('text'), True, payload.get('truncated', False)
            )
            # Admit before generator cleanup or TTS. The run preserves this
            # result if cancellation races with cleanup of the native stream.
            accepted(output)
            observed.append(output)

        clauses = None
        try:
            clauses = (
                brain.vision_clauses(step['text'], image, [], current, update)
                if is_vision
                else brain.clauses(step['text'], [], current, update)
            )
            for _ in clauses:
                self.check(current)
            if observed:
                return observed[-1]
            # Do not promote streamed prose, clarification, or an iterator's
            # normal exit without the provider's operation-completion callback.
            raise WorkflowUnconfirmed('No native operation completion receipt')
        finally:
            image = None
            if clauses is not None:
                clauses.close()

"""One frozen range over explicitly registered sources; partial coverage stays visible."""

from copy import deepcopy
import time
import uuid

from dot_handoff import Handoff
from dot_client import Cancelled, DotError
from report_sources import ReportScope, MAX_SOURCES
from source_identity import identifier, title
from work_reports import WorkReportReader, ReportWindow, MAX_CARDS

COUNTERS = (
    'failed_turns',
    'interrupted_turns',
    'active_turns_seen',
    'unknown_completion_time',
    'missing_final_answer',
)


class GroupWorkReports:
    def __init__(self, rpc_factory, *, clock=time.monotonic):
        self.rpc_factory, self.clock = rpc_factory, clock

    def read(self, scope, window, *, is_current=lambda: True):
        if not isinstance(scope, ReportScope) or not isinstance(window, ReportWindow):
            raise ValueError('Choose registered sources and a report range first')
        identifier(scope.source_id)
        if not 0 < len(scope.members) <= MAX_SOURCES or len(
            {m.thread_id for m in scope.members}
        ) != len(scope.members):
            raise ValueError('Invalid registered report scope')
        for member in scope.members:
            identifier(member.thread_id)
            if title(member.title) != member.title or title(member.project) != member.project:
                raise ValueError('Invalid registered report scope label')
        result = dict(
            snapshot_id=uuid.uuid4().hex,
            source='codex_registered',
            source_id=scope.source_id,
            source_title=scope.source_title,
            registry_revision=scope.revision,
            window_start=window.start,
            window_end=window.end,
            timezone='Asia/Shanghai',
            items=[],
            sources=[],
            coverage_complete=True,
            history_persisted=False,
            original_chat_modified=False,
            **dict.fromkeys(COUNTERS, 0),
        )
        deadline = self.clock() + 45
        reader = WorkReportReader(self.rpc_factory, clock=self.clock)
        for member in scope.members:
            if not is_current():
                raise Cancelled('Registered report cancelled')
            source = dict(
                provider='codex',
                host='local',
                thread_id=member.thread_id,
                title=member.title,
                project=member.project,
                status='not_read_budget',
            )
            result['sources'].append(source)
            if self.clock() >= deadline:
                result['coverage_complete'] = False
                continue
            selected = Handoff(member.thread_id, member.title, (), False)
            try:
                report = reader.read(
                    selected,
                    window,
                    is_current=is_current,
                    deadline=min(deadline, self.clock() + 12),
                )
                from report_voice import ReportVoice

                if not ReportVoice.valid_report(selected, report):
                    raise DotError('Unconfirmed registered-source report')
                source['status'] = 'read'
                source['coverage_complete'] = report['coverage_complete']
                result['coverage_complete'] &= report['coverage_complete']
                for key in COUNTERS:
                    result[key] += report[key]
                for original in report['items']:
                    card = deepcopy(original)
                    card.update(
                        source_provider='codex',
                        source_host='local',
                        source_thread_id=member.thread_id,
                        source_title=member.title,
                        source_project=member.project,
                    )
                    result['items'].append(card)
            except Cancelled:
                raise
            except (DotError, OSError, ValueError, KeyError, TypeError):
                source['status'] = 'unavailable'
                result['coverage_complete'] = False
        if not is_current():
            raise Cancelled('Registered report cancelled')
        result['items'].sort(
            key=lambda row: (
                row['completed_at'],
                row['source_thread_id'],
                row['turn_id'],
                row['item_id'],
            ),
            reverse=True,
        )
        result['items_truncated'] = len(result['items']) > MAX_CARDS
        if result['items_truncated']:
            result['items'] = result['items'][:MAX_CARDS]
            result['coverage_complete'] = False
        result['unavailable_sources'] = sum(s['status'] != 'read' for s in result['sources'])
        result['empty_confirmed'] = result['coverage_complete'] and not result['items']
        return result

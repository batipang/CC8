"""Read one explicitly selected Codex source into a bounded, RAM-only report."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
import time
import uuid
from dot_handoff import Handoff, identifier
from dot_client import Cancelled, DotError
from report_display_protocol import PERIODS, CALENDAR_PERIODS

BEIJING = timezone(timedelta(hours=8))
MAX_TURN_PAGES, MAX_ITEM_PAGES, PAGE_SIZE, MAX_CARDS = 4, 2, 25, 20
PARTIAL_READ_ERRORS = frozenset(('timeout', 'transport_or_io'))


# Accept only direct test runners with simple arguments. Shell composition,
# discovery-only flags, and arbitrary wrapper scripts have ambiguous exits.
SIMPLE_COMMAND = re.compile(r'[A-Za-z0-9_./:@ -]{1,1024}')
TEST_PYTHON = re.compile(r'python(?:3(?:\.\d+)?)?')
TEST_TARGET = re.compile(r'[A-Za-z0-9_./:]+')
TEST_FLAGS = frozenset(
    ('-q', '-v', '-vv', '-x', '--quiet', '--verbose', '--fail-fast', '--exitfirst')
)
CC8_TEST_SUITES = frozenset(('installer', 'host', 'tooling', 'java'))


def direct_test_command(value):
    if not isinstance(value, str) or not SIMPLE_COMMAND.fullmatch(value):
        return False
    args = value.split()
    if not args:
        return False
    runner = args.pop(0).rsplit('/', 1)[-1]
    python_runner = TEST_PYTHON.fullmatch(runner) is not None
    python_module = python_runner and len(args) >= 2 and args[0] == '-m'
    cc8_script = python_runner and args[:1] in (['tools/test_all.py'], ['./tools/test_all.py'])
    if python_module:
        runner, args = args[1], args[2:]
    elif cc8_script:
        args = args[1:]
    if cc8_script or (python_module and runner == 'tools.test_all'):
        # The repository's test entry point returns a failure if any selected
        # suite fails. Recognize only the invocation forms that actually run it.
        return len(args) % 2 == 0 and all(
            flag == '--suite' and suite in CC8_TEST_SUITES
            for flag, suite in zip(args[::2], args[1::2])
        )
    if runner == 'unittest' and python_module:
        if args[:1] == ['discover']:
            args = args[1:]
            if len(args) >= 2 and args[0] == '-s' and TEST_TARGET.fullmatch(args[1]):
                args = args[2:]
    elif runner != 'pytest':
        return False
    return all(arg in TEST_FLAGS or TEST_TARGET.fullmatch(arg) for arg in args)


@dataclass(frozen=True)
class ReportWindow:
    start: int
    end: int

    def __post_init__(self):
        if not (
            type(self.start) is type(self.end) is int
            and 0 <= self.start <= self.end <= 253402300799
            and self.end - self.start <= 31 * 86400
        ):
            raise ValueError('Invalid report window')

    @classmethod
    def period(cls, period, now):
        if (
            period not in (*PERIODS, *CALENDAR_PERIODS)
            or type(now) not in (int, float)
            or not 0 < now < 253402300799
        ):
            raise ValueError('Invalid report period')
        end = int(now)
        midnight = datetime.fromtimestamp(end, BEIJING).replace(hour=0, minute=0, second=0)
        if period in CALENDAR_PERIODS:
            stop = (
                midnight if period == 'yesterday' else midnight - timedelta(days=midnight.weekday())
            )
            start = stop - timedelta(days=1 if period == 'yesterday' else 7)
            return cls(int(start.timestamp()), int(stop.timestamp()))
        start = (
            int(midnight.timestamp())
            if period == 'today'
            else end - (1800 if period == 'half_hour' else 7200)
        )
        return cls(start, end)


def timestamp(value):
    return value if type(value) is int and 0 <= value <= 253402300799 else None


def cursor(value, seen):
    if value is not None and (
        not isinstance(value, str) or not 0 < len(value) <= 2048 or value in seen
    ):
        raise DotError('Invalid report pagination')
    if value is not None:
        seen.add(value)
    return value


class WorkReportReader:
    def __init__(self, rpc_factory, *, clock=time.monotonic):
        self.rpc_factory, self.clock = rpc_factory, clock

    def read(self, source, window, *, is_current=lambda: True, deadline=None):
        if not isinstance(source, Handoff) or not isinstance(window, ReportWindow):
            raise ValueError('Select a source before requesting a work report')
        identifier(source.source_id)
        if deadline is not None and (
            type(deadline) not in (int, float) or not math.isfinite(deadline)
        ):
            raise ValueError('Invalid work report deadline')
        rpc = self.rpc_factory()
        deadline = min(self.clock() + 30, deadline) if deadline is not None else self.clock() + 30

        def current():
            if not is_current():
                raise Cancelled('Work report cancelled')

        def request(method, params):
            current()
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise DotError('Work report read deadline exceeded', kind='timeout')
            result = rpc.request(method, params, timeout=min(5, remaining))
            current()
            if not isinstance(result, dict):
                raise DotError('Invalid work report response')
            return result

        report = {
            'snapshot_id': uuid.uuid4().hex,
            'source': 'codex',
            'source_id': source.source_id,
            'source_title': source.source_title,
            'window_start': window.start,
            'window_end': window.end,
            'timezone': 'Asia/Shanghai',
            'items': [],
            'failed_turns': 0,
            'interrupted_turns': 0,
            'active_turns_seen': 0,
            'unknown_completion_time': 0,
            'missing_final_answer': 0,
            'coverage_complete': False,
            'history_persisted': False,
            'original_chat_modified': False,
        }
        try:
            current()
            if self.clock() >= deadline:
                raise DotError('Work report read deadline exceeded', kind='timeout')
            rpc.start()
            current()
            turns, page_cursor, seen_cursors = {}, None, set()
            for page_index in range(MAX_TURN_PAGES):
                params = dict(
                    threadId=source.source_id,
                    limit=PAGE_SIZE,
                    sortDirection='desc',
                    itemsView='notLoaded',
                )
                if page_cursor is not None:
                    params['cursor'] = page_cursor
                try:
                    page = request('thread/turns/list', params)
                except DotError as error:
                    if not page_index or error.kind not in PARTIAL_READ_ERRORS:
                        raise
                    break
                rows = page.get('data')
                if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                    raise DotError('Invalid work report turns')
                for row in rows:
                    if not isinstance(row, dict):
                        raise DotError('Invalid work report turn')
                    ident = identifier(row.get('id'))
                    state = row.get('status')
                    if state not in ('completed', 'failed', 'interrupted', 'inProgress'):
                        raise DotError('Unknown work report state')
                    meta = (state, row.get('startedAt'), row.get('completedAt'))
                    if ident in turns and turns[ident] != meta:
                        raise DotError('Work report history changed while reading')
                    turns[ident] = meta
                page_cursor = cursor(page.get('nextCursor'), seen_cursors)
                if page_cursor is None:
                    report['coverage_complete'] = True
                    break
            # Pagination order is not a completion-time index. Scan every
            # bounded metadata page before filtering, including older starts.
            candidates = []
            for ident, (state, started, completed) in turns.items():
                if state == 'inProgress':
                    report['active_turns_seen'] += 1
                    continue
                end, begin = timestamp(completed), timestamp(started)
                if end is None or begin is not None and end < begin:
                    report['unknown_completion_time'] += 1
                    continue
                if not window.start <= end < window.end:
                    continue
                if state == 'failed':
                    report['failed_turns'] += 1
                elif state == 'interrupted':
                    report['interrupted_turns'] += 1
                else:
                    candidates.append((end, ident))
            candidates.sort(reverse=True)
            if len(candidates) > MAX_CARDS:
                report['coverage_complete'] = False
            for completed, ident in candidates[:MAX_CARDS]:
                try:
                    card = self.items(request, source.source_id, ident)
                except DotError as error:
                    if not report['items'] or error.kind not in PARTIAL_READ_ERRORS:
                        raise
                    report['coverage_complete'] = False
                    break
                if card is None:
                    report['missing_final_answer'] += 1
                else:
                    card.update(turn_id=ident, completed_at=completed)
                    report['items'].append(card)
                    if not card['evidence_complete']:
                        report['coverage_complete'] = False
            if report['unknown_completion_time'] or report['missing_final_answer']:
                report['coverage_complete'] = False
            report['empty_confirmed'] = report['coverage_complete'] and not report['items']
            current()
            return report
        finally:
            rpc.close()

    @staticmethod
    def items(request, source_id, turn_id):
        last_final, page_cursor, seen_cursors, seen_items = None, None, set(), {}
        evidence = dict(
            command_successes=0,
            command_failures=0,
            file_change_successes=0,
            file_change_failures=0,
            unconfirmed_tool_items=0,
        )
        tests = dict(exit_zero=0, exit_nonzero=0, unconfirmed=0)
        complete = False
        for _ in range(MAX_ITEM_PAGES):
            params = dict(threadId=source_id, turnId=turn_id, limit=50, sortDirection='desc')
            if page_cursor is not None:
                params['cursor'] = page_cursor
            try:
                page = request('thread/items/list', params)
            except DotError as error:
                if last_final is None or error.kind not in PARTIAL_READ_ERRORS:
                    raise
                break
            rows = page.get('data')
            if not isinstance(rows, list) or len(rows) > 50:
                raise DotError('Invalid work report items')
            for entry in rows:
                if not isinstance(entry, dict) or entry.get('turnId') != turn_id:
                    raise DotError('Work report item belongs to another turn')
                item = entry.get('item')
                if not isinstance(item, dict):
                    raise DotError('Invalid work report item')
                ident = identifier(item.get('id'))
                kind, state = item.get('type'), item.get('status')
                is_test = kind == 'commandExecution' and direct_test_command(item.get('command'))
                signature = (
                    kind,
                    state,
                    type(item.get('exitCode')),
                    item.get('exitCode'),
                    item.get('phase'),
                    is_test,
                    item.get('text')
                    if kind == 'agentMessage' and item.get('phase') == 'final_answer'
                    else None,
                )
                if ident in seen_items:
                    if seen_items[ident] != signature:
                        raise DotError('Work report item changed while reading')
                    continue
                seen_items[ident] = signature
                if (
                    kind == 'agentMessage'
                    and item.get('phase') == 'final_answer'
                    and last_final is None
                ):
                    text = item.get('text')
                    if not isinstance(text, str) or len(text) > 64000:
                        raise DotError('Invalid work report final answer')
                    if text.strip():
                        last_final = dict(
                            item_id=ident,
                            excerpt=''.join(c for c in text if ord(c) >= 32 or c == '\n')[:500],
                            excerpt_truncated=len(text) > 500,
                        )
                elif kind == 'commandExecution':
                    if is_test:
                        exit_code = item.get('exitCode')
                        if state in ('completed', 'failed') and type(exit_code) is int:
                            if exit_code != 0 or state == 'completed':
                                tests['exit_zero' if exit_code == 0 else 'exit_nonzero'] += 1
                            else:
                                tests['unconfirmed'] += 1
                        else:
                            tests['unconfirmed'] += 1
                    if state == 'completed' and type(item.get('exitCode')) is int:
                        evidence[
                            'command_successes' if item['exitCode'] == 0 else 'command_failures'
                        ] += 1
                    elif state in ('failed', 'declined'):
                        evidence['command_failures'] += 1
                    else:
                        evidence['unconfirmed_tool_items'] += 1
                elif kind == 'fileChange':
                    if state == 'completed':
                        evidence['file_change_successes'] += 1
                    elif state in ('failed', 'declined'):
                        evidence['file_change_failures'] += 1
                    else:
                        evidence['unconfirmed_tool_items'] += 1
            page_cursor = cursor(page.get('nextCursor'), seen_cursors)
            if page_cursor is None:
                complete = True
                break
        if last_final is None:
            return None
        return dict(
            **last_final,
            evidence=evidence,
            evidence_complete=complete and evidence['unconfirmed_tool_items'] == 0,
            verification='not_assessed',
            result_kind='quoted_final_answer',
            **({'test_processes': tests} if any(tests.values()) else {}),
        )

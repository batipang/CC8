"""Explicit selected-chat excerpts; read-only RPC, RAM only, never resume a writer."""

from dataclasses import dataclass
import json
import threading
import time
from pathlib import Path
from codex_rpc import CodexRpc
from dot_receipt import DotError
from minimax_client import Cancelled
from source_identity import identifier as identifier, title as title


@dataclass(frozen=True)
class Handoff:
    source_id: str
    source_title: str
    messages: tuple[tuple[str, str], ...]
    truncated: bool
    cwd: str | None = None

    def input(self):
        # JSON quoting separates historical instructions from the new request.
        return json.dumps(
            {
                'kind': 'quoted_chat_excerpts',
                'mode': 'summary_handoff',
                'source_title': self.source_title,
                'source_thread': self.source_id,
                'source_project': self.cwd,
                'truncated': self.truncated,
                'messages': [{'role': role, 'text': text} for role, text in self.messages],
            },
            ensure_ascii=False,
        )


class ChatCatalog:
    def __init__(self, rpc_factory=CodexRpc, clock=time.monotonic):
        self.rpc_factory = rpc_factory
        self.clock = clock
        self.lock = threading.Lock()
        self.offered = {}
        self.cursors = {}
        self.registration_offers = {}

    def report(self, selected, period, is_current=lambda: True):
        from work_reports import ReportWindow, WorkReportReader
        from report_sources import ReportScope
        from group_work_reports import GroupWorkReports

        reader = (
            GroupWorkReports(self.rpc_factory)
            if isinstance(selected, ReportScope)
            else WorkReportReader(self.rpc_factory)
        )
        return reader.read(
            selected,
            period
            if isinstance(period, ReportWindow)
            else ReportWindow.period(period, time.time()),
            is_current=is_current,
        )

    def list(self, cursor=None, is_current=lambda: True):
        if not is_current():
            raise Cancelled('Chat listing cancelled')
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError('Invalid chat cursor')
            with self.lock:
                if self.cursors.get(cursor, 0) <= self.clock():
                    raise ValueError('Expired chat cursor')
        rpc = self.rpc_factory()
        try:
            rpc.start()
            params = {
                'limit': 10,
                'archived': False,
                'sortKey': 'updated_at',
                'sourceKinds': ['cli', 'vscode', 'appServer'],
                'useStateDbOnly': True,
            }
            if cursor is not None:
                params['cursor'] = cursor
            reply = rpc.request('thread/list', params, timeout=10)
            if not is_current():
                raise Cancelled('Chat listing cancelled')
            rows = reply.get('data')
            if not isinstance(rows, list) or len(rows) > 10:
                raise DotError('Invalid chat listing')
            result = []
            for row in rows:
                if not isinstance(row, dict) or row.get('ephemeral') is True:
                    continue
                ident = identifier(row.get('id'))
                cwd = row.get('cwd')
                project = Path(cwd).name if isinstance(cwd, str) and len(cwd) <= 4096 else ''
                result.append(
                    {
                        'id': ident,
                        'title': title(row.get('name')),
                        'project': title(project) if project else '未标明项目',
                    }
                )
            next_cursor = reply.get('nextCursor')
            if next_cursor is not None and (
                not isinstance(next_cursor, str) or not 0 < len(next_cursor) <= 2048
            ):
                raise DotError('Invalid chat pagination')
            with self.lock:
                now = self.clock()
                if cursor is None:
                    self.offered.clear()
                    self.cursors.clear()
                self.offered = {k: v for k, v in self.offered.items() if v[1] > now}
                self.registration_offers = {
                    k: v for k, v in self.registration_offers.items() if k in self.offered
                }
                self.cursors = {k: v for k, v in self.cursors.items() if v > now}
                if len(self.offered) + len(result) > 100:
                    raise ValueError('Refresh the chat list')
                self.offered.update({r['id']: (r['title'], now + 300) for r in result})
                self.registration_offers.update({r['id']: dict(r) for r in result})
                if next_cursor is not None:
                    self.cursors[next_cursor] = now + 300
            return {'items': result, 'next_cursor': next_cursor}
        finally:
            rpc.close()

    def registration(self, source_id):
        """Return offered metadata only; registration never reads history or resumes a chat."""
        identifier(source_id)
        with self.lock:
            offered = self.offered.get(source_id)
            row = self.registration_offers.get(source_id)
            if not offered or offered[1] <= self.clock() or row is None:
                raise ValueError('Select a current listed report source')
            return dict(
                provider='codex',
                host='local',
                thread_id=source_id,
                title=row['title'],
                project=row['project'],
            )

    def select(self, source_id, is_current=lambda: True):
        if not is_current():
            raise Cancelled('Chat selection cancelled')
        identifier(source_id)
        with self.lock:
            offered = self.offered.get(source_id)
            if not offered or offered[1] <= self.clock():
                raise ValueError('Select a current listed chat')
            source_title = offered[0]
        rpc = self.rpc_factory()
        try:
            rpc.start()
            if not is_current():
                raise Cancelled('Chat selection cancelled')
            row = rpc.request(
                'thread/read', {'threadId': source_id, 'includeTurns': False}, timeout=10
            ).get('thread')
            if not isinstance(row, dict) or row.get('id') != source_id:
                raise DotError('Selected chat is unavailable')
            cwd = row.get('cwd')
            if row.get('environments') or not isinstance(cwd, str) or len(cwd) > 4096:
                cwd = None
            elif not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                cwd = None
            if not is_current():
                raise Cancelled('Chat selection cancelled')
            reply = rpc.request(
                'thread/turns/list',
                {'threadId': source_id, 'limit': 3, 'sortDirection': 'desc', 'itemsView': 'full'},
                timeout=10,
            )
            turns = reply.get('data')
            if not is_current():
                raise Cancelled('Chat selection cancelled')
            if not isinstance(turns, list) or len(turns) > 3:
                raise DotError('Invalid selected chat history')
            messages = []
            truncated = reply.get('nextCursor') is not None
            for turn in reversed(turns):
                if not isinstance(turn, dict) or turn.get('status') != 'completed':
                    continue
                items = turn.get('items')
                if not isinstance(items, list) or len(items) > 2048:
                    raise DotError('Selected chat items unavailable')
                users, finals = [], []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get('type') == 'userMessage':
                        content = item.get('content')
                        if not isinstance(content, list):
                            continue
                        values = [
                            part['text']
                            for part in content
                            if isinstance(part, dict)
                            and part.get('type') == 'text'
                            and isinstance(part.get('text'), str)
                        ]
                        if values:
                            users.append('\n'.join(values))
                    elif item.get('type') == 'agentMessage' and item.get('phase') == 'final_answer':
                        if isinstance(item.get('text'), str):
                            finals.append(item['text'])
                # Never hand over an unanswered historical instruction alone.
                if not users or not finals:
                    continue
                # Exclude tools, reasoning, commentary, images and incomplete turns.
                for role, texts in (('user', users[-1:]), ('assistant', finals[-1:])):
                    for text in texts:
                        truncated |= len(text) > 1000
                        if text.strip():
                            messages.append((role, text[:1000]))
            if not messages:
                raise DotError('No completed text available for selected chat')
            return Handoff(source_id, source_title, tuple(messages), bool(truncated), cwd)
        finally:
            rpc.close()

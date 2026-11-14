"""Explicit private delivery checkpoints; restoration never reads history or starts media."""

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import threading
import time
import uuid

from dot_handoff import Handoff
from report_sources import ReportSources, ReportScope, ReportMember
from report_archive import stamp
from report_archive_protocol import token, REVISION_LIMIT
from reminder_store import ReminderStore, MAX_BYTES
from report_progress import ReportProgress, Entry, key, version, MAX_RECORDS
from report_source_metadata import validate as validate_source_metadata
from work_reports import ReportWindow, MAX_CARDS

LIMIT = 8
ITEM_FIELDS = frozenset(
    (
        'turn_id',
        'item_id',
        'completed_at',
        'excerpt',
        'excerpt_truncated',
        'evidence',
        'evidence_complete',
        'verification',
        'result_kind',
    )
)
MEMBER_FIELDS = frozenset(
    (
        'source_provider',
        'source_host',
        'source_thread_id',
        'source_title',
        'source_project',
    )
)
EVIDENCE_FIELDS = frozenset(
    (
        'command_successes',
        'command_failures',
        'file_change_successes',
        'file_change_failures',
        'unconfirmed_tool_items',
    )
)


def describe(source):
    if not isinstance(source, (Handoff, ReportScope)):
        raise ValueError('Choose a report source first')
    return dict(
        kind='registered' if isinstance(source, ReportScope) else 'selected',
        source_id=source.source_id,
        source_title=source.source_title,
        members=[
            dict(
                registration_id=m.registration_id,
                thread_id=m.thread_id,
                title=m.title,
                project=m.project,
            )
            for m in source.members
        ]
        if isinstance(source, ReportScope)
        else [],
    )


def source_from(value):
    validate_source_metadata(value)
    members = value['members']
    if value['kind'] == 'selected' and not members:
        return Handoff(value['source_id'], value['source_title'], (), False)
    result = [ReportMember(**m) for m in members]
    return ReportScope(0, tuple(result), value['source_id'], value['source_title'])


def payload(progress):
    if not isinstance(progress, ReportProgress):
        raise ValueError('No current report progress')
    entries = []
    for entry in progress.entries.values():
        item = {k: deepcopy(v) for k, v in entry.item.items() if k in ITEM_FIELDS | MEMBER_FIELDS}
        item['evidence'] = {k: entry.item['evidence'][k] for k in EVIDENCE_FIELDS}
        entries.append(dict(item=item, delivered=entry.delivered == entry.fingerprint))
    return dict(
        source=describe(progress.source),
        start=progress.start,
        end=progress.end,
        entries=entries,
    )


def validate_record(item):
    from report_voice import ReportVoice

    if not isinstance(item, dict) or set(item) != {
        'id',
        'saved_at',
        'source',
        'start',
        'end',
        'entries',
    }:
        raise ValueError('Invalid report progress record')
    token(item['id'])
    stamp(item['saved_at'])
    source = source_from(item['source'])
    ReportWindow(item['start'], item['end'])
    rows = item['entries']
    if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
        raise ValueError('Invalid report progress entries')
    seen = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {'item', 'delivered'}
            or type(row['delivered']) is not bool
        ):
            raise ValueError('Invalid report delivery entry')
        result = row['item']
        fields = ITEM_FIELDS | MEMBER_FIELDS if isinstance(source, ReportScope) else ITEM_FIELDS
        if (
            not isinstance(result, dict)
            or set(result) != fields
            or result['result_kind'] != 'quoted_final_answer'
        ):
            raise ValueError('Invalid report delivery result')
        if not isinstance(result['evidence'], dict) or set(result['evidence']) != EVIDENCE_FIELDS:
            raise ValueError('Invalid report delivery evidence')
        text = result['excerpt']
        if not isinstance(text, str) or any(ord(c) < 32 and c != '\n' for c in text):
            raise ValueError('Invalid report delivery excerpt')
        text.encode('utf-8')
        ident = key(result)
        if ident in seen:
            raise ValueError('Duplicate report delivery result')
        seen.add(ident)
    # Reuse the actual voice reader's namespace, range and evidence validation
    # in bounded chunks. Checkpoints do not claim current coverage or task state.
    for offset in range(0, max(1, len(rows)), MAX_CARDS):
        report = dict(
            source_id=source.source_id,
            source_title=source.source_title,
            window_start=item['start'],
            window_end=item['end'],
            items=[r['item'] for r in rows[offset : offset + MAX_CARDS]],
            coverage_complete=False,
            empty_confirmed=False,
            failed_turns=0,
            interrupted_turns=0,
            active_turns_seen=0,
            unknown_completion_time=0,
            missing_final_answer=0,
        )
        if isinstance(source, ReportScope):
            report.update(
                source='codex_registered',
                registry_revision=0,
                items_truncated=False,
                unavailable_sources=0,
                sources=[
                    dict(
                        provider='codex',
                        host='local',
                        thread_id=m.thread_id,
                        title=m.title,
                        project=m.project,
                        status='read',
                        coverage_complete=False,
                        active_turns_seen=0,
                        failed_turns=0,
                        interrupted_turns=0,
                    )
                    for m in source.members
                ],
            )
        if not ReportVoice.valid_report(source, report):
            raise ValueError('Unconfirmed saved report progress')
    return item


def record(progress, *, clock=time.time):
    return validate_record(dict(id=uuid.uuid4().hex, saved_at=int(clock()), **payload(progress)))


def restore(item, source):
    validate_record(item)
    if not matches(source, item['source']):
        raise ValueError('Select the exact saved source or scope before restoring')
    result = ReportProgress.__new__(ReportProgress)
    result.source, result.epoch = source, uuid.uuid4().hex
    result.start, result.end = item['start'], item['end']
    result.entries = OrderedDict()
    for row in item['entries']:
        value = deepcopy(row['item'])
        fingerprint = version(value)
        result.entries[key(value)] = Entry(
            value, fingerprint, fingerprint if row['delivered'] else None
        )
    return result


def matches(source, saved):
    """A scope hash includes unrelated registry revisions; membership is its real identity."""
    if not isinstance(source, (Handoff, ReportScope)):
        return False
    active = describe(source)
    if active['kind'] == 'registered' and saved.get('kind') == 'registered':
        active, saved = deepcopy(active), deepcopy(saved)
        active.pop('source_id')
        saved.pop('source_id')
        active['members'].sort(key=lambda row: row['registration_id'])
        saved['members'].sort(key=lambda row: row['registration_id'])
    return active == saved


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) != {'schema', 'revision', 'items'}
        or type(value['schema']) is not int
        or value['schema'] != 1
        or type(value['revision']) is not int
        or not 0 <= value['revision'] <= REVISION_LIMIT
        or not isinstance(value['items'], list)
        or len(value['items']) > LIMIT
    ):
        raise ValueError('Invalid report progress store')
    for item in value['items']:
        validate_record(item)
    if len({r['id'] for r in value['items']}) != len(value['items']):
        raise ValueError('Duplicate report progress checkpoint')
    return value


class ReportCheckpoints:
    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()
        self.writer = ReportSources(path)

    def read(self, disk):
        try:
            value = disk.load()
        except RecursionError:
            raise ValueError('Report progress nesting exceeds limit') from None
        return dict(schema=1, revision=0, items=[]) if value is None else validate(value)

    def snapshot(self):
        with self.lock:
            return deepcopy(self.read(ReminderStore(self.path)))

    @contextmanager
    def selected(self, ident, revision, current):
        token(ident)
        with self.lock:
            disk = ReminderStore(self.path)
            with self.writer.writer(disk, lock_name='.work-report-progress.lock'):
                state = self.read(disk)
                if type(revision) is not int or state['revision'] != revision or not current():
                    raise ValueError('Saved progress revision or owner changed')
                item = next((r for r in state['items'] if r['id'] == ident), None)
                if item is None:
                    raise ValueError('Saved progress no longer exists')
                yield deepcopy(item)

    def apply(self, action, revision, *, item=None, ident=None, current=lambda: True):
        if (
            action not in ('save', 'delete', 'clear')
            or type(revision) is not int
            or not 0 <= revision < REVISION_LIMIT
        ):
            raise ValueError('Invalid report progress mutation')
        if action == 'save':
            validate_record(item)
        elif item is not None:
            raise ValueError('Unexpected report progress body')
        if action == 'delete':
            token(ident)
        elif ident is not None:
            raise ValueError('Unexpected report progress ID')
        with self.lock:
            disk = ReminderStore(self.path)
            with self.writer.writer(disk, lock_name='.work-report-progress.lock'):
                value = self.read(disk)
                if revision != value['revision'] or not current():
                    raise ValueError('Report progress revision or owner changed')
                if action == 'save':
                    if len(value['items']) >= LIMIT or any(
                        r['id'] == item['id'] for r in value['items']
                    ):
                        raise ValueError('Report progress capacity or duplicate')
                    value['items'].append(deepcopy(item))
                elif action == 'delete':
                    rows = [r for r in value['items'] if r['id'] != ident]
                    if len(rows) == len(value['items']):
                        raise ValueError('Report progress no longer exists')
                    value['items'] = rows
                else:
                    value['items'] = []
                value['revision'] += 1
                validate(value)
                if not current():
                    raise ValueError('Report progress owner changed')
                disk.save(value)
                return deepcopy(value)

    @staticmethod
    def listing(state):
        return dict(
            revision=state['revision'],
            limit=LIMIT,
            max_bytes=MAX_BYTES,
            items=[
                dict(
                    id=r['id'],
                    source_title=r['source']['source_title'],
                    saved_at=r['saved_at'],
                    start=r['start'],
                    end=r['end'],
                    total=len(r['entries']),
                    delivered=sum(e['delivered'] for e in r['entries']),
                )
                for r in reversed(state['items'])
            ],
        )

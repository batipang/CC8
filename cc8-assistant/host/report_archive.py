"""Explicit bounded report excerpts and identity indexes; never restore voice or run work."""

from copy import deepcopy
import threading
import time
import uuid

from reminder_store import ReminderStore, MAX_BYTES
from report_sources import ReportSources, ReportScope
from source_identity import identifier, title
from work_reports import ReportWindow, MAX_CARDS
from dot_handoff import Handoff
from report_archive_protocol import token

LIMIT = 16
REVISION_LIMIT = 2**31 - 1


def stamp(value):
    if type(value) is not int or not 0 <= value < 253402300799:
        raise ValueError('Invalid saved report time')


def label(value):
    if not isinstance(value, str) or title(value) != value:
        raise ValueError('Invalid saved report label')


def validate_record(item):
    if not isinstance(item, dict) or set(item) != {
        'id',
        'provider',
        'host',
        'snapshot_id',
        'saved_at',
        'source_title',
        'window_start',
        'window_end',
        'coverage_complete',
        'text',
        'sources',
        'index',
    }:
        raise ValueError('Invalid saved report fields')
    token(item['id'])
    if item['provider'] != 'codex' or item['host'] != 'local':
        raise ValueError('Unsupported saved report origin')
    token(item['snapshot_id'])
    stamp(item['saved_at'])
    ReportWindow(item['window_start'], item['window_end'])
    label(item['source_title'])
    text = item['text']
    if (
        type(item['coverage_complete']) is not bool
        or not isinstance(text, str)
        or not 0 < len(text) <= 24000
        or any(ord(c) < 32 and c not in '\n\t' for c in text)
    ):
        raise ValueError('Invalid saved report excerpt')
    text.encode('utf-8')
    sources = item['sources']
    if not isinstance(sources, list) or not 0 < len(sources) <= 8:
        raise ValueError('Invalid saved report sources')
    threads = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {'thread_id', 'title', 'project'}:
            raise ValueError('Invalid saved report source fields')
        identifier(source['thread_id'])
        label(source['title'])
        label(source['project'])
        if source['thread_id'] in threads:
            raise ValueError('Duplicate saved report source')
        threads.add(source['thread_id'])
    if not isinstance(item['index'], list) or len(item['index']) > MAX_CARDS:
        raise ValueError('Invalid saved report result index')
    seen = set()
    for result in item['index']:
        if not isinstance(result, dict) or set(result) != {
            'thread_id',
            'turn_id',
            'item_id',
            'completed_at',
        }:
            raise ValueError('Invalid saved result fields')
        for key in ('thread_id', 'turn_id', 'item_id'):
            identifier(result[key])
        ident = tuple(result[k] for k in ('thread_id', 'turn_id', 'item_id'))
        if (
            ident in seen
            or result['thread_id'] not in threads
            or type(result['completed_at']) is not int
            or not item['window_start'] <= result['completed_at'] < item['window_end']
        ):
            raise ValueError('Invalid saved result identity or completion time')
        seen.add(ident)
    return item


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
        raise ValueError('Invalid saved report store')
    for item in value['items']:
        validate_record(item)
    if any(
        len({r[k] for r in value['items']}) != len(value['items']) for k in ('id', 'snapshot_id')
    ):
        raise ValueError('Duplicate saved report identity')
    return value


def record(source, report, *, clock=time.time):
    # Project an actual validated server snapshot, never arbitrary device text.
    # No raw chat, model history, credentials or extra provider fields survive.
    from report_voice import ReportVoice, report_text

    if not isinstance(source, (Handoff, ReportScope)) or not ReportVoice.valid_report(
        source, report
    ):
        raise ValueError('Unconfirmed report cannot be saved')
    sources = (
        [dict(thread_id=m.thread_id, title=m.title, project=m.project) for m in source.members]
        if isinstance(source, ReportScope)
        else [dict(thread_id=source.source_id, title=source.source_title, project='未记录项目')]
    )
    item = dict(
        id=uuid.uuid4().hex,
        provider='codex',
        host='local',
        snapshot_id=token(report.get('snapshot_id')),
        saved_at=int(clock()),
        source_title=report['source_title'],
        window_start=report['window_start'],
        window_end=report['window_end'],
        coverage_complete=report['coverage_complete'],
        text=report_text(report),
        sources=sources,
        index=[
            dict(
                thread_id=r.get('source_thread_id', source.source_id),
                turn_id=r['turn_id'],
                item_id=r['item_id'],
                completed_at=r['completed_at'],
            )
            for r in report['items']
        ],
    )
    return validate_record(item)


class ReportArchive:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.writer = ReportSources(path)

    def read(self, disk):
        try:
            value = disk.load()
        except RecursionError:
            raise ValueError('Saved report nesting exceeds limit') from None
        return dict(schema=1, revision=0, items=[]) if value is None else validate(value)

    def snapshot(self):
        with self.lock:
            return deepcopy(self.read(ReminderStore(self.path)))

    def apply(self, action, revision, *, item=None, ident=None, current=lambda: True):
        if (
            action not in ('save', 'delete', 'clear')
            or type(revision) is not int
            or not 0 <= revision < REVISION_LIMIT
        ):
            raise ValueError('Invalid saved report mutation')
        if action == 'save':
            validate_record(item)
        elif item is not None:
            raise ValueError('Unexpected saved report body')
        if action == 'delete':
            token(ident)
        elif ident is not None:
            raise ValueError('Unexpected saved report ID')
        with self.lock:
            disk = ReminderStore(self.path)
            with self.writer.writer(disk, lock_name='.work-report-archive.lock'):
                value = self.read(disk)
                if revision != value['revision'] or not current():
                    raise ValueError('Saved report revision or owner changed')
                if action == 'save':
                    if len(value['items']) >= LIMIT:
                        raise ValueError('Saved report capacity reached')
                    if any(r['snapshot_id'] == item['snapshot_id'] for r in value['items']):
                        raise ValueError('Report already saved; refresh instead of retrying')
                    value['items'].append(deepcopy(item))
                elif action == 'delete':
                    rows = [r for r in value['items'] if r['id'] != ident]
                    if len(rows) == len(value['items']):
                        raise ValueError('Saved report no longer exists')
                    value['items'] = rows
                else:
                    value['items'] = []
                value['revision'] += 1
                validate(value)
                if not current():
                    raise ValueError('Saved report owner changed')
                # Includes a total 256 KiB quota; never silently evict an old result.
                disk.save(value)
                return deepcopy(value)

    @staticmethod
    def listing(state):
        return dict(
            revision=state['revision'],
            limit=LIMIT,
            max_bytes=MAX_BYTES,
            items=[
                {
                    k: r[k]
                    for k in (
                        'id',
                        'source_title',
                        'saved_at',
                        'window_start',
                        'window_end',
                        'coverage_complete',
                    )
                }
                for r in reversed(state['items'])
            ],
        )


def historical_text(item):
    validate_record(item)
    from datetime import datetime
    from work_reports import BEIJING

    saved = datetime.fromtimestamp(item['saved_at'], BEIJING).strftime('%Y-%m-%d %H:%M:%S')
    origins = '\n'.join(
        f'{s["title"]} · {s["project"]} · {s["thread_id"]}' for s in item['sources']
    )
    identities = '\n'.join(
        f'{i + 1}. {r["thread_id"]} / {r["turn_id"]} / {r["item_id"]}'
        for i, r in enumerate(item['index'])
    )
    return (
        '已保存的历史快照 · 非实时结果\n保存时间（北京时间）：'
        + saved
        + '\n本机 Codex 来源：\n'
        + origins
        + '\n不会恢复编号语音上下文、已播账本、自动播报或执行原任务。\n\n'
        + item['text']
        + '\n\n结果索引（原聊天 / 回合 / 结果）：\n'
        + (identities or '本次未保存结果条目，仍须结合覆盖提示判断。')
    )

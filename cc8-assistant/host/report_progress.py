"""Bounded report delivery ledger, distinct from source-task completion."""

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import uuid
from dot_handoff import identifier
from work_reports import MAX_CARDS

MAX_RECORDS = 256
VERSION_FIELDS = (
    'completed_at',
    'excerpt',
    'excerpt_truncated',
    'evidence',
    'evidence_complete',
    'verification',
)
SINCE_NOTES = {
    'unheard': '上次未确认播完，本次补播。',
    'cached_unheard': '上次未确认播完，引用上次已读快照；当前尚未重新核验。',
    'late_collected': '在上次截止前已完成，本次才收集到。',
    'updated': '原结果有更新，本次重新汇报。',
    'new': '上次截止后完成的新结果。',
}


class ReportCapacityError(ValueError):
    pass


def key(item):
    local = identifier(item['turn_id']), identifier(item['item_id'])
    if 'source_thread_id' in item:
        if item.get('source_host') != 'local' or item.get('source_provider') != 'codex':
            raise ValueError('Invalid report source namespace')
        return ('local', identifier(item['source_thread_id']), *local)
    return local


def version(item):
    return hashlib.sha256(
        json.dumps(
            {name: item[name] for name in VERSION_FIELDS},
            sort_keys=True,
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode()
    ).hexdigest()


@dataclass
class Entry:
    item: dict
    fingerprint: str
    delivered: str | None = None


class ReportProgress:
    def __init__(self, source, report):
        self.source = source
        self.epoch = uuid.uuid4().hex
        self.start, self.end = report['window_start'], report['window_end']
        self.entries = OrderedDict()
        self.collect(report)

    def collect(self, report):
        incoming = [(key(item), item) for item in report['items']]
        if len({ident for ident, _ in incoming}) != len(incoming):
            raise ValueError('Duplicate report item')
        if len(set(self.entries) | {ident for ident, _ in incoming}) > MAX_RECORDS:
            raise ReportCapacityError('Report delivery ledger capacity exceeded')
        for ident, item in incoming:
            fingerprint = version(item)
            entry = self.entries.get(ident)
            delivered = entry.delivered if entry else None
            if entry and entry.fingerprint == entry.delivered and fingerprint != entry.fingerprint:
                self.entries.pop(ident)
            self.entries[ident] = Entry(deepcopy(item), fingerprint, delivered)

    def merge(self, source_report):
        # Work on a copy: timeout/cancellation/capacity errors cannot move the
        # previous baseline or discard an unheard result.
        if (
            source_report['window_start'] != self.start
            or source_report['window_end'] < self.end
            or source_report['source_id'] != self.source.source_id
            or source_report['source_title'] != self.source.source_title
        ):
            raise ValueError('Report source or range changed')
        result = ReportProgress.__new__(ReportProgress)
        result.source, result.epoch = self.source, self.epoch
        result.start, result.end = self.start, source_report['window_end']
        result.entries = deepcopy(self.entries)
        result.collect(source_report)
        fresh = {key(item): version(item) for item in source_report['items']}
        pending = [
            (ident, entry)
            for ident, entry in result.entries.items()
            if entry.fingerprint != entry.delivered
        ]
        report = deepcopy(source_report)
        report['items'] = []
        cached = False
        for ident, entry in pending[:MAX_CARDS]:
            item = deepcopy(entry.item)
            old = self.entries.get(ident)
            if ident not in fresh:
                label, cached = 'cached_unheard', True
            elif old is not None and old.fingerprint != entry.fingerprint:
                label = 'updated'
            elif old is not None:
                label = 'unheard'
            elif item['completed_at'] < self.end:
                label = 'late_collected'
            else:
                label = 'new'
            item['since_status'] = label
            report['items'].append(item)
        report.update(
            view='since_last_unheard',
            since_last_end=self.end,
            source_completed_items_read=len(source_report['items']),
            delivered_items_skipped=sum(
                result.entries[ident].delivered == fingerprint
                for ident, fingerprint in fresh.items()
            ),
            pending_items=len(pending),
        )
        report['coverage_complete'] = (
            source_report['coverage_complete'] and not cached and len(pending) <= MAX_CARDS
        )
        report['empty_confirmed'] = report['coverage_complete'] and not report['items']
        return result, report

    def confirm(self, ident, fingerprint):
        entry = self.entries.get(ident)
        if entry is not None and entry.fingerprint == fingerprint:
            entry.delivered = fingerprint

    def status(self):
        pending = sum(entry.fingerprint != entry.delivered for entry in self.entries.values())
        return dict(
            ready=True,
            pending=pending,
            summary_delivered=len(self.entries) - pending,
            baseline_start=self.start,
            last_read_end=self.end,
            persisted=False,
        )


@dataclass(frozen=True)
class DeliveryToken:
    plan: str
    index: int


class DeliveryPlan:
    def __init__(self, progress, requirements):
        self.ident, self.epoch = uuid.uuid4().hex, progress.epoch
        self.requirements = requirements
        self.confirmed = set()

    def token(self, index):
        return (
            DeliveryToken(self.ident, index)
            if any(index in indices for indices in self.requirements.values())
            else None
        )

    def confirm(self, token, progress):
        if (
            not isinstance(token, DeliveryToken)
            or type(token.index) is not int
            or token.plan != self.ident
            or progress.epoch != self.epoch
            or not any(token.index in indices for indices in self.requirements.values())
        ):
            return
        self.confirmed.add(token.index)
        for (ident, fingerprint), required in self.requirements.items():
            if required <= self.confirmed:
                progress.confirm(ident, fingerprint)

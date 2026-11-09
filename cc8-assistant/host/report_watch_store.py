"""Explicit private report subscriptions and once-only delivery claims; no report bodies."""

from copy import deepcopy
import threading
import time
import uuid

from reminder_store import ReminderStore
from report_sources import ReportSources
from report_checkpoint import source_from
from report_archive import stamp
from report_archive_protocol import token, REVISION_LIMIT
from source_identity import identifier
from work_reports import ReportWindow

PERIODS = (300, 900, 1800)
READER_VERSION = 2
MAX_SEEN = 256
MAX_NOTICE = 3
CHECK_STATES = frozenset(
    (
        'empty',
        'new',
        'partial',
        'read_error',
        'source_changed',
        'capacity',
        'clock_error',
        'age_limit',
        'claimed',
        'delivered',
        'delivery_unconfirmed',
    )
)
DETAILS = frozenset(
    (
        'completed',
        'notification_center',
        'cancelled',
        'failed',
        'unconfirmed',
        'not_authorized',
        'unavailable',
    )
)


def pair(value, source):
    if not isinstance(value, dict) or set(value) != {'key', 'fingerprint'}:
        raise ValueError('Invalid report subscription result identity')
    key = value['key']
    if not isinstance(key, list) or len(key) != (4 if source['kind'] == 'registered' else 2):
        raise ValueError('Invalid report subscription namespace')
    if len(key) == 4:
        identifier(key[1])
        if key[0] != 'local' or key[1] not in {m['thread_id'] for m in source['members']}:
            raise ValueError('Report result is outside the subscription scope')
    for component in key[-2:]:
        identifier(component)
    token(value['fingerprint'], 64)
    return tuple(key), value['fingerprint']


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) != {'schema', 'revision', 'watch'}
        or type(value['schema']) is not int
        or value['schema'] not in (1, READER_VERSION)
        or type(value['revision']) is not int
        or not 0 <= value['revision'] <= REVISION_LIMIT
    ):
        raise ValueError('Invalid report subscription store')
    watch = value['watch']
    if watch is None:
        return value
    expected = {
        'id',
        'source',
        'started_at',
        'period_seconds',
        'channel',
        'enabled',
        'seen',
        'attempt',
        'last_check',
    }
    if value['schema'] == 2:
        expected.add('mode')
    if not isinstance(watch, dict) or set(watch) != expected:
        raise ValueError('Invalid report subscription fields')
    token(watch['id'])
    source_from(watch['source'])
    stamp(watch['started_at'])
    if (
        type(watch['enabled']) is not bool
        or type(watch['period_seconds']) is not int
        or watch['period_seconds'] not in PERIODS
        or watch['channel'] not in ('cc8', 'desktop')
        or value['schema'] == 2
        and (
            watch['mode'] not in ('count', 'summary')
            or watch['mode'] == 'summary'
            and watch['channel'] != 'cc8'
        )
    ):
        raise ValueError('Invalid report subscription settings')
    seen = watch['seen']
    if not isinstance(seen, list) or len(seen) > MAX_SEEN:
        raise ValueError('Report subscription index exceeds limit')
    identities = [pair(row, watch['source']) for row in seen]
    if len(set(identities)) != len(identities):
        raise ValueError('Duplicate report subscription delivery')
    attempt = watch['attempt']
    if attempt is not None:
        if not isinstance(attempt, dict) or set(attempt) != {
            'id',
            'claimed_at',
            'read_end',
            'items',
            'status',
            'detail',
        }:
            raise ValueError('Invalid report subscription attempt')
        token(attempt['id'])
        stamp(attempt['claimed_at'])
        ReportWindow(watch['started_at'], attempt['read_end'])
        if (
            attempt['status'] not in ('claimed', 'unconfirmed')
            or not isinstance(attempt['detail'], str)
            or attempt['detail'] not in DETAILS
            or attempt['read_end'] > attempt['claimed_at']
            or not isinstance(attempt['items'], list)
            or not 0 < len(attempt['items']) <= MAX_NOTICE
        ):
            raise ValueError('Invalid report subscription delivery claim')
        pending = [pair(row, watch['source']) for row in attempt['items']]
        if (
            len(set(pending)) != len(pending)
            or set(pending) & set(identities)
            or len(identities) + len(pending) > MAX_SEEN
        ):
            raise ValueError('Duplicate or full report subscription claim')
    check = watch['last_check']
    if check is not None:
        if not isinstance(check, dict) or set(check) != {'at', 'status', 'confirmed_new'}:
            raise ValueError('Invalid report subscription observation')
        stamp(check['at'])
        if (
            not isinstance(check['status'], str)
            or check['status'] not in CHECK_STATES
            or check['confirmed_new'] is not None
            and (type(check['confirmed_new']) is not int or not 0 <= check['confirmed_new'] <= 20)
        ):
            raise ValueError('Invalid report subscription observation fields')
        if check['status'] == 'empty' and check['confirmed_new'] != 0:
            raise ValueError('Empty report subscription requires confirmed zero')
        if check['status'] in ('new', 'claimed', 'delivered', 'delivery_unconfirmed') and (
            check['confirmed_new'] is None or check['confirmed_new'] < 1
        ):
            raise ValueError('New subscription results require a positive confirmed count')
        if (
            check['status'] not in ('empty', 'new', 'claimed', 'delivered', 'delivery_unconfirmed')
            and check['confirmed_new'] is not None
        ):
            raise ValueError('Incomplete subscription read cannot claim zero results')
    return value


class ReportWatchStore:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock, self.lock = path, clock, threading.RLock()
        self.writer = ReportSources(path)

    def read(self, disk):
        try:
            value = disk.load()
        except RecursionError:
            raise ValueError('Report subscription nesting exceeds limit') from None
        return dict(schema=1, revision=0, watch=None) if value is None else validate(value)

    def snapshot(self):
        with self.lock:
            return deepcopy(self.read(ReminderStore(self.path)))

    def _change(self, revision, change, current, *, allow_any_revision=False):
        if revision is None and not allow_any_revision:
            raise ValueError('Explicit report subscription revision required')
        if revision is not None and (
            type(revision) is not int or not 0 <= revision < REVISION_LIMIT
        ):
            raise ValueError('Invalid report subscription revision')
        with self.lock:
            disk = ReminderStore(self.path)
            with self.writer.writer(disk, lock_name='.work-report-watch.lock'):
                value = self.read(disk)
                if (revision is not None and value['revision'] != revision) or not current():
                    raise ValueError('Report subscription owner or revision changed')
                if value['revision'] >= REVISION_LIMIT:
                    raise ValueError('Report subscription revision exhausted')
                if change(value) is False:
                    return None
                value['revision'] += 1
                validate(value)
                if not current():
                    raise ValueError('Report subscription owner changed')
                disk.save(value)
                return deepcopy(value)

    def configure(
        self, revision, source, period_seconds, channel, *, mode='count', current=lambda: True
    ):
        source_from(source)
        if (
            type(period_seconds) is not int
            or period_seconds not in PERIODS
            or channel not in ('cc8', 'desktop')
            or mode not in ('count', 'summary')
            or mode == 'summary'
            and channel != 'cc8'
        ):
            raise ValueError('Invalid report subscription interval or channel')
        watch = dict(
            id=uuid.uuid4().hex,
            source=deepcopy(source),
            started_at=int(self.clock()),
            period_seconds=period_seconds,
            channel=channel,
            enabled=True,
            seen=[],
            attempt=None,
            last_check=None,
        )

        def change(value):
            value['watch'] = deepcopy(watch)
            # Legacy count-only stores remain byte-format compatible. Upgrade
            # only on explicit summary consent, never as a read/startup migration.
            value['schema'] = 2 if mode == 'summary' else 1
            if value['schema'] == 2:
                value['watch']['mode'] = mode

        return self._change(revision, change, current)

    def part(self, watch_id, attempt_id, item):
        """A whole literal item received playback ACKs; never infer it from job completion."""
        token(watch_id)
        token(attempt_id)

        def change(value):
            watch = value['watch']
            if watch is None or watch['id'] != watch_id or watch.get('mode') != 'summary':
                return False
            attempt = watch['attempt']
            if attempt is None or attempt['id'] != attempt_id or attempt['status'] != 'claimed':
                return False
            identity = pair(item, watch['source'])
            matches = [row for row in attempt['items'] if pair(row, watch['source']) == identity]
            if not matches:
                return False
            watch['seen'].append(deepcopy(matches[0]))
            attempt['items'].remove(matches[0])
            if not attempt['items']:
                watch['attempt'] = None
                watch['last_check'].update(at=int(self.clock()), status='delivered')

        return self._change(None, change, lambda: True, allow_any_revision=True)

    def pause(self, revision, *, current=lambda: True):
        def change(value):
            if value['watch'] is None:
                raise ValueError('No report subscription configured')
            value['watch']['enabled'] = False

        return self._change(revision, change, current)

    def clear(self, revision, *, current=lambda: True):
        def change(value):
            value['watch'] = None
            # Explicit deletion removes every summary index/claim as requested;
            # an empty legacy document is safe for the original reader again.
            value['schema'] = 1

        return self._change(revision, change, current)

    def checked(self, revision, status, *, confirmed_new=None, current=lambda: True):
        if status in ('claimed', 'delivered', 'delivery_unconfirmed'):
            raise ValueError('A read observation cannot settle report delivery')

        def change(value):
            watch = value['watch']
            if watch is None or not watch['enabled'] or watch['attempt'] is not None:
                raise ValueError('Report subscription is not ready for a read receipt')
            watch['last_check'] = dict(
                at=int(self.clock()), status=status, confirmed_new=confirmed_new
            )
            if status in ('source_changed', 'capacity', 'clock_error', 'age_limit'):
                watch['enabled'] = False

        return self._change(revision, change, current)

    def claim(self, revision, items, read_end, *, current=lambda: True):
        def change(value):
            watch = value['watch']
            if watch is None or not watch['enabled'] or watch['attempt'] is not None:
                raise ValueError('Report subscription cannot send another notice')
            watch['attempt'] = dict(
                id=uuid.uuid4().hex,
                claimed_at=int(self.clock()),
                read_end=read_end,
                items=deepcopy(items),
                status='claimed',
                detail='unconfirmed',
            )
            watch['last_check'] = dict(
                at=int(self.clock()), status='claimed', confirmed_new=len(items)
            )

        return self._change(revision, change, current)

    def settle(
        self, watch_id, attempt_id, completed, *, detail='unconfirmed', current=lambda: True
    ):
        token(watch_id)
        token(attempt_id)
        if type(completed) is not bool or not isinstance(detail, str) or detail not in DETAILS:
            raise ValueError('Invalid report subscription delivery receipt')

        def change(value):
            watch = value['watch']
            if watch is None or watch['id'] != watch_id:
                return False
            attempt = watch['attempt']
            if attempt is None or attempt['id'] != attempt_id or attempt['status'] != 'claimed':
                return False
            if completed and detail != (
                'completed' if watch['channel'] == 'cc8' else 'notification_center'
            ):
                raise ValueError('Report notice lacks a matching channel delivery receipt')
            if completed and watch.get('mode') == 'summary':
                # Only part() may mark summary items heard. An apparently
                # successful job with missing item ACKs is still uncertain.
                completed_receipt = False
                receipt_detail = 'unconfirmed'
            else:
                completed_receipt = completed
                receipt_detail = detail
            if completed_receipt:
                watch['seen'].extend(deepcopy(attempt['items']))
                watch['attempt'] = None
            else:
                attempt.update(status='unconfirmed', detail=receipt_detail)
                watch['enabled'] = False
            watch['last_check'] = dict(
                at=int(self.clock()),
                status='delivered' if completed_receipt else 'delivery_unconfirmed',
                confirmed_new=len(attempt['items']),
            )

        return self._change(None, change, current, allow_any_revision=True)

"""Explicit owner-private report-source metadata; never retain chat bodies or run work."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Callable, Iterator, TypedDict, cast
import uuid

from reminder_store import ReminderStore
from source_identity import identifier, title

MAX_SOURCES = 8
REVISION_LIMIT = 2**31 - 1
REGISTRY_ACTIONS = frozenset(
    (
        'sources',
        'source_candidates',
        'register_source',
        'delete_source',
        'clear_sources',
        'use_sources',
        'use_selected_report',
        'source_queue',
    )
)
MUTATIONS = frozenset(('register_source', 'delete_source', 'clear_sources'))


def validate_request(value: object) -> None:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get('action'), str)
        or value['action'] not in REGISTRY_ACTIONS
    ):
        raise ValueError('Invalid report sources request')
    action = value['action']
    fields = {'event', 'request_id', 'action'}
    if action == 'source_candidates':
        fields |= {'cursor'} if 'cursor' in value else set()
        if 'cursor' in value and (
            not isinstance(value['cursor'], str) or not 0 < len(value['cursor']) <= 2048
        ):
            raise ValueError('Invalid report sources cursor')
    if action in MUTATIONS or action in ('use_sources', 'source_queue'):
        fields |= {'revision'}
        if type(value.get('revision')) is not int or not 0 <= value['revision'] < REVISION_LIMIT:
            raise ValueError('Invalid report sources revision')
    if action in MUTATIONS:
        fields |= {'confirmed'}
        if value.get('confirmed') is not True:
            raise ValueError('Explicit source metadata consent required')
    if action == 'register_source':
        fields |= {'source_id'}
        identifier(value.get('source_id'))
    if action in ('delete_source', 'source_queue'):
        fields |= {'registration_id'}
        if not isinstance(value.get('registration_id'), str) or not re.fullmatch(
            '[a-f0-9]{16}', value['registration_id']
        ):
            raise ValueError('Invalid registered source ID')
    if (
        set(value) != fields
        or value.get('event') != 'dot_chat_request'
        or not isinstance(value.get('request_id'), str)
        or not re.fullmatch('[a-f0-9]{32}', value['request_id'])
    ):
        raise ValueError('Invalid report sources request fields')


class SourceMetadata(TypedDict):
    provider: str
    host: str
    thread_id: str
    title: str
    project: str


class RegisteredSource(SourceMetadata):
    id: str
    registered_at: int


class SourcesState(TypedDict):
    schema: int
    revision: int
    items: list[RegisteredSource]


def metadata(value: object) -> SourceMetadata:
    if not isinstance(value, dict) or set(value) != {
        'provider',
        'host',
        'thread_id',
        'title',
        'project',
    }:
        raise ValueError('Invalid report source metadata')
    if value['provider'] != 'codex' or value['host'] != 'local':
        raise ValueError('Report source is not connected')
    identifier(value['thread_id'])
    for key in ('title', 'project'):
        if not isinstance(value[key], str) or title(value[key]) != value[key]:
            raise ValueError('Invalid report source display name')
    return cast(SourceMetadata, value)


def validate(value: object) -> SourcesState:
    if (
        not isinstance(value, dict)
        or set(value) != {'schema', 'revision', 'items'}
        or type(value['schema']) is not int
        or value['schema'] != 1
        or type(value['revision']) is not int
        or not 0 <= value['revision'] <= REVISION_LIMIT
        or not isinstance(value['items'], list)
        or len(value['items']) > MAX_SOURCES
    ):
        raise ValueError('Invalid registered report sources')
    ids, threads = set(), set()
    for row in value['items']:
        if (
            not isinstance(row, dict)
            or set(row)
            != {'provider', 'host', 'thread_id', 'title', 'project', 'id', 'registered_at'}
            or not isinstance(row['id'], str)
            or not re.fullmatch('[a-f0-9]{16}', row['id'])
            or type(row['registered_at']) is not int
            or not 0 <= row['registered_at'] <= 253402300799
        ):
            raise ValueError('Invalid registered source')
        metadata({k: v for k, v in row.items() if k not in ('id', 'registered_at')})
        key = row['host'], row['thread_id']
        if row['id'] in ids or key in threads:
            raise ValueError('Duplicate registered source')
        ids.add(row['id'])
        threads.add(key)
    return cast(SourcesState, value)


@dataclass(frozen=True)
class ReportMember:
    registration_id: str
    thread_id: str
    title: str
    project: str


@dataclass(frozen=True)
class ReportScope:
    revision: int
    members: tuple[ReportMember, ...]
    source_id: str
    source_title: str

    @classmethod
    def from_state(cls, state: object) -> 'ReportScope':
        value = validate(state)
        if not value['items']:
            raise ValueError('Register a report source first')
        members = tuple(
            ReportMember(r['id'], r['thread_id'], r['title'], r['project']) for r in value['items']
        )
        fingerprint = hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:32]
        return cls(
            value['revision'],
            members,
            'registered_' + fingerprint,
            f'已登记聊天（{len(members)}个）',
        )


class ReportSources:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path, self.clock = path, clock
        self.lock = threading.RLock()

    @staticmethod
    def read(disk: ReminderStore) -> SourcesState:
        try:
            value = disk.load()
        except RecursionError:
            raise ValueError('Registered sources nesting exceeds limit') from None
        return {'schema': 1, 'revision': 0, 'items': []} if value is None else validate(value)

    def snapshot(self) -> SourcesState:
        # An absent store stays absent until the owner's explicit mutation.
        with self.lock:
            return deepcopy(self.read(ReminderStore(self.path)))

    @contextmanager
    def writer(
        self, disk: ReminderStore, *, lock_name: str = '.work-report-sources.lock'
    ) -> Iterator[None]:
        if lock_name not in ('.work-report-sources.lock', '.work-report-archive.lock'):
            raise ValueError('Invalid report store lock')
        with disk.directory(create=True) as directory:
            flags = os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW
            created = False
            try:
                fd = os.open(
                    lock_name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory,
                )
                created = True
            except FileExistsError:
                fd = os.open(lock_name, flags, dir_fd=directory)
            try:
                if created:
                    os.fchmod(fd, 0o600)
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise OSError('Unsafe registered sources lock')
                deadline = time.monotonic() + 1
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Registered sources store busy') from None
                        time.sleep(0.01)
                yield
            finally:
                os.close(fd)

    def apply(
        self,
        action: str,
        revision: int,
        *,
        entry: object = None,
        ident: str | None = None,
        current: Callable[[], bool] = lambda: True,
    ) -> SourcesState:
        if (
            action not in ('register', 'delete', 'clear')
            or type(revision) is not int
            or not 0 <= revision < REVISION_LIMIT
        ):
            raise ValueError('Invalid registered source mutation')
        if action == 'register':
            item = metadata(entry)
            if ident is not None:
                raise ValueError('Unexpected registered source ID')
        elif entry is not None or action == 'clear' and ident is not None:
            raise ValueError('Unexpected registered source metadata')
        elif action == 'delete' and (
            not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{16}', ident)
        ):
            raise ValueError('Invalid registered source ID')
        with self.lock:
            disk = ReminderStore(self.path)
            with self.writer(disk):
                value = self.read(disk)
                if value['revision'] != revision or not current():
                    raise ValueError('Registered source revision or owner changed')
                if action == 'register':
                    if len(value['items']) >= MAX_SOURCES or any(
                        r['thread_id'] == item['thread_id'] for r in value['items']
                    ):
                        raise ValueError('Registered source capacity or duplicate')
                    row: RegisteredSource = {
                        **item,
                        'id': uuid.uuid4().hex[:16],
                        'registered_at': int(self.clock()),
                    }
                    value['items'].append(row)
                elif action == 'delete':
                    remaining = [r for r in value['items'] if r['id'] != ident]
                    if len(remaining) == len(value['items']):
                        raise ValueError('Registered source no longer exists')
                    value['items'] = remaining
                else:
                    value['items'] = []
                value['revision'] += 1
                validate(value)
                if not current():
                    raise ValueError('Registered source owner changed')
                # Preserve ReminderCommitUncertain; callers must query, never retry.
                disk.save(value)
                return deepcopy(value)

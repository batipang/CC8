"""Explicit local spelling references; never rewrite ASR or inject provider parameters."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import fcntl
import os
from pathlib import Path
import re
import stat
import threading
import time
from zoneinfo import ZoneInfo
from typing import Callable, Iterator, NotRequired, TypedDict, cast

from preference_protocol import field, REVISION_LIMIT
from reminder_store import ReminderStore

LIMIT = 64
ZONE = ZoneInfo('Asia/Shanghai')


class VocabularyEntry(TypedDict):
    term: str
    aliases: list[str]
    source: str
    created_at: str
    updated_at: str


class VocabularyState(TypedDict):
    schema: int
    revision: int
    items: list[VocabularyEntry]


class VocabularyRequest(TypedDict):
    event: str
    request_id: str
    action: str
    revision: NotRequired[int]
    term: NotRequired[str]
    aliases: NotRequired[list[str]]
    alias: NotRequired[str]
    source_id: NotRequired[str]


def word(value: object) -> str:
    value = field(value, 40)
    # The correction protocol uses a literal “new, not old” pair. A dictionary
    # entry must not smuggle another pair/utterance into that explicit action.
    if ',' in value or '，' in value or '不是' in value:
        raise ValueError('Word contains correction delimiters')
    return value


def aliases(value: object, term: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError('Invalid vocabulary aliases')
    values = [word(item) for item in value]
    folded = [item.casefold() for item in values]
    if len(set(folded)) != len(folded) or term in values:
        raise ValueError('Ambiguous vocabulary alias')
    return values


def validate_request(value: object) -> VocabularyRequest:
    shapes: dict[str, set[str]] = {
        'list': set(),
        'put': {'revision', 'term', 'aliases'},
        'delete': {'revision', 'term'},
        'clear': {'revision'},
        'prepare': {'revision', 'term', 'alias', 'source_id'},
    }
    if (
        not isinstance(value, dict)
        or value.get('event') != 'vocabulary_request'
        or not isinstance(value.get('action'), str)
        or value['action'] not in shapes
        or set(value) != {'event', 'request_id', 'action'} | shapes[value['action']]
        or not isinstance(value.get('request_id'), str)
        or not re.fullmatch('[a-f0-9]{32}', value['request_id'])
    ):
        raise ValueError('Invalid vocabulary request')
    action = value['action']
    if action != 'list' and (
        type(value['revision']) is not int or not 0 <= value['revision'] < REVISION_LIMIT
    ):
        raise ValueError('Invalid vocabulary revision')
    if action in ('put', 'delete', 'prepare'):
        term = word(value['term'])
        if action == 'put':
            aliases(value['aliases'], term)
        if action == 'prepare':
            word(value['alias'])
            if not isinstance(value['source_id'], str) or not re.fullmatch(
                '[a-f0-9]{32}', value['source_id']
            ):
                raise ValueError('Invalid correction source token')
    # The branch-specific key set and every optional value were checked above.
    return cast(VocabularyRequest, value)


def validate_state(value: object) -> VocabularyState:
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
        raise ValueError('Invalid vocabulary state')
    terms = set()
    for item in value['items']:
        if not isinstance(item, dict) or set(item) != {
            'term',
            'aliases',
            'source',
            'created_at',
            'updated_at',
        }:
            raise ValueError('Invalid vocabulary entry')
        term = word(item['term'])
        if term != item['term'] or term.casefold() in terms or item['source'] != 'device':
            raise ValueError('Invalid vocabulary provenance')
        if aliases(item['aliases'], term) != item['aliases']:
            raise ValueError('Noncanonical vocabulary aliases')
        dates = []
        for key in ('created_at', 'updated_at'):
            if not isinstance(item[key], str) or len(item[key]) > 40:
                raise ValueError('Invalid vocabulary date')
            date = datetime.fromisoformat(item[key])
            if date.tzinfo is None:
                raise ValueError('Vocabulary date needs timezone')
            dates.append(date)
        if dates[1] < dates[0]:
            raise ValueError('Vocabulary dates reversed')
        terms.add(term.casefold())
    # Nested entry/key/date/alias validation establishes this disk boundary.
    return cast(VocabularyState, value)


class VocabularyStore:
    def __init__(
        self, path: str | Path, *, clock: Callable[[], datetime] = lambda: datetime.now(ZONE)
    ) -> None:
        self.path, self.clock = Path(path), clock
        self.lock = threading.RLock()

    def read(self, disk: ReminderStore) -> VocabularyState:
        try:
            state = disk.load()
        except RecursionError:
            raise ValueError('Vocabulary nesting exceeds limit') from None
        return {'schema': 1, 'revision': 0, 'items': []} if state is None else validate_state(state)

    def snapshot(self) -> VocabularyState:
        with self.lock:
            return deepcopy(self.read(ReminderStore(self.path)))

    @contextmanager
    def writer(self, disk: ReminderStore) -> Iterator[None]:
        # Reuse the existing atomic/private file adapter. This separate bounded
        # lock serializes cross-process read/revision/write, not just rename.
        with disk.directory(create=True) as directory:
            fd = os.open(
                '.recognition-vocabulary.lock',
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_nlink != 1
                ):
                    raise OSError('Unsafe vocabulary lock')
                until = time.monotonic() + 1
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= until:
                            raise TimeoutError('Vocabulary is busy') from None
                        time.sleep(0.01)
                yield
            finally:
                os.close(fd)

    def apply(self, request: object) -> VocabularyState:
        request = validate_request(request)
        action = request['action']
        if action not in ('put', 'delete', 'clear'):
            raise ValueError('Vocabulary action does not write')
        with self.lock:
            disk = ReminderStore(self.path)
            with self.writer(disk):
                state = self.read(disk)
                if state['revision'] != request['revision']:
                    raise ValueError('Vocabulary revision changed')
                old = next(
                    (
                        item
                        for item in state['items']
                        if action != 'clear'
                        and item['term'].casefold() == word(request['term']).casefold()
                    ),
                    None,
                )
                if action == 'clear':
                    state['items'] = []
                elif action == 'delete':
                    if old is None:
                        raise ValueError('Vocabulary term missing')
                    state['items'].remove(old)
                else:
                    if old is None and len(state['items']) >= LIMIT:
                        raise ValueError('Vocabulary capacity reached')
                    now = self.clock().isoformat(timespec='seconds')
                    item: VocabularyEntry = dict(
                        term=word(request['term']),
                        aliases=aliases(request['aliases'], word(request['term'])),
                        source='device',
                        created_at=old['created_at'] if old else now,
                        updated_at=now,
                    )
                    if old is not None:
                        state['items'].remove(old)
                    state['items'].append(item)
                state['revision'] += 1
                validate_state(state)
                disk.save(state)
                return deepcopy(state)

    def pair(self, request: object) -> tuple[str, str]:
        request = validate_request(request)
        if request['action'] != 'prepare':
            raise ValueError('Not a correction request')
        state = self.snapshot()
        if state['revision'] != request['revision']:
            raise ValueError('Vocabulary changed')
        term, alias = word(request['term']), word(request['alias'])
        item = next((item for item in state['items'] if item['term'] == term), None)
        if item is None or alias not in item['aliases']:
            raise ValueError('Unknown vocabulary pair')
        return term, alias

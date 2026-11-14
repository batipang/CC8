"""Explicit terminal-task snapshots; lazy, bounded owner-private storage."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid
from zoneinfo import ZoneInfo
from result_provenance import validate as validate_provenance
from workspace import EXECUTION_UNCONFIRMED_STEP
from window_receipt import validate_window_receipt

LIMIT = 32
READER_VERSION = 3
MAX_BYTES = 512 * 1024
LOCK_WAIT_SECONDS = 2
TERMINAL = ('completed', 'failed', 'cancelled')
HANDOFF_DRAFT_LABEL = '\n\n本次识别到的下一步，请核对后复制：\n'


class SavedTaskCapacityError(ValueError):
    pass


def desktop_handoff_pending(item):
    return (
        item['status'] == 'completed'
        and item.get('provenance', {}).get('provider') == 'work_report'
        and any(
            step['id'] == 'desktop_handoff' and step['status'] == 'completed'
            for step in item['steps']
        )
    )


def desktop_handoff_source(item):
    """Resolve the source from a validated task receipt."""
    if not desktop_handoff_pending(item):
        return None
    provenance = item['provenance']
    source = provenance['original_source']
    if (
        source is None
        or provenance['turn_id'] is None
        or source['thread_id'] != provenance['thread_id']
    ):
        return None
    return source


def execution_unconfirmed(item):
    return item['status'] in ('failed', 'cancelled') and any(
        step['id'] == EXECUTION_UNCONFIRMED_STEP and step['status'] == item['status']
        for step in item['steps']
    )


def text(value, maximum):
    valid = (
        isinstance(value, str)
        and len(value) <= maximum
        and not any(ord(c) < 32 and c not in '\n\t' for c in value)
    )
    if valid:
        try:
            value.encode('utf-8')
        except UnicodeError:
            return False
    return valid


def ident(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{16}', value):
        raise ValueError('Invalid saved task ID')
    return value


def date(value):
    if (
        not isinstance(value, str)
        or len(value) > 40
        or datetime.fromisoformat(value).tzinfo is None
    ):
        raise ValueError('Invalid saved task date')


def validate(item):
    required = {
        'id',
        'title',
        'status',
        'result',
        'steps',
        'started_at',
        'saved_at',
    }
    if (
        not isinstance(item, dict)
        or not required <= set(item)
        or set(item) - required - {'provenance', 'finished_at', 'window_input'}
    ):
        raise ValueError('Invalid saved task fields')
    ident(item['id'])
    if (
        not text(item['title'], 80)
        or not text(item['result'], 1600)
        or item['status'] not in TERMINAL
    ):
        raise ValueError('Invalid saved task receipt')
    date(item['started_at'])
    date(item['saved_at'])
    if 'finished_at' in item:
        date(item['finished_at'])
    if 'provenance' in item:
        validate_provenance(item['provenance'])
    if 'window_input' in item:
        validate_window_receipt(item['window_input'])
    steps = item['steps']
    if not isinstance(steps, list) or len(steps) > 5:
        raise ValueError('Invalid saved task steps')
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'id', 'title', 'status'}:
            raise ValueError('Invalid saved step fields')
        if (
            not text(step['id'], 80)
            or not text(step['title'], 40)
            or step['status'] not in (*TERMINAL, 'running')
        ):
            raise ValueError('Invalid saved step receipt')
    return item


class SavedTasks:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    @contextmanager
    def directory(self, create=False):
        if create:
            self.path.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError('Saved task directory must be owner-private')
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def writer(self, directory):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        created = False
        try:
            fd = os.open(
                '.saved-task-results.lock', flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
            )
            created = True
        except FileExistsError:
            fd = os.open('.saved-task-results.lock', flags, dir_fd=directory)
        try:
            if created:
                os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise OSError('Unsafe saved task lock')
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Saved task store busy') from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)

    def read(self, directory):
        try:
            fd = os.open(
                self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return [], None
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise OSError('Saved task file must be owner-private')
            raw = source.read(MAX_BYTES + 1)
            after = os.fstat(source.fileno())
            if (info.st_mtime_ns, info.st_size) != (after.st_mtime_ns, after.st_size):
                raise OSError('Saved task file changed during read')
        if len(raw) > MAX_BYTES:
            raise ValueError('Saved task store exceeds size limit')

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('Duplicate saved task field')
                result[key] = value
            return result

        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs)
        if (
            not isinstance(value, dict)
            or set(value) != {'schema_version', 'items'}
            or type(value['schema_version']) is not int
            or not 1 <= value['schema_version'] <= READER_VERSION
        ):
            raise ValueError('Invalid saved task store')
        items = value['items']
        if not isinstance(items, list) or len(items) > LIMIT:
            raise ValueError('Invalid saved task capacity')
        for item in items:
            validate(item)
            if 'window_input' in item and value['schema_version'] < 3:
                raise ValueError('Window receipt requires archive format 3')
        if len({item['id'] for item in items}) != len(items):
            raise ValueError('Duplicate saved task ID')
        return items, (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)

    def write(self, directory, items, expected):
        try:
            current = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            actual = (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size)
        except FileNotFoundError:
            actual = None
        if actual != expected:
            raise OSError('Saved task store changed externally')
        version = 3 if any('window_input' in item for item in items) else 2
        raw = (
            json.dumps(
                {'schema_version': version, 'items': items}, ensure_ascii=False, allow_nan=False
            )
            + '\n'
        ).encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise ValueError('Saved task store exceeds size limit')
        temporary = '.saved-' + uuid.uuid4().hex + '.tmp'
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(fd, 'wb') as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def snapshot(self):
        with self.lock:
            try:
                with self.directory() as directory:
                    items, _ = self.read(directory)
                    return deepcopy(items)
            except FileNotFoundError:
                return []

    def save(self, task):
        # Only a backend-owned terminal receipt is accepted, never client text.
        if not isinstance(task, dict) or task.get('status') not in TERMINAL:
            raise ValueError('Only ended task receipts can be saved')
        item = {
            key: deepcopy(task[key])
            for key in ('id', 'title', 'status', 'result', 'steps', 'started_at')
        }
        item['saved_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
        for key in ('provenance', 'finished_at', 'window_input'):
            if key in task:
                item[key] = deepcopy(task[key])
        validate(item)
        with self.lock, self.directory(create=True) as directory, self.writer(directory):
            items, identity = self.read(directory)
            existing = next((entry for entry in items if entry['id'] == item['id']), None)
            if existing:
                if existing != {**item, 'saved_at': existing['saved_at']}:
                    raise ValueError('Saved receipt differs from original')
                return deepcopy(existing)
            if len(items) >= LIMIT:
                raise SavedTaskCapacityError('Saved task capacity reached')
            items.append(item)
            self.write(directory, items, identity)
            return deepcopy(item)

    def delete(self, task_id):
        ident(task_id)
        with self.lock:
            try:
                with self.directory() as directory, self.writer(directory):
                    items, identity = self.read(directory)
                    kept = [item for item in items if item['id'] != task_id]
                    if len(kept) == len(items):
                        return False
                    self.write(directory, kept, identity)
                    return True
            except FileNotFoundError:
                return False

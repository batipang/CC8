"""Bounded atomic reminder storage; reject links and externally replaced state."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
from typing import Iterator
import uuid

MAX_BYTES = 256 * 1024
Identity = tuple[int, int, int, int]


class ReminderCommitUncertain(OSError):
    """Rename committed; callers must retain the new state and query it."""


def identity(info: os.stat_result) -> Identity:
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size


def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('Duplicate reminder store field')
    return result


class ReminderStore:
    def __init__(self, path: Path, *, max_bytes: int = MAX_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.expected: Identity | None = None

    @contextmanager
    def directory(self, create: bool = False) -> Iterator[int]:
        if create:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError('Reminder directory must be owner-private')
            yield fd
        finally:
            os.close(fd)

    def load(self) -> object:
        try:
            with self.directory() as directory:
                fd = os.open(
                    self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(fd, 'rb') as source:
                    before = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_uid != os.getuid()
                        or before.st_mode & 0o077
                        or before.st_nlink != 1
                        or not 0 < before.st_size <= self.max_bytes
                    ):
                        raise ValueError('Invalid private reminder file')
                    raw = source.read(self.max_bytes + 1)
                    after = os.fstat(source.fileno())
                    if identity(before) != identity(after) or len(raw) != before.st_size:
                        raise OSError('Reminder file changed during read')
                    self.expected = identity(after)
                value = json.loads(raw, object_pairs_hook=unique)
                if value is None:
                    # Only a missing file may request a store's default state.
                    raise ValueError('Unexpected null in private store')
                return value
        except FileNotFoundError:
            return None

    def save(self, value: object) -> None:
        raw = (json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        if len(raw) > self.max_bytes:
            raise ValueError('Reminder store exceeds size limit')
        with self.directory(create=True) as directory:
            try:
                info = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_nlink != 1
                ):
                    raise OSError('Reminder file privacy changed')
                actual = identity(info)
            except FileNotFoundError:
                actual = None
            if actual != self.expected:
                raise OSError('Reminder file changed externally')
            name = '.reminder-' + uuid.uuid4().hex + '.tmp'
            fd = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            try:
                with os.fdopen(fd, 'wb') as target:
                    os.fchmod(target.fileno(), 0o600)
                    target.write(raw)
                    target.flush()
                    os.fsync(target.fileno())
                    saved = os.fstat(target.fileno())
                os.replace(name, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
                self.expected = identity(saved)
                try:
                    os.fsync(directory)
                except OSError as error:
                    raise ReminderCommitUncertain(
                        'Reminder commit durability unconfirmed'
                    ) from error
            finally:
                try:
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    pass

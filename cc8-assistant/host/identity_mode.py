"""Explicit owner/guest selection with private CAS persistence; constructors perform no I/O."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
import threading

from reminder_store import ReminderStore, ReminderCommitUncertain
from protocol import JsonObject

SCHEMA = 1
MAX_REVISION = 2**31 - 1


@dataclass(frozen=True)
class ModeState:
    mode: str = 'owner'
    revision: int = 0

    def public(self) -> JsonObject:
        return {'schema': SCHEMA, 'mode': self.mode, 'revision': self.revision}


def validate(value: object) -> ModeState:
    if (
        not isinstance(value, dict)
        or set(value) != {'schema', 'mode', 'revision'}
        or type(value['schema']) is not int
        or value['schema'] != SCHEMA
        or value['mode'] not in ('owner', 'guest')
        or type(value['revision']) is not int
        or not 0 <= value['revision'] <= MAX_REVISION
    ):
        raise ValueError('Invalid identity mode state')
    return ModeState(value['mode'], value['revision'])


class ModeConflict(RuntimeError):
    pass


class ModeCommitUncertain(OSError):
    def __init__(self, state: ModeState) -> None:
        super().__init__('Identity mode rename committed; durability unconfirmed')
        self.state = state


class IdentityModeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()

    def read(self, disk: ReminderStore) -> ModeState:
        try:
            raw = disk.load()
        except RecursionError:
            raise ValueError('Invalid nested identity mode state') from None
        return ModeState() if raw is None else validate(raw)

    def snapshot(self) -> ModeState:
        with self.lock:
            return self.read(ReminderStore(self.path))

    @contextmanager
    def writer(self, disk: ReminderStore) -> Iterator[None]:
        # Do not chmod an externally supplied directory or follow a lock link.
        with disk.directory(create=True) as directory:
            fd = os.open(
                '.identity-mode.lock',
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                ):
                    raise OSError('Unsafe identity mode lease')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield
            finally:
                os.close(fd)

    def change(self, expected: ModeState, mode: str) -> ModeState:
        if not isinstance(expected, ModeState) or mode not in ('owner', 'guest'):
            raise ValueError('Invalid identity mode change')
        # Validate the caller's snapshot too: bool revisions are never CAS tokens.
        validate(expected.public())
        with self.lock:
            disk = ReminderStore(self.path)
            with self.writer(disk):
                current = self.read(disk)
                if current != expected:
                    raise ModeConflict('Identity mode changed; refresh before selecting')
                if current.revision == MAX_REVISION:
                    raise ModeConflict('Identity mode revision exhausted')
                updated = ModeState(mode, current.revision + 1)
                try:
                    disk.save(updated.public())
                except ReminderCommitUncertain:
                    # The rename happened. A caller must retain this actual state,
                    # report uncertain persistence and never silently become owner.
                    raise ModeCommitUncertain(updated) from None
                return updated

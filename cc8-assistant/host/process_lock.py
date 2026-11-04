"""OS-owned leases: a stale PID file never grants process ownership."""

import fcntl
import os
from pathlib import Path
import stat


class ProcessLease:
    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
                raise OSError('Unsafe process lease')
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *unused):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def read_private_bytes(path, limit):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), 'rb') as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise OSError('Unsafe owner state')
        value = source.read(limit + 1)
        if len(value) > limit:
            raise OSError('Oversized owner state')
        return value


def atomic_private_bytes(path, value, *, private_parent=True):
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if private_parent:
        os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_private_json(path, value):
    import json

    atomic_private_bytes(path, json.dumps(value).encode())

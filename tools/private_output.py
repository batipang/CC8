"""Read and atomically publish bounded owner-private JSON reports."""

import json
import os
from pathlib import Path
import stat
import uuid


def read_private_json(path, max_bytes):
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('Duplicate report field')
        return result

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or not 0 < info.st_size <= max_bytes
        ):
            raise ValueError('Invalid private report')
        raw = stream.read(max_bytes + 1)
        if len(raw) != info.st_size:
            raise ValueError('Report changed while reading')
    return json.loads(raw, object_pairs_hook=unique_object)


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError('Report directory must be owner-private')
    return path


class PrivateOutput:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.directory = None
        self.identity = None

    def __enter__(self):
        if (
            self.path.suffix != '.json'
            or len(self.path.name) > 128
            or any(ord(c) < 32 for c in self.path.name)
        ):
            raise ValueError('Report filename must be bounded JSON')
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError('Report directory must be owner-private')
            report = os.open(
                self.path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd,
            )
            try:
                os.fchmod(report, 0o600)
                created = os.fstat(report)
                self.identity = created.st_dev, created.st_ino
            finally:
                os.close(report)
        except BaseException:
            os.close(fd)
            raise
        self.directory = fd
        return self

    def write(self, value):
        if self.directory is None:
            raise RuntimeError('Report output is not open')
        current = os.stat(self.path.name, dir_fd=self.directory, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.getuid()
            or current.st_mode & 0o077
            or (current.st_dev, current.st_ino) != self.identity
        ):
            raise OSError('Report output changed externally')
        temporary = '.cc8-' + uuid.uuid4().hex + '.tmp'
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.directory,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
                saved = os.fstat(stream.fileno())
            os.replace(
                temporary, self.path.name, src_dir_fd=self.directory, dst_dir_fd=self.directory
            )
            self.identity = saved.st_dev, saved.st_ino
        finally:
            try:
                os.unlink(temporary, dir_fd=self.directory)
            except FileNotFoundError:
                pass

    def __exit__(self, *unused):
        if self.directory is not None:
            os.close(self.directory)
            self.directory = None

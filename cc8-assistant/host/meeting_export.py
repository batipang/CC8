"""Explicit complete text exports, atomic private files and bounded deletion receipts."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from protocol import decode_json_object

from meeting_session import MeetingSession
from meeting_view import checked
from meeting_protocol import EXPORT_NAME as NAME, export_name as valid_name, sha256 as digest

MAX_BYTES = 2 * 1024 * 1024
MAX_FILES = 16
MAX_TOTAL = 16 * 1024 * 1024
LOCK = '.meeting-exports.lock'


class CommitUncertain(OSError):
    """A link/unlink happened; report uncertainty and inspect, never auto retry."""


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def private_file(info, maximum):
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
        and 0 <= info.st_size <= maximum
    )


def at(samples):
    milliseconds = samples * 1000 // 16000
    seconds, fraction = divmod(milliseconds, 1000)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f'{hour:02}:{minute:02}:{second:02}.{fraction:03}'


def render(model, ident, revision, format, *, analyses=()):
    """Call under the domain guard; no silent text truncation or guessed silence."""
    checked(model, ident, revision)
    if model.state != 'ended' or any(row['state'] == 'pending' for row in model.rows):
        raise ValueError('End the meeting and wait for recognition before exporting')
    if format not in ('txt', 'json'):
        raise ValueError('Unsupported meeting export')
    snapshot = model.snapshot()
    if type(analyses) is not tuple or len(analyses) > 3:
        raise ValueError('Invalid meeting derived documents')
    snapshot['analyses'] = list(analyses)
    if format == 'json':
        raw = (
            json.dumps(
                dict(schema=1, meeting=snapshot), ensure_ascii=False, allow_nan=False, indent=2
            )
            + '\n'
        ).encode('utf-8')
    else:
        lines = [
            '大黄蜂会议转写',
            '会议：' + ident,
            '修订：' + str(revision),
            '时长（按采样位置，包含已知传输缺口）：' + at(model.position),
            '原音未保存。识别文字需要校对；未识别出文字不代表没有人说话。',
            '时间范围属于音频片段，不是逐字时间戳。说话人标签只在各识别片段内有效，不代表身份。',
            '处理状态：'
            + (
                '所有片段已返回识别结果；准确度未独立核验。'
                if snapshot['processing_complete']
                else '完整性未确认，可能存在中断、缺口、过短或未确认片段，请核对。'
            ),
        ]
        for row in model.rows:
            lines.append('\n[' + at(row['start_sample']) + ' – ' + at(row['end_sample']) + ']')
            if row['state'] in ('recognized', 'empty'):
                lines.append(
                    ('人工校对：' if row['edited'] else '识别文字：')
                    + (row['text'] or '[未识别出文字]')
                )
                if row['edited']:
                    lines.append('校对前识别：' + (row['original'] or '[未识别出文字]'))
            else:
                labels = {
                    'gap': '传输缺口',
                    'unconfirmed': '结果未确认',
                    'too_short': '片段过短，未转写',
                }
                lines.append('[' + labels.get(row['state'], '结果未确认') + ']')
                if row['reason']:
                    lines.append('状态代码：' + row['reason'])
        for value in analyses:
            lines.extend(
                [
                    '\nDot ' + ('总结' if value['kind'] == 'summarize' else '翻译'),
                    '依据修订：' + str(value['source_revision']),
                    '依据旧正文，已过时，请重新生成。'
                    if value['stale']
                    else '依据当前正文；生成内容仍需核对。',
                    value['text'],
                ]
            )
        raw = ('\n'.join(lines) + '\n').encode('utf-8')
    if not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Meeting export exceeds file budget; no partial export written')
    return raw


@dataclass(frozen=True)
class Read:
    name: str
    size: int
    sha256: str
    identity: tuple
    modified_ns: int
    complete: bool

    def receipt(self):
        match = NAME.fullmatch(self.name)
        return dict(
            id=self.name,
            bytes=self.size,
            sha256=self.sha256,
            format=match[3] if match and self.complete else 'incomplete',
            integrity_confirmed=self.complete,
            meeting_id=match[1] if match else '',
            meeting_revision=int(match[2]) if match else None,
            modified_ns=self.modified_ns,
        )


class MeetingExports:
    """Publish a fully synced private directory; never replace a document in place."""

    def __init__(self, directory):
        self.path = Path(directory)

    @contextmanager
    def directory(self, create=False):
        if create:
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise OSError('Meeting exports directory must be owner-private')
            yield fd
            try:
                current = os.stat(self.path, follow_symlinks=False)
            except OSError:
                raise CommitUncertain('Meeting export directory moved during operation') from None
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise CommitUncertain('Meeting export directory changed during operation')
        finally:
            os.close(fd)

    @contextmanager
    def writer(self, directory):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        created = False
        try:
            fd = os.open(LOCK, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            created = True
        except FileExistsError:
            fd = os.open(LOCK, flags, dir_fd=directory)
        try:
            if created:
                os.fchmod(fd, 0o600)
            if not private_file(os.fstat(fd), 0):
                raise OSError('Unsafe meeting export lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if identity(os.fstat(fd)) != identity(
                os.stat(LOCK, dir_fd=directory, follow_symlinks=False)
            ):
                raise OSError('Meeting export lock changed')
            yield
        finally:
            os.close(fd)

    def scan(self, directory):
        names, unknown = [], False
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= 64:
                    raise OSError('Meeting export directory scan exceeds budget')
                if entry.name == LOCK:
                    continue
                if not valid_name(entry.name):
                    unknown = True
                    continue
                names.append(entry.name)
                if len(names) > MAX_FILES:
                    raise ValueError('Meeting export file capacity exceeded')
        return sorted(names), unknown

    def _bytes(self, directory, name, maximum):
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if not private_file(before, maximum):
                raise OSError('Unsafe meeting export file')
            raw = source.read(maximum + 1)
            after = os.fstat(source.fileno())
            path_info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (
                identity(before) != identity(after)
                or identity(after) != identity(path_info)
                or not private_file(path_info, maximum)
                or len(raw) != before.st_size
            ):
                raise OSError('Meeting export changed during read')
        return raw, identity(after)

    def _bundle(self, directory, name):
        if not valid_name(name):
            raise ValueError('Invalid meeting export identity')
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o700:
                raise OSError('Unsafe meeting export bundle')
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if len(names) >= 2 or entry.name not in {
                        'meeting.txt',
                        'meeting.json',
                        'receipt.json',
                    }:
                        raise OSError('Unexpected meeting export bundle entries')
                    names.append(entry.name)
            raw, ids = {}, []
            for child in sorted(names):
                raw[child], child_id = self._bytes(
                    fd, child, 4096 if child == 'receipt.json' else MAX_BYTES
                )
                ids.append((child, child_id))
            if sum(len(value) for value in raw.values()) > MAX_BYTES + 4096:
                raise OSError('Meeting export bundle exceeds budget')
            after = os.fstat(fd)
            if identity(before) != identity(after) or identity(after) != identity(
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            ):
                raise OSError('Meeting export bundle changed during read')
            return raw, (identity(after), tuple(ids)), after.st_mtime_ns
        finally:
            os.close(fd)

    def read(self, directory, name):
        raw, owned, modified = self._bundle(directory, name)
        match = NAME.fullmatch(name)
        complete, checksum = False, None
        if match and set(raw) == {'meeting.' + match[3], 'receipt.json'}:
            try:
                meta = decode_json_object(raw['receipt.json'], 4096)
                content = raw['meeting.' + match[3]]
                checksum = hashlib.sha256(content).hexdigest()
                complete = (
                    set(meta) == {'schema', 'id', 'bytes', 'sha256'}
                    and type(meta['schema']) is int
                    and meta['schema'] == 1
                    and meta['id'] == name
                    and type(meta['bytes']) is int
                    and meta['bytes'] == len(content)
                    and meta['sha256'] == checksum
                )
            except ValueError:
                pass
        if not complete:
            # A crash/partial delete stays explicitly visible and deletable;
            # corrupt metadata is never passed to a model or called a success.
            combined = hashlib.sha256()
            for child in sorted(raw):
                combined.update(
                    child.encode() + b'\0' + str(len(raw[child])).encode() + b'\0' + raw[child]
                )
            checksum = combined.hexdigest()
        return Read(
            name, sum(len(value) for value in raw.values()), checksum, owned, modified, complete
        )

    def snapshot(self):
        try:
            with self.directory() as directory, self.writer(directory):
                names, unknown = self.scan(directory)
                rows = [self.read(directory, name) for name in names]
                if sum(row.size for row in rows) > MAX_TOTAL:
                    raise ValueError('Meeting export total capacity exceeded')
                return dict(items=[row.receipt() for row in rows], unknown_files=unknown)
        except FileNotFoundError:
            if self.path.exists() or self.path.is_symlink():
                raise OSError('Meeting export changed while listing') from None
            return dict(items=[], unknown_files=False)

    def _write(self, directory, name, raw):
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def _remove(self, directory, item):
        # Verify all children before deleting anything. A partial OS failure
        # keeps a bounded incomplete bundle for a fresh explicit delete.
        fd = os.open(item.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            if identity(os.fstat(fd)) != item.identity[0]:
                raise OSError('Meeting export bundle changed before deletion')
            for name, expected in item.identity[1]:
                if identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != expected:
                    raise OSError('Meeting export file changed before deletion')
            for name, expected in item.identity[1]:
                if identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != expected:
                    raise OSError('Meeting export file changed during deletion')
                os.unlink(name, dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)
        if (
            identity(os.stat(item.name, dir_fd=directory, follow_symlinks=False))[:2]
            != item.identity[0][:2]
        ):
            raise OSError('Meeting export bundle replaced before removal')
        os.rmdir(item.name, dir_fd=directory)

    def save(self, model, ident, revision, format, *, current=lambda: True, analyses=()):
        if not isinstance(model, MeetingSession):
            raise ValueError('Expected a current meeting')
        raw = render(model, ident, revision, format, analyses=analyses)
        name = ident + '_r' + str(revision) + '.' + format
        checksum = hashlib.sha256(raw).hexdigest()
        meta = (
            json.dumps(dict(schema=1, id=name, bytes=len(raw), sha256=checksum)) + '\n'
        ).encode()
        with self.directory(create=True) as directory, self.writer(directory):
            names, unknown = self.scan(directory)
            if unknown:
                raise OSError('Unknown export directory entries; no files replaced')
            rows = [self.read(directory, item) for item in names]
            if name in names:
                row = next(item for item in rows if item.name == name)
                if not row.complete or row.sha256 != checksum:
                    raise OSError('Existing export differs; refusing to overwrite')
                checked(model, ident, revision)
                if current() is not True:
                    raise ValueError('Meeting export owner changed')
                try:
                    os.fsync(directory)
                except OSError:
                    raise CommitUncertain(
                        'Existing meeting export durability unconfirmed'
                    ) from None
                return row.receipt()
            if (
                len(rows) >= MAX_FILES
                or sum(row.size for row in rows) + len(raw) + len(meta) > MAX_TOTAL
            ):
                raise ValueError('Meeting export capacity reached; delete an explicit file first')
            temporary = 'pending-' + uuid.uuid4().hex
            os.mkdir(temporary, mode=0o700, dir_fd=directory)
            published = False
            try:
                fd = os.open(
                    temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                try:
                    os.fchmod(fd, 0o700)
                    self._write(fd, 'meeting.' + format, raw)
                    self._write(fd, 'receipt.json', meta)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                checked(model, ident, revision)
                if current() is not True:
                    raise ValueError('Meeting export owner changed')
                # Existing valid exports are nonempty directories, so rename
                # cannot replace them; source payload/receipt publish together.
                os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                published = True
                os.fsync(directory)
            except OSError:
                if published:
                    raise CommitUncertain('Meeting export exists; durability unconfirmed') from None
                raise
            finally:
                if not published:
                    try:
                        self._remove(directory, self.read(directory, temporary))
                    except (OSError, ValueError):
                        # Keep an incomplete pending bundle visible if cleanup
                        # fails; never replace the original failed operation.
                        pass
            return self.read(directory, name).receipt()

    def delete(self, name, sha256, *, current=lambda: True):
        if not valid_name(name) or not digest(sha256):
            raise ValueError('Invalid meeting export deletion receipt')
        with self.directory() as directory, self.writer(directory):
            item = self.read(directory, name)
            if item.sha256 != sha256 or current() is not True:
                raise ValueError('Meeting export changed; refresh before deleting')
            try:
                self._remove(directory, item)
                os.fsync(directory)
            except OSError:
                raise CommitUncertain('Meeting deletion result must be inspected') from None
        return dict(id=name, deleted=True)

"""One-shot, silent macOS notifications via a source-verified bundled app.

Only explicit setup installs an app; status never asks permission. Notification
content crosses RAM and an owner-local socket, with no shell interpolation.
macOS may retain delivered notifications according to the owner's settings.
"""

from contextlib import contextmanager
from itertools import islice
import fcntl
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import socket
import stat
import subprocess
import threading
import tempfile
import time

from protocol import decode_json_object

ROOT = Path(__file__).resolve().parents[2]
APP_NAME = 'CC8Notifier.app'
BUNDLE_ID = 'com.happyhome.cc8.notifier'
SOURCES = ('mac-notifier/Package.swift', 'mac-notifier/Sources/CC8Notifier/main.swift')
RESULTS = {
    'notification_center',
    'submitted_unconfirmed',
    'permission_denied',
    'rejected',
    'status',
    'authorization_failed',
    'invalid_request',
    'unconfirmed',
}
AUTHORIZATIONS = {'authorized', 'provisional', 'denied', 'not_determined', 'unknown'}


def source_hashes(root=ROOT):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCES}


def install_directory():
    return Path.home() / 'Library/Application Support/CC8/Notifications'


def safe_file(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or info.st_nlink != 1
            or not 0 < info.st_size <= maximum
        ):
            raise OSError('Unsafe notification artifact')
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if len(data) != info.st_size or (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise OSError('Notification artifact changed')
        return data


def verify_bundle(directory, root=ROOT, *, current_sources=True):
    directory = Path(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError('Notification directory must be owner-private')
    receipt = decode_json_object(safe_file(directory / 'build.json', 16384), max_bytes=16384)
    if (
        type(receipt.get('protocol')) is not int
        or receipt.get('protocol') != 1
        or not isinstance(receipt.get('sources'), dict)
        or set(receipt['sources']) != set(SOURCES)
        or not isinstance(receipt.get('files'), dict)
        or (current_sources and receipt.get('sources') != source_hashes(root))
        or set(receipt.get('files', {}))
        != {
            'Contents/Info.plist',
            'Contents/MacOS/CC8Notifier',
            'Contents/_CodeSignature/CodeResources',
        }
    ):
        raise ValueError('Notification helper source mismatch; run ./cc8 notifications setup')
    bundle = directory / APP_NAME
    for path in (
        bundle,
        bundle / 'Contents',
        bundle / 'Contents/MacOS',
        bundle / 'Contents/_CodeSignature',
    ):
        entry = path.lstat()
        if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_mode & 0o022:
            raise OSError('Unsafe notification bundle')
    expected_entries = set(receipt['files']) | {
        'Contents',
        'Contents/MacOS',
        'Contents/_CodeSignature',
    }
    entries = list(islice(bundle.rglob('*'), len(expected_entries) + 1))
    if {str(path.relative_to(bundle)) for path in entries} != expected_entries:
        raise ValueError('Unknown notification bundle contents; preserving existing app')
    for name, digest in receipt['files'].items():
        if hashlib.sha256(safe_file(bundle / name, 16 * 1024 * 1024)).hexdigest() != digest:
            raise ValueError('Notification helper artifact mismatch')
    metadata = plistlib.loads(safe_file(bundle / 'Contents/Info.plist', 16384))
    if (
        not isinstance(metadata, dict)
        or metadata.get('CFBundleIdentifier') != BUNDLE_ID
        or metadata.get('CFBundleExecutable') != 'CC8Notifier'
        or metadata.get('LSUIElement') is not True
    ):
        raise ValueError('Unexpected native notification app identity')
    subprocess.run(
        ['/usr/bin/codesign', '--verify', '--strict', str(bundle)],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    return bundle


@contextmanager
def directory_lock(directory):
    # A single app instance/request; setup cannot replace an in-flight binary.
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError('Notification directory must be owner-private')
        descriptor = os.open(
            '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise OSError('Unsafe notification lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def peer_uid(connection):
    # Darwin exposes getpeereid(3), while CPython's socket has no such method.
    library = ctypes.CDLL(None, use_errno=True)
    function = library.getpeereid
    function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    function.restype = ctypes.c_int
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError('Notification peer identity unavailable')
    return uid.value


class DesktopNotifier:
    def __init__(self, directory=None, root=ROOT, run=subprocess.Popen):
        self.directory = Path(directory) if directory else install_directory()
        self.root = root
        self.run = run

    def request(self, request, *, is_current=lambda: True):
        action = request.get('action') if isinstance(request, dict) else None
        if action not in ('status', 'authorize', 'post'):
            raise ValueError('Invalid notification operation')
        if set(request) != ({'action', 'id', 'body'} if action == 'post' else {'action'}):
            raise ValueError('Invalid notification fields')
        if action == 'post' and (
            not isinstance(request['id'], str)
            or not re.fullmatch('[a-f0-9]{32}', request['id'])
            or not isinstance(request['body'], str)
            or not request['body']
            or len(request['body'].encode('utf-8')) > 4096
            or any(ord(c) < 32 and c != '\n' for c in request['body'])
        ):
            raise ValueError('Invalid notification content')
        if platform.system() != 'Darwin':
            return {'result': 'unsupported'}
        if not self.directory.exists():
            return {'result': 'not_installed'}
        try:
            with directory_lock(self.directory):
                bundle = verify_bundle(self.directory, self.root)
                return self._exchange(bundle, request, is_current)
        except (OSError, ValueError, subprocess.SubprocessError):
            # No arbitrary platform errors or user content enter logs/health.
            return {'result': 'unconfirmed'}

    def _exchange(self, bundle, request, is_current):
        # Keep the address below Darwin's 104-byte sockaddr_un path budget.
        temporary = tempfile.TemporaryDirectory(prefix='cc8-n-', dir='/tmp')
        private = Path(temporary.name)
        path = private / 'ipc'
        process = None
        bound = False
        timeout = 50 if request['action'] == 'authorize' else 10
        deadline = time.monotonic() + timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                bound = True
                path.chmod(0o600)
                server.listen(1)
                server.settimeout(min(5, timeout))
                if not is_current():
                    return {'result': 'cancelled'}
                process = self.run(
                    [
                        '/usr/bin/open',
                        '-W',
                        '-n',
                        '-g',
                        str(bundle),
                        '--args',
                        '--socket',
                        str(path),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                with server.accept()[0] as connection:
                    if peer_uid(connection) != os.getuid():
                        raise OSError('Notification peer is not owner')
                    if not is_current():
                        return {'result': 'cancelled'}
                    connection.settimeout(max(0.1, deadline - time.monotonic()))
                    connection.sendall((json.dumps(request, ensure_ascii=False) + '\n').encode())
                    data = bytearray()
                    while b'\n' not in data and len(data) <= 1024:
                        block = connection.recv(1024)
                        if not block:
                            raise OSError('Incomplete notification receipt')
                        data.extend(block)
                    if len(data) > 1024 or data.count(b'\n') != 1 or not data.endswith(b'\n'):
                        raise ValueError('Invalid notification receipt')
                    result = decode_json_object(bytes(data), max_bytes=1024)
                    if (
                        not isinstance(result, dict)
                        or set(result) - {'protocol', 'result', 'authorization', 'id'}
                        or type(result.get('protocol')) is not int
                        or result['protocol'] != 1
                        or not isinstance(result.get('result'), str)
                        or result.get('result') not in RESULTS
                        or (
                            request['action'] != 'post'
                            and result.get('result')
                            not in (
                                'status',
                                'authorization_failed',
                                'invalid_request',
                                'unconfirmed',
                            )
                        )
                        or (request['action'] == 'post' and result.get('result') == 'status')
                        or (
                            'authorization' in result
                            and (
                                not isinstance(result['authorization'], str)
                                or result['authorization'] not in AUTHORIZATIONS
                            )
                        )
                        or (result.get('result') == 'status' and 'authorization' not in result)
                        or (request['action'] == 'post' and result.get('id') != request['id'])
                        or (request['action'] != 'post' and 'id' in result)
                    ):
                        raise ValueError('Invalid notification receipt')
                    return result
        finally:
            if bound:
                path.unlink(missing_ok=True)
            temporary.cleanup()
            if process is not None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # open -W is our launcher, not the app or another owner's process.
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)

    def post(self, ident, body, is_current=lambda: True):
        return self.request({'action': 'post', 'id': ident, 'body': body}, is_current=is_current)


class DesktopDelivery:
    """An asynchronously resolved reminder receipt; never runs on capture reader."""

    def __init__(self, notifier, ident, body, is_current):
        self.done = threading.Event()
        self.result = 'unconfirmed'
        self.detail = 'unconfirmed'
        self.reminder_id = None
        self.reminder_revision = None

        def deliver():
            try:
                receipt = notifier.post(ident, body, is_current)
                self.result = (
                    'notification_center'
                    if receipt.get('result') == 'notification_center'
                    else 'unconfirmed'
                )
                if receipt.get('result') in (
                    'permission_denied',
                    'not_installed',
                    'unsupported',
                    'submitted_unconfirmed',
                    'rejected',
                ):
                    self.detail = receipt['result']
            except Exception:
                self.result = 'unconfirmed'
            finally:
                self.done.set()

        self.worker = threading.Thread(target=deliver, name='cc8-desktop-notification', daemon=True)
        self.worker.start()

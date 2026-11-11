"""Inspect and explicitly isolate an unreadable reminder store from the Mac."""

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

from process_lock import ProcessLease
from release_info import ROOT
from reminder_store import MAX_BYTES, ReminderStore, identity
from runtime_guard import RuntimeLease, active_roots, enabled_service_roots
from workspace import Workspace


def private_snapshot(directory, name):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(name, flags, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > 4 * MAX_BYTES
        ):
            raise OSError('Unsafe reminder file')
        raw = stream.read(4 * MAX_BYTES + 1)
        if len(raw) != info.st_size or identity(info) != identity(os.fstat(stream.fileno())):
            raise OSError('Reminder file changed during recovery')
    return identity(info), hashlib.sha256(raw).hexdigest()


def inspect(path, directory):
    before = private_snapshot(directory, path.name)
    if before is None:
        return {'state': 'missing'}, None
    try:
        Workspace(path)
    except (ValueError, RecursionError, OverflowError):
        state = 'unreadable'
    else:
        state = 'readable'
    if private_snapshot(directory, path.name) != before:
        raise OSError('Reminder file changed during recovery')
    return {'state': state, 'sha256': before[1]}, before


def recover(root, confirmed_sha256=None):
    root = Path(root).resolve()
    path = root / 'device-access/private/voice-runtime/reminders.json'
    if confirmed_sha256 is None:
        try:
            with ReminderStore(path).directory() as directory:
                return inspect(path, directory)[0]
        except FileNotFoundError:
            return {'state': 'missing'}

    with (
        ReminderStore(path).directory() as directory,
        ProcessLease(path.parent / 'service-change.lock'),
        ProcessLease(path.parent / 'launcher.lock'),
        RuntimeLease(root),
    ):
        if root in active_roots() or root in enabled_service_roots():
            raise RuntimeError('Stop the CC8 backend before recovery')
        status, inspected = inspect(path, directory)
        if status['state'] != 'unreadable' or not hmac.compare_digest(
            status['sha256'], confirmed_sha256
        ):
            raise ValueError('Reminder file is readable, missing, or no longer matches the digest')
        archive = 'reminders-recovery-' + uuid.uuid4().hex
        os.mkdir(archive, 0o700, dir_fd=directory)
        moved = False
        try:
            with ReminderStore(path.parent / archive / path.name).directory() as destination:
                if private_snapshot(directory, path.name) != inspected:
                    raise OSError('Reminder file changed before quarantine')
                os.rename(path.name, path.name, src_dir_fd=directory, dst_dir_fd=destination)
                moved = True
                try:
                    os.fsync(destination)
                    os.fsync(directory)
                except OSError:
                    return {
                        **status,
                        'state': 'quarantined_durability_unconfirmed',
                        'archive': str(path.parent / archive / path.name),
                    }
        finally:
            if not moved:
                os.rmdir(archive, dir_fd=directory)
        return {**status, 'state': 'quarantined', 'archive': str(path.parent / archive / path.name)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='cc8 recover-reminders', description=__doc__)
    parser.add_argument('--quarantine-sha256', help='Isolate the unreadable file with this SHA-256')
    args = parser.parse_args(argv)
    confirmed = args.quarantine_sha256
    if confirmed is not None and not re.fullmatch(r'[a-f0-9]{64}', confirmed):
        parser.error('Expected a lowercase SHA-256 from the inspection')
    try:
        result = recover(ROOT, confirmed)
    except (OSError, ValueError, RuntimeError):
        print(
            'Reminder recovery refused. Check private file ownership, digest and stopped service.',
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result))
    return 2 if result['state'] == 'quarantined_durability_unconfirmed' else 0

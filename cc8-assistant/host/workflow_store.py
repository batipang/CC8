"""Passive explicit workflow configuration and durable once-only schedule claims."""

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import os
import stat
import threading
import time
import uuid

from reminder_store import ReminderStore
from workflow_schedule import due_slot, timestamp
from workflow_definition import (
    definition,
    validate,
    empty,
    token,
    integer,
    fingerprint,
    schedulable,
    MAX_WORKFLOWS,
    MAX_ATTEMPTS,
    REVISION_LIMIT,
)


class WorkflowStore:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = path, clock
        self.lock = threading.RLock()

    def read(self, disk):
        value = disk.load()
        return empty() if value is None else validate(value)

    def snapshot(self):
        with self.lock:
            return deepcopy(self.read(ReminderStore(self.path)))

    @contextmanager
    def writer(self, disk):
        with disk.directory(create=True) as directory:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                fd = os.open(
                    '.workflows.lock', flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
                )
            except FileExistsError:
                fd = os.open('.workflows.lock', flags, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise OSError('Unsafe workflow writer lock')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield
            finally:
                os.close(fd)

    def apply(self, action, revision, *, value=None, ident=None, current=lambda: True):
        integer(revision, 0, REVISION_LIMIT - 1)
        if action not in ('put', 'delete', 'reset', 'clear_history'):
            raise ValueError('Invalid workflow mutation')
        if action == 'put':
            value = deepcopy(value)
            definition(value)
            if value['schedule'] is not None:
                # Client clocks cannot backdate activation and claim old slots.
                # Every edit starts a fresh schedule window, including re-enable.
                value['schedule']['configured_at'] = timestamp(self.clock())
            if ident is not None:
                raise ValueError('Unexpected workflow ID')
        else:
            if value is not None:
                raise ValueError('Unexpected workflow body')
            if action == 'delete':
                token(ident)
            elif ident is not None:
                raise ValueError('Unexpected reset identity')
        with self.lock:
            disk = ReminderStore(self.path)
            with self.writer(disk):
                state = self.read(disk)
                if state['revision'] != revision or current() is not True:
                    raise ValueError('Workflow state changed')
                if action == 'clear_history':
                    at = timestamp(self.clock())
                    for flow in state['items']:
                        if flow['schedule'] is not None:
                            flow['schedule'].update(enabled=False, configured_at=at)
                    state['attempts'] = []
                elif action == 'reset':
                    replacement = empty()['items']
                    # Keep all uncertain schedule claims even when definitions
                    # are explicitly reset. Reset does not authorize a retry.
                    state['items'] = replacement
                elif action == 'delete':
                    if not any(item['id'] == ident for item in state['items']):
                        raise ValueError('Workflow missing')
                    state['items'] = [item for item in state['items'] if item['id'] != ident]
                else:
                    found = next(
                        (i for i, item in enumerate(state['items']) if item['id'] == value['id']),
                        None,
                    )
                    if found is None:
                        if len(state['items']) == MAX_WORKFLOWS:
                            raise ValueError('Workflow capacity reached')
                        state['items'].append(deepcopy(value))
                    else:
                        state['items'][found] = deepcopy(value)
                state['revision'] += 1
                validate(state)
                if current() is not True:
                    raise ValueError('Workflow owner changed')
                disk.save(state)
                return deepcopy(state)

    def claim(self, ident, revision, slot, *, current=lambda: True):
        token(ident)
        integer(revision, 0, REVISION_LIMIT - 1)
        integer(slot, 0, 253402300799)
        with self.lock:
            disk = ReminderStore(self.path)
            with self.writer(disk):
                state = self.read(disk)
                if state['revision'] != revision or current() is not True:
                    raise ValueError('Workflow schedule changed')
                flow = next((item for item in state['items'] if item['id'] == ident), None)
                if (
                    flow is None
                    or flow['schedule'] is None
                    or not flow['schedule']['enabled']
                    or not schedulable(flow)
                ):
                    raise ValueError('Workflow not scheduled')
                at = timestamp(self.clock())
                if slot != due_slot(flow, at) or any(
                    at < row['claimed_at'] for row in state['attempts']
                ):
                    raise ValueError('Workflow occurrence outside configured window')
                if any(
                    row['workflow_id'] == ident and row['slot'] == slot for row in state['attempts']
                ):
                    return None
                if len(state['attempts']) == MAX_ATTEMPTS:
                    raise ValueError('Workflow journal capacity reached')
                row = dict(
                    id=uuid.uuid4().hex,
                    workflow_id=ident,
                    definition_hash=fingerprint(flow),
                    slot=slot,
                    claimed_at=at,
                    execution='claimed',
                    delivery='not_started',
                )
                state['attempts'].append(row)
                state['revision'] += 1
                validate(state)
                if current() is not True:
                    raise ValueError('Workflow schedule owner changed')
                disk.save(state)
                return deepcopy(row)

    def finish_execution(self, ident, status):
        token(ident)
        if status not in ('completed', 'failed', 'cancelled', 'unconfirmed'):
            raise ValueError('Invalid workflow settlement')
        with self.lock:
            disk = ReminderStore(self.path)
            with self.writer(disk):
                state = self.read(disk)
                row = next((item for item in state['attempts'] if item['id'] == ident), None)
                if row is None or row['execution'] != 'claimed':
                    return False
                row.update(
                    execution=status, delivery='pending' if status == 'completed' else 'not_started'
                )
                state['revision'] += 1
                validate(state)
                disk.save(state)
                return True

    def finish_delivery(self, ident, delivered):
        token(ident)
        if type(delivered) is not bool:
            raise ValueError('Invalid workflow delivery receipt')
        with self.lock:
            disk = ReminderStore(self.path)
            with self.writer(disk):
                state = self.read(disk)
                row = next((item for item in state['attempts'] if item['id'] == ident), None)
                if row is None or row['execution'] != 'completed' or row['delivery'] != 'pending':
                    return False
                row['delivery'] = 'completed' if delivered else 'unconfirmed'
                state['revision'] += 1
                validate(state)
                disk.save(state)
                return True

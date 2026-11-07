"""Stop only server terminal handles proven to belong to the interrupted turn.

Turn interruption is not a rollback and may leave unified-exec sessions alive.
Never clean an entire thread: an earlier completed turn may intentionally have
started a background service. No command text, OS PID, or tool output is retained.
"""

import threading
import time

from dot_receipt import DotError


def identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 4096


class CommandScope:
    def __init__(self, thread_id):
        self.thread_id = thread_id
        self.turn_id = None
        self.items = {}
        self.overflow = False
        self.lock = threading.Lock()

    def observe(self, event):
        if not isinstance(event, dict) or event.get('method') not in (
            'item/started',
            'item/completed',
        ):
            return
        params = event.get('params')
        if not isinstance(params, dict) or params.get('threadId') != self.thread_id:
            return
        item = params.get('item')
        turn_id = params.get('turnId')
        if not isinstance(item, dict) or item.get('type') != 'commandExecution':
            return
        item_id = item.get('id')
        if not identifier(turn_id) or not identifier(item_id):
            return
        with self.lock:
            if self.overflow or self.turn_id is not None and self.turn_id != turn_id:
                return
            owned = self.items.setdefault(turn_id, set())
            if sum(map(len, self.items.values())) >= 256 and item_id not in owned:
                self.overflow = True
                self.items.clear()
                return
            owned.add(item_id)

    def bind(self, turn_id):
        with self.lock:
            self.turn_id = turn_id
            self.items = {turn_id: self.items.get(turn_id, set())}

    def snapshot(self, turn_id):
        with self.lock:
            if self.overflow or self.turn_id != turn_id:
                return frozenset()
            return frozenset(self.items.get(turn_id, ()))


def stop_owned_terminals(rpc, scope, turn_id):
    """One bounded inventory, then at most one terminate per exact owned handle.

    Missing/unsupported APIs or incomplete inventories leave results unconfirmed.
    A successful terminate response is not proof of rollback or arbitrary tool
    descendant cleanup. New turns and other threads are never broad-cleaned.
    """
    owned = scope.snapshot(turn_id)
    if not owned:
        return
    deadline = time.monotonic() + 4

    def request(method, params):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DotError('Terminal cleanup deadline expired', kind='timeout')
        return rpc.request(method, params, timeout=min(remaining, 2))

    cursor = None
    cursors = set()
    handles = {}
    # Collect before terminating: mutating a paginated list invalidates cursors.
    for _ in range(4):
        result = request(
            'thread/backgroundTerminals/list',
            {
                'threadId': scope.thread_id,
                'cursor': cursor,
                'limit': 64,
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get('data'), list):
            return
        rows = result['data']
        if len(rows) > 64:
            return
        for row in rows:
            if not isinstance(row, dict):
                return
            handle, item = row.get('processId'), row.get('itemId')
            if not identifier(handle) or not identifier(item) or handle in handles:
                return
            handles[handle] = item
        cursor = result.get('nextCursor')
        if cursor is None:
            break
        if not identifier(cursor) or cursor in cursors:
            return
        cursors.add(cursor)
    else:
        return
    for handle, item in handles.items():
        if item in owned:
            request(
                'thread/backgroundTerminals/terminate',
                {
                    'threadId': scope.thread_id,
                    'processId': handle,
                },
            )

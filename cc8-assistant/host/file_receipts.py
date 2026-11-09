"""Observe structured file receipts before narration can suspend consumption."""

from copy import deepcopy
import json
import threading
from result_provenance import absolute_path, validate, MAX_FILES, MAX_ITEMS, MAX_BYTES
from source_identity import identifier


class FileReceipts:
    """Receive before speech consumption; freeze only bounded path metadata."""

    def __init__(self, thread_id, cwd, handoff, valid, publish):
        self.thread_id, self.cwd, self.handoff = thread_id, cwd, handoff
        self.valid, self.publish = valid, publish
        self.lock = threading.RLock()
        self.turn_id = None
        self.early = []
        self.items = {}
        self.conflicts = set()
        self.truncated = self.finished = False

    def send(self, data):
        # Optional display metadata cannot prevent an already queued source
        # completion from reaching the authoritative receipt watcher.
        try:
            self.publish(data)
        except (OSError, EOFError):
            pass

    def project(self, item):
        ident = identifier(item.get('id'))
        status, changes = item.get('status'), item.get('changes')
        if status not in ('completed', 'failed', 'declined') or not isinstance(changes, list):
            raise ValueError('Unconfirmed file receipt')
        rows, truncated = [], len(changes) > MAX_FILES
        for change in changes[:MAX_FILES]:
            try:
                kind = change['kind']
                mode = kind['type']
                if mode not in ('add', 'update', 'delete'):
                    raise ValueError('Unknown file operation')
                move = kind.get('move_path')
                if move is not None and mode != 'update':
                    raise ValueError('Invalid moved file')
                rows.append(
                    dict(
                        item_id=ident,
                        path=absolute_path(change['path'], self.cwd),
                        kind=mode,
                        status=status,
                        move_to=absolute_path(move, self.cwd) if move is not None else None,
                    )
                )
            except (KeyError, TypeError, ValueError, UnicodeError):
                truncated = True
        return ident, rows, truncated

    def snapshot(self):
        with self.lock:
            files = [
                row
                for ident, rows in self.items.items()
                if ident not in self.conflicts
                for row in rows
            ]
            value = dict(
                version=1,
                provider='dot',
                host='local',
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                ephemeral=True,
                original_source=dict(
                    thread_id=self.handoff.source_id, title=self.handoff.source_title
                )
                if self.handoff
                else None,
                files=deepcopy(files[:MAX_FILES]),
                files_truncated=self.truncated or len(files) > MAX_FILES,
            )
            while (
                len(json.dumps(value, ensure_ascii=False).encode()) > MAX_BYTES and value['files']
            ):
                value['files'].pop()
                value['files_truncated'] = True
            return validate(value)

    def apply(self, turn_id, receipt):
        if self.finished or turn_id != self.turn_id:
            return False
        if receipt is None:
            self.finished = True
            return False
        ident, rows, truncated = receipt
        before = (self.truncated, len(self.conflicts), len(self.items))
        self.truncated |= truncated
        if ident is None:
            self.truncated = True
        elif ident in self.items and self.items[ident] != rows:
            self.conflicts.add(ident)
            self.truncated = True
        elif ident not in self.items:
            if len(self.items) >= MAX_ITEMS:
                self.truncated = True
            else:
                self.items[ident] = rows
        return before != (self.truncated, len(self.conflicts), len(self.items))

    def feed(self, event):
        with self.lock:
            if not self.valid() or self.finished:
                return
            p = event.get('params') or {}
            if not isinstance(p, dict):
                return
            if p.get('threadId') != self.thread_id:
                return
            method, receipt = event.get('method'), None
            item = p.get('item')
            if method == 'turn/completed' and isinstance(p.get('turn'), dict):
                turn_id = p['turn'].get('id')
            elif (
                method == 'item/completed'
                and isinstance(item, dict)
                and item.get('type') == 'fileChange'
            ):
                turn_id = p.get('turnId')
                try:
                    receipt = self.project(p['item'])
                except (KeyError, TypeError, ValueError, UnicodeError):
                    receipt = (None, [], True)
            else:
                return
            try:
                identifier(turn_id)
            except (ValueError, TypeError):
                return
            if self.turn_id is None:
                if len(self.early) < MAX_ITEMS:
                    self.early.append((turn_id, receipt))
                else:
                    self.truncated = True
                return
            changed = self.apply(turn_id, receipt)
            data = self.snapshot() if changed else None
        if data is not None:
            self.send(data)

    def bind(self, turn_id):
        with self.lock:
            # Optional provenance must not take down an otherwise valid RPC
            # when a future server uses an identifier outside this UI schema.
            try:
                identifier(self.thread_id)
                self.turn_id = identifier(turn_id)
                self.snapshot()
            except (ValueError, TypeError, UnicodeError):
                self.finished = True
                self.early.clear()
                return
            early, self.early = self.early, []
            for ident, receipt in early:
                self.apply(ident, receipt)
            data = self.snapshot()
        if self.valid():
            self.send(data)

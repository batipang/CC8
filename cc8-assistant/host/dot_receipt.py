"""Bounded final-answer validation shared by delivery and speech consumption."""

import threading
import time
from operation_failure import OperationFailure
from dot_errors import GENERIC, error_kind, error_notice, turn_completion


class DotError(OperationFailure):
    def __init__(self, message, *, kind='provider_or_runtime'):
        super().__init__(message, stage='brain', kind=kind)


class DotApprovalRequired(DotError):
    def __init__(self, message):
        super().__init__(message, kind='approval_declined')


class NoFinalAnswer(DotError):
    pass


class FinalAnswer:
    def __init__(self, thread_id, turn_id):
        self.thread_id, self.turn_id = thread_id, turn_id
        self.phases = {}
        self.phase_aware = False
        self.last_message = {}
        self.last_final = {}
        self.stream_id = None
        self.stream_text = ''
        self.status = None
        self.failure_kind = None
        self.failure_hint = None
        self.finished = False

    def feed(self, event):
        if self.finished:
            return None
        p = event.get('params') or {}
        if p.get('threadId') != self.thread_id:
            return None
        method = event.get('method')
        if method == 'turn/completed':
            turn = p.get('turn') or {}
            if turn.get('id') == self.turn_id:
                self.finished = True
                self.status = turn.get('status')
                if self.status == 'failed':
                    reason = turn.get('error')
                    missing = reason is None or (
                        type(reason) is dict and reason.get('codexErrorInfo') is None
                    )
                    self.failure_kind = (
                        self.failure_hint or GENERIC if missing else error_kind(reason)
                    )
                self.failure_hint = None
            return None
        if p.get('turnId') != self.turn_id:
            return None
        if method == 'error':
            notice = error_notice(event)
            if notice is not None:
                fields = notice['params']
                # A warning alone never fails a task. Only the matching failed
                # terminal can use a non-retried hint; a retry clears it.
                self.failure_hint = None if fields['willRetry'] else error_kind(fields['error'])
            return None
        if method in ('item/started', 'item/completed'):
            item = p.get('item') or {}
            if item.get('type') != 'agentMessage':
                return None
            ident = item.get('id')
            if method == 'item/started':
                if isinstance(ident, str) and ident:
                    if ident not in self.phases and len(self.phases) >= 256:
                        raise DotError('Codex answer metadata exceeds size limit')
                    self.phases[ident] = item.get('phase')
                self.phase_aware |= item.get('phase') is not None
                return None
            value = item.get('text') or ''
            if not isinstance(value, str) or len(value) > 64000:
                raise DotError('Codex answer exceeds size limit')
            phase = item.get('phase')
            if phase is None and isinstance(ident, str):
                phase = self.phases.get(ident)
            self.phase_aware |= phase is not None
            self.last_message = {'id': ident, 'text': value}
            if phase == 'final_answer':
                self.last_final = self.last_message
        elif method == 'item/agentMessage/delta':
            ident, value = p.get('itemId'), p.get('delta')
            if (
                not isinstance(ident, str)
                or not ident
                or not isinstance(value, str)
                or self.phases.get(ident) != 'final_answer'
            ):
                return None
            if self.stream_id is None:
                self.stream_id = ident
            if ident != self.stream_id:
                return None
            self.stream_text += value
            if len(self.stream_text) > 64000:
                raise DotError('Codex answer exceeds size limit')
            return value
        return None

    def answer(self):
        chosen = self.last_final if self.phase_aware else self.last_message
        raw = chosen.get('text', '')
        if not raw.strip():
            raise NoFinalAnswer('Codex returned no final answer')
        if self.stream_id is not None and (
            chosen.get('id') != self.stream_id or not raw.startswith(self.stream_text)
        ):
            raise DotError('Final answer changed after streaming')
        return raw, raw.strip()


class ReceiptWatch:
    """Observe the receive queue without waiting for a yielded speech clause.

    No provider or worker thread is added. The app-server reader delivers these
    notifications in protocol order. Before turn/start acknowledges its ID,
    retain only bounded references, then filter by both exact IDs on binding.
    Listener errors remain local and are raised by the ordinary turn consumer.
    """

    def __init__(self, notifications, thread_id, valid, publish, *, first_delta=None, clock=None):
        self.lock = threading.RLock()
        self.thread_id, self.valid, self.publish = thread_id, valid, publish
        self.state = None
        self.early = []
        self.early_chars = 0
        self.closed = False
        self.delivered = False
        self.error = None
        self.clock = clock or time.monotonic
        self.first_delta = first_delta
        self.delta_seen = False
        self.unsubscribe = notifications.listen(self.feed)

    def _feed(self, event, observed_at):
        delta = self.state.feed(event)
        if delta and delta.strip() and not self.delta_seen:
            self.delta_seen = True
            if self.first_delta:
                self.first_delta(observed_at)
        if self.state.status == 'completed' and not self.delivered:
            _, answer = self.state.answer()
            self.delivered = True
            self.publish(answer, observed_at)

    def feed(self, event, *, observed_at=None):
        # Timestamp before waiting for binding/admission, not when speech later
        # resumes. Buffered events keep this same local monotonic observation.
        observed_at = self.clock() if observed_at is None else observed_at
        with self.lock:
            if self.closed or self.error:
                return
            try:
                if not self.valid():
                    return
                if event.get('method') == 'error':
                    event = error_notice(event)
                    if event is None:
                        return
                elif event.get('method') == 'turn/completed':
                    event = turn_completion(event)
                if self.state is None:
                    p = event.get('params') or {}
                    method = event.get('method')
                    if p.get('threadId') != self.thread_id:
                        return
                    if method in ('item/started', 'item/completed'):
                        if (p.get('item') or {}).get('type') != 'agentMessage':
                            return
                    elif method not in ('item/agentMessage/delta', 'turn/completed', 'error'):
                        return
                    if len(self.early) >= 256:
                        raise DotError('Codex pending receipt exceeds size limit')
                    text = (
                        p.get('delta')
                        if method == 'item/agentMessage/delta'
                        else (p.get('item') or {}).get('text', '')
                    )
                    if isinstance(text, str):
                        self.early_chars += len(text)
                        if len(text) > 64000 or self.early_chars > 192000:
                            raise DotError('Codex pending receipt exceeds size limit')
                    # Queue coalescing mutates a queued delta in place. Freeze
                    # its fields now so binding cannot count later deltas twice.
                    params = dict(p)
                    for key in ('item', 'turn'):
                        if isinstance(params.get(key), dict):
                            params[key] = dict(params[key])
                    self.early.append(({'method': method, 'params': params}, observed_at))
                else:
                    self._feed(event, observed_at)
            except Exception as error:
                self.error = error
                self.early.clear()

    def bind(self, turn_id):
        with self.lock:
            self.state = FinalAnswer(self.thread_id, turn_id)
            early, self.early = self.early, []
            self.early_chars = 0
            for event, observed_at in early:
                self.feed(event, observed_at=observed_at)

    def check(self):
        with self.lock:
            if self.error:
                raise self.error

    def close(self):
        # Wait for an admitted callback before marking this watcher closed.
        # Unsubscribe is outside our lock: receive takes notification -> watcher.
        self.unsubscribe()
        with self.lock:
            self.closed = True
            self.early.clear()

"""One explicit meeting ASR worker, two queued chunks, no recording or disk I/O."""

from dataclasses import dataclass, field
import queue
import threading

from diagnostics import error_kind
from minimax_client import Cancelled
from meeting_session import Chunk, MeetingSession


@dataclass
class Work:
    model: MeetingSession = field(repr=False)
    chunk: Chunk = field(repr=False)
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    client: object = field(default=None, repr=False)


class MeetingWorker:
    """changed is an advisory nonblocking signal, never device/transport I/O."""

    def __init__(self, lock, client_factory, *, changed=lambda: None):
        self.lock, self.factory, self.changed = lock, client_factory, changed
        self.model = None
        self.queue = queue.Queue(maxsize=2)
        self.current = None
        self.inflight = {}
        self.thread = None
        self.closed = threading.Event()
        self.notification_failures = self.cleanup_failures = 0

    def bind(self, model):
        with self.lock:
            if (
                self.closed.is_set()
                or self.inflight
                or self.model is not None
                and self.model.state != 'deleted'
            ):
                raise ValueError('Previous meeting must be deleted and recognition stopped first')
            if not isinstance(model, MeetingSession) or model.state != 'ready':
                raise ValueError('Expected a fresh passive meeting')
            self.model = model

    def start(self):
        # Only the controller's confirmed Start path should call this method.
        # Constructor and list/status operations cannot create a worker.
        with self.lock:
            if self.closed.is_set() or self.model is None or self.model.state != 'capturing':
                raise ValueError('Meeting capture must be explicitly started first')
            if self.thread is None:
                thread = threading.Thread(target=self._run, name='cc8-meeting-asr', daemon=True)
                try:
                    thread.start()
                except RuntimeError:
                    self.model.interrupt('asr_failed')
                    raise
                self.thread = thread

    def _valid_locked(self, work):
        return (
            not self.closed.is_set()
            and not work.cancelled.is_set()
            and work.model is self.model
            and work.model._pending(work.chunk) is not None
        )

    def _valid(self, work):
        with self.lock:
            return self._valid_locked(work)

    def _notify(self):
        # Notification is advisory: callers can always poll the RAM timeline.
        # A broken UI/connection callback cannot kill transcription or leak text.
        try:
            self.changed()
        except Exception:
            with self.lock:
                self.notification_failures = min(65535, self.notification_failures + 1)

    def _cancel_clients(self, clients):
        for client in clients:
            try:
                client.cancel()
            except Exception:
                # Logical cancellation happened under the lock first, so a
                # transport cleanup failure can never authorize a late result.
                with self.lock:
                    self.cleanup_failures = min(65535, self.cleanup_failures + 1)

    def submit(self, chunks):
        """Nonblocking admission; never replay a chunk already queued/in flight."""
        if type(chunks) not in (tuple, list) or len(chunks) > 3:
            raise ValueError('Invalid meeting chunk batch')
        clients = []
        with self.lock:
            if self.model is None or self.closed.is_set() or self.thread is None:
                raise ValueError('Meeting worker unavailable')
            seen = set()
            for chunk in chunks:
                if self.model._pending(chunk) is None:
                    raise ValueError('Meeting chunk is not pending')
                if chunk.ident in seen or chunk.ident in self.inflight:
                    raise ValueError('Meeting chunk already submitted')
                seen.add(chunk.ident)
            accepted = True
            for chunk in chunks:
                work = Work(self.model, chunk)
                try:
                    self.queue.put_nowait(work)
                    self.inflight[chunk.ident] = work
                except queue.Full:
                    clients = self._invalidate('backpressure')
                    accepted = False
                    break
        self._cancel_clients(clients)
        self._notify()
        return accepted

    def _invalidate(self, reason):
        """Under the owner lock; cancel only old per-job clients outside it."""
        clients = []
        if self.model is not None:
            self.model.interrupt(reason)
        # A worker may have dequeued an item and not yet published current;
        # inflight retains ownership across that interval as well.
        for work in self.inflight.values():
            work.cancelled.set()
            if work.client is not None:
                clients.append(work.client)
        while True:
            try:
                work = self.queue.get_nowait()
            except queue.Empty:
                break
            self.inflight.pop(work.chunk.ident, None)
            self.queue.task_done()
        return clients

    def interrupt(self, reason):
        with self.lock:
            clients = self._invalidate(reason)
        self._cancel_clients(clients)
        self._notify()

    def _perform(self, work):
        try:
            if not self._valid(work):
                return
            # Exact digital zeros are not proof of acoustic silence. They can
            # mean a muted/missing microphone; show a gap, not invented words.
            if not any(work.chunk.pcm):
                with self.lock:
                    clients = self._invalidate('no_signal') if self._valid_locked(work) else []
                self._cancel_clients(clients)
                return
            client = self.factory()
            with self.lock:
                valid = self._valid_locked(work)
                if valid:
                    work.client = client
            if not valid:
                self._cancel_clients((client,))
                return
            result = client.transcribe_chunk(work.chunk.pcm, lambda: self._valid(work))
            with self.lock:
                clients = []
                if self._valid_locked(work):
                    if not work.model.complete(work.chunk, result):
                        row = next(r for r in work.model.rows if r['id'] == work.chunk.ident)
                        clients = self._invalidate(row['reason'])
            self._cancel_clients(clients)
        except Cancelled:
            with self.lock:
                clients = self._invalidate('cancelled') if self._valid_locked(work) else []
            self._cancel_clients(clients)
        except Exception as error:
            kind = error_kind(error)
            reason = {
                'transport_or_io': 'network',
                'timeout': 'timeout',
                'quota': 'quota',
                'rate_limit': 'asr_failed',
                'credential_or_permission': 'credential_or_permission',
            }.get(kind, 'asr_failed')
            with self.lock:
                clients = self._invalidate(reason) if self._valid_locked(work) else []
            self._cancel_clients(clients)
        finally:
            # Transport closes its request in its own finally. Never cancel a
            # shared/next request or keep the previous client in Work.
            with self.lock:
                work.client = None

    def _run(self):
        while not self.closed.is_set():
            try:
                work = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                with self.lock:
                    self.current = work
                self._perform(work)
            finally:
                with self.lock:
                    if self.current is work:
                        self.current = None
                    self.inflight.pop(work.chunk.ident, None)
                self.queue.task_done()
                # An idle thread must not retain its last chunk or transcript.
                work = None
                self._notify()

    def busy(self):
        """Outstanding ASR only; capture reservation belongs to the controller."""
        with self.lock:
            return bool(self.inflight)

    def close(self):
        self.closed.set()
        self.interrupt('cancelled')
        # Never join a cloud request from a device/UI/lifecycle thread. Requests
        # are cancelled through their own bounded transport; shutdown is observable.

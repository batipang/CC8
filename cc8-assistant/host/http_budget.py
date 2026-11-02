"""Per-request cumulative network I/O budget for the pinned Requests stack.

Socket reads include buffered HTTP headers, chunk framing and body reads, so a
responsive drip cannot evade the budget. Time spent playing audio or consuming
an already-yielded event is not charged. DNS resolution is still owned by the
OS; it cannot be interrupted here, and is charged when connect returns.
"""

from contextlib import contextmanager
import math
import socket
import threading
import time
import weakref

from requests.adapters import HTTPAdapter


class IOBudget:
    def __init__(self, seconds, *, clock=time.monotonic):
        if type(seconds) not in (float, int) or not 0 < seconds <= 600:
            raise ValueError('Invalid request I/O budget')
        if not math.isfinite(seconds):
            raise ValueError('Invalid request I/O budget')
        self.seconds, self.clock = float(seconds), clock
        self.used = 0.0
        self.cancelled = False
        self.lock = threading.RLock()
        self.sockets = weakref.WeakSet()

    @property
    def expired(self):
        with self.lock:
            return self.used >= self.seconds

    def remaining(self):
        with self.lock:
            if self.cancelled:
                raise OSError('Request cancelled')
            left = self.seconds - self.used
            if left <= 0:
                raise TimeoutError('Request network I/O budget exhausted')
            return left

    @contextmanager
    def io(self, *, nested=False):
        left = self.remaining()
        started = self.clock()
        with self.lock:
            before = self.used
        try:
            yield left
        finally:
            elapsed = max(0.0, self.clock() - started)
            with self.lock:
                # connect also contains CONNECT-proxy reads. Charge them once.
                already = max(0.0, self.used - before) if nested else 0.0
                self.used += max(0.0, elapsed - already)
        self.remaining()

    def wrap(self, peer):
        if isinstance(peer, BudgetSocket) and peer.budget is self:
            return peer
        result = BudgetSocket(peer, self)
        with self.lock:
            self.sockets.add(result)
            invalid = self.cancelled or self.used >= self.seconds
        if invalid:
            result.abort()
            self.remaining()
        return result

    def cancel(self):
        with self.lock:
            self.cancelled = True
            peers = list(self.sockets)
        # Shutdown wakes a blocked read without waiting for the HTTPResponse's
        # buffered-read lock. Only this request's owned socket is touched.
        for peer in peers:
            peer.abort()


def limited_timeout(timeout, remaining):
    return min(timeout, remaining) if isinstance(timeout, (int, float)) else remaining


class BudgetSocket:
    def __init__(self, peer, budget):
        self.peer, self.budget = peer, budget
        self.requested_timeout = peer.gettimeout()
        self._io_refs, self._closed = 0, False

    def __getattr__(self, name):
        return getattr(self.peer, name)

    def settimeout(self, value):
        self.requested_timeout = value
        self.peer.settimeout(limited_timeout(value, self.budget.remaining()))

    def call(self, operation, *args):
        with self.budget.io(nested=True) as left:
            self.peer.settimeout(limited_timeout(self.requested_timeout, left))
            return getattr(self.peer, operation)(*args)

    def recv(self, *args):
        return self.call('recv', *args)

    def recv_into(self, *args):
        return self.call('recv_into', *args)

    def send(self, *args):
        return self.call('send', *args)

    def sendall(self, *args):
        return self.call('sendall', *args)

    def makefile(self, *args, **kwargs):
        # SocketIO must call our recv_into, including inside HTTP header and
        # chunk parsers; delegating peer.makefile would bypass the budget.
        return socket.socket.makefile(self, *args, **kwargs)

    def _decref_socketios(self):
        if self._io_refs > 0:
            self._io_refs -= 1
        if self._closed:
            self.close()

    def close(self):
        self._closed = True
        if self._io_refs == 0:
            self.peer.close()

    def abort(self):
        try:
            self.peer.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass


class BudgetAdapter(HTTPAdapter):
    def __init__(self, budget):
        self.budget = budget
        super().__init__(max_retries=0)

    def bind(self, manager):
        original = manager.pool_classes_by_scheme['https']
        if getattr(original, '_cc8_budget', None) is self.budget:
            return
        budget = self.budget

        class Connection(original.ConnectionCls):
            def connect(self):
                with budget.io(nested=True) as left:
                    self.timeout = limited_timeout(self.timeout, left)
                    super().connect()
                    self.sock = budget.wrap(self.sock)

            def _tunnel(self):
                self.sock = budget.wrap(self.sock)
                return super()._tunnel()

        class Pool(original):
            ConnectionCls = Connection
            _cc8_budget = budget

        # Never mutate urllib3's shared class map or another request's pool.
        manager.pool_classes_by_scheme = {**manager.pool_classes_by_scheme, 'https': Pool}

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.bind(self.poolmanager)

    def proxy_manager_for(self, *args, **kwargs):
        manager = super().proxy_manager_for(*args, **kwargs)
        self.bind(manager)
        return manager

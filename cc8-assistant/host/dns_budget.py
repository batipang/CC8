"""Bound caller DNS wait and TCP attempts without changing global socket APIs.

The OS resolver itself is not cancellable. At most four daemon lookups may
outlive cancelled/timed-out requests; there is no queued work or DNS cache.
Only the requesting thread can create/connect a socket after resolution.
"""

import socket
import threading
import time

from urllib3.util.connection import allowed_gai_family


class Resolver:
    def __init__(self, capacity=4):
        self.slots = threading.BoundedSemaphore(capacity)

    def lookup(self, host, port, budget, deadline):
        remaining(budget, deadline)
        if not self.slots.acquire(blocking=False):
            raise OSError('DNS resolver capacity is busy')
        done = threading.Event()
        result, failure = [], []

        def resolve():
            try:
                # Cancellation can win before the worker starts. A late result
                # has no callback capable of connecting or resuming a request.
                remaining(budget, deadline)
                result.extend(
                    socket.getaddrinfo(host, port, allowed_gai_family(), socket.SOCK_STREAM)
                )
            except BaseException as error:
                failure.append(error)
            finally:
                self.slots.release()
                done.set()

        worker = threading.Thread(target=resolve, name='cc8-dns', daemon=True)
        try:
            worker.start()
        except RuntimeError:
            self.slots.release()
            raise
        while True:
            left = remaining(budget, deadline)
            if done.wait(min(0.05, left)):
                remaining(budget, deadline)
                if failure:
                    raise failure[0]
                if not result or len(result) > 128:
                    raise OSError('Invalid DNS result count')
                return result


RESOLVER = Resolver()


def remaining(budget, deadline):
    left = min(budget.remaining(), deadline - time.monotonic())
    if left <= 0:
        raise TimeoutError('Connection deadline exhausted')
    return left


def connect(address, timeout, budget, *, source_address=None, socket_options=None):
    host, port = address
    if host.startswith('['):
        host = host.strip('[]')
    host.encode('idna')
    limit = budget.remaining()
    if isinstance(timeout, (int, float)):
        limit = min(limit, timeout)
    deadline = time.monotonic() + limit
    results = RESOLVER.lookup(host, port, budget, deadline)
    sources = None
    if source_address and source_address[0]:
        sources = RESOLVER.lookup(*source_address, budget, deadline)
    last_error = None
    for family, kind, protocol, _, target in results:
        left = remaining(budget, deadline)
        peer = socket.socket(family, kind, protocol)
        try:
            for option in socket_options or ():
                peer.setsockopt(*option)
            peer.settimeout(left)
            if source_address:
                local = source_address
                if sources is not None:
                    local = next((entry[4] for entry in sources if entry[0] == family), None)
                    if local is None:
                        raise OSError('No matching local address family')
                peer.bind(local)
            # getaddrinfo returns numeric socket addresses. Never re-resolve the
            # requested host here, and never connect from a detached DNS worker.
            remaining(budget, deadline)
            peer.connect(target)
            # The TLS handshake inherits this socket timeout. Carry forward
            # the remaining connection deadline after the TCP attempt.
            peer.settimeout(remaining(budget, deadline))
            return peer
        except OSError as error:
            last_error = error
            peer.close()
        except BaseException:
            peer.close()
            raise
    if last_error is not None:
        raise last_error
    raise OSError('No usable DNS addresses')

"""Bounded, serialized JSON-RPC writes to this client's owned POSIX pipe."""

import math
import os
import selectors
import threading
import time

from dot_receipt import DotError


def deadline_after(timeout):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('RPC timeout must be finite and positive')
    deadline = time.monotonic() + timeout
    if not math.isfinite(deadline):
        raise ValueError('RPC deadline exceeds clock range')
    return deadline


class RpcPipeWriter:
    def __init__(self):
        self.lock = threading.Lock()
        self.unusable = False

    def send(self, pipe, wire, deadline, is_current):
        # Do not hold the response/pending mutex while waiting for pipe space
        # or another writer. A blocked outbound packet must not block receipts.
        while True:
            self.require_current(is_current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DotError('Codex request timed out', kind='timeout')
            if self.lock.acquire(timeout=min(0.1, remaining)):
                break
        offset = 0
        try:
            self.require_current(is_current)
            fd = pipe.fileno()
            os.set_blocking(fd, False)
            with selectors.DefaultSelector() as selector:
                selector.register(fd, selectors.EVENT_WRITE)
                view = memoryview(wire)
                while offset < len(view):
                    self.require_current(is_current)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DotError('Codex request timed out', kind='timeout')
                    if not selector.select(min(0.1, remaining)):
                        continue
                    if time.monotonic() >= deadline:
                        continue
                    try:
                        count = os.write(fd, view[offset : offset + 65536])
                    except (BlockingIOError, InterruptedError):
                        continue
                    if count <= 0:
                        raise OSError('Closed owned RPC pipe')
                    offset += count
        except DotError:
            # Appending a new JSON line after an incomplete packet would join
            # two operations. Reject reuse rather than retrying uncertain work.
            self.unusable |= offset > 0
            raise
        except (OSError, ValueError):
            self.unusable = True
            raise DotError('Codex request could not be sent', kind='transport_or_io') from None
        finally:
            self.lock.release()

    def require_current(self, is_current):
        if self.unusable or not is_current():
            raise DotError('Codex is disconnected', kind='transport_or_io')

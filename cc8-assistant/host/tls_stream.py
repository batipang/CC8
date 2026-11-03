"""Serialize native SSL operations without holding a lock across network waits.

Each established LAN socket stays encrypted/authenticated as before. Nonblocking
SSL calls share one short lock; readiness waits happen outside it so a blocked
reader cannot prevent a writer or cancellation from making progress.
"""

from __future__ import annotations
import select
import math
import socket
import ssl
import threading
import time
from typing import Callable, TypeVar

T = TypeVar('T')


class TLSStream:
    def __init__(self, stream: ssl.SSLSocket, timeout: float = 10.0):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Invalid TLS I/O timeout')
        self.socket = stream
        self.timeout = timeout
        self._io_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        stream.setblocking(False)

    def _wait(self, reading: bool, deadline: float) -> None:
        if self._closed.is_set():
            raise EOFError('TLS transport closed')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('TLS transport I/O timed out')
        # Another SSL operation may consume the relevant record while this
        # waiter is asleep. A bounded poll retries SSL even without a new fd edge.
        try:
            select.select(
                [self.socket] if reading else [],
                [] if reading else [self.socket],
                [],
                min(0.05, remaining),
            )
        except (OSError, ValueError):
            if self._closed.is_set():
                raise EOFError('TLS transport closed') from None
            raise

    def _operation(self, action: Callable[[], T], deadline: float) -> T:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._io_lock.acquire(timeout=remaining):
                raise TimeoutError('TLS transport I/O timed out')
            try:
                if self._closed.is_set():
                    raise EOFError('TLS transport closed')
                if time.monotonic() >= deadline:
                    raise TimeoutError('TLS transport I/O timed out')
                try:
                    return action()
                except ssl.SSLWantReadError:
                    reading = True
                except ssl.SSLWantWriteError:
                    reading = False
            finally:
                self._io_lock.release()
            self._wait(reading, deadline)

    def recv(self, count: int) -> bytes:
        return self._operation(lambda: self.socket.recv(count), time.monotonic() + self.timeout)

    def sendall(self, raw: bytes) -> None:
        deadline = time.monotonic() + self.timeout
        if not self._write_lock.acquire(timeout=self.timeout):
            raise TimeoutError('TLS transport I/O timed out')
        try:
            if self._closed.is_set():
                raise EOFError('TLS transport closed')
            offset = 0
            while offset < len(raw):
                # SSL WANT retries must retain the same pending write bytes.
                pending = raw[offset:]
                count = self._operation(lambda: self.socket.send(pending), deadline)
                if count <= 0:
                    raise EOFError('TLS transport write closed')
                offset += count
        finally:
            self._write_lock.release()

    def shutdown(self, how: int = socket.SHUT_RDWR) -> None:
        self._closed.set()
        with self._io_lock:
            self.socket.shutdown(how)

    def close(self) -> None:
        self._closed.set()
        with self._io_lock:
            self.socket.close()

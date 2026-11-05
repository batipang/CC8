"""Own a single USB or TLS stream and its bounded packet reader."""

from __future__ import annotations
import queue
import socket
import threading
import time
import uuid
from typing import Literal, Protocol, TypeAlias
from protocol import Packet, receive, EVENT, decode_event, JsonValue
from release_info import RELEASE

TransportKind: TypeAlias = Literal['usb', 'wifi']
TransportEvent: TypeAlias = tuple[Literal['up', 'down', 'packet'], 'Channel', Packet | None]


class DuplexStream(Protocol):
    def recv(self, count: int, /) -> bytes: ...
    def sendall(self, data: bytes, /) -> None: ...
    def shutdown(self, how: int, /) -> None: ...
    def close(self) -> None: ...


class Channel:
    # USB 与 TLS 统一为包流；读线程投递事件，主循环选择唯一生效的通道。
    def __init__(
        self, stream: DuplexStream, kind: TransportKind, events: queue.Queue[TransportEvent]
    ) -> None:
        self.stream = stream
        self.kind = kind
        self.events = events
        self.ident = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.reader: threading.Thread | None = None
        self.reader_running = False
        self.io_closed = False
        self.closed = False
        self.ready = False
        self.last_rx = time.monotonic()

    def start(self) -> None:
        with self.state_lock:
            if self.closed:
                return
            if self.reader is not None:
                raise RuntimeError('Channel reader already started')
            self.reader_running = True
            self.reader = threading.Thread(
                target=self.read, daemon=True, name='cc8-' + self.kind + '-reader'
            )
        self.reader.start()

    def sendall(self, raw: bytes) -> None:
        with self.lock:
            if self.closed:
                raise EOFError('Transport closed')
            try:
                self.stream.sendall(raw)
            except (OSError, EOFError):
                # Invalidate before another writer can append to a partial frame.
                self.close()
                raise

    def event(self, name: str, generation: int = 0, **fields: JsonValue) -> None:
        self.sendall(Packet.event(name, generation, **fields).encode())

    def read(self) -> None:
        with self.state_lock:
            self.reader_running = True
        try:
            if self.closed:
                return
            first = receive(self.stream)
            hello = decode_event(first.data) if first.kind == EVENT else None
            if (
                not isinstance(hello, dict)
                or hello.get('event') != 'transport_hello'
                or not RELEASE.compatible(hello.get('protocol'))
            ):
                raise ValueError('Missing transport hello')
            self.ready = True
            self.last_rx = time.monotonic()
            self.events.put(('up', self, None), timeout=0.2)
            while not self.closed:
                packet = receive(self.stream)
                self.last_rx = time.monotonic()
                if packet.kind == EVENT:
                    event = decode_event(packet.data)
                    if event.get('event') == 'transport_pong':
                        continue
                self.events.put(('packet', self, packet), timeout=0.2)
        except (OSError, EOFError, ValueError, queue.Full):
            pass
        finally:
            self.close()
            # A foreign thread may shutdown() to wake recv(), but must not release the
            # TLS descriptor while SSL_read is still using it. Only this reader finalizes it.
            self._finish_close()
            with self.state_lock:
                self.reader_running = False
            try:
                self.events.put(('down', self, None), timeout=0.2)
            except queue.Full:
                pass

    def shutdown(self, *unused: object) -> None:
        self.close()

    def close(self) -> None:
        with self.state_lock:
            if self.closed:
                return
            self.closed = True
            self.ready = False
            finish = not self.reader_running
        try:
            self.stream.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        if finish:
            self._finish_close()

    def _finish_close(self) -> None:
        with self.state_lock:
            if self.io_closed:
                return
            self.io_closed = True
        with self.lock:
            try:
                self.stream.close()
            except OSError:
                pass

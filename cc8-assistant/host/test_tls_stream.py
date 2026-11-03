"""Serialized native SSL calls, nonblocking progress and real paired TLS traffic."""

import hashlib
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tls_stream
from tls_stream import TLSStream
import transport
from protocol import EVENT, RENDER, SPEAKER, Packet, receive


class FakeSocket:
    def __init__(self):
        self.blocking = None
        self.sent = bytearray()
        self.closed = False
        self.stopped = False

    def setblocking(self, value):
        self.blocking = value

    def send(self, data):
        count = min(3, len(data))
        self.sent.extend(data[:count])
        return count

    def recv(self, count):
        raise ssl.SSLWantReadError()

    def shutdown(self, how):
        self.stopped = True

    def close(self):
        self.closed = True


class TLSStreamTests(unittest.TestCase):
    def test_want_retries_keep_the_same_pending_bytes_and_short_writes_are_complete(self):
        calls = []

        class Socket(FakeSocket):
            def send(self, data):
                calls.append(data)
                if len(calls) == 1:
                    raise ssl.SSLWantWriteError()
                if len(calls) == 3:
                    raise ssl.SSLWantReadError()
                return super().send(data)

        raw = Socket()
        stream = TLSStream(raw)
        directions = []
        with patch.object(
            tls_stream,
            'select',
            SimpleNamespace(select=lambda r, w, e, t: directions.append('r' if r else 'w')),
        ):
            stream.sendall(b'public-payload')
        self.assertFalse(raw.blocking)
        self.assertEqual(raw.sent, b'public-payload')
        self.assertIs(calls[0], calls[1])
        self.assertIs(calls[2], calls[3])
        self.assertEqual(directions, ['w', 'r'])

    def test_network_read_wait_does_not_hold_ssl_lock_or_block_a_writer(self):
        waiting, written = threading.Event(), threading.Event()
        result, failures = [], []

        class Socket(FakeSocket):
            def recv(self, count):
                if written.is_set():
                    return b'R'
                raise ssl.SSLWantReadError()

            def send(self, data):
                written.set()
                return super().send(data)

        def wait(*args):
            waiting.set()
            written.wait(1)

        stream = TLSStream(Socket(), timeout=2)

        def read():
            try:
                result.append(stream.recv(1))
            except Exception as error:
                failures.append(type(error).__name__)

        with patch.object(tls_stream, 'select', SimpleNamespace(select=wait)):
            worker = threading.Thread(target=read)
            worker.start()
            try:
                self.assertTrue(waiting.wait(1))
                stream.sendall(b'W')
            finally:
                written.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(result, [b'R'])

    def test_native_read_and_write_calls_cannot_overlap(self):
        entered, release, wrote = threading.Event(), threading.Event(), threading.Event()
        overlap, errors = [], []

        class Socket(FakeSocket):
            active = False

            def recv(self, count):
                self.active = True
                entered.set()
                try:
                    release.wait(1)
                    return b'R'
                finally:
                    self.active = False

            def send(self, data):
                if self.active:
                    overlap.append(True)
                wrote.set()
                return super().send(data)

        stream = TLSStream(Socket(), timeout=2)

        def write():
            try:
                stream.sendall(b'W')
            except Exception as error:
                errors.append(type(error).__name__)

        reader = threading.Thread(target=lambda: stream.recv(1))
        writer = threading.Thread(target=write)
        reader.start()
        try:
            self.assertTrue(entered.wait(1))
            writer.start()
            self.assertFalse(wrote.wait(0.05))
        finally:
            release.set()
            reader.join(2)
            if writer.ident is not None:
                writer.join(2)
        self.assertFalse(reader.is_alive() or writer.is_alive())
        self.assertEqual(overlap, [])
        self.assertEqual(errors, [])
        self.assertTrue(wrote.is_set())

    def test_repeated_wants_and_partial_progress_share_one_operation_deadline(self):
        for operation in ('read', 'write'):
            with self.subTest(operation=operation):
                clock = [0.0]
                calls = []

                class Socket(FakeSocket):
                    def recv(self, count):
                        calls.append('read')
                        raise ssl.SSLWantReadError()

                    def send(self, data):
                        calls.append('write')
                        clock[0] += 0.4
                        return 1

                def wait(*args):
                    clock[0] += 0.4

                with (
                    patch.object(tls_stream, 'time', SimpleNamespace(monotonic=lambda: clock[0])),
                    patch.object(tls_stream, 'select', SimpleNamespace(select=wait)),
                ):
                    stream = TLSStream(Socket(), timeout=1)
                    with self.assertRaises(TimeoutError):
                        stream.recv(1) if operation == 'read' else stream.sendall(b'four')
                self.assertEqual(len(calls), 3)

    def test_close_wakes_a_waiter_and_prevents_more_native_io(self):
        waiting, wake = threading.Event(), threading.Event()
        errors, calls = [], []

        class Socket(FakeSocket):
            def recv(self, count):
                calls.append('read')
                return super().recv(count)

            def shutdown(self, how):
                super().shutdown(how)
                wake.set()

        def wait(*args):
            waiting.set()
            wake.wait(1)

        raw = Socket()
        stream = TLSStream(raw)

        def read():
            try:
                stream.recv(1)
            except Exception as error:
                errors.append(type(error).__name__)

        with patch.object(tls_stream, 'select', SimpleNamespace(select=wait)):
            reader = threading.Thread(target=read)
            reader.start()
            self.assertTrue(waiting.wait(1))
            stream.shutdown(socket.SHUT_RDWR)
            reader.join(2)
        stream.close()
        with self.assertRaises(EOFError):
            stream.sendall(b'closed')
        with self.assertRaises(EOFError):
            stream.recv(1)
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, ['EOFError'])
        self.assertEqual(calls, ['read'])
        self.assertTrue(raw.stopped and raw.closed)

    def test_zero_write_and_invalid_timeouts_fail_without_spinning_or_reconfiguring(self):
        raw = FakeSocket()
        for timeout in (False, True, 0, -1, float('inf'), float('nan')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                TLSStream(raw, timeout=timeout)
        self.assertIsNone(raw.blocking)
        raw.send = lambda data: 0
        with self.assertRaises(EOFError):
            TLSStream(raw).sendall(b'public')


class TLSIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.pairing = transport.Pairing(Path(cls.temporary.name) / 'pair')

    def hub(self):
        def absent():
            raise RuntimeError('No USB in isolated TLS test')

        hub = transport.TransportHub(
            self.pairing, absent, lambda: False, bind='127.0.0.1', tls_port=0, discovery_port=0
        )
        hub.start()
        self.addCleanup(hub.close)
        return hub

    def connect(self, hub):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        client = context.wrap_socket(socket.socket(), server_hostname='Bumblebee Local Bridge')
        self.addCleanup(client.close)
        client.settimeout(3)
        client.connect(hub.server.getsockname())
        self.assertEqual(
            hashlib.sha256(client.getpeercert(binary_form=True)).hexdigest(), self.pairing.pin
        )
        client.sendall(Packet.event('transport_auth', token=self.pairing.token).encode())
        self.assertEqual(json.loads(receive(client).data)['event'], 'transport_welcome')
        client.sendall(Packet.event('transport_hello', protocol=2).encode())
        kind, channel, _ = hub.events.get(timeout=3)
        self.assertEqual(kind, 'up')
        return client, channel

    def test_authenticated_wifi_registration_uses_serialized_tls_stream(self):
        hub = self.hub()
        client, channel = self.connect(hub)
        self.assertIsInstance(channel.stream, TLSStream)
        client.close()

    def test_real_tls_guard_rejects_overlapping_native_calls_before_corruption(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors, overlaps = [], []

        class GuardedSocket(ssl.SSLSocket):
            def recv(self, count, flags=0):
                if (
                    getattr(self, '_channel', None)
                    and self._channel.ready
                    and not getattr(self, '_held_once', False)
                ):
                    self._held_once = True
                    self._read_active = True
                    entered.set()
                    try:
                        release.wait(2)
                        return super().recv(count, flags)
                    finally:
                        self._read_active = False
                return super().recv(count, flags)

            def send(self, data, flags=0):
                if getattr(self, '_read_active', False):
                    overlaps.append(True)
                    raise OSError('Test guard rejected overlapping SSL calls')
                return super().send(data, flags)

        original = transport.Channel

        class ObservedChannel(original):
            def __init__(self, stream, *args):
                super().__init__(stream, *args)
                raw = stream.socket if isinstance(stream, TLSStream) else stream
                raw._channel = self

        previous = self.pairing.context.sslsocket_class
        self.pairing.context.sslsocket_class = GuardedSocket
        writer = None
        try:
            with patch.object(transport, 'Channel', ObservedChannel):
                hub = self.hub()
                client, channel = self.connect(hub)
                self.assertTrue(entered.wait(2))

                def select_channel():
                    try:
                        hub.selected(channel)
                    except Exception as error:
                        errors.append(type(error).__name__)
                    finally:
                        finished.set()

                writer = threading.Thread(target=select_channel)
                writer.start()
                finished.wait(0.05)
                release.set()
                self.assertTrue(finished.wait(2))
                # Also release the old blocking reader in the before-fix run.
                client.sendall(Packet.event('screen_touch').encode())
                self.assertEqual(overlaps, [], 'Native SSL read/write overlapped')
                self.assertEqual(errors, [])
                self.assertEqual(json.loads(receive(client).data)['event'], 'transport_activate')
        finally:
            release.set()
            self.pairing.context.sslsocket_class = previous
            if writer is not None:
                writer.join(3)

    def test_real_encrypted_duplex_packets_remain_complete_and_close_cleanly(self):
        hub = self.hub()
        client, channel = self.connect(hub)
        peer = TLSStream(client, timeout=3)
        errors = []
        payload = b'\x01\x02' * 160

        def server_write():
            try:
                for i in range(60):
                    channel.sendall(Packet(SPEAKER, 1, i, i * 160, i + 1, payload).encode())
            except Exception as error:
                errors.append(type(error).__name__)

        def client_write():
            try:
                for i in range(60):
                    peer.sendall(Packet(RENDER, 1, i, i * 160, i + 1, payload).encode())
            except Exception as error:
                errors.append(type(error).__name__)

        workers = [threading.Thread(target=server_write), threading.Thread(target=client_write)]
        for worker in workers:
            worker.start()
        try:
            seen = []
            while len(seen) < 60:
                packet = receive(peer)
                if packet.kind == EVENT:
                    self.assertEqual(json.loads(packet.data)['event'], 'transport_ping')
                    peer.sendall(Packet.event('transport_pong').encode())
                else:
                    self.assertEqual(packet.kind, SPEAKER)
                    self.assertEqual(packet.data, payload)
                    seen.append(packet.sequence)
            self.assertEqual(seen, list(range(60)))
            for i in range(60):
                kind, source, packet = hub.events.get(timeout=3)
                self.assertEqual(
                    (kind, source, packet.kind, packet.sequence, packet.data),
                    ('packet', channel, RENDER, i, payload),
                )
        finally:
            peer.shutdown()
            peer.close()
            for worker in workers:
                worker.join(3)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        channel.reader.join(3)
        self.assertFalse(channel.reader.is_alive())
        self.assertTrue(channel.closed and channel.io_closed)

    def test_real_tls_backpressure_has_a_deadline_and_shutdown_releases_reader(self):
        hub = self.hub()
        client, channel = self.connect(hub)
        channel.stream.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        channel.stream.timeout = 0.2
        started = time.monotonic()
        try:
            # Deliberately stop consuming TLS records. This exceeds both socket
            # buffers and exercises WANT_WRITE on a real encrypted connection.
            with self.assertRaises(TimeoutError):
                channel.sendall(b'x' * (4 * 1024**2))
            self.assertLess(time.monotonic() - started, 2)
        finally:
            channel.close()
            client.close()
            channel.reader.join(3)
        self.assertFalse(channel.reader.is_alive())
        self.assertTrue(channel.closed and channel.io_closed)


if __name__ == '__main__':
    unittest.main()

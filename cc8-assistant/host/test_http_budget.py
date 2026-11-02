"""Real loopback TLS drip responses, without devices or provider requests."""

import json
from pathlib import Path
import select
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import requests

from minimax_client import Transport, MiniMaxError, Cancelled, RequestSlot
from http_budget import IOBudget, BudgetAdapter, BudgetSocket


class DripServer:
    def __init__(self, context, mode, body):
        self.context, self.mode, self.body = context, mode, body
        self.stopped = threading.Event()
        self.headers_received = threading.Event()
        self.errors = []
        self.sent = 0
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(2)
        self.port = self.listener.getsockname()[1]
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def run(self):
        peer = None
        try:
            peer, _ = self.listener.accept()
            peer.settimeout(2)
            if self.mode != 'proxy_headers':
                peer = self.context.wrap_socket(peer, server_side=True)
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = peer.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, _, payload = data.partition(b'\r\n\r\n')
            length = next(
                (
                    int(line.split(b':', 1)[1])
                    for line in head.split(b'\r\n')
                    if line.lower().startswith(b'content-length:')
                ),
                0,
            )
            if length > 65536:
                raise ValueError('Fixture request too large')
            while len(payload) < length:
                chunk = peer.recv(4096)
                if not chunk:
                    return
                payload += chunk
            self.headers_received.set()
            if self.mode == 'hold_headers':
                self.stopped.wait(2)
                return
            if self.mode == 'proxy_headers':
                wire = b'HTTP/1.0 200 Connection established\r\nX-Test: ' + b'a' * 60 + b'\r\n\r\n'
            else:
                headers = (
                    b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: '
                    + str(len(self.body)).encode()
                    + b'\r\nConnection: close\r\n\r\n'
                )
                if self.mode == 'immediate':
                    peer.sendall(headers + self.body)
                    return
                if self.mode == 'hold_body':
                    peer.sendall(headers)
                    self.stopped.wait(2)
                    return
                if self.mode == 'headers':
                    wire = headers + self.body
                else:
                    peer.sendall(headers)
                    wire = self.body
            for byte in wire:
                if self.stopped.wait(0.01):
                    return
                peer.sendall(bytes((byte,)))
                self.sent += 1
        except (OSError, ssl.SSLError):
            # A deadline/cancellation deliberately interrupts the local peer.
            pass
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            if peer is not None:
                peer.close()

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.worker.join(3)
        if self.worker.is_alive() or self.errors:
            raise AssertionError('Local fixture did not terminate cleanly')


class ConnectProxy:
    """One bounded plaintext CONNECT tunnel to the local TLS fixture."""

    def __init__(self, target_port):
        self.target_port = target_port
        self.stopped, self.tunnelled = threading.Event(), threading.Event()
        self.errors = []
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(2)
        self.port = self.listener.getsockname()[1]
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def run(self):
        client, upstream = None, None
        try:
            client, _ = self.listener.accept()
            client.settimeout(1)
            data = b''
            while b'\r\n\r\n' not in data:
                piece = client.recv(4096)
                if not piece:
                    return
                data += piece
                if len(data) > 8192:
                    raise ValueError('Fixture CONNECT header too large')
            if not data.startswith(f'CONNECT localhost:{self.target_port} '.encode()):
                raise ValueError('Unexpected fixture CONNECT target')
            upstream = socket.create_connection(('127.0.0.1', self.target_port), timeout=1)
            client.sendall(b'HTTP/1.0 200 Connection established\r\n\r\n')
            self.tunnelled.set()
            deadline = time.monotonic() + 4
            while not self.stopped.is_set() and time.monotonic() < deadline:
                for peer in select.select([client, upstream], [], [], 0.05)[0]:
                    chunk = peer.recv(8192)
                    if not chunk:
                        return
                    (upstream if peer is client else client).sendall(chunk)
        except OSError:
            pass
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            if client is not None:
                client.close()
            if upstream is not None:
                upstream.close()

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.worker.join(3)
        if self.worker.is_alive() or self.errors:
            raise AssertionError('Local proxy fixture did not terminate cleanly')


class HTTPBudgetTLS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        cls.cert = root / 'cert.pem'
        key = root / 'key.pem'
        subprocess.run(
            [
                'openssl',
                'req',
                '-x509',
                '-newkey',
                'rsa:2048',
                '-nodes',
                '-days',
                '1',
                '-keyout',
                str(key),
                '-out',
                str(cls.cert),
                '-subj',
                '/CN=localhost',
                '-addext',
                'subjectAltName=DNS:localhost',
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(cls.cert, key)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def client(self, server):
        def session():
            result = requests.Session()
            result.trust_env = False
            result.verify = str(self.cert)
            if server.mode == 'proxy_headers':
                result.proxies = {'https': f'http://127.0.0.1:{server.port}'}
            return result

        transport = Transport(key='sk-cp-test-not-a-secret', session_factory=session)
        # Only this test instance targets the ephemeral, certificate-verified
        # loopback server. Production endpoint validation stays unchanged.
        transport.base = f'https://localhost:{server.port}'
        transport.timeout = (0.15, 0.15)
        return transport

    def assert_drip_timeout(self, mode, operation):
        body = (
            b'data: {"data":{"status":2,"value":"public-fixture"}}\n\ndata: [DONE]\n\n'
            if operation == 'events'
            else json.dumps({'base_resp': {'status_code': 0}, 'text': 'public-fixture'}).encode()
        )
        server = DripServer(self.context, mode, body)
        try:
            client = self.client(server)
            started = time.monotonic()
            with self.assertRaises(MiniMaxError) as failure:
                if operation == 'transcribe':
                    client.transcribe(b'public-fixture')
                elif operation == 'post_json':
                    client.post_json('/v1/test', {})
                else:
                    list(client.events('/v1/test', {}))
            self.assertEqual(failure.exception.kind, 'timeout')
            self.assertNotIn('public-fixture', str(failure.exception))
            self.assertLess(time.monotonic() - started, 2)
            self.assertGreater(server.sent, 5)
            self.assertEqual(client.slots, set())
        finally:
            server.close()

    def test_responsive_drip_headers_cannot_extend_request_forever(self):
        self.assert_drip_timeout('headers', 'transcribe')

    def test_responsive_drip_json_cannot_extend_request_forever(self):
        for operation in ('transcribe', 'post_json'):
            with self.subTest(operation=operation):
                self.assert_drip_timeout('body', operation)

    def test_responsive_drip_sse_cannot_extend_request_forever(self):
        self.assert_drip_timeout('body', 'events')

    def test_proxy_connect_drip_is_subject_to_the_same_budget(self):
        self.assert_drip_timeout('proxy_headers', 'post_json')

    def assert_normal_verified_tls_asr_json_and_sse(self):
        bodies = (
            ('transcribe', b'{"text":"public-fixture"}'),
            ('post_json', b'{"value":1}'),
            ('events', b'data: {"value":1}\n\ndata: [DONE]\n\n'),
        )
        for operation, body in bodies:
            server = DripServer(self.context, 'immediate', body)
            try:
                client = self.client(server)
                client.timeout = (0.5, 0.5)
                if operation == 'transcribe':
                    self.assertEqual(client.transcribe(b'public-fixture'), 'public-fixture')
                elif operation == 'post_json':
                    self.assertEqual(client.post_json('/v1/test', {}), {'value': 1})
                else:
                    self.assertEqual(list(client.events('/v1/test', {})), [{'value': 1}])
                self.assertEqual(client.slots, set())
            finally:
                server.close()

    def test_normal_verified_tls_asr_json_and_sse_still_complete_once(self):
        self.assert_normal_verified_tls_asr_json_and_sse()

    def assert_cancel_interrupts_read(self, mode):
        server = DripServer(self.context, mode, b'{"value":1}')
        client = self.client(server)
        client.timeout = (2, 2)
        body_reading = threading.Event()
        original_factory = client.factory

        def factory():
            session = original_factory()
            original_post = session.post

            def post(*args, **kwargs):
                response = original_post(*args, **kwargs)
                original_content = response.iter_content

                def content(*args, **kwargs):
                    body_reading.set()
                    yield from original_content(*args, **kwargs)

                response.iter_content = content
                return response

            session.post = post
            return session

        client.factory = factory
        outcomes = []

        def request():
            try:
                client.post_json('/v1/test', {})
            except Exception as error:
                outcomes.append(type(error))

        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        try:
            self.assertTrue(server.headers_received.wait(2))
            if mode == 'hold_body':
                self.assertTrue(body_reading.wait(2))
            client.cancel()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes, [Cancelled])
            self.assertEqual(client.slots, set())
        finally:
            server.close()
            worker.join(3)
        # Cancellation must not mutate the globally shared urllib3 pool map.
        self.assert_normal_verified_tls_asr_json_and_sse()

    def test_cancellation_interrupts_a_header_read_without_waiting_for_read_timeout(self):
        self.assert_cancel_interrupts_read('hold_headers')

    def test_cancellation_interrupts_a_body_read_without_waiting_for_read_timeout(self):
        self.assert_cancel_interrupts_read('hold_body')

    def test_hostname_verification_is_not_relaxed_by_socket_wrapping(self):
        server = DripServer(self.context, 'immediate', b'{"value":1}')
        try:
            client = self.client(server)
            client.base = f'https://127.0.0.1:{server.port}'
            with self.assertRaises(MiniMaxError):
                client.post_json('/v1/test', {})
            self.assertFalse(server.headers_received.is_set())
            self.assertEqual(client.slots, set())
        finally:
            server.close()

    def test_successful_connect_tunnel_preserves_verified_tls_and_json(self):
        server = DripServer(self.context, 'immediate', b'{"value":1}')
        proxy = ConnectProxy(server.port)
        try:
            client = self.client(server)
            original_factory = client.factory

            def factory():
                session = original_factory()
                session.proxies = {'https': f'http://127.0.0.1:{proxy.port}'}
                return session

            client.factory = factory
            client.timeout = (0.5, 0.5)
            self.assertEqual(client.post_json('/v1/test', {}), {'value': 1})
            self.assertTrue(proxy.tunnelled.is_set())
            self.assertTrue(server.headers_received.is_set())
            self.assertEqual(client.slots, set())
        finally:
            proxy.close()
            server.close()


class BudgetUnitTests(unittest.TestCase):
    def test_invalid_limits_fail_without_io(self):
        for value in (True, 0, -1, 'SECRET', float('nan'), float('inf'), 10**1000):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                IOBudget(value)

    def test_nested_connect_and_proxy_reads_are_charged_once(self):
        now = [0.0]
        budget = IOBudget(10, clock=lambda: now[0])
        with budget.io(nested=True):
            now[0] = 1
            with budget.io(nested=True):
                now[0] = 3
            now[0] = 4
        self.assertEqual(budget.used, 4)
        self.assertEqual(budget.remaining(), 6)

    def test_consumer_playback_and_processing_time_does_not_consume_network_budget(self):
        now = [0.0]
        budget = IOBudget(2, clock=lambda: now[0])
        with budget.io():
            now[0] += 0.25
        now[0] += 900  # Not inside an I/O operation: yielded text/PCM is in use.
        self.assertEqual(budget.remaining(), 1.75)
        with budget.io():
            now[0] += 0.25
        self.assertEqual(budget.used, 0.5)

    def test_last_read_cannot_return_success_after_exhausting_budget(self):
        now = [0.0]
        budget = IOBudget(0.1, clock=lambda: now[0])
        with self.assertRaises(TimeoutError):
            with budget.io():
                now[0] += 0.11
        self.assertTrue(budget.expired)

    def test_makefile_buffered_reads_cannot_bypass_budget_socket(self):
        now = [0.0]

        class Peer:
            gettimeout = lambda self: 1
            settimeout = lambda self, value: None
            close = lambda self: None

            def recv_into(self, buffer):
                now[0] += 0.04
                buffer[0] = ord('a')
                return 1

        budget = IOBudget(0.1, clock=lambda: now[0])
        peer = BudgetSocket(Peer(), budget)
        with peer.makefile('rb') as reader:
            with self.assertRaises(TimeoutError):
                reader.read(10)
        peer.close()
        self.assertTrue(budget.expired)
        self.assertEqual(peer._io_refs, 0)

    def test_direct_and_proxy_pools_remain_per_request(self):
        from urllib3.poolmanager import pool_classes_by_scheme

        original = dict(pool_classes_by_scheme)
        first, second = IOBudget(1), IOBudget(1)
        a, b = BudgetAdapter(first), BudgetAdapter(second)
        try:
            self.assertEqual(pool_classes_by_scheme, original)
            self.assertIs(a.poolmanager.pool_classes_by_scheme['https']._cc8_budget, first)
            self.assertIs(b.poolmanager.pool_classes_by_scheme['https']._cc8_budget, second)
            proxy = a.proxy_manager_for('http://localhost:1234')
            pool = proxy.pool_classes_by_scheme['https']
            self.assertIs(pool._cc8_budget, first)
            self.assertIs(
                a.proxy_manager_for('http://localhost:1234').pool_classes_by_scheme['https'], pool
            )
            self.assertEqual(pool_classes_by_scheme, original)
        finally:
            a.close()
            b.close()

    def test_cancel_only_shuts_down_owned_sockets_and_rejects_new_ones(self):
        first, second = IOBudget(1), IOBudget(1)
        a, b = Mock(), Mock()
        pa, pb = first.wrap(a), second.wrap(b)
        first.cancel()
        a.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        b.shutdown.assert_not_called()
        self.assertIsNotNone(pa)
        self.assertIsNotNone(pb)
        c = Mock()
        with self.assertRaises(OSError):
            first.wrap(c)
        c.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_repeated_cancel_does_not_create_unbounded_closer_threads(self):
        response = Mock()
        slot = RequestSlot(response=response, budget=IOBudget(1))
        for _ in range(30):
            slot.cancel()
        deadline = time.monotonic() + 1
        while response.close.call_count == 0 and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        response.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()

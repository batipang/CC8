"""Real loopback TLS drip responses, without devices or provider requests."""

from http.server import BaseHTTPRequestHandler, HTTPServer
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
from unittest.mock import Mock, patch

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
        self.first_byte_at = self.last_byte_at = None
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
                wire = b'HTTP/1.0 200 Connection established\r\nX-Test: ' + b'a' * 512 + b'\r\n\r\n'
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
                self.last_byte_at = time.monotonic()
                if self.first_byte_at is None:
                    self.first_byte_at = self.last_byte_at
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
    """One bounded CONNECT tunnel to the local TLS fixture."""

    def __init__(self, target_port, context=None):
        self.target_port = target_port
        self.context = context
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
            if self.context is not None:
                client = self.context.wrap_socket(client, server_side=True)
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
        # The old 300ms total left too few bytes after TLS/setup on busy macOS
        # runners. Use a longer finite drip AND budget; a completed response is
        # still impossible within that budget, without changing production.
        body = (b':' + b'a' * 512 + b'\n\n' if operation == 'events' else b' ' * 512) + body
        server = DripServer(self.context, mode, body)
        try:
            client = self.client(server)
            client.timeout = (0.75, 0.75)
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
            self.assertLess(time.monotonic() - started, 3.5)
            self.assertGreater(server.sent, 5)
            # This is a responsive-drip test, not an ordinary inactive socket:
            # traffic must have continued beyond a whole read-timeout window.
            self.assertIsNotNone(server.first_byte_at)
            self.assertIsNotNone(server.last_byte_at)
            self.assertGreater(server.last_byte_at - server.first_byte_at, client.timeout[1])
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

    def test_proxy_connect_drip_cannot_spend_the_response_read_budget(self):
        proxy = DripServer(self.context, 'proxy_headers', b'')
        try:
            client = self.client(proxy)
            client.timeout = (0.35, 1.65)
            started = time.monotonic()
            with self.assertRaises(MiniMaxError) as failure:
                client.post_json('/v1/test', {})
            self.assertEqual(failure.exception.kind, 'timeout')
            self.assertTrue(proxy.headers_received.is_set())
            self.assertLess(time.monotonic() - started, 1.25)
            self.assertEqual(client.slots, set())
        finally:
            proxy.close()

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

    def scripted_json_server(self, replies):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append((self.path, payload))
                status, headers, data = replies[min(len(seen), len(replies)) - 1]
                wire = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(wire)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(wire)

        server = HTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        server.socket = self.context.wrap_socket(server.socket, server_side=True)
        server.port, server.mode = server.server_port, 'scripted'
        worker = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        client = self.client(server)
        client.timeout = (0.5, 0.5)
        return client, seen

    def test_search_retries_one_explicit_gateway_failure_over_verified_tls(self):
        route = '/v1/coding_plan/search'
        query = {'q': 'public-fixture'}
        result = {'base_resp': {'status_code': 0}, 'organic': []}
        for status in (502, 503, 504):
            with self.subTest(status=status):
                client, seen = self.scripted_json_server([(status, {}, {}), (200, {}, result)])
                self.assertEqual(client.post_json(route, query), result)
                self.assertEqual(seen, [(route, query), (route, query)])
                self.assertFalse(client.slots)

    def test_search_gateway_retry_stops_at_second_response(self):
        client, seen = self.scripted_json_server([(503, {}, {}), (504, {}, {})])
        with self.assertRaises(MiniMaxError) as failure:
            client.post_json('/v1/coding_plan/search', {'q': 'public-fixture'})
        self.assertEqual(failure.exception.http_status, 504)
        self.assertEqual(len(seen), 2)
        self.assertFalse(client.slots)

    def test_search_never_retries_deferred_or_non_gateway_errors_or_other_routes(self):
        search = '/v1/coding_plan/search'
        for route, status, headers, body in (
            (search, 503, {'Retry-After': '120'}, {}),
            (search, 500, {}, {}),
            (search, 401, {}, {}),
            (search, 402, {}, {}),
            (search, 403, {}, {}),
            (search, 429, {}, {}),
            (search, 200, {}, {'base_resp': {'status_code': 1001}}),
            ('/v1/chat/completions', 503, {}, {}),
        ):
            with self.subTest(route=route, status=status):
                client, seen = self.scripted_json_server([(status, headers, body)])
                with self.assertRaises(MiniMaxError):
                    client.post_json(route, {'q': 'public-fixture'})
                self.assertEqual(len(seen), 1)
                self.assertFalse(client.slots)

    def test_search_retry_checks_original_budget_and_cancellation_after_closing_response(self):
        for change in ('expired', 'cancelled', 'stale'):
            with self.subTest(change=change):
                client, seen = self.scripted_json_server([(502, {}, {})])
                factory = client.factory
                current = True

                def session_factory():
                    session = factory()

                    def attach(response, **kwargs):
                        close = response.close

                        def end():
                            nonlocal current
                            close()
                            # A repeated cleanup after the slot is gone has no effect.
                            if client.slots:
                                slot = next(iter(client.slots))
                                if change == 'expired':
                                    slot.budget.used = slot.budget.seconds
                                elif change == 'cancelled':
                                    slot.stopped.set()
                                    slot.budget.cancel()
                                else:
                                    current = False

                        response.close = end

                    session.hooks['response'].append(attach)
                    return session

                client.factory = session_factory
                expected = MiniMaxError if change == 'expired' else Cancelled
                with self.assertRaises(expected) as failure:
                    client.post_json('/v1/coding_plan/search', {'q': 'fixture'}, lambda: current)
                if change == 'expired':
                    self.assertEqual(failure.exception.kind, 'timeout')
                self.assertEqual(len(seen), 1)
                self.assertFalse(client.slots)

    def test_http_200_business_failures_cross_verified_tls_without_retry_or_content_leak(self):
        from diagnostics import error_kind

        secret = 'PRIVATE-business-status-body'
        for code, wanted in (
            (1001, 'timeout'),
            (1002, 'rate_limit'),
            (1004, 'credential_or_permission'),
            (1008, 'quota'),
        ):
            for operation in ('transcribe', 'post_json', 'events'):
                for repeat in range(3):
                    with self.subTest(code=code, operation=operation, repeat=repeat):
                        data = {
                            'base_resp': {'status_code': code, 'status_msg': secret},
                            'text': secret,
                        }
                        wire = json.dumps(data).encode()
                        if operation == 'events':
                            wire = b'data: ' + wire + b'\n\n'
                        server = DripServer(self.context, 'immediate', wire)
                        try:
                            client = self.client(server)
                            client.timeout = (2, 2)
                            with self.assertRaises(MiniMaxError) as caught:
                                if operation == 'transcribe':
                                    client.transcribe(b'public-fixture')
                                elif operation == 'post_json':
                                    client.post_json('/fixture', {})
                                else:
                                    list(client.events('/fixture', {}))
                            self.assertEqual(error_kind(caught.exception), wanted)
                            self.assertEqual(caught.exception.code, code)
                            self.assertNotIn(secret, str(caught.exception))
                            self.assertEqual(client.slots, set())
                            self.assertTrue(server.headers_received.is_set())
                        finally:
                            server.close()
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

    def test_direct_tls_receives_only_the_unspent_tcp_connection_deadline(self):
        original_connect = socket.socket.connect
        original_wrap = ssl.SSLContext.wrap_socket
        tcp_elapsed = 0
        handshake_timeouts = []

        def connect(peer, address):
            nonlocal tcp_elapsed
            original_connect(peer, address)
            tcp_elapsed = 0.75

        def wrap(context, peer, *args, **kwargs):
            if not kwargs.get('server_side'):
                handshake_timeouts.append(peer.gettimeout())
            return original_wrap(context, peer, *args, **kwargs)

        server = DripServer(self.context, 'immediate', b'{"value":1}')
        client = self.client(server)
        client.timeout = (1, 1)
        try:
            with (
                patch('dns_budget.time') as clock,
                patch.object(socket.socket, 'connect', connect),
                patch.object(ssl.SSLContext, 'wrap_socket', wrap),
            ):
                clock.monotonic.side_effect = lambda: tcp_elapsed
                self.assertEqual(client.post_json('/v1/test', {}), {'value': 1})
            self.assertEqual(handshake_timeouts, [0.25])
            self.assertEqual(client.slots, set())
        finally:
            server.close()

    def test_stalled_dns_times_out_before_the_os_lookup_finishes(self):
        original = socket.getaddrinfo
        for operation in ('transcribe', 'post_json', 'events'):
            with self.subTest(operation=operation):
                release, finished = threading.Event(), threading.Event()
                server = DripServer(self.context, 'immediate', b'{"value":1}')
                timer = threading.Timer(1.5, release.set)

                def stalled(*args, **kwargs):
                    try:
                        release.wait(3)
                        return original(*args, **kwargs)
                    finally:
                        finished.set()

                client = self.client(server)
                client.timeout = (0.15, 0.15)
                timer.start()
                try:
                    with patch('socket.getaddrinfo', side_effect=stalled):
                        began = time.monotonic()
                        with self.assertRaises(MiniMaxError) as failure:
                            if operation == 'transcribe':
                                client.transcribe(b'public-fixture')
                            elif operation == 'events':
                                list(client.events('/v1/test', {}))
                            else:
                                client.post_json('/v1/test', {})
                        elapsed = time.monotonic() - began
                        self.assertEqual(failure.exception.kind, 'timeout')
                        self.assertLess(elapsed, 0.9)
                        self.assertFalse(release.is_set())
                        self.assertFalse(server.headers_received.is_set())
                        self.assertEqual(client.slots, set())
                        release.set()
                        self.assertTrue(finished.wait(2))
                finally:
                    release.set()
                    timer.cancel()
                    timer.join(2)
                    server.close()
        self.assert_normal_verified_tls_asr_json_and_sse()

    def test_cancellation_returns_while_dns_is_still_blocked(self):
        original = socket.getaddrinfo
        entered, release, finished = (threading.Event() for _ in range(3))
        server = DripServer(self.context, 'immediate', b'{"value":1}')
        client = self.client(server)
        client.timeout = (2, 2)
        outcomes = []

        def stalled(*args, **kwargs):
            entered.set()
            try:
                release.wait(4)
                return original(*args, **kwargs)
            finally:
                finished.set()

        def request():
            try:
                client.post_json('/v1/test', {})
            except Exception as error:
                outcomes.append(type(error))

        worker = threading.Thread(target=request, daemon=True)
        try:
            with patch('socket.getaddrinfo', side_effect=stalled):
                try:
                    worker.start()
                    self.assertTrue(entered.wait(2))
                    client.cancel()
                    worker.join(0.6)
                    self.assertFalse(worker.is_alive())
                    self.assertFalse(release.is_set())
                    self.assertEqual(outcomes, [Cancelled])
                    self.assertEqual(client.slots, set())
                    self.assertFalse(server.headers_received.is_set())
                finally:
                    release.set()
                    self.assertTrue(finished.wait(2))
                    worker.join(3)
        finally:
            server.close()
        self.assert_normal_verified_tls_asr_json_and_sse()

    def test_proxy_dns_uses_the_same_deadline_before_any_connect(self):
        original = socket.getaddrinfo
        release, finished = threading.Event(), threading.Event()
        server = DripServer(self.context, 'immediate', b'{"value":1}')
        proxy = ConnectProxy(server.port)
        client = self.client(server)
        factory = client.factory

        def session():
            value = factory()
            value.proxies = {'https': f'http://localhost:{proxy.port}'}
            return value

        def stalled(*args, **kwargs):
            try:
                release.wait(3)
                return original(*args, **kwargs)
            finally:
                finished.set()

        client.factory = session
        timer = threading.Timer(1.5, release.set)
        timer.start()
        try:
            with patch('socket.getaddrinfo', side_effect=stalled):
                try:
                    began = time.monotonic()
                    with self.assertRaises(MiniMaxError) as failure:
                        client.post_json('/v1/test', {})
                    self.assertEqual(failure.exception.kind, 'timeout')
                    self.assertLess(time.monotonic() - began, 0.9)
                    self.assertFalse(proxy.tunnelled.is_set())
                    self.assertFalse(server.headers_received.is_set())
                finally:
                    release.set()
                    self.assertTrue(finished.wait(2))
        finally:
            timer.cancel()
            timer.join(2)
            proxy.close()
            server.close()

    def test_successful_connect_tunnel_preserves_verified_tls_and_json(self):
        for scheme in ('http', 'https'):
            with self.subTest(proxy_scheme=scheme):
                server = DripServer(self.context, 'immediate', b'{"value":1}')
                proxy = ConnectProxy(server.port, self.context if scheme == 'https' else None)
                try:
                    client = self.client(server)
                    original_factory = client.factory

                    def factory():
                        session = original_factory()
                        session.proxies = {'https': f'{scheme}://localhost:{proxy.port}'}
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

    def test_tls_after_connect_inherits_only_remaining_connection_time(self):
        now = 0.0
        original_recv = BudgetSocket.recv_into
        original_wrap = ssl.SSLContext.wrap_socket
        timeouts = []

        def recv(peer, *args):
            nonlocal now
            count = original_recv(peer, *args)
            if now == 0:
                now = 0.75
            return count

        def wrap(context, peer, *args, **kwargs):
            if not kwargs.get('server_side'):
                timeouts.append(peer.gettimeout())
            return original_wrap(context, peer, *args, **kwargs)

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
            client.timeout = (1, 1)
            with (
                patch(
                    'minimax_client.IOBudget', side_effect=lambda n: IOBudget(n, clock=lambda: now)
                ),
                patch.object(BudgetSocket, 'recv_into', recv),
                patch.object(ssl.SSLContext, 'wrap_socket', wrap),
            ):
                self.assertEqual(client.post_json('/v1/test', {}), {'value': 1})
            self.assertEqual(timeouts, [0.25])
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

    def test_timeout_classification_uses_only_bounded_structured_proxy_causes(self):
        from urllib3.exceptions import MaxRetryError, ProxyError

        slot = RequestSlot(budget=IOBudget(1))
        ordinary = requests.exceptions.ProxyError(
            MaxRetryError(None, '/', ProxyError('timeout in untrusted text', OSError('timeout')))
        )
        self.assertEqual(Transport.network_kind(ordinary, slot), 'transport_or_io')
        cyclic = requests.ConnectionError()
        cyclic.args = (cyclic,)
        self.assertEqual(Transport.network_kind(cyclic, slot), 'transport_or_io')

    def test_repeated_cancel_does_not_create_unbounded_closer_threads(self):
        response = Mock()
        slot = RequestSlot(response=response, budget=IOBudget(1))
        for _ in range(30):
            slot.cancel()
        deadline = time.monotonic() + 1
        while response.close.call_count == 0 and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        response.close.assert_called_once()

    def test_tls_in_tls_cancel_reaches_owned_underlying_socket(self):
        from urllib3.util.ssltransport import SSLTransport

        budget = IOBudget(1)
        raw = Mock()
        inner = budget.wrap(raw)
        # Exercise the actual pinned transport type without a second network
        # service. Its gettimeout/socket protocol is real, not a fake shutdown.
        tls = object.__new__(SSLTransport)
        tls.socket = inner
        outer = budget.wrap(tls)
        outer.abort()
        raw.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        budget.cancel()
        self.assertTrue(budget.cancelled)


if __name__ == '__main__':
    unittest.main()

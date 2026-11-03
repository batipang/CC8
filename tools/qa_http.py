"""Certificate-verified loopback faults for isolated QA, never owner providers.

This module is loaded only for explicitly selected network fixtures. The default
failure-UI plan and fake-I/O matrix remain offline and standard-library-only.
"""

from collections import Counter
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time


class LoopbackHTTP:
    def __init__(self, current_case, before_request):
        self.current_case, self.before_request = current_case, before_request
        self.stopped = threading.Event()
        self.guard = threading.Lock()
        self.counts = Counter()
        self.errors = []
        self.handlers = []
        self.folder = None
        self.listener = None
        self.worker = None

    def __enter__(self):
        self.folder = tempfile.TemporaryDirectory(prefix='cc8-qa-local-tls-')
        root = Path(self.folder.name)
        self.cert, key = root / 'cert.pem', root / 'key.pem'
        try:
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
                    str(self.cert),
                    '-subj',
                    '/CN=CC8 local QA',
                    '-addext',
                    'subjectAltName=IP:127.0.0.1',
                ],
                capture_output=True,
                timeout=15,
                check=True,
            )
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.context.load_cert_chain(self.cert, key)
            self.listener = socket.socket()
            self.listener.bind(('127.0.0.1', 0))
            self.listener.listen(4)
            self.listener.settimeout(0.1)
            self.base = f'https://127.0.0.1:{self.listener.getsockname()[1]}'
            self.worker = threading.Thread(target=self.accept, daemon=True)
            self.worker.start()
            return self
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt):
            self.__exit__(None, None, None)
            raise

    def accept(self):
        while not self.stopped.is_set():
            try:
                peer, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.stopped.is_set():
                peer.close()
                return
            with self.guard:
                if len(self.handlers) >= 64:
                    peer.close()
                    self.errors.append('fixture_request_limit')
                    self.stopped.set()
                    return
                thread = threading.Thread(target=self.respond, args=(peer,), daemon=True)
                self.handlers.append(thread)
                thread.start()

    def respond(self, peer):
        try:
            peer.settimeout(1)
            peer = self.context.wrap_socket(peer, server_side=True)
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = peer.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > 65536:
                    raise ValueError('fixture_headers_too_large')
            head, _, body = data.partition(b'\r\n\r\n')
            lines = head.split(b'\r\n')
            if lines[0] not in (b'POST /v1/speech_to_text HTTP/1.1', b'POST /v1/t2a_v2 HTTP/1.1'):
                raise ValueError('fixture_route_not_allowed')
            headers = {}
            for line in lines[1:]:
                name, separator, value = line.partition(b':')
                if not separator or name.lower() in headers:
                    raise ValueError('fixture_invalid_headers')
                headers[name.lower()] = value.strip()
            if headers.get(b'authorization') != b'Bearer qa-not-a-real-credential':
                raise ValueError('fixture_unexpected_credential')
            ident = headers.get(b'x-cc8-qa-case', b'').decode('ascii')
            parts = ident.split('-')
            if (
                len(parts) != 3
                or parts[0] not in ('asr', 'tts')
                or parts[1]
                not in (
                    'timeout',
                    'rate_limit',
                    'quota',
                    'credential_or_permission',
                    'transport_or_io',
                )
                or parts[2] not in ('1', '2', '3')
            ):
                raise ValueError('fixture_unknown_case')
            route = b'/v1/speech_to_text' if parts[0] == 'asr' else b'/v1/t2a_v2'
            if lines[0] != b'POST ' + route + b' HTTP/1.1':
                raise ValueError('fixture_stage_route_mismatch')
            length = int(headers.get(b'content-length', b'-1'))
            if not 0 < length <= 65536:
                raise ValueError('fixture_body_limit')
            while len(body) < length:
                chunk = peer.recv(min(4096, length - len(body)))
                if not chunk:
                    return
                body += chunk
            with self.guard:
                self.counts[ident] += 1
            kind = parts[1]
            if kind == 'timeout':
                self.stopped.wait(4)
                return
            if kind == 'transport_or_io':
                return
            status = {'rate_limit': 429, 'quota': 402, 'credential_or_permission': 401}[kind]
            payload = b'{"error":"qa-injected-sensitive-body"}'
            peer.sendall(
                f'HTTP/1.1 {status} QA injected failure\r\n'.encode()
                + b'Content-Type: application/json\r\nConnection: close\r\nContent-Length: '
                + str(len(payload)).encode()
                + b'\r\n\r\n'
                + payload
            )
        except (OSError, ssl.SSLError):
            # The client intentionally times out or closes the failed response.
            pass
        except (ValueError, UnicodeError, KeyError) as error:
            with self.guard:
                self.errors.append(type(error).__name__)
        finally:
            peer.close()

    def session_factory(self):
        import requests

        fixture = self

        class LocalSession(requests.Session):
            def __init__(self):
                super().__init__()
                self.trust_env = False
                self.verify = str(fixture.cert)

            def post(self, url, **kwargs):
                if url not in (fixture.base + '/v1/speech_to_text', fixture.base + '/v1/t2a_v2'):
                    raise AssertionError('QA cannot reach a non-fixture endpoint')
                if kwargs.get('allow_redirects', False) is not False:
                    raise AssertionError('QA does not follow redirects')
                kwargs['allow_redirects'] = False
                case = fixture.current_case()
                headers = dict(kwargs.get('headers', {}))
                headers['X-CC8-QA-Case'] = case.ident
                kwargs['headers'] = headers
                fixture.before_request()
                return super().post(url, **kwargs)

        return LocalSession()

    def count(self, ident):
        with self.guard:
            return self.counts[ident]

    def __exit__(self, *unused):
        self.stopped.set()
        if self.listener is not None:
            self.listener.close()
        if self.worker is not None:
            self.worker.join(2)
        deadline = time.monotonic() + 3
        for handler in self.handlers:
            handler.join(max(0, deadline - time.monotonic()))
        alive = (self.worker is not None and self.worker.is_alive()) or any(
            handler.is_alive() for handler in self.handlers
        )
        if self.folder is not None:
            self.folder.cleanup()
        if alive or self.errors:
            raise AssertionError('Local QA TLS fixture did not complete cleanly')

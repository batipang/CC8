"""Authenticated LAN standby and verified USB priority for the CC8 packet stream."""

from __future__ import annotations
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
import uuid
from typing import Callable, Literal, Protocol, TypeAlias
from protocol import Packet, receive, EVENT, decode_event, decode_json_object, JsonValue, JsonObject
from release_info import RELEASE

ROOT = Path(__file__).resolve().parents[2]
PAIR_DIR = ROOT / '.toolchains/cc8/transport'
TLS_PORT = 18766
DISCOVERY_PORT = 18767
TransportKind: TypeAlias = Literal['usb', 'wifi']
TransportEvent: TypeAlias = tuple[Literal['up', 'down', 'packet'], 'Channel', Packet | None]
CHANNEL_PRIORITY: tuple[TransportKind, ...] = ('usb', 'wifi')


class DuplexStream(Protocol):
    def recv(self, count: int, /) -> bytes: ...
    def sendall(self, data: bytes, /) -> None: ...
    def shutdown(self, how: int, /) -> None: ...
    def close(self) -> None: ...


class LogSink(Protocol):
    def __call__(self, event: str, **fields: JsonValue) -> None: ...


def quiet_log(event: str, **fields: JsonValue) -> None:
    pass


def private_ipv4(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
        return ip.version == 4 and (
            ip.is_loopback
            or any(
                ip in ipaddress.ip_network(n)
                for n in ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16']
            )
        )
    except ValueError:
        return False


def signature(token: str, message: str) -> str:
    return hmac.new(bytes.fromhex(token), message.encode(), hashlib.sha256).hexdigest()


def local_addresses() -> list[str]:
    values = []
    try:
        text = subprocess.run(['/sbin/ifconfig'], capture_output=True, text=True, timeout=3).stdout
        values = [
            value
            for value in re.findall(r'\binet (\d+\.\d+\.\d+\.\d+)', text)
            if private_ipv4(value) and not value.startswith('127.')
        ]
    except (OSError, subprocess.SubprocessError):
        pass
    return list(dict.fromkeys(values))[:8]


class Pairing:
    # 私有文件保存共享密钥与 TLS 证书；USB 负责初次交付设备侧配对信息。
    def __init__(self, folder: str | Path = PAIR_DIR, port: int = TLS_PORT) -> None:
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('Invalid TLS port')
        self.port = port
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)
        cert = self.folder / 'server.pem'
        key = self.folder / 'server-key.pem'
        config = self.folder / 'pairing.json'
        if not cert.exists() or not key.exists():
            subprocess.run(
                [
                    '/usr/bin/openssl',
                    'req',
                    '-x509',
                    '-newkey',
                    'rsa:2048',
                    '-sha256',
                    '-nodes',
                    '-days',
                    '3650',
                    '-subj',
                    '/CN=Bumblebee Local Bridge',
                    '-keyout',
                    str(key),
                    '-out',
                    str(cert),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=20,
            )
        os.chmod(key, 0o600)
        os.chmod(cert, 0o600)
        if config.exists():
            data = decode_json_object(config.read_bytes(), max_bytes=4096)
        else:
            data = {'server_id': secrets.token_hex(16), 'token': secrets.token_hex(32)}
            fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out)
        os.chmod(config, 0o600)
        server_id, token = data.get('server_id'), data.get('token')
        if (
            not isinstance(server_id, str)
            or not re.fullmatch('[0-9a-f]{32}', server_id)
            or not isinstance(token, str)
            or not re.fullmatch('[0-9a-f]{64}', token)
        ):
            raise ValueError('Invalid local transport pairing file')
        self.server_id = server_id
        self.token = token
        der = ssl.PEM_cert_to_DER_cert(cert.read_text())
        self.pin = hashlib.sha256(der).hexdigest()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(str(cert), str(key))

    def device_config(self) -> JsonObject:
        hosts: list[JsonValue] = [*local_addresses()]
        return {
            'server_id': self.server_id,
            'token': self.token,
            'certificate_sha256': self.pin,
            'port': self.port,
            'hosts': hosts,
        }

    def offer(self, data: object) -> JsonObject | None:
        if not isinstance(data, dict) or data.get('type') != 'cc8_discover':
            return None
        nonce = data.get('nonce', '')
        mac = data.get('mac', '')
        if (
            not isinstance(nonce, str)
            or not re.fullmatch('[0-9a-f]{32}', nonce)
            or not isinstance(mac, str)
            or not re.fullmatch('[0-9a-f]{64}', mac)
        ):
            return None
        if not hmac.compare_digest(signature(self.token, 'discover:' + nonce), mac):
            return None
        return {
            'type': 'cc8_offer',
            'nonce': nonce,
            'server_id': self.server_id,
            'port': self.port,
            'mac': signature(
                self.token, 'offer:' + nonce + ':' + self.server_id + ':' + str(self.port)
            ),
        }


class Channel:
    # USB 与 TLS 统一为包流；读线程投递事件，主循环选择唯一生效的通道。
    def __init__(
        self, stream: DuplexStream, kind: TransportKind, events: queue.Queue[TransportEvent]
    ) -> None:
        self.stream = stream
        self.kind = kind
        self.events = events
        self.ident = uuid.uuid4().hex
        self.lock = threading.Lock()
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
            self.stream.sendall(raw)

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


class TransportHub:
    def __init__(
        self,
        pairing: Pairing,
        usb_connect: Callable[[], DuplexStream],
        usb_present: Callable[[], bool],
        log: LogSink = quiet_log,
        bind: str = '0.0.0.0',
        tls_port: int = TLS_PORT,
        discovery_port: int = DISCOVERY_PORT,
    ) -> None:
        self.pairing = pairing
        self.usb_connect = usb_connect
        self.usb_present = usb_present
        self.log = log
        self.bind = bind
        self.tls_port = tls_port
        self.discovery_port = discovery_port
        self.events: queue.Queue[TransportEvent] = queue.Queue(maxsize=512)
        self.channels: dict[TransportKind, Channel] = {}
        self.active: Channel | None = None
        self.guard = threading.RLock()
        self.stop = threading.Event()
        self.usb_disabled_until = 0.0
        self.switches = 0
        self.auth_failures = 0
        self.server: socket.socket | None = None
        self.discovery: socket.socket | None = None
        self.workers: list[threading.Thread] = []

    def start(self) -> None:
        if self.workers or self.stop.is_set():
            raise RuntimeError('Transport hub cannot be started twice or after close')
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((self.bind, self.tls_port))
            self.server.listen(4)
            self.server.settimeout(0.5)
            self.discovery = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.discovery.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.discovery.bind((self.bind, self.discovery_port))
            self.discovery.settimeout(0.5)
            self.pairing.port = self.server.getsockname()[1]
        except OSError:
            self.close()
            raise
        for method in [self.accept_wifi, self.discover, self.monitor_usb, self.heartbeat]:
            worker = threading.Thread(target=method, daemon=True, name='cc8-' + method.__name__)
            self.workers.append(worker)
            worker.start()

    def register(self, stream: DuplexStream, kind: TransportKind) -> Channel:
        channel = Channel(stream, kind, self.events)
        with self.guard:
            if self.stop.is_set():
                channel.close()
                raise RuntimeError('Transport hub is closed')
            previous = self.channels.get(kind)
            if previous and not previous.closed:
                previous.close()
            self.channels[kind] = channel
        channel.start()
        return channel

    def accept_wifi(self) -> None:
        server = self.server
        if server is None:
            raise RuntimeError('Transport hub has not started')
        while not self.stop.is_set():
            try:
                raw, peer = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not private_ipv4(peer[0]):
                raw.close()
                continue
            # Bound expensive unauthenticated handshakes. One paired device.
            tls: ssl.SSLSocket | None = None
            try:
                raw.settimeout(3)
                tls = self.pairing.context.wrap_socket(raw, server_side=True)
                packet = receive(tls)
                auth: JsonObject = (
                    decode_json_object(packet.data, max_bytes=1024)
                    if packet.kind == EVENT and len(packet.data) <= 1024
                    else {}
                )
                if not isinstance(auth, dict):
                    raise ValueError('Invalid authentication object')
                token = auth.get('token', '')
                if (
                    auth.get('event') != 'transport_auth'
                    or not isinstance(token, str)
                    or not re.fullmatch('[0-9a-f]{64}', token)
                    or not hmac.compare_digest(token, self.pairing.token)
                ):
                    raise ValueError('LAN authentication failed')
                tls.settimeout(10)
                tls.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                tls.sendall(Packet.event('transport_welcome').encode())
                self.register(tls, 'wifi')
            except (OSError, EOFError, ValueError, RuntimeError):
                if not self.stop.is_set():
                    self.auth_failures += 1
                try:
                    (tls or raw).close()
                except OSError:
                    pass

    def discover(self) -> None:
        discovery = self.discovery
        if discovery is None:
            raise RuntimeError('Transport hub has not started')
        while not self.stop.is_set():
            try:
                raw, peer = discovery.recvfrom(2048)
                if not private_ipv4(peer[0]):
                    continue
                value = self.pairing.offer(decode_json_object(raw, max_bytes=2048))
                if value:
                    discovery.sendto(json.dumps(value, separators=(',', ':')).encode(), peer)
            except socket.timeout:
                continue
            except (OSError, ValueError, TypeError):
                if self.stop.is_set():
                    break

    def monitor_usb(self) -> None:
        while not self.stop.is_set():
            try:
                with self.guard:
                    channel = self.channels.get('usb')
                allowed = time.monotonic() >= self.usb_disabled_until
                if channel and not channel.closed:
                    if not allowed or not self.usb_present():
                        channel.close()
                elif allowed:
                    stream = self.usb_connect()
                    if self.stop.is_set():
                        stream.close()
                        break
                    self.register(stream, 'usb')
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
            self.stop.wait(1)

    def heartbeat(self) -> None:
        while not self.stop.wait(2):
            with self.guard:
                channels = list(self.channels.values())
            for channel in channels:
                if channel.closed:
                    continue
                try:
                    if time.monotonic() - channel.last_rx > 9:
                        channel.close()
                    else:
                        channel.event('transport_ping')
                except (OSError, EOFError):
                    channel.close()

    def preferred(self) -> Channel | None:
        # socket 连通不等于已就绪；只从可用通道中优先选 USB，否则选 Wi-Fi。
        with self.guard:
            for kind in CHANNEL_PRIORITY:
                channel = self.channels.get(kind)
                if kind == 'usb' and time.monotonic() < self.usb_disabled_until:
                    continue
                if channel and channel.ready and not channel.closed:
                    return channel
        return None

    def selected(self, channel: Channel | None) -> None:
        # 明确激活/备用角色，防止两路同时处理音频；配对配置只经 USB 下发。
        self.active = channel
        self.switches += 1
        with self.guard:
            others = list(self.channels.values())
        for other in others:
            if other is not channel and other.ready and not other.closed:
                try:
                    other.event('transport_standby')
                except (OSError, EOFError):
                    other.close()
        if channel:
            channel.event('transport_activate', transport=channel.kind)
            if channel.kind == 'usb':
                channel.event('lan_config', config=self.pairing.device_config())
        self.log(
            'transport_selected',
            transport=channel.kind if channel else 'none',
            switches=self.switches,
        )

    def diagnostics(self) -> JsonObject:
        with self.guard:
            return {
                'transport': self.active.kind if self.active and not self.active.closed else 'none',
                'usb_ready': bool(
                    self.channels.get('usb')
                    and self.channels['usb'].ready
                    and not self.channels['usb'].closed
                ),
                'wifi_ready': bool(
                    self.channels.get('wifi')
                    and self.channels['wifi'].ready
                    and not self.channels['wifi'].closed
                ),
                'transport_switches': self.switches,
                'lan_auth_failures': self.auth_failures,
                'usb_test_suppressed': time.monotonic() < self.usb_disabled_until,
            }

    def suppress_usb_for_test(self, seconds: int | float) -> JsonObject:
        if type(seconds) not in (int, float) or not 1 <= seconds <= 60:
            raise ValueError('USB test suppression must be 1–60 seconds')
        self.usb_disabled_until = time.monotonic() + seconds
        with self.guard:
            channel = self.channels.get('usb')
        if channel:
            channel.close()
        return {'ok': True, 'seconds': seconds}

    def close(self) -> None:
        self.stop.set()
        for listener in [self.server, self.discovery]:
            if listener:
                try:
                    listener.close()
                except OSError:
                    pass
        with self.guard:
            channels = list(self.channels.values())
        for channel in channels:
            channel.close()

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
from protocol import Packet, receive, EVENT

ROOT = Path(__file__).resolve().parents[2]
PAIR_DIR = ROOT / '.toolchains/cc8/transport'
TLS_PORT = 18766
DISCOVERY_PORT = 18767


def private_ipv4(value):
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


def signature(token, message):
    return hmac.new(bytes.fromhex(token), message.encode(), hashlib.sha256).hexdigest()


def local_addresses():
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
    def __init__(self, folder=PAIR_DIR):
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
            data = json.loads(config.read_text())
        else:
            data = {'server_id': secrets.token_hex(16), 'token': secrets.token_hex(32)}
            fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out)
        os.chmod(config, 0o600)
        if not re.fullmatch('[0-9a-f]{32}', data.get('server_id', '')) or not re.fullmatch(
            '[0-9a-f]{64}', data.get('token', '')
        ):
            raise ValueError('Invalid local transport pairing file')
        self.server_id = data['server_id']
        self.token = data['token']
        der = ssl.PEM_cert_to_DER_cert(cert.read_text())
        self.pin = hashlib.sha256(der).hexdigest()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(str(cert), str(key))

    def device_config(self):
        return {
            'server_id': self.server_id,
            'token': self.token,
            'certificate_sha256': self.pin,
            'port': TLS_PORT,
            'hosts': local_addresses(),
        }

    def offer(self, data):
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
            'port': TLS_PORT,
            'mac': signature(
                self.token, 'offer:' + nonce + ':' + self.server_id + ':' + str(TLS_PORT)
            ),
        }


class Channel:
    # USB 与 TLS 统一为包流；读线程投递事件，主循环选择唯一生效的通道。
    def __init__(self, stream, kind, events):
        self.stream = stream
        self.kind = kind
        self.events = events
        self.ident = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.closed = False
        self.ready = False
        self.last_rx = time.monotonic()

    def start(self):
        threading.Thread(target=self.read, daemon=True, name='cc8-' + self.kind + '-reader').start()

    def sendall(self, raw):
        with self.lock:
            if self.closed:
                raise EOFError('Transport closed')
            self.stream.sendall(raw)

    def event(self, name, **fields):
        self.sendall(Packet.event(name, **fields).encode())

    def read(self):
        try:
            first = receive(self.stream)
            hello = json.loads(first.data) if first.kind == EVENT else None
            if not isinstance(hello, dict) or hello.get('event') != 'transport_hello':
                raise ValueError('Missing transport hello')
            self.ready = True
            self.last_rx = time.monotonic()
            self.events.put(('up', self, None), timeout=0.2)
            while not self.closed:
                packet = receive(self.stream)
                self.last_rx = time.monotonic()
                if packet.kind == EVENT:
                    event = json.loads(packet.data)
                    if not isinstance(event, dict):
                        raise ValueError('Invalid event object')
                    if event.get('event') == 'transport_pong':
                        continue
                self.events.put(('packet', self, packet), timeout=0.2)
        except (OSError, EOFError, ValueError, queue.Full):
            pass
        finally:
            self.close()
            try:
                self.events.put(('down', self, None), timeout=0.2)
            except queue.Full:
                pass

    def shutdown(self, *unused):
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.ready = False
        try:
            self.stream.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            self.stream.close()
        except OSError:
            pass


class TransportHub:
    def __init__(
        self,
        pairing,
        usb_connect,
        usb_present,
        log=lambda *a, **kw: None,
        bind='0.0.0.0',
        tls_port=TLS_PORT,
        discovery_port=DISCOVERY_PORT,
    ):
        self.pairing = pairing
        self.usb_connect = usb_connect
        self.usb_present = usb_present
        self.log = log
        self.bind = bind
        self.tls_port = tls_port
        self.discovery_port = discovery_port
        self.events = queue.Queue(maxsize=512)
        self.channels = {}
        self.active = None
        self.guard = threading.RLock()
        self.stop = threading.Event()
        self.usb_disabled_until = 0.0
        self.switches = 0
        self.auth_failures = 0
        self.server = None
        self.discovery = None

    def start(self):
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
        except OSError:
            self.close()
            raise
        for method in [self.accept_wifi, self.discover, self.monitor_usb, self.heartbeat]:
            threading.Thread(target=method, daemon=True).start()

    def register(self, stream, kind):
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

    def accept_wifi(self):
        while not self.stop.is_set():
            try:
                raw, peer = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not private_ipv4(peer[0]):
                raw.close()
                continue
            # Bound expensive unauthenticated handshakes. One paired device.
            tls = None
            try:
                raw.settimeout(3)
                tls = self.pairing.context.wrap_socket(raw, server_side=True)
                packet = receive(tls)
                auth = (
                    json.loads(packet.data)
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

    def discover(self):
        while not self.stop.is_set():
            try:
                raw, peer = self.discovery.recvfrom(2048)
                if not private_ipv4(peer[0]):
                    continue
                value = self.pairing.offer(json.loads(raw))
                if value:
                    self.discovery.sendto(json.dumps(value, separators=(',', ':')).encode(), peer)
            except socket.timeout:
                continue
            except (OSError, ValueError, TypeError):
                if self.stop.is_set():
                    break

    def monitor_usb(self):
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

    def heartbeat(self):
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

    def preferred(self):
        # socket 连通不等于已就绪；只从可用通道中优先选 USB，否则选 Wi-Fi。
        with self.guard:
            for kind in ['usb', 'wifi']:
                channel = self.channels.get(kind)
                if kind == 'usb' and time.monotonic() < self.usb_disabled_until:
                    continue
                if channel and channel.ready and not channel.closed:
                    return channel
        return None

    def selected(self, channel):
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

    def diagnostics(self):
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

    def suppress_usb_for_test(self, seconds):
        if type(seconds) not in (int, float) or not 1 <= seconds <= 60:
            raise ValueError('USB test suppression must be 1–60 seconds')
        self.usb_disabled_until = time.monotonic() + seconds
        with self.guard:
            channel = self.channels.get('usb')
        if channel:
            channel.close()
        return {'ok': True, 'seconds': seconds}

    def close(self):
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

"""Authenticated LAN standby and verified USB priority for the CC8 packet stream."""

from __future__ import annotations
import hmac
import json
import queue
import re
import socket
import ssl
import subprocess
import threading
import time
import uuid
from typing import Callable, Protocol
from protocol import HEADER, Packet, receive, EVENT, decode_json_object, JsonValue, JsonObject
from tls_stream import TLSStream
from transport_channel import Channel as Channel, DuplexStream, TransportEvent, TransportKind
from transport_pairing import (
    PAIR_DIR as PAIR_DIR,
    TLS_PORT,
    Pairing as Pairing,
    local_addresses as local_addresses,
    private_ipv4 as private_ipv4,
    signature as signature,
)

DISCOVERY_PORT = 18767
AUTH_TIMEOUT_SECONDS = 3
MAX_AUTH_JSON_BYTES = 1024
CHANNEL_PRIORITY: tuple[TransportKind, ...] = ('usb', 'wifi')


class LogSink(Protocol):
    def __call__(self, event: str, **fields: JsonValue) -> None: ...


def quiet_log(event: str, **fields: JsonValue) -> None:
    pass


class AuthenticationReader:
    """Use the same deadline for the handshake and each read of its auth frame."""

    def __init__(self, stream: ssl.SSLSocket, deadline: float):
        self.stream = stream
        self.deadline = deadline

    def recv(self, count: int) -> bytes:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Transport authentication deadline exceeded')
        self.stream.settimeout(remaining)
        return self.stream.recv(count)


def authenticate_device(raw: socket.socket, pairing: Pairing, kind: TransportKind) -> ssl.SSLSocket:
    deadline = time.monotonic() + AUTH_TIMEOUT_SECONDS
    raw.settimeout(AUTH_TIMEOUT_SECONDS)
    tls = pairing.context.wrap_socket(raw, server_side=True)
    try:
        packet = receive(
            AuthenticationReader(tls, deadline), max_packet=HEADER.size + MAX_AUTH_JSON_BYTES
        )
        auth = (
            decode_json_object(packet.data, max_bytes=MAX_AUTH_JSON_BYTES)
            if packet.kind == EVENT
            else {}
        )
        token = auth.get('token')
        if (
            auth.get('event') != 'transport_auth'
            or not isinstance(token, str)
            or not re.fullmatch('[0-9a-f]{64}', token)
            or not hmac.compare_digest(token, pairing.token)
            or (kind == 'usb' and set(auth) != {'event', 'token', 'transport'})
            or (kind == 'usb' and auth.get('transport') != 'usb')
            or (kind == 'wifi' and auth.get('transport') not in (None, 'wifi'))
        ):
            raise ValueError('Transport authentication failed')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Transport authentication deadline exceeded')
        tls.settimeout(remaining)
        tls.sendall(Packet.event('transport_welcome').encode())
        tls.settimeout(10)
        return tls
    except BaseException:
        tls.close()
        raise


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
        self.role_uncertain = False
        self.guard = threading.RLock()
        self.stop = threading.Event()
        self.usb_disabled_until = 0.0
        self.all_disabled_until = 0.0
        self.switches = 0
        self.activation_owner = uuid.uuid4().hex
        self.activation_serial = 0
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
        if isinstance(stream, ssl.SSLSocket):
            stream = TLSStream(stream)
        channel = Channel(stream, kind, self.events)
        with self.guard:
            if self.stop.is_set():
                channel.close()
                raise RuntimeError('Transport hub is closed')
            if time.monotonic() < self.all_disabled_until:
                channel.close()
                raise RuntimeError('Transport test outage')
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
            if time.monotonic() < self.all_disabled_until or not private_ipv4(peer[0]):
                raw.close()
                continue
            tls: ssl.SSLSocket | None = None
            try:
                tls = authenticate_device(raw, self.pairing, 'wifi')
                tls.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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
                allowed = time.monotonic() >= max(self.usb_disabled_until, self.all_disabled_until)
                if channel and not channel.closed:
                    if not allowed or not self.usb_present():
                        channel.close()
                elif allowed:
                    stream = self.usb_connect()
                    if self.stop.is_set():
                        stream.close()
                        break
                    self.register(stream, 'usb')
            except (OSError, EOFError, RuntimeError, ValueError, subprocess.SubprocessError):
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
            if time.monotonic() < self.all_disabled_until:
                return None
            for kind in CHANNEL_PRIORITY:
                channel = self.channels.get(kind)
                if kind == 'usb' and time.monotonic() < self.usb_disabled_until:
                    continue
                if channel and channel.ready and not channel.closed:
                    return channel
        return None

    def selected(self, channel: Channel | None) -> bool:
        # 明确激活/备用角色，防止两路同时处理音频；配对配置只经 USB 下发。
        with self.guard:
            if channel is not None and time.monotonic() < self.all_disabled_until:
                return False
            others = list(self.channels.values())
            if self.activation_serial >= (1 << 63) - 1:
                raise RuntimeError('Transport activation budget exhausted')
            self.role_uncertain = True
            for other in others:
                other.application_ready = False
            if channel is not None:
                channel.application_ready = False
            self.activation_serial += 1
            serial = self.activation_serial
        for other in others:
            if other is not channel and other.ready and not other.closed:
                try:
                    other.event('transport_standby')
                except (OSError, EOFError):
                    other.close()
        if channel:
            try:
                # Even a reused Channel needs a new wire-level activation fence.
                channel.route_token = uuid.uuid4().hex if channel.route_envelope else None
                if channel.route_token:
                    fields: JsonObject = {
                        'transport': channel.kind,
                        'route_token': channel.route_token,
                    }
                    if channel.route_duplex:
                        fields.update(
                            route_output_version=1,
                            route_owner=self.activation_owner,
                            route_serial=serial,
                        )
                    channel.event(
                        'transport_activate',
                        0,
                        **fields,
                    )
                else:
                    channel.event('transport_activate', transport=channel.kind)
                if channel.kind == 'usb':
                    channel.event('lan_config', config=self.pairing.device_config())
            except (OSError, EOFError):
                # This failure belongs to the selected route, regardless of
                # which channel's queued event triggered the reevaluation.
                channel.close()
                return False
        with self.guard:
            if channel is not None and channel.closed:
                return False
            self.active = channel
            if channel is not None:
                channel.application_ready = True
            self.role_uncertain = False
            self.switches += 1
        self.log(
            'transport_selected',
            transport=channel.kind if channel else 'none',
            switches=self.switches,
        )
        return True

    def diagnostics(self) -> JsonObject:
        with self.guard:
            now = time.monotonic()
            return {
                'transport': self.active.kind if self.active and not self.active.closed else 'none',
                'route_envelope_version': int(
                    bool(
                        self.active
                        and self.active.application_ready
                        and not self.active.closed
                        and self.active.route_token
                    )
                ),
                'route_output_version': int(
                    bool(
                        self.active
                        and self.active.application_ready
                        and not self.active.closed
                        and self.active.route_token
                        and self.active.route_duplex
                    )
                ),
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
                'usb_test_suppressed': now < max(self.usb_disabled_until, self.all_disabled_until),
                'all_routes_test_suppressed': now < self.all_disabled_until,
            }

    def suppress_usb_for_test(self, seconds: int | float) -> JsonObject:
        if type(seconds) not in (int, float) or not 1 <= seconds <= 60:
            raise ValueError('USB test suppression must be 1–60 seconds')
        with self.guard:
            now = time.monotonic()
            if now < self.all_disabled_until:
                raise RuntimeError('Transport test outage already active')
            self.usb_disabled_until = now + seconds
            channel = self.channels.get('usb')
        if channel:
            channel.close()
        return {'ok': True, 'seconds': seconds}

    def suppress_all_for_test(self) -> JsonObject:
        seconds = 10
        with self.guard:
            now = time.monotonic()
            if self.stop.is_set() or now < max(self.all_disabled_until, self.usb_disabled_until):
                raise RuntimeError('Transport test outage unavailable')
            self.all_disabled_until = now + seconds
            # Close the standby peer before the active one so neither can serve
            # as a lasting fallback during the controlled outage.
            channels = sorted(self.channels.values(), key=lambda channel: channel is self.active)
        for channel in channels:
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

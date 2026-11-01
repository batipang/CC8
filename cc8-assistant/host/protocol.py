"""Bounded framing shared by USB-loopback and authenticated LAN connections."""

from dataclasses import dataclass
import json
import math
import re
import struct

HEADER = struct.Struct('!BIQQQ')
# 长度与头字段为网络字节序，PCM 载荷为小端有符号 16 位。
# frame 是采样位置，nanos 是设备时间，不能当作日历时间。
MIC, RENDER, EVENT, SPEAKER = 1, 2, 3, 4
MAX_PACKET = 65536
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096


def decode_json_object(raw, max_bytes=MAX_PACKET - HEADER.size):
    """Reject ambiguous encodings, duplicate keys and parser resource attacks."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= max_bytes:
        raise ValueError('Invalid JSON byte length')

    def pairs(values):
        result = {}
        for key, value in values:
            key.encode('utf-8')
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError('Non-finite JSON number')

    try:
        value = json.loads(
            raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid_constant
        )
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Invalid JSON encoding or nesting') from error
    if not isinstance(value, dict):
        raise ValueError('Expected JSON object')
    pending = [(value, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > MAX_JSON_DEPTH or count > MAX_JSON_NODES:
            raise ValueError('JSON resource budget exceeded')
        if isinstance(node, dict):
            pending.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            pending.extend((child, depth + 1) for child in node)
        elif isinstance(node, str):
            try:
                node.encode('utf-8')
            except UnicodeError as error:
                raise ValueError('Invalid JSON Unicode scalar') from error
        elif isinstance(node, float) and not math.isfinite(node):
            raise ValueError('Non-finite JSON number')
    return value


def validate_device_event(value):
    """Validate fields before any capture, cancellation or playback state changes."""
    if not isinstance(value, dict):
        raise ValueError('Invalid device event')
    name = value.get('event')
    if not isinstance(name, str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,63}', name):
        raise ValueError('Invalid device event name')
    required = {
        'session_start': ('epoch',),
        'session_rebind': ('epoch',),
        'play_done': ('generation',),
        'playback_paused': ('generation', 'token', 'nanos'),
        'playback_resumed': ('generation', 'token', 'nanos'),
    }.get(name, ())
    for key in required:
        if key not in value:
            raise ValueError('Missing device event field: ' + key)
    integers = {
        'epoch': 32,
        'generation': 32,
        'vision_generation': 32,
        'nanos': 64,
        'desktop_camera_version': 32,
    }
    if name in ('playback_paused', 'playback_resumed'):
        integers['token'] = 32
    for key, bits in integers.items():
        if key in value and (type(value[key]) is not int or not 0 <= value[key] < (1 << bits)):
            raise ValueError('Invalid device event integer: ' + key)
    for key in (
        'capture_active',
        'wake_mode',
        'supports_playback_pause',
        'foreground',
        'ok',
        'microphone_active',
        'wifi_ready',
    ):
        if key in value and type(value[key]) is not bool:
            raise ValueError('Invalid device event boolean: ' + key)
    return value


def decode_event(raw):
    return validate_device_event(decode_json_object(raw))


@dataclass
class Packet:
    kind: int
    generation: int = 0
    sequence: int = 0
    frame: int = 0
    nanos: int = 0
    data: bytes = b''

    def encode(self):
        if type(self.kind) is not int or self.kind not in (MIC, RENDER, EVENT, SPEAKER):
            raise ValueError('Unknown packet type')
        for value, bits in (
            (self.generation, 32),
            (self.sequence, 64),
            (self.frame, 64),
            (self.nanos, 64),
        ):
            if type(value) is not int or not 0 <= value < (1 << bits):
                raise ValueError('Invalid packet integer')
        if not isinstance(self.data, bytes):
            raise ValueError('Packet payload must be bytes')
        if len(self.data) + HEADER.size > MAX_PACKET:
            raise ValueError('Packet too large')
        if self.kind != EVENT and (len(self.data) % 2 or len(self.data) > 6400):
            raise ValueError('Invalid PCM length')
        payload = (
            HEADER.pack(self.kind, self.generation, self.sequence, self.frame, self.nanos)
            + self.data
        )
        return struct.pack('!I', len(payload)) + payload

    @classmethod
    def event(cls, event, generation=0, **fields):
        return cls(
            EVENT,
            generation,
            data=json.dumps(
                {'event': event, **fields}, ensure_ascii=False, allow_nan=False
            ).encode(),
        )


def read_exact(stream, count):
    result = bytearray()
    while len(result) < count:
        chunk = stream.recv(count - len(result))
        if not chunk:
            raise EOFError('Device transport closed')
        result.extend(chunk)
    return bytes(result)


def receive(stream):
    # 底层可能拆包，须读满声明长度并复用发送侧校验，不能把一次 recv 当作一包。
    size = struct.unpack('!I', read_exact(stream, 4))[0]
    if size < HEADER.size or size > MAX_PACKET:
        raise ValueError('Invalid packet length')
    payload = read_exact(stream, size)
    p = Packet(*HEADER.unpack(payload[: HEADER.size]), payload[HEADER.size :])
    p.encode()  # Same validation in both directions.
    return p

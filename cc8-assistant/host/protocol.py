"""Bounded framing shared by USB-loopback and authenticated LAN connections."""

from dataclasses import dataclass
from typing import Protocol, TypeAlias, Union, cast
import json
import math
import re
import struct

HEADER = struct.Struct('!BIQQQ')
# 长度与头字段为网络字节序，PCM 载荷为小端有符号 16 位。
# frame 是采样位置，nanos 是设备时间，不能当作日历时间。
MIC, RENDER, EVENT, SPEAKER, MIC_PAIRED = 1, 2, 3, 4, 5
MAX_PACKET = 65536
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096
JsonValue: TypeAlias = Union[None, bool, int, float, str, list['JsonValue'], dict[str, 'JsonValue']]
JsonObject: TypeAlias = dict[str, JsonValue]


class PacketStream(Protocol):
    def recv(self, count: int, /) -> bytes: ...


def decode_json_object(raw: bytes, max_bytes: int = MAX_PACKET - HEADER.size) -> JsonObject:
    """Reject ambiguous encodings, duplicate keys and parser resource attacks."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= max_bytes:
        raise ValueError('Invalid JSON byte length')

    def pairs(values: list[tuple[str, JsonValue]]) -> JsonObject:
        result: JsonObject = {}
        for key, value in values:
            key.encode('utf-8')
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError('Non-finite JSON number')

    try:
        value: object = json.loads(
            raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid_constant
        )
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Invalid JSON encoding or nesting') from error
    return _json_object(value)


def _json_object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError('Expected JSON object')
    pending: list[tuple[object, int]] = [(value, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > MAX_JSON_DEPTH or count > MAX_JSON_NODES:
            raise ValueError('JSON resource budget exceeded')
        if isinstance(node, dict):
            for key in node:
                if not isinstance(key, str):
                    raise ValueError('Invalid JSON object key')
                try:
                    key.encode('utf-8')
                except UnicodeError as error:
                    raise ValueError('Invalid JSON Unicode scalar') from error
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
        elif node is not None and not isinstance(node, (bool, int, float, str)):
            raise ValueError('Invalid JSON value type')
    # json.loads produces only JSON builtins; shape, Unicode, numeric and resource
    # checks above establish this recursive type at the untyped stdlib boundary.
    return cast(JsonObject, value)


def validate_device_event(value: object) -> JsonObject:
    """Validate fields before any capture, cancellation or playback state changes."""
    value = _json_object(value)
    name = value.get('event')
    if not isinstance(name, str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,63}', name):
        raise ValueError('Invalid device event name')
    required = {
        'session_start': ('epoch',),
        'session_rebind': ('epoch',),
        'play_done': ('generation',),
        'dot_chat_request': ('request_id', 'action'),
        'dot_chat_cancel': ('request_id',),
        'saved_task_request': ('request_id', 'action'),
        'playout_progress': (
            'generation',
            'playback_id',
            'first_signal_frame',
            'played_frames',
            'accepted_frames',
            'nanos',
        ),
        'playback_paused': ('generation', 'token', 'nanos'),
        'playback_resumed': ('generation', 'token', 'nanos'),
    }.get(name, ())
    for key in required:
        if key not in value:
            raise ValueError('Missing device event field: ' + key)
    if name in ('dot_chat_request', 'dot_chat_cancel', 'saved_task_request'):
        request_id = value['request_id']
        if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
            raise ValueError('Invalid chat request ID')
        if name == 'dot_chat_request' and value['action'] not in (
            'state',
            'list',
            'select',
            'clear',
        ):
            raise ValueError('Invalid chat action')
        if name == 'saved_task_request':
            if value['action'] not in ('list', 'get', 'save', 'delete'):
                raise ValueError('Invalid saved result action')
            if value['action'] != 'list':
                task_id = value.get('id')
                if not isinstance(task_id, str) or not re.fullmatch('[a-f0-9]{16}', task_id):
                    raise ValueError('Invalid saved task ID')
    if name in ('session_start', 'session_rebind', 'session_stop'):
        for key in ('capture_id', 'capture_start_token'):
            if key in value:
                identifier = value[key]
                if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
                    raise ValueError('Invalid capture identity field: ' + key)
    integers = {
        'epoch': 32,
        'generation': 32,
        'vision_generation': 32,
        'nanos': 64,
        'desktop_camera_version': 32,
        'capture_reject_version': 32,
        'capture_start_ack_version': 32,
        'paired_capture_version': 32,
        'capture_sample_rate': 32,
        'capture_channels': 32,
        'playout_receipt_version': 32,
        'render_clock_report_version': 32,
        'playback_done_id_version': 32,
        'playback_pause_applied_version': 32,
    }
    if (
        name in ('session_start', 'session_rebind')
        and 'capture_frontend' in value
        and value['capture_frontend'] not in ('mono', 'paired')
    ):
        raise ValueError('Invalid capture frontend')
    if (
        name in ('session_start', 'session_rebind')
        and 'capture_fallback_reason' in value
        and value['capture_fallback_reason'] not in ('platform_api', 'paired_format_unavailable')
    ):
        raise ValueError('Invalid capture fallback reason')
    if name == 'capture_gap':
        if ('epoch' in value) != ('sequence' in value):
            raise ValueError('Incomplete capture-gap identity')
        integers['sequence'] = 64
    if name == 'playout_progress':
        integers.update(playback_id=32, first_signal_frame=64, played_frames=64, accepted_frames=64)
    if name in ('play_done', 'playback_error'):
        integers['playback_id'] = 32
    if name == 'play_done':
        integers['played_frames'] = 64
    if name in ('playback_paused', 'playback_resumed'):
        integers['token'] = 32
    for key, bits in integers.items():
        if key in value:
            number = value[key]
            if type(number) is not int or not 0 <= number < (1 << bits):
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


def decode_event(raw: bytes) -> JsonObject:
    return validate_device_event(decode_json_object(raw))


@dataclass
class Packet:
    kind: int
    generation: int = 0
    sequence: int = 0
    frame: int = 0
    nanos: int = 0
    data: bytes = b''

    def encode(self) -> bytes:
        if type(self.kind) is not int or self.kind not in (MIC, RENDER, EVENT, SPEAKER, MIC_PAIRED):
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
        if self.kind == MIC_PAIRED and (not self.data or len(self.data) % 4):
            raise ValueError('Invalid paired PCM length')
        if self.kind != EVENT and (len(self.data) % 2 or len(self.data) > 6400):
            raise ValueError('Invalid PCM length')
        payload = (
            HEADER.pack(self.kind, self.generation, self.sequence, self.frame, self.nanos)
            + self.data
        )
        return struct.pack('!I', len(payload)) + payload

    @classmethod
    def event(cls, event: str, generation: int = 0, **fields: JsonValue) -> 'Packet':
        return cls(
            EVENT,
            generation,
            data=json.dumps(
                {'event': event, **fields}, ensure_ascii=False, allow_nan=False
            ).encode(),
        )


def read_exact(stream: PacketStream, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = stream.recv(count - len(result))
        if not chunk:
            raise EOFError('Device transport closed')
        result.extend(chunk)
    return bytes(result)


def receive(stream: PacketStream) -> Packet:
    # 底层可能拆包，须读满声明长度并复用发送侧校验，不能把一次 recv 当作一包。
    size = struct.unpack('!I', read_exact(stream, 4))[0]
    if size < HEADER.size or size > MAX_PACKET:
        raise ValueError('Invalid packet length')
    payload = read_exact(stream, size)
    kind, generation, sequence, frame, nanos = HEADER.unpack(payload[: HEADER.size])
    p = Packet(kind, generation, sequence, frame, nanos, payload[HEADER.size :])
    p.encode()  # Same validation in both directions.
    return p

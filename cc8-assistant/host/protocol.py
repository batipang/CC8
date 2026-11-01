"""Bounded framing shared by USB-loopback and authenticated LAN connections."""
from dataclasses import dataclass
import json
import struct

HEADER = struct.Struct('!BIQQQ')
# 长度与头字段为网络字节序，PCM 载荷为小端有符号 16 位。
# frame 是采样位置，nanos 是设备时间，不能当作日历时间。
MIC, RENDER, EVENT, SPEAKER = 1, 2, 3, 4
MAX_PACKET = 65536


@dataclass
class Packet:
    kind: int
    generation: int = 0
    sequence: int = 0
    frame: int = 0
    nanos: int = 0
    data: bytes = b''

    def encode(self):
        if self.kind not in (MIC, RENDER, EVENT, SPEAKER):
            raise ValueError('Unknown packet type')
        if len(self.data) + HEADER.size > MAX_PACKET:
            raise ValueError('Packet too large')
        if self.kind != EVENT and (len(self.data) % 2 or len(self.data) > 6400):
            raise ValueError('Invalid PCM length')
        payload = HEADER.pack(self.kind, self.generation, self.sequence, self.frame, self.nanos) + self.data
        return struct.pack('!I', len(payload)) + payload

    @classmethod
    def event(cls, event, generation=0, **fields):
        return cls(EVENT, generation, data=json.dumps({'event': event, **fields}, ensure_ascii=False).encode())


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
    p = Packet(*HEADER.unpack(payload[:HEADER.size]), payload[HEADER.size:])
    p.encode()  # Same validation in both directions.
    return p

"""Read an already completed foreground analysis into caller-owned RAM; never starts capture."""

import base64
import hashlib
import json
import re
from collections.abc import Callable

COMPONENT = 'com.happyhome.cc8diagnostics/.MainActivity'
MIN_BYTES = 48000 * 4 * 2
MAX_BYTES = MIN_BYTES * 6
CHUNK_BYTES = 192000
MAX_REPLY = 300000


class RamAudioError(RuntimeError):
    """Fixed error codes only: responses can contain private PCM and must not enter errors."""


def _unique(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError('duplicate field')
    return value


def _decode(reply: str, source: int, total: int, offset: int, rate: int, channels: int) -> bytes:
    if type(reply) is not str or len(reply) > MAX_REPLY:
        raise RamAudioError('analysis_reply_invalid')
    for prefix in ('CC8_ANALYSIS_CHUNK ', 'CC8_ANALYSIS_PCM '):
        if sum(line.lstrip().startswith(prefix) for line in reply.splitlines()) != 1:
            raise RamAudioError('analysis_chunk_missing')
    header = re.findall(r'^[ \t]*CC8_ANALYSIS_CHUNK (.+)$', reply, re.M)
    encoded = re.findall(r'^[ \t]*CC8_ANALYSIS_PCM ([A-Za-z0-9+/]+={0,2})[ \t]*$', reply, re.M)
    if len(header) != 1 or len(header[0]) > 2048 or len(encoded) != 1:
        raise RamAudioError('analysis_chunk_invalid')
    try:
        meta = json.loads(header[0], object_pairs_hook=_unique)
    except (ValueError, TypeError, RecursionError):
        raise RamAudioError('analysis_metadata_invalid') from None
    count = min(CHUNK_BYTES, total - offset)
    expected = {
        'offset_bytes': offset,
        'total_bytes': total,
        'chunk_bytes': count,
        'sample_rate': rate,
        'channels': channels,
        'source': source,
    }
    if (
        not isinstance(meta, dict)
        or set(meta) != {*expected, 'sha256'}
        or any(type(meta[key]) is not int or meta[key] != value for key, value in expected.items())
        or type(meta['sha256']) is not str
        or not re.fullmatch('[a-f0-9]{64}', meta['sha256'])
    ):
        raise RamAudioError('analysis_metadata_mismatch')
    try:
        chunk = base64.b64decode(encoded[0], validate=True)
    except ValueError:
        raise RamAudioError('analysis_encoding_invalid') from None
    if len(chunk) != count or hashlib.sha256(chunk).hexdigest() != meta['sha256']:
        raise RamAudioError('analysis_integrity_mismatch')
    return chunk


def read_into(
    shell: Callable[[str], str],
    token: str,
    source: int,
    destination: bytearray,
    *,
    sample_rate: int = 48000,
    channels: int = 4,
) -> None:
    """Read once; wipe on failure and attempt lease revocation after an attempted transfer.

    The caller must wipe this buffer after analysis. The shell callback must verify
    the USB target and bound each command, as device_smoke.Device.shell does.
    Use the fresh token returned by the device's prepare-reference-analysis reply.
    """
    if type(destination) is not bytearray:
        raise RamAudioError('analysis_buffer_invalid')
    attempted = False
    try:
        if (
            type(token) is not str
            or not re.fullmatch('[a-f0-9]{32}', token)
            or type(source) is not int
            or source not in (1, 6, 7)
            or type(sample_rate) is not int
            or type(channels) is not int
            or (sample_rate, channels) not in ((16000, 1), (48000, 4))
            or not sample_rate * channels * 2 <= len(destination) <= sample_rate * channels * 2 * 6
            or len(destination) % (channels * 2)
        ):
            raise RamAudioError('analysis_request_invalid')
        offset = 0
        while offset < len(destination):
            attempted = True
            try:
                reply = shell(
                    f'dumpsys activity {COMPONENT} read-reference-analysis {token} {offset}'
                )
            except Exception:
                raise RamAudioError('analysis_transfer_unconfirmed') from None
            chunk = _decode(reply, source, len(destination), offset, sample_rate, channels)
            destination[offset : offset + len(chunk)] = chunk
            offset += len(chunk)
    except BaseException:
        destination[:] = b'\0' * len(destination)
        if attempted:
            try:
                shell(f'dumpsys activity {COMPONENT} clear-reference-analysis {token}')
            except Exception:
                pass
        raise

"""One explicitly requested camera frame, bounded in RAM and tied to a turn."""

from __future__ import annotations
from dataclasses import dataclass, field
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from operation_failure import OperationFailure


def desktop_signature(token, challenge, nonce, generation):
    if (
        not isinstance(token, str)
        or not re.fullmatch('[a-f0-9]{64}', token)
        or not isinstance(challenge, str)
        or not re.fullmatch('[a-f0-9]{32}', challenge)
        or not isinstance(nonce, str)
        or not re.fullmatch('[a-f0-9]{32}', nonce)
        or type(generation) is not int
        or generation < 0
    ):
        raise ValueError('Invalid desktop camera authorization')
    return hmac.new(
        bytes.fromhex(token),
        f'desktop-camera:{challenge}:{nonce}:{generation}'.encode(),
        hashlib.sha256,
    ).hexdigest()


MAX_IMAGE = 384 * 1024
CHUNK = 12288


@dataclass(frozen=True)
class ImagePolicy:
    version: int
    max_edge: int
    max_pixels: int
    max_bytes: int

    def valid_dimensions(self, width, height):
        return (
            type(width) is type(height) is int
            and min(width, height) > 0
            and max(width, height) <= self.max_edge
            and width * height <= self.max_pixels
        )

    def fields(self):
        return {
            'capture_version': self.version,
            'max_image_edge': self.max_edge,
            'max_image_pixels': self.max_pixels,
            'max_image_bytes': self.max_bytes,
        }


LEGACY_IMAGE = ImagePolicy(1, 640, 640 * 480, MAX_IMAGE)
HD_IMAGE = ImagePolicy(2, 1280, 1280 * 960, 768 * 1024)


def image_policy(version):
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Unsupported capture policy')
    return HD_IMAGE if version == 2 else LEGACY_IMAGE


ERROR_CODES = frozenset(
    (
        'invalid_jpeg',
        'invalid_jpeg_header',
        'invalid_jpeg_segment',
        'unsupported_jpeg',
        'image_dimensions_exceeded',
        'missing_jpeg_dimensions',
        'cancelled',
        'stale_capture',
        'capture_timeout',
        'capture_cancelled_or_timeout',
        'camera_timeout',
        'camera_failed',
        'permission_required',
        'permission_denied',
        'not_foreground',
        'request_not_authorized',
        'camera_unavailable',
        'unsupported_preview',
        'invalid_frame',
        'preview_failed',
        'image_encode_failed',
        'camera_open_failed',
        'preview_surface_lost',
        'image_transfer_failed',
        'duplicate_image_begin',
        'image_size_exceeded',
        'invalid_image_hash',
        'invalid_image_chunk',
        'invalid_base64',
        'image_size_mismatch',
        'incomplete_image',
        'image_hash_mismatch',
        'image_dimensions_mismatch',
    )
)


class VisionError(OperationFailure):
    def __init__(self, code):
        # Device input and arbitrary exception text must not become metric/IPC
        # content merely because they resemble a lower-case error identifier.
        self.code = code if isinstance(code, str) and code in ERROR_CODES else 'camera_failed'
        if self.code in (
            'permission_required',
            'permission_denied',
            'not_foreground',
            'request_not_authorized',
        ):
            kind = 'credential_or_permission'
        elif self.code in ('capture_timeout', 'camera_timeout'):
            kind = 'timeout'
        elif self.code == 'image_transfer_failed':
            kind = 'transport_or_io'
        else:
            kind = 'provider_or_runtime'
        super().__init__(self.code, stage='vision', kind=kind)


def visual_request(text):
    # 只有明确的当前拍摄/观察意图才可触发镜头；否定、引用或普通查询不能授权拍照。
    """Explicit present-camera requests; descriptions/quotes of commands aren't consent."""
    value = re.sub(r'\s+', '', text).lower()
    value = re.sub(r'^(?:大黄蜂|大黃蜂|大黄峰|bumble-?bee)[，,。:：！!]*', '', value)
    value = re.sub(
        r'^(?:(?:请你|麻烦你|你能不能|能不能|你可以|你能|帮我|给我|替我|麻烦|能否|可以|重新|请|再|来|你)[，,：:]*)+',
        '',
        value,
    )
    if re.search(r'不要|别(?:开|拍|看|用)|不用|不需要|不能|不许|别再', value):
        return False
    if not re.search(r'摄像头|相机|镜头|手里|面前|纸|上面|周围', value) and re.search(
        r'项目|代码|网页|链接|新闻|天气|文件|电影|视频|仓库|文章', value
    ):
        return False
    if re.match(r'^(?:打开|调用|使用|用|拍一张|拍张).{0,8}(?:摄像头|相机|照片)', value):
        return True
    if re.match(
        r'^(?:看看|看一看|看一下|看下|观察|识别|辨认|分析|读一下|读出|看).{0,12}(?:我手里|我面前|我穿|镜头|眼前|周围|这(?:个|是|里|上面|张|些|件)|摄像头|相机|画面)',
        value,
    ):
        return True
    return bool(
        re.fullmatch(r'(?:看看|看一看|看看我|这是什么|(?:能)?看到(?:了)?什么)[。.!！?？]*', value)
    )


def jpeg_dimensions(data, *, policy=LEGACY_IMAGE):
    if (
        not isinstance(data, bytes)
        or not 32 <= len(data) <= policy.max_bytes
        or data[:2] != b'\xff\xd8'
        or data[-2:] != b'\xff\xd9'
    ):
        raise VisionError('invalid_jpeg')
    at = 2
    while at + 4 <= len(data):
        if data[at] != 255:
            raise VisionError('invalid_jpeg_header')
        while at < len(data) and data[at] == 255:
            at += 1
        if at >= len(data):
            break
        marker = data[at]
        at += 1
        if marker in (0xD9, 0xDA):
            break
        size = int.from_bytes(data[at : at + 2], 'big')
        if size < 2 or at + size > len(data):
            raise VisionError('invalid_jpeg_segment')
        if marker in (0xC0, 0xC2):
            if size < 8 or data[at + 2] != 8:
                raise VisionError('unsupported_jpeg')
            height = int.from_bytes(data[at + 3 : at + 5], 'big')
            width = int.from_bytes(data[at + 5 : at + 7], 'big')
            if not policy.valid_dimensions(width, height):
                raise VisionError('image_dimensions_exceeded')
            return width, height
        at += size
    raise VisionError('missing_jpeg_dimensions')


@dataclass
class Capture:
    generation: int
    policy: ImagePolicy = LEGACY_IMAGE
    review_allowed: bool = False
    followup_allowed: bool = False
    followup_requested: bool = False
    ident: str = field(default_factory=lambda: secrets.token_hex(16))
    expires: float = field(default_factory=lambda: time.monotonic() + 16)
    done: threading.Event = field(default_factory=threading.Event)
    data: bytearray = field(default_factory=bytearray)
    image: bytes | None = None
    size: int = 0
    sequence: int = 0
    width: int = 0
    height: int = 0
    digest: str = ''
    error: str = ''


@dataclass
class DesktopSnapshot:
    generation: int
    ident: str
    policy: ImagePolicy = LEGACY_IMAGE
    done: threading.Event = field(default_factory=threading.Event)
    image: bytes | None = None
    result: str = 'interrupted'
    error: str = ''
    metadata_only: bool = False


class CameraInbox:
    # 一次只接收一个有代次和请求 ID 的分块 JPEG，校验大小、尺寸、哈希与超时。
    # 取消后不复用旧图；普通语音看图数据只在内存中等待 Dot 消费。
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = None

    def begin(self, generation, *, policy=LEGACY_IMAGE, review=False, followup=False):
        if (
            policy not in (LEGACY_IMAGE, HD_IMAGE)
            or type(review) is not bool
            or type(followup) is not bool
        ):
            raise ValueError('Invalid capture grant')
        with self.lock:
            self._cancel()
            self.pending = Capture(
                generation,
                policy=policy,
                review_allowed=review,
                followup_allowed=followup,
                expires=time.monotonic() + (75 if review else 16),
            )
            return self.pending

    def _cancel(self):
        if self.pending:
            self.pending.error = 'cancelled'
            self.pending.data.clear()
            self.pending.image = None
            self.pending.done.set()
        self.pending = None

    def cancel(self):
        with self.lock:
            self._cancel()

    def cancel_capture(self, capture):
        with self.lock:
            if self.pending is capture:
                self._cancel()

    def feed(self, value):
        with self.lock:
            c = self.pending
            if (
                not c
                or c.done.is_set()
                or value.get('request_id') != c.ident
                or value.get('vision_generation') != c.generation
            ):
                return False
            try:
                if time.monotonic() > c.expires:
                    raise VisionError('capture_timeout')
                event = value.get('event')
                if event == 'camera_error':
                    raise VisionError(value.get('error'))
                if event == 'camera_image_begin':
                    if c.size:
                        raise VisionError('duplicate_image_begin')
                    retain = value.get('retain_for_followup', False)
                    if type(retain) is not bool or (retain and not c.followup_allowed):
                        raise VisionError('request_not_authorized')
                    size = value.get('bytes')
                    width = value.get('width')
                    height = value.get('height')
                    digest = value.get('sha256')
                    if type(size) is not int or not 32 <= size <= c.policy.max_bytes:
                        raise VisionError('image_size_exceeded')
                    if not c.policy.valid_dimensions(width, height):
                        raise VisionError('image_dimensions_exceeded')
                    if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                        raise VisionError('invalid_image_hash')
                    c.size = size
                    c.followup_requested = retain
                    c.width = width
                    c.height = height
                    c.digest = digest
                elif event == 'camera_image_chunk':
                    raw = value.get('data')
                    if (
                        not c.size
                        or type(value.get('index')) is not int
                        or value['index'] != c.sequence
                        or not isinstance(raw, str)
                        or len(raw) > CHUNK * 4 // 3
                    ):
                        raise VisionError('invalid_image_chunk')
                    try:
                        chunk = base64.b64decode(raw, validate=True)
                    except (ValueError, TypeError):
                        raise VisionError('invalid_base64') from None
                    if not chunk or len(c.data) + len(chunk) > c.size:
                        raise VisionError('image_size_mismatch')
                    c.data.extend(chunk)
                    c.sequence += 1
                elif event == 'camera_image_end':
                    if not c.size or len(c.data) != c.size:
                        raise VisionError('incomplete_image')
                    image = bytes(c.data)
                    if hashlib.sha256(image).hexdigest() != c.digest:
                        raise VisionError('image_hash_mismatch')
                    if jpeg_dimensions(image, policy=c.policy) != (c.width, c.height):
                        raise VisionError('image_dimensions_mismatch')
                    c.image = image
                    c.data.clear()
                    c.done.set()
                else:
                    return False
            except VisionError as error:
                c.error = str(error)
                c.image = None
                c.data.clear()
                c.done.set()
            return True

    def wait(self, capture, is_current):
        while not capture.done.wait(0.05):
            current = is_current()
            if not current or time.monotonic() > capture.expires:
                self.cancel_capture(capture)
                capture.error = 'cancelled' if not current else 'capture_timeout'
                raise VisionError(capture.error)
        if not is_current():
            raise VisionError('cancelled')
        if capture.error or capture.image is None:
            raise VisionError(capture.error or 'stale_capture')
        image = capture.image
        with self.lock:
            capture.image = None
            if self.pending is capture:
                self.pending = None
        return image

"""One explicitly retained photo in RAM; never a camera or persistence entry point."""

from dataclasses import dataclass, field
import math
import re
import secrets
import threading
import time

from vision import HD_IMAGE, LEGACY_IMAGE, ImagePolicy, jpeg_dimensions
from request_text import validate_request_text

TTL_SECONDS = 120


def spoken_photo_question(text):
    """Only an explicit reference to the retained photograph selects its pixels."""
    if not isinstance(text, str):
        return None
    value = re.sub(
        r'^(?:大黄蜂|大黃蜂|大黄峰|bumble-?bee)[，,：:\s]*', '', text.strip(), flags=re.I
    )
    match = re.fullmatch(
        r'(?:追问(?:这张|刚才的|上一张)(?:照片|图片|图)|针对这张(?:照片|图片|图)(?:追问)?)'
        r'[，,：:\s]*(.*)',
        value,
        flags=re.S,
    )
    if match is None:
        return None
    try:
        return validate_request_text(match[1], 1000)
    except ValueError:
        return ''


@dataclass(frozen=True)
class RetainedPhoto:
    ident: str
    generation: int
    expires: float
    width: int
    height: int
    policy: ImagePolicy
    image: bytes = field(repr=False)


class PhotoFollowup:
    """Consume each token once. A subsequent explicit question retains the original expiry.

    The caller must obtain foreground consent before arm() and clear() on session
    invalidation. Expiry notification contains only an ID and generation. Timer
    callbacks notify outside this lock, avoiding a session/cache lock inversion.
    """

    def __init__(
        self,
        expired=lambda ident, generation: None,
        *,
        clock=time.monotonic,
        timer_factory=threading.Timer,
    ):
        self._clock = clock
        self._timer_factory = timer_factory
        self._expired = expired
        self._lock = threading.RLock()
        self._photo = None
        self._timer = None

    def _drop(self):
        photo, self._photo = self._photo, None
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        return photo

    def clear(self):
        with self._lock:
            self._drop()

    def arm(self, generation, image, policy, *, consent, expires=None):
        if consent is not True:
            raise ValueError('Explicit photo retention consent required')
        if type(generation) is not int or not 0 <= generation < 2**31:
            raise ValueError('Invalid photo generation')
        if policy not in (LEGACY_IMAGE, HD_IMAGE):
            raise ValueError('Invalid retained image policy')
        width, height = jpeg_dimensions(image, policy=policy)
        now = self._clock()
        if expires is None:
            expires = now + TTL_SECONDS
        if (
            type(expires) not in (int, float)
            or not math.isfinite(expires)
            or not now < expires <= now + TTL_SECONDS
        ):
            raise ValueError('Invalid retained photo expiry')
        photo = RetainedPhoto(
            secrets.token_hex(16), generation, expires, width, height, policy, image
        )
        timer = self._timer_factory(expires - now, self._expire, args=(photo.ident, generation))
        timer.daemon = True
        with self._lock:
            self._drop()
            self._photo, self._timer = photo, timer
            try:
                timer.start()
            except BaseException:
                self._drop()
                raise
            return self._metadata(generation)

    def _metadata(self, generation):
        photo = self._photo
        now = self._clock()
        if photo is not None and now >= photo.expires:
            self._drop()
            return None
        if photo is None or type(generation) is not int or generation != photo.generation:
            return None
        return dict(
            photo_id=photo.ident,
            generation=photo.generation,
            width=photo.width,
            height=photo.height,
            ttl_ms=max(1, min(TTL_SECONDS * 1000, math.ceil((photo.expires - now) * 1000))),
        )

    def metadata(self, generation):
        with self._lock:
            return self._metadata(generation)

    def consume(self, ident, generation):
        with self._lock:
            current = self._metadata(generation)
            if (
                current is None
                or type(ident) is not str
                or not re.fullmatch('[a-f0-9]{32}', ident)
                or current['photo_id'] != ident
            ):
                return None
            return self._drop()

    def advance_voice(self, previous, generation):
        with self._lock:
            state = self._metadata(previous)
            if state is None or type(generation) is not int or generation != previous + 1:
                return None
            photo = self._photo
            return self.arm(
                generation, photo.image, photo.policy, consent=True, expires=photo.expires
            )

    def _expire(self, ident, generation):
        with self._lock:
            photo = self._photo
            if (
                photo is None
                or photo.ident != ident
                or photo.generation != generation
                or self._clock() < photo.expires
            ):
                return
            # Do not pass or retain the JPEG in the expiry callback's locals.
            photo = None
            self._drop()
        try:
            self._expired(ident, generation)
        except Exception:
            # The picture has already been released; a disconnected UI cannot
            # keep it alive or justify another notification/retention attempt.
            pass

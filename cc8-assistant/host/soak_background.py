"""Load a pinned background track and own its bounded Mac playback."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import wave

KINDS = ('tv', 'low-noise')
RATE = 16000
PLAYBACK_SECONDS = 1050
MAX_BYTES = 44 * 1024**2


def valid_background(value):
    return (
        isinstance(value, dict)
        and set(value) == {'kind', 'sha256'}
        and value['kind'] in KINDS
        and isinstance(value['sha256'], str)
        and re.fullmatch('[0-9a-f]{64}', value['sha256']) is not None
    )


@dataclass(frozen=True)
class Background:
    kind: str
    sha256: str
    wav: bytes

    @classmethod
    def load(cls, path, kind, sha256):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as track:
            info = os.fstat(track.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
                raise ValueError('Invalid background fixture')
            data = track.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError('Background fixture does not match its pinned digest')
        try:
            with wave.open(io.BytesIO(data), 'rb') as wav:
                channels, width, rate, frames = wav.getparams()[:4]
                if (channels, width, rate) != (1, 2, RATE) or frames < RATE * PLAYBACK_SECONDS:
                    raise ValueError('Expected a long 16 kHz mono PCM16 WAV')
                pcm = wav.readframes(frames)
                if len(pcm) != frames * width or not pcm.strip(b'\0'):
                    raise ValueError('Background fixture is truncated or silent')
        except (EOFError, wave.Error) as error:
            raise ValueError('Invalid background WAV') from error
        return cls(kind, sha256, data)

    @property
    def receipt(self):
        return {'kind': self.kind, 'sha256': self.sha256}

    @contextmanager
    def play(self):
        # afplay receives the same bytes verified above, independent of the
        # original file's path or changes while the test is running.
        with tempfile.TemporaryDirectory(prefix='cc8-background-') as folder:
            path = Path(folder) / 'background.wav'
            path.write_bytes(self.wav)
            child = subprocess.Popen(
                ['/usr/bin/afplay', '-t', str(PLAYBACK_SECONDS), str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                yield lambda: child.poll() is None
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)

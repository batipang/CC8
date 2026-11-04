"""Load a pinned background track and own its bounded Mac playback."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import wave

KINDS = ('tv', 'low-noise')
RATE = 16000
PLAYBACK_SECONDS = 1050
MAX_BYTES = 44 * 1024**2
PLAYER = '/usr/bin/afplay'


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
        from rpc_process import close_guardian, start_guardian

        # The guardian owns the audio process and closes its group on socket
        # EOF, including when the test process is killed before its finally.
        process, owner = start_guardian(
            subprocess.Popen,
            [sys.executable, '-I', '-S', str(Path(__file__).resolve()), '--owned-player'],
            cwd=None,
            env=None,
        )
        try:
            pending = memoryview(self.wav)
            with process.stdin as track:
                while pending:
                    count = track.write(pending)
                    if not count:
                        raise OSError('Background player input closed')
                    pending = pending[count:]
            if not select.select([process.stdout], [], [], 5)[0] or process.stdout.read(1) != b'1':
                raise OSError('Background player did not start')
            yield (
                lambda: process.poll() is None and not select.select([process.stdout], [], [], 0)[0]
            )
        finally:
            try:
                if not close_guardian(process, owner):
                    raise OSError('Background player cleanup unconfirmed')
            finally:
                process.stdout.close()
                process.stderr.close()


def owned_player():
    def stop(_signal, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if not 0 < len(data) <= MAX_BYTES:
        return 2
    # The process watched by the guardian owns the private copy. Its signal
    # handler lets this context remove the copy even if the original test dies.
    with tempfile.TemporaryDirectory(prefix='cc8-background-') as folder:
        path = Path(folder) / 'background.wav'
        path.write_bytes(data)
        with subprocess.Popen(
            [PLAYER, '-t', str(PLAYBACK_SECONDS), str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as player:
            try:
                sys.stdout.buffer.write(b'1')
                sys.stdout.buffer.flush()
                return player.wait()
            finally:
                if player.poll() is None:
                    player.terminate()
                    try:
                        player.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        player.kill()
                        player.wait(timeout=1)


if __name__ == '__main__':
    raise SystemExit(owned_player() if sys.argv[1:] == ['--owned-player'] else 2)

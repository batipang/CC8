"""Capture-owned local classifier with anonymous, bounded PCM IPC."""

from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import time

SCRIPT = Path(__file__).resolve()
MAX_SAMPLES = 28_800  # 1.8 seconds of 16 kHz mono input.
STARTUP_SECONDS = 5.0
REQUEST_SECONDS = 0.15
READY = b'R'
UNAVAILABLE = (False, False)


def classify_text(text):
    text = text.replace('黃', '黄')
    return '大黄蜂' in text, any(word in text for word in ('打黄蜂', '大黄风筝'))


class WakeVerifierProcess:
    # The wake controller serializes start, readiness, decode and close.
    def __init__(self):
        self._process = None
        self._channel = None
        self._ready = False
        self._startup_deadline = 0.0

    def start(self):
        if self._channel is not None:
            return
        # A previous process is never replaced until it has been reaped.
        if self._process is not None and self._process.poll() is None:
            return
        self._process = None
        try:
            parent, child = socket.socketpair()
            try:
                with child:
                    self._process = subprocess.Popen(
                        [sys.executable, '-I', str(SCRIPT), str(child.fileno())],
                        pass_fds=(child.fileno(),),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'},
                    )
            except BaseException:
                parent.close()
                raise
        except (OSError, ValueError):
            return
        self._channel = parent
        self._startup_deadline = time.monotonic() + STARTUP_SECONDS

    @property
    def ready(self):
        if self._channel is None:
            if self._process is not None and self._process.poll() is not None:
                self._process = None
            return False
        if self._process.poll() is not None:
            self.close()
            return False
        if not self._ready:
            try:
                readable, _, _ = select.select((self._channel,), (), (), 0)
                if not readable:
                    if time.monotonic() >= self._startup_deadline:
                        self.close()
                    return False
                if self._channel.recv(1) == READY:
                    self._ready = True
                else:
                    self.close()
            except (OSError, ValueError):
                self.close()
        return self._ready

    @property
    def last_ready(self):
        # Read-only fallback for health polling while the owner is decoding.
        return self._ready

    def classify(self, samples):
        if not self.ready:
            return UNAVAILABLE
        import numpy as np

        audio = np.array(samples, dtype='<f4', copy=True)
        try:
            if audio.ndim != 1 or not 0 < len(audio) <= MAX_SAMPLES:
                raise ValueError('Invalid local wake PCM')
            deadline = time.monotonic() + REQUEST_SECONDS
            channel = self._channel
            channel.settimeout(REQUEST_SECONDS)
            channel.sendall(struct.pack('>I', audio.nbytes))
            _set_remaining(channel, deadline)
            channel.sendall(memoryview(audio).cast('B'))
            _set_remaining(channel, deadline)
            result = channel.recv(1)
            if len(result) != 1 or result[0] > 3:
                raise ValueError('Invalid local wake reply')
            return bool(result[0] & 1), bool(result[0] & 2)
        except (OSError, ValueError):
            self.close()
            return UNAVAILABLE
        finally:
            audio.fill(0)

    def close(self):
        channel, self._channel = self._channel, None
        self._ready = False
        if channel is not None:
            channel.close()
        if self._process is not None:
            try:
                self._process.kill()
                self._process.wait(timeout=0.05)
            except (OSError, subprocess.TimeoutExpired):
                return
            self._process = None


def _set_remaining(channel, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Local wake response deadline')
    channel.settimeout(remaining)


def _receive(channel, size):
    data = bytearray(size)
    remaining = memoryview(data)
    while remaining:
        count = channel.recv_into(remaining)
        if count == 0:
            data[:] = bytes(size)
            raise EOFError
        remaining = remaining[count:]
    return data


def _worker(fd):
    # The native inference deadline remains effective if the parent disappears
    # during a decode and the extension cannot return to Python.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, STARTUP_SECONDS)
    import numpy as np
    from recognition import MandarinRecognizer

    with socket.socket(fileno=fd) as channel:
        model = MandarinRecognizer(engine='sensevoice')
        signal.setitimer(signal.ITIMER_REAL, 0)
        channel.sendall(READY)
        while True:
            (size,) = struct.unpack('>I', _receive(channel, 4))
            if size % 4 or not 0 < size <= MAX_SAMPLES * 4:
                raise ValueError('Invalid local wake payload')
            pcm = _receive(channel, size)
            try:
                samples = np.frombuffer(pcm, dtype='<f4')
                if not np.all(np.isfinite(samples)):
                    raise ValueError('Invalid local wake PCM')
                signal.setitimer(signal.ITIMER_REAL, 1)
                target, confusable = classify_text(model.recognize(samples))
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:
                pcm[:] = bytes(size)
            channel.sendall(bytes((int(target) + 2 * int(confusable),)))


if __name__ == '__main__':
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        _worker(int(sys.argv[1]))
    except (EOFError, BrokenPipeError):
        pass
